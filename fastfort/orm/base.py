"""The contract every ORM integration implements.

These protocols are the entire surface the rest of FastFort is allowed to use.
They are written against `ModelSpec` and `ListQuery` rather than against rows or
sessions, which is what lets a second ORM be added without the admin noticing.

Two decisions are worth stating explicitly, because getting them wrong is the
usual source of data corruption in tools like this:

*Transactions belong to the caller.* An adapter never commits. It writes through
the unit of work it was given, and the view decides when the work is durable.
Otherwise a request that fails halfway leaves half of its changes behind.

*Primary keys are tuples.* A single-column key is a one-element tuple. Composite
keys are not a special case bolted on later; they are the shape everything uses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, TypeAlias, runtime_checkable

from fastfort.spec import ListQuery, ModelSpec, Page

__all__ = [
    "Backend",
    "ModelAdapter",
    "PrimaryKey",
    "RelatedChoice",
    "UnitOfWork",
]

#: A primary key value. Always a tuple, even for single-column keys, so that
#: composite keys never need a separate code path.
PrimaryKey: TypeAlias = tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class RelatedChoice:
    """One option offered by an autocomplete or a relation dropdown."""

    value: Any
    label: str


@runtime_checkable
class UnitOfWork(Protocol):
    """A transactional scope shared by every adapter used in one request.

    Implementations wrap the ORM's session. Entering the context begins the
    scope; leaving it without an exception commits, and with one rolls back.
    """

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def flush(self) -> None:
        """Send pending changes without ending the transaction.

        Needed after a create so that database-generated values (identity keys,
        defaults, triggers) can be read back before the caller commits.
        """
        ...


@runtime_checkable
class ModelAdapter(Protocol):
    """Reads and writes rows of one model.

    An adapter is cheap and request-scoped: it is bound to a unit of work, so it
    must not be cached across requests.
    """

    @property
    def spec(self) -> ModelSpec:
        """The introspected description of the model this adapter serves."""
        ...

    # -- reading ------------------------------------------------------------

    async def list(self, query: ListQuery) -> Page[Any]:
        """Return one page of rows.

        The adapter is responsible for eager-loading whatever the list needs, so
        that rendering a page never triggers a query per row.
        """
        ...

    async def count(self, query: ListQuery) -> int:
        """Count rows matching `query`, ignoring its pagination.

        Separate from `list` because on a large table this is the expensive half
        and callers may want to cap or skip it.
        """
        ...

    async def get(self, pk: PrimaryKey) -> Any | None:
        """Return one row by primary key, or None when it does not exist."""
        ...

    async def related_choices(
        self, field: str, term: str, *, limit: int
    ) -> Sequence[RelatedChoice]:
        """Search the target of a relation field, for autocomplete widgets."""
        ...

    # -- writing ------------------------------------------------------------

    async def create(self, data: Mapping[str, Any]) -> Any:
        """Insert a row.

        `data` has already been filtered against the spec, so an adapter may
        assume every key names an editable field.
        """
        ...

    async def update(self, obj: Any, data: Mapping[str, Any]) -> Any:
        """Apply `data` to an existing row."""
        ...

    async def delete(self, obj: Any) -> None: ...

    async def bulk_update(self, query: ListQuery, data: Mapping[str, Any]) -> int:
        """Update every row matching `query`, returning how many were affected.

        Implementations process in chunks: a bulk action over a large selection
        must not load the whole result set into memory.
        """
        ...

    async def bulk_delete(self, query: ListQuery) -> int: ...

    # -- inspection ---------------------------------------------------------

    def primary_key_of(self, obj: Any) -> PrimaryKey: ...

    def snapshot(self, obj: Any) -> dict[str, Any]:
        """Capture the current field values, for diffing into an audit record."""
        ...

    def label_for(self, obj: Any) -> str:
        """A short human-readable identifier, used in headings and audit logs."""
        ...


@runtime_checkable
class Backend(Protocol):
    """Binds FastFort to one ORM and one database connection."""

    @property
    def name(self) -> str:
        """Short identifier, e.g. ``"sqlalchemy"``. Appears in diagnostics."""
        ...

    @property
    def dialect(self) -> str:
        """The database in use: ``sqlite``, ``postgresql`` or ``mysql``."""
        ...

    def supports(self, model: type) -> bool:
        """Whether this backend can handle `model`.

        Used to produce a clear error when a Tortoise model is registered against
        a SQLAlchemy backend, instead of failing later with an attribute error.
        """
        ...

    def introspect(self, model: type, *, key: str) -> ModelSpec:
        """Derive the canonical description of `model`."""
        ...

    def adapter(self, model: type, uow: UnitOfWork) -> ModelAdapter:
        """Return an adapter for `model` bound to `uow`."""
        ...

    def unit_of_work(self) -> UnitOfWork:
        """Open a new transactional scope."""
        ...

    async def check_connection(self) -> None:
        """Verify the database is reachable, raising `AdapterError` if not.

        Called by ``fastfort check`` so that a misconfigured connection string is
        reported at deploy time rather than on the first request.
        """
        ...
