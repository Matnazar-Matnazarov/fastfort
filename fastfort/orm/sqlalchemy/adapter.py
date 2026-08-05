"""Reading and writing rows through an async SQLAlchemy session.

The adapter never commits. It writes into the session it was handed, and the unit
of work that owns that session decides when the change becomes durable. A request
that fails halfway therefore leaves nothing behind.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql.ranges import MultiRange, Range
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from fastfort.core.exceptions import AdapterError, ObjectNotFound, ValidationError
from fastfort.core.registry import default_model_key
from fastfort.orm.base import PrimaryKey, RelatedChoice
from fastfort.spec import DeletionPlan, FieldSpec, FieldType, ListQuery, ModelSpec, Page

from .deletion import collect_deletion
from .dialects import DialectProfile, icontains
from .query import QueryBuilder

__all__ = ["SQLAlchemyAdapter", "constraint_message"]

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
        resolve_key: Callable[[type], str] = default_model_key,
    ) -> None:
        self.model = model
        self.session = session
        self._spec = spec
        self._count_cap = count_cap
        self._resolve_key = resolve_key
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
        self, field: str, term: str, *, limit: int, search_fields: Sequence[str] = ()
    ) -> Sequence[RelatedChoice]:
        spec = self._spec.field(field)
        if spec.relation is None:
            raise ValidationError(f"{field!r} is not a relation field.")

        target = self._relation_target(field)
        # What the caller declared wins. The label a person reads is the target's
        # `__str__`, which cannot be searched in SQL, so the columns it is built
        # from have to be named -- and the target's own `ModelAdmin.search_fields`
        # is where a project has already said which those are. The conventional
        # names are the fallback for a model that never said.
        declared = [name for name in search_fields if hasattr(target, name)]
        target_spec_names: list[str] = declared or [
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
        await self._flush()
        return obj

    async def update(self, obj: Any, data: Mapping[str, Any]) -> Any:
        await self._apply(obj, data)
        await self._flush()
        return obj

    async def delete(self, obj: Any) -> None:
        await self.session.delete(obj)
        await self._flush()

    async def deletion_plan(self, objects: Sequence[Any]) -> DeletionPlan:
        """What deleting `objects` would do to everything pointing at them.

        Read-only, and read *before* the delete: the confirmation page needs it
        to say what else goes, and the delete view needs it to refuse a write the
        database would reject anyway -- a foreign key that is `NOT NULL` with no
        cascade behind it turns an ordinary delete into an IntegrityError, and a
        stack trace is not an answer to "can I remove this category".
        """
        return await collect_deletion(
            self.session,
            self.model,
            objects,
            resolve_key=self._resolve_key,
            label_of=_row_label,
        )

    async def _flush(self) -> None:
        """Flush, turning a constraint violation into something a form can show.

        The database is the last line of validation and it catches what the spec
        cannot: a unique index, a check constraint, a foreign key that no longer
        points anywhere. Left as `IntegrityError` those surfaced as a 500 -- the
        person filling the form saw a stack trace instead of the field they had
        to change, and their input was gone. `AdapterError` is what the views
        already catch and re-render the form with.
        """
        try:
            await self.session.flush()
        except sa.exc.IntegrityError as exc:
            raise AdapterError(constraint_message(exc)) from exc

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
                setattr(obj, name, _coerce_for_driver(field, value))

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


def constraint_message(error: sa.exc.IntegrityError) -> str:
    """What to tell someone whose save the database refused.

    The driver's text names the constraint and often the column, which is more
    use than "integrity error" -- but it also carries the whole failing row,
    which can be a screenful and may contain data the person should not see
    echoed back. Only the first line is kept.
    """
    detail = str(getattr(error, "orig", error)).strip().splitlines()
    first = detail[0].strip() if detail else ""
    # asyncpg stringifies as "<class 'asyncpg.exceptions.X'>: the real message".
    # The class name is noise to everyone who is not reading a traceback.
    _, separator, tail = first.partition(">: ")
    message = (tail if separator else first).strip()
    # No "could not be saved" prefix: the banner that renders this already says
    # so, and repeating it produced "This could not be saved: This could not be
    # saved: duplicate key…".
    return message or "The database rejected this change."


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


def _row_label(obj: Any) -> str:
    """Name a row of *any* model, for a deletion plan.

    `label_for` knows one model and reads its spec; this one is handed rows from
    across the schema, so it falls back to the class name and the identity. The
    `__str__` it calls belongs to the project and may reach for a relation that
    was never loaded, which raises rather than returning a bad string -- and a
    confirmation page must not 500 because one related model's `__str__` is
    chatty.
    """
    try:
        text = _display(obj)
    except Exception:
        text = ""
    if text:
        return text
    identity = _identity(obj)
    return f"{type(obj).__name__} {identity}" if identity is not None else type(obj).__name__


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


# ---------------------------------------------------------------------------
# Driver-specific write conversions.
#
# `fastfort/admin/values.py` (Phase 2, the column-types phase) produces plain,
# portable Python values for every field type -- it may not import SQLAlchemy
# at all, let alone a driver. This is the one place allowed to know that the
# project's asyncpg driver disagrees with some of those plain values, verified
# against a live PostGIS sandbox rather than assumed (see the phase report for
# the full session):
#
#   - `inet`/`cidr`/`macaddr` take a plain `str` outright, and so do
#     SQLAlchemy's own `postgresql.HSTORE` (a `dict`) and `postgresql.MONEY`
#     (a `str` -- a `Decimal` is refused). None of those need converting here.
#   - `bit`/`varbit` refuse a `str` of "0"/"1" characters -- asyncpg's codec
#     wants its own `BitString` instead.
#   - `int4range`/`daterange`/etc. refuse a plain tuple or a bare
#     `sqlalchemy.dialects.postgresql.Range` sent through `text()` with no
#     type info, but *do* accept one assigned to a properly-typed mapped
#     column, because the asyncpg dialect's own bind processor converts it --
#     so the fix is building that `Range`/`MultiRange` here, not an asyncpg
#     type. `MULTIRANGE` is the same story, one level up.
# ---------------------------------------------------------------------------


def _coerce_for_driver(field: FieldSpec, value: Any) -> Any:
    """The portable value `values.py` produced, turned into whatever this
    project's driver actually accepts for `field`'s type -- a no-op for every
    type except the three above.
    """
    if value is None:
        return None
    if field.type is FieldType.BITS:
        return _bits_for_driver(value)
    if field.type is FieldType.RANGE:
        return _range_for_driver(value)
    if field.type is FieldType.MULTIRANGE:
        return _multirange_for_driver(value)
    return value


def _bits_for_driver(value: Any) -> Any:
    if not isinstance(value, str):
        return value  # already a driver object -- a programmatic caller's, not a form's
    try:
        import asyncpg  # type: ignore[import-untyped]
    except ImportError:
        # No asyncpg installed: `bit`/`varbit` are PostgreSQL-only types to
        # begin with, so a project without it could not have reached this
        # column either. Left as-is rather than raising here, so the error the
        # person sees is the driver's own, not a confusing one from this shim.
        return value
    return asyncpg.BitString(value)


def _range_for_driver(value: Any) -> Any:
    """`(lower, upper, bounds)` from `values.py` into a real `Range` --
    `bounds == "empty"` is the one shape that tuple cannot otherwise express.
    """
    if not isinstance(value, tuple) or len(value) != 3:
        return value
    lower, upper, bounds = value
    if bounds == "empty":
        return Range(empty=True)
    return Range(lower, upper, bounds=bounds)


def _multirange_for_driver(value: Any) -> Any:
    if not isinstance(value, list | tuple):
        return value
    return MultiRange(_range_for_driver(item) for item in value)
