"""Binding FastFort to Tortoise ORM.

The second backend, and the point of the layering: nothing above `fastfort/orm/`
changed to make it work. The admin, the forms, the templates and the query
boundary all read a `ModelSpec` and a `ListQuery`, and this module produces the
first and consumes the second.

Where the SQLAlchemy backend takes the project's own session factory, this one
takes nothing: Tortoise keeps its connections in a process-global registry that
`Tortoise.init()` populates, and there is no per-request object to hand over.
That is Tortoise's design rather than a choice made here, and it is why the
backend's job is smaller -- it checks that initialisation has happened and gets
out of the way.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any

from tortoise import Tortoise, connections
from tortoise.transactions import in_transaction

from fastfort.core.exceptions import AdapterError, ImproperlyConfigured
from fastfort.core.registry import default_model_key
from fastfort.orm.base import UnitOfWork
from fastfort.spec import ModelSpec

from ..sqlalchemy.dialects import DialectProfile, profile_for
from .adapter import TortoiseAdapter
from .introspect import introspect_model, is_tortoise_model

__all__ = ["TortoiseBackend", "TortoiseUnitOfWork"]

#: The substring Tortoise 1.1+ raises with when its state is not reachable from
#: the task asking for it. Matched on text because the exception it comes in is a
#: bare `RuntimeError` -- there is no type to catch.
_NO_CONTEXT = "No TortoiseContext is currently active"

#: Told to whoever hits it, in both places it can surface. Tortoise 1.1 moved its
#: connection registry into a `contextvars.ContextVar`, and an ASGI server runs
#: the lifespan in a different task from the requests -- so a `Tortoise.init()`
#: in the lifespan sets the variable in a context the request handlers never see.
#: Everything looks right at start-up and the first page that touches the
#: database is a 500, which is exactly what happened to this project's own
#: Tortoise sandbox.
_CONTEXT_HINT = (
    "Tortoise 1.1 keeps its connections in a contextvar, and an ASGI server runs the "
    "lifespan in a different task from the requests -- so `await Tortoise.init(...)` in a "
    "lifespan is invisible to every view. Pass `_enable_global_fallback=True` to it, or use "
    "`async with RegisterTortoise(app, db_url=..., modules=...):` from "
    "`tortoise.contrib.fastapi`, which passes it for you."
)


def _missing_context(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and _NO_CONTEXT in str(exc)


class TortoiseUnitOfWork:
    """One transaction.

    Same contract as the SQLAlchemy one: leaving the context commits unless an
    exception is propagating, in which case it rolls back. Adapters never commit
    -- that invariant is the whole reason this type exists on both sides, and a
    request that fails halfway leaves nothing behind on either.

    Tortoise's `in_transaction()` already does the commit-or-rollback, so this is
    mostly a name the rest of FastFort recognises. `rollback()` is the one place
    it does more: the views call it explicitly before re-rendering a failed
    form, and Tortoise's context manager would otherwise commit on the way out
    of a block that ended without raising.
    """

    def __init__(self, connection_name: str = "default") -> None:
        self._connection_name = connection_name
        self._context: Any = None
        self._connection: Any = None
        self._done = False

    @property
    def connection(self) -> Any:
        if self._connection is None:
            raise AdapterError(
                "This unit of work is not open.",
                hint="Use it as an async context manager: `async with backend.unit_of_work():`.",
            )
        return self._connection

    async def __aenter__(self) -> TortoiseUnitOfWork:
        try:
            self._context = in_transaction(self._connection_name)
            self._connection = await self._context.__aenter__()
        except RuntimeError as exc:
            if not _missing_context(exc):
                raise
            # Left as it was found, so the failed unit of work cannot be exited
            # into a transaction that was never opened.
            self._context = None
            raise ImproperlyConfigured(
                "Tortoise is initialised, but its connections are not reachable from the "
                "task handling this request.",
                hint=_CONTEXT_HINT,
            ) from exc
        self._done = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._context is None:
            return
        try:
            if self._done:
                # Already rolled back by hand. Handing Tortoise a transaction it
                # has been told is finished raises rather than being ignored, so
                # the exit is faked with an exception it will swallow into a
                # rollback it has already performed.
                await self._context.__aexit__(type(exc) if exc else None, exc, tb)
            else:
                await self._context.__aexit__(exc_type, exc, tb)
        finally:
            self._context = None
            self._connection = None

    async def commit(self) -> None:
        """A no-op that exists so the views do not have to know which ORM they
        are on.

        Tortoise commits when its own context manager exits cleanly, which is
        what `__aexit__` above delegates to. Committing here as well would end
        the transaction under the block that is still using it.
        """

    async def rollback(self) -> None:
        await self.connection.rollback()
        self._done = True

    async def flush(self) -> None:
        """Also a no-op.

        SQLAlchemy has a unit of work that holds pending changes until they are
        flushed; Tortoise issues each write as it is made. Nothing is ever
        waiting to be sent, so there is nothing to send.
        """


class TortoiseBackend:
    """FastFort's Tortoise ORM integration.

    `Tortoise.init()` is the project's job and has to have happened before the
    admin is mounted -- the backend refuses to guess at a configuration, for the
    same reason the SQLAlchemy one takes a session factory instead of building
    an engine: connection lifetime belongs to the application.
    """

    name = "tortoise"

    def __init__(
        self,
        *,
        models: tuple[type, ...] | None = None,
        connection_name: str = "default",
        resolve_key: Callable[[type], str] = default_model_key,
        count_cap: int | None = None,
    ) -> None:
        #: The models this backend claims, when a project runs two ORMs side by
        #: side. `None` means "every Tortoise model", which is the usual case.
        self._models = models
        self._connection_name = connection_name
        self._resolve_key = resolve_key
        self._count_cap = count_cap
        self._specs: dict[type, ModelSpec] = {}

    def __repr__(self) -> str:
        return f"<TortoiseBackend {self.dialect}>"

    def set_key_resolver(self, resolve: Callable[[type], str]) -> None:
        """Replace the rule that names a relation's target model.

        Called by `FastFort` so a relation points at the key its target is
        *registered* under. Anything already introspected is dropped, because a
        cached spec would still carry the old names.
        """
        self._resolve_key = resolve
        self._specs.clear()

    # -- identity -----------------------------------------------------------

    @property
    def dialect(self) -> str:
        """What Tortoise is connected to, or `"unknown"` before it is.

        Never raises. `dialect` is read while rendering error pages among other
        things, and a property that throws when the database is not up turns one
        problem into two.
        """
        try:
            client = connections.get(self._connection_name)
        except Exception:
            return "unknown"
        capabilities = getattr(client, "capabilities", None)
        return str(getattr(capabilities, "dialect", "unknown"))

    @property
    def profile(self) -> DialectProfile:
        """Reused from the SQLAlchemy side, deliberately.

        `DialectProfile` describes a *database*, not an ORM -- MySQL has no
        `NULLS LAST` whichever library is talking to it -- so the two backends
        share one table of those facts rather than keeping two that could
        disagree.
        """
        return profile_for(self.dialect)

    def supports(self, model: type) -> bool:
        if not is_tortoise_model(model):
            return False
        return self._models is None or model in self._models

    # -- specs and adapters -------------------------------------------------

    def introspect(self, model: type, *, key: str) -> ModelSpec:
        """Derive and cache the spec for `model`.

        Cached for the same reason as on the other side: introspection walks the
        model's metadata on every call, and a list page would repeat that work
        per request.
        """
        cached = self._specs.get(model)
        if cached is not None and cached.key == key:
            return cached

        spec = introspect_model(model, key=key, resolve_key=self._resolve_key)
        self._specs[model] = spec
        return spec

    def adapter(
        self,
        model: type,
        uow: UnitOfWork,
        *,
        key: str | None = None,
        search_fields: tuple[str, ...] = (),
        select_related: tuple[str, ...] = (),
        prefetch_related: tuple[str, ...] = (),
    ) -> TortoiseAdapter:
        # See the note in the SQLAlchemy backend: narrowing the parameter broke
        # structural compatibility with `Backend` for every typed project.
        if not isinstance(uow, TortoiseUnitOfWork):
            raise TypeError(
                f"TortoiseBackend.adapter() needs a unit of work from this backend, "
                f"got {type(uow).__name__}. Use `backend.unit_of_work()`."
            )

        spec = self.introspect(model, key=key or self._resolve_key(model))
        return TortoiseAdapter(
            model,
            spec,
            uow,
            self.profile,
            search_fields=search_fields,
            select_related=select_related,
            prefetch_related=prefetch_related,
            count_cap=self._count_cap,
            resolve_key=self._resolve_key,
        )

    def unit_of_work(self) -> TortoiseUnitOfWork:
        return TortoiseUnitOfWork(self._connection_name)

    async def check_connection(self) -> None:
        """Verify Tortoise is initialised and the database answers.

        Two failures with two different fixes, so they get two messages: not
        calling `Tortoise.init()` is a wiring mistake, and a database that will
        not answer is an operational one.
        """
        if not Tortoise._inited:
            # `_inited` reads the *current* context on Tortoise 1.1+, so this is
            # also what a caller sees when init happened in another task. Both
            # fixes are named, because from here the two are indistinguishable.
            raise ImproperlyConfigured(
                "Tortoise has not been initialised, so FastFort has no connection to use.",
                hint=(
                    "Call `await Tortoise.init(db_url=..., modules=...)` before mounting "
                    f"the admin -- usually from the application's lifespan. {_CONTEXT_HINT}"
                ),
            )
        try:
            await connections.get(self._connection_name).execute_query("SELECT 1")
        except RuntimeError as exc:
            if not _missing_context(exc):
                raise
            raise ImproperlyConfigured(
                "Tortoise is initialised, but its connections are not reachable from here.",
                hint=_CONTEXT_HINT,
            ) from exc
        except Exception as exc:
            raise AdapterError(
                f"Cannot reach the database on the {self._connection_name!r} connection: {exc}",
                hint="Check the connection URL, that the server is running, and the credentials.",
            ) from exc
