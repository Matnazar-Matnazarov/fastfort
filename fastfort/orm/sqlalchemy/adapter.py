"""Reading and writing rows through an async SQLAlchemy session.

The adapter never commits. It writes into the session it was handed, and the unit
of work that owns that session decides when the change becomes durable. A request
that fails halfway therefore leaves nothing behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from fastfort.core.exceptions import ObjectNotFound, ValidationError
from fastfort.orm.base import PrimaryKey, RelatedChoice
from fastfort.spec import ListQuery, ModelSpec, Page

from .dialects import DialectProfile, icontains
from .query import QueryBuilder

__all__ = ["SQLAlchemyAdapter"]

#: Rows touched per statement in a bulk operation. Bounded so that an action over
#: a large selection cannot pull the whole table into memory.
BULK_CHUNK_SIZE = 500


class SQLAlchemyAdapter:
    """A `ModelAdapter` over one mapped class, bound to one session."""

    def __init__(
        self,
        model: type,
        spec: ModelSpec,
        session: AsyncSession,
        profile: DialectProfile,
        *,
        search_fields: Sequence[str] = (),
        select_related: Sequence[str] = (),
        prefetch_related: Sequence[str] = (),
        count_cap: int | None = None,
    ) -> None:
        self.model = model
        self.session = session
        self._spec = spec
        self._count_cap = count_cap
        self._builder = QueryBuilder(
            model,
            spec,
            profile,
            search_fields=search_fields,
            select_related=select_related,
            prefetch_related=prefetch_related,
        )

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    # -- reading ------------------------------------------------------------

    async def list(self, query: ListQuery) -> Page[Any]:
        rows = await self.session.execute(self._builder.select(query))
        items = tuple(rows.unique().scalars().all())
        return Page(
            items=items,
            total=await self.count(query),
            page=query.page,
            page_size=query.page_size,
        )

    async def count(self, query: ListQuery) -> int:
        statement = self._builder.count(query)
        if self._count_cap is not None:
            # Counting every row of a large table dominates the request. Stopping
            # at a cap lets the interface show "10 000+" instead of stalling.
            statement = self._builder.count(query).limit(self._count_cap)
        result = await self.session.execute(statement)
        return int(result.scalar_one())

    async def get(self, pk: PrimaryKey) -> Any | None:
        result = await self.session.execute(self._builder.by_primary_key(pk))
        return result.unique().scalars().first()

    async def require(self, pk: PrimaryKey) -> Any:
        """Like `get`, but raises when the row is absent."""
        obj = await self.get(pk)
        if obj is None:
            raise ObjectNotFound(f"No {self._spec.verbose_name} with key {pk!r}.")
        return obj

    async def related_choices(
        self, field: str, term: str, *, limit: int
    ) -> Sequence[RelatedChoice]:
        spec = self._spec.field(field)
        if spec.relation is None:
            raise ValidationError(f"{field!r} is not a relation field.")

        target = self._relation_target(field)
        target_spec_names: list[str] = [
            name
            for name in ("name", "title", "label", "slug", "email", "username")
            if hasattr(target, name)
        ]

        statement = sa.select(target).limit(limit)
        if term and target_spec_names:
            statement = statement.where(
                sa.or_(
                    *(
                        icontains(getattr(target, name), term, self._builder.profile)
                        for name in target_spec_names
                    )
                )
            )

        rows = (await self.session.execute(statement)).unique().scalars().all()
        return [RelatedChoice(value=_identity(obj), label=_display(obj)) for obj in rows]

    # -- writing ------------------------------------------------------------

    async def create(self, data: Mapping[str, Any]) -> Any:
        obj = self.model()
        await self._apply(obj, data)
        self.session.add(obj)
        # Flush rather than commit: the caller owns the transaction, but generated
        # keys and defaults have to be readable before it decides.
        await self.session.flush()
        return obj

    async def update(self, obj: Any, data: Mapping[str, Any]) -> Any:
        await self._apply(obj, data)
        await self.session.flush()
        return obj

    async def delete(self, obj: Any) -> None:
        await self.session.delete(obj)
        await self.session.flush()

    async def bulk_update(self, query: ListQuery, data: Mapping[str, Any]) -> int:
        writable = self._writable(data)
        if not writable:
            return 0

        affected = 0
        for chunk in await self._matching_keys(query):
            statement = sa.update(self.model).where(self._key_predicate(chunk)).values(**writable)
            result = await self.session.execute(statement)
            affected += cast("Any", result).rowcount or 0
        await self.session.flush()
        return affected

    async def bulk_delete(self, query: ListQuery) -> int:
        affected = 0
        for chunk in await self._matching_keys(query):
            result = await self.session.execute(
                sa.delete(self.model).where(self._key_predicate(chunk))
            )
            affected += cast("Any", result).rowcount or 0
        await self.session.flush()
        return affected

    # -- inspection ---------------------------------------------------------

    def primary_key_of(self, obj: Any) -> PrimaryKey:
        return tuple(getattr(obj, name) for name in self._spec.primary_key)

    def snapshot(self, obj: Any) -> dict[str, Any]:
        """Current values, with to-one relations reduced to their identity.

        Sensitive fields are omitted entirely rather than masked here, so their
        values cannot reach an audit record even by accident.
        """
        state: dict[str, Any] = {}
        for field in self._spec:
            if field.sensitive or field.type.is_multi_valued:
                continue
            value = getattr(obj, field.name, None)
            state[field.name] = _identity(value) if field.is_relation else value
        return state

    def label_for(self, obj: Any) -> str:
        return _display(obj) or f"{self._spec.verbose_name} {self.primary_key_of(obj)}"

    # -- internals ----------------------------------------------------------

    def _writable(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Discard anything the spec does not mark editable.

        This is the mass-assignment boundary. A request that posts
        ``is_superuser=true`` for a field the admin never rendered is dropped
        here, whichever endpoint it arrived through.
        """
        allowed = {field.name for field in self._spec.editable_fields}
        return {name: value for name, value in data.items() if name in allowed}

    async def _apply(self, obj: Any, data: Mapping[str, Any]) -> None:
        for name, value in self._writable(data).items():
            field = self._spec.field(name)
            if field.is_relation:
                await self._assign_relation(
                    obj, field.name, value, is_list=field.type.is_multi_valued
                )
            else:
                setattr(obj, name, value)

    async def _assign_relation(self, obj: Any, name: str, value: Any, *, is_list: bool) -> None:
        """Set a relation from either model instances or bare identities.

        Forms submit identities, while programmatic callers pass objects; both
        have to work, so anything that is not already a mapped instance is looked
        up by primary key.
        """
        target = self._relation_target(name)

        if is_list:
            values = value if isinstance(value, list | tuple | set) else [value]
            setattr(
                obj,
                name,
                [await self._resolve(target, item) for item in values if item is not None],
            )
            return

        setattr(obj, name, None if value in (None, "") else await self._resolve(target, value))

    async def _resolve(self, target: type[Any], value: Any) -> Any:
        if isinstance(value, target):
            return value
        found: Any = await self.session.get(target, _coerce_key(target, value))
        if found is None:
            raise ValidationError(f"No {target.__name__} with key {value!r}.")
        return found

    def _relation_target(self, name: str) -> type[Any]:
        attribute = getattr(self.model, name)
        return cast("type[Any]", attribute.property.mapper.class_)

    async def _matching_keys(self, query: ListQuery) -> Sequence[Sequence[PrimaryKey]]:
        """Primary keys of every matching row, split into bounded chunks."""
        unpaged = ListQuery(
            search=query.search, filters=query.filters, ordering=(), page=1, page_size=1
        )
        rows = (await self.session.execute(self._builder.key_select(unpaged))).all()
        keys: Sequence[PrimaryKey] = [tuple(row) for row in rows]
        return [keys[i : i + BULK_CHUNK_SIZE] for i in range(0, len(keys), BULK_CHUNK_SIZE)]

    def _key_predicate(self, keys: Sequence[PrimaryKey]) -> sa.ColumnElement[bool]:
        columns = [self._attribute(name) for name in self._spec.primary_key]
        if len(columns) == 1:
            return columns[0].in_([key[0] for key in keys])
        # Composite keys cannot use a single IN, so they become an OR of ANDs.
        return sa.or_(
            *(
                sa.and_(*(column == value for column, value in zip(columns, key, strict=True)))
                for key in keys
            )
        )

    def _attribute(self, name: str) -> InstrumentedAttribute[Any]:
        return cast("InstrumentedAttribute[Any]", getattr(self.model, name))


def _identity(obj: Any) -> Any:
    """The primary key of a mapped instance, or the value itself."""
    if obj is None:
        return None
    state = sa.inspect(obj, raiseerr=False)
    if state is None or not hasattr(state, "identity"):
        return obj
    identity = state.identity
    if identity is None:
        return None
    return identity[0] if len(identity) == 1 else identity


def _display(obj: Any) -> str:
    """A model's own ``__str__``, when it defines one."""
    if obj is None:
        return ""
    if any("__str__" in vars(cls) for cls in type(obj).__mro__ if cls is not object):
        return str(obj)
    for name in ("name", "title", "label", "email", "username", "slug"):
        value = getattr(obj, name, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _coerce_key(target: type[Any], value: Any) -> Any:
    """Convert a submitted identity to the target's primary key type.

    A form posts strings. PostgreSQL's driver refuses "1" for an integer column
    outright, where SQLite and MySQL cast it -- so without this a relation
    dropdown works on two databases and fails on the third.
    """
    if not isinstance(value, str):
        return value

    mapper = sa.inspect(target, raiseerr=False)
    if mapper is None or len(mapper.primary_key) != 1:
        return value

    python_type: type[Any] | None
    try:
        python_type = mapper.primary_key[0].type.python_type
    except NotImplementedError:
        return value

    if python_type is int:
        try:
            return int(value)
        except ValueError:
            return value
    if python_type is uuid.UUID:
        try:
            return uuid.UUID(value)
        except ValueError:
            return value
    return value
