"""Binding FastFort to SQLAlchemy.

The backend takes the project's own session factory rather than an engine. The
lifecycle of a session -- when it opens, what it is bound to, whether it is
shared with the rest of the request -- belongs to the application, and taking an
engine would quietly create a second one alongside it.
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fastfort.core.exceptions import AdapterError, ImproperlyConfigured
from fastfort.core.registry import default_model_key
from fastfort.spec import ModelSpec

from .adapter import SQLAlchemyAdapter
from .dialects import DialectProfile, profile_for
from .introspect import introspect_model, is_sqlalchemy_model

__all__ = ["SQLAlchemyBackend", "SQLAlchemyUnitOfWork"]


class SQLAlchemyUnitOfWork:
    """One transaction, wrapping one session.

    Leaving the context commits, unless an exception is propagating, in which
    case it rolls back. Adapters only ever flush, so this is the single place
    that decides whether a request's writes survive.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise AdapterError(
                "This unit of work is not open.",
                hint="Use it as an async context manager: `async with backend.unit_of_work():`.",
            )
        return self._session

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        self._session = self._session_factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        if session is None:
            return
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        finally:
            await session.close()
            self._session = None

    async def commit(self) -> None:
        await self.session.commit()

    async def rollback(self) -> None:
        await self.session.rollback()

    async def flush(self) -> None:
        await self.session.flush()


class SQLAlchemyBackend:
    """FastFort's SQLAlchemy 2.0 integration.

    Async sessions only. A synchronous session cannot be awaited, and pretending
    otherwise by hiding a thread pool inside the adapter would make transaction
    boundaries impossible to reason about; a clear error at start-up is better.
    """

    name = "sqlalchemy"

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        base: type | None = None,
        resolve_key: Callable[[type], str] = default_model_key,
        count_cap: int | None = None,
    ) -> None:
        if not isinstance(session_factory, async_sessionmaker):
            raise ImproperlyConfigured(
                f"session_factory must be an async_sessionmaker, got "
                f"{type(session_factory).__name__}.",
                hint=(
                    "Build one with "
                    "`async_sessionmaker(create_async_engine(url), expire_on_commit=False)`."
                ),
            )
        self._session_factory = session_factory
        self._base = base
        self._resolve_key = resolve_key
        self._count_cap = count_cap
        self._specs: dict[type, ModelSpec] = {}

    def __repr__(self) -> str:
        return f"<SQLAlchemyBackend {self.dialect}>"

    def set_key_resolver(self, resolve: Callable[[type], str]) -> None:
        """Replace the rule that names a relation's target model.

        Called by `FastFort` so that a relation points at the key the target is
        *registered* under rather than one derived from its module. Anything
        already introspected is dropped, because a cached spec would still carry
        the old names.
        """
        self._resolve_key = resolve
        self._specs.clear()

    # -- identity -----------------------------------------------------------

    @property
    def engine(self) -> Any:
        bind = self._session_factory.kw.get("bind")
        if bind is None:
            raise ImproperlyConfigured(
                "The session factory has no bind, so FastFort cannot tell which "
                "database it is talking to.",
                hint="Pass the engine: `async_sessionmaker(engine, ...)`.",
            )
        return bind

    @property
    def dialect(self) -> str:
        return str(self.engine.dialect.name)

    @property
    def profile(self) -> DialectProfile:
        return profile_for(self.dialect)

    def supports(self, model: type) -> bool:
        if not is_sqlalchemy_model(model):
            return False
        if self._base is None:
            return True
        return issubclass(model, self._base)

    # -- specs and adapters -------------------------------------------------

    def introspect(self, model: type, *, key: str) -> ModelSpec:
        """Derive and cache the spec for `model`.

        Caching matters: introspection walks the mapper on every call, and a list
        page would otherwise repeat that work for each request.
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
        uow: SQLAlchemyUnitOfWork,
        *,
        key: str | None = None,
        search_fields: tuple[str, ...] = (),
        select_related: tuple[str, ...] = (),
        prefetch_related: tuple[str, ...] = (),
    ) -> SQLAlchemyAdapter:
        spec = self.introspect(model, key=key or self._resolve_key(model))
        return SQLAlchemyAdapter(
            model,
            spec,
            uow.session,
            self.profile,
            search_fields=search_fields,
            select_related=select_related,
            prefetch_related=prefetch_related,
            count_cap=self._count_cap,
        )

    def unit_of_work(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self._session_factory)

    # -- diagnostics --------------------------------------------------------

    async def check_connection(self) -> None:
        """Verify the database answers, so a bad URL fails at deploy time."""
        try:
            async with self._session_factory() as session:
                await session.execute(sa.text("SELECT 1"))
        except Exception as exc:
            raise AdapterError(
                f"Cannot reach the database: {exc}",
                hint="Check the connection URL, that the server is running, and the credentials.",
            ) from exc
