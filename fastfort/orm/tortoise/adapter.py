"""Reading and writing rows through Tortoise.

The counterpart of `orm/sqlalchemy/adapter.py`, holding the same two rules.

*The adapter never commits.* It writes; the unit of work that owns the
transaction decides whether those writes survive. A request that fails halfway
leaves nothing behind.

*`FieldSpec.editable` is the mass-assignment boundary.* `_writable` filters
every write against it, and there is deliberately no second flag that could
disagree -- the same sentence, and the same single filter, as on the other side.

Where the two differ is only in how a query is built. SQLAlchemy composes a
`Select`; Tortoise composes a `QuerySet` from keyword arguments, so
`ListQuery`'s validated filters become a dict of `field__op=value` pairs. The
names in that dict have already been checked against the spec by
`ListQuery.from_params`, which is why building them by string concatenation here
is safe: none of it is text a request chose.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeAlias

from tortoise.exceptions import DoesNotExist, IntegrityError
from tortoise.expressions import Q
from tortoise.queryset import QuerySet

from fastfort.core.exceptions import AdapterError, ObjectNotFound, ValidationError
from fastfort.core.registry import default_model_key
from fastfort.orm.base import PrimaryKey, RelatedChoice
from fastfort.spec import (
    DeletionEffect,
    DeletionPlan,
    FieldSpec,
    Filter,
    FilterOperator,
    ListQuery,
    ModelSpec,
    Page,
    RelatedRows,
)

from ..coerce import as_bool, coerce_filter_value
from ..sqlalchemy.dialects import DialectProfile
from ..sqlalchemy.introspect import humanise, pluralise

__all__ = ["TortoiseAdapter"]

#: Rows touched per statement in a bulk operation, matching the SQLAlchemy side.
BULK_CHUNK_SIZE = 500

#: How `FilterOperator` spells each comparison in Tortoise's own vocabulary.
#:
#: Every one of these is a lookup Tortoise implements identically on SQLite,
#: PostgreSQL and MySQL, which is the rule `FilterOperator` itself lives by --
#: so the two backends answer the same question the same way, and a saved link
#: means what it meant before the ORM was swapped.
_LOOKUPS: dict[FilterOperator, str] = {
    FilterOperator.EXACT: "",
    FilterOperator.IEXACT: "__iexact",
    FilterOperator.NE: "__not",
    FilterOperator.LT: "__lt",
    FilterOperator.LTE: "__lte",
    FilterOperator.GT: "__gt",
    FilterOperator.GTE: "__gte",
    FilterOperator.IN: "__in",
    FilterOperator.ISNULL: "__isnull",
    FilterOperator.RANGE: "__range",
    FilterOperator.ICONTAINS: "__icontains",
    FilterOperator.ISTARTSWITH: "__istartswith",
    FilterOperator.IENDSWITH: "__iendswith",
}

#: Operators that match text and therefore never coerce their value.
_TEXT_OPERATORS = frozenset(
    {
        FilterOperator.ICONTAINS,
        FilterOperator.ISTARTSWITH,
        FilterOperator.IENDSWITH,
        FilterOperator.IEXACT,
    }
)

#: A to-many write: each relation's name, and the rows to link to it.
#:
#: Declared here rather than spelled inline, because the class below has a
#: method called `list` -- as every adapter does -- and inside its body an
#: annotation saying `list[Any]` resolves to that method rather than to the
#: builtin. Naming the shape once, out here, is clearer than the alternatives.
_ManyValues: TypeAlias = dict[str, list[Any]]


class TortoiseAdapter:
    """A `ModelAdapter` over one Tortoise model."""

    def __init__(
        self,
        model: type,
        spec: ModelSpec,
        uow: Any,
        profile: DialectProfile,
        *,
        search_fields: Sequence[str] = (),
        select_related: Sequence[str] = (),
        prefetch_related: Sequence[str] = (),
        count_cap: int | None = None,
        resolve_key: Callable[[type], str] = default_model_key,
    ) -> None:
        self.model = model
        #: The same class, typed loosely. `model: type` is what the `Backend`
        #: protocol hands over, and a bare `type` carries none of Tortoise's own
        #: API -- so every call through it would need its own cast. One here
        #: instead of nine.
        self._orm: Any = model
        self._spec = spec
        self._uow = uow
        self._profile = profile
        self._search_fields = tuple(search_fields) or spec.searchable_fields
        self._select_related = tuple(select_related)
        self._prefetch_related = tuple(prefetch_related)
        self._count_cap = count_cap
        self._resolve_key = resolve_key

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    # -- reading ------------------------------------------------------------

    def _queryset(self, query: ListQuery) -> QuerySet[Any]:
        """One `QuerySet` carrying this query's filters and search.

        `using_db` binds it to the unit of work's connection, which is what puts
        every read and write of a request inside the same transaction. Without
        it Tortoise takes a fresh connection from the pool and a rollback leaves
        the writes behind.
        """
        queryset: QuerySet[Any] = self._orm.all().using_db(self._uow.connection)

        conditions = [self._condition(condition) for condition in query.filters]
        if query.search and self._search_fields:
            # `icontains` on every searchable field, or-ed. Tortoise lowers both
            # sides where the database has no ILIKE, which is the same rule
            # `dialects.icontains` follows on the other backend.
            clauses = [Q(**{f"{name}__icontains": query.search}) for name in self._search_fields]
            conditions.append(Q(*clauses, join_type="OR"))

        for condition in conditions:
            queryset = queryset.filter(condition)
        return queryset

    def _condition(self, condition: Filter) -> Q:
        """One validated `field op value` as a Tortoise `Q`.

        The field name reached here through `ListQuery.from_params`, which
        checked it against the spec's own list -- so it is a name this package
        knows, not a string a request chose, and building a lookup out of it is
        not injection.
        """
        lookup = _LOOKUPS.get(condition.operator)
        if lookup is None:
            raise ValidationError(f"{condition.operator.value!r} is not a supported filter.")

        # A to-one relation is filtered by the identity of its target, which is
        # what the dropdown submits. Tortoise reads `category_id` for that.
        field = self._spec.get(condition.field)
        name = self._column_for(condition.field, field)

        # Coerced against the column's type, through the same table the
        # SQLAlchemy backend uses. Left as strings, `?active__in=0` compared
        # the *text* "0" against a boolean column and matched the rows it was
        # meant to exclude -- silently, because a string is a perfectly valid
        # thing to compare.
        raw: Any = condition.value
        if condition.operator is FilterOperator.ISNULL:
            value: Any = as_bool(str(_first(raw)))
        elif condition.operator.takes_many_values:
            parts = list(raw) if isinstance(raw, tuple) else [raw]
            if condition.operator is FilterOperator.RANGE and len(parts) != 2:
                raise ValidationError(
                    f"Filter {condition.field!r} needs exactly two values, got {len(parts)}.",
                    field_errors={condition.field: ["Provide a lower and an upper bound."]},
                )
            value = [coerce_filter_value(field, str(part), condition.field) for part in parts]
        elif condition.operator in _TEXT_OPERATORS:
            # A text match compares against text whatever the column is, so
            # coercing "20" to an integer here would refuse a perfectly good
            # `name__icontains=20`.
            value = str(_first(raw))
        else:
            value = coerce_filter_value(field, str(_first(raw)), condition.field)

        return Q(**{f"{name}{lookup}": value})

    def _related(self, queryset: QuerySet[Any]) -> QuerySet[Any]:
        if self._select_related:
            queryset = queryset.select_related(*self._select_related)
        if self._prefetch_related:
            queryset = queryset.prefetch_related(*self._prefetch_related)
        return queryset

    def _ordering(self, query: ListQuery) -> list[str]:
        """`ORDER BY` terms in Tortoise's `-name` spelling.

        The primary key is appended as a tiebreaker for the same reason the
        other backend appends it: without one, two rows with equal sort keys can
        swap places between pages and one of them is never shown.
        """
        terms: list[str] = []
        for sort in query.ordering:
            # A to-one relation sorts through its foreign key, the same column
            # `_condition` filters through. Handed the relation's own name,
            # Tortoise raises "Filtering by relation is not possible" -- and the
            # list header renders every sortable field as a link, so that was a
            # 500 one click away on any list showing a relation.
            name = self._column_for(sort.field, self._spec.get(sort.field))
            terms.append(f"-{name}" if sort.descending else name)
        terms.extend(self._spec.primary_key)
        return terms

    def _column_for(self, name: str, field: FieldSpec | None) -> str:
        """The column a query names for `field` -- itself, unless it is a relation.

        A to-one relation is read through the key column beside it. That column
        is asked for by `source_field` rather than assembled as `f"{name}_id"`,
        because a project may name it, and a guess would filter and sort on a
        column that is not there.
        """
        if field is None or not field.is_relation or field.type.is_multi_valued:
            return name
        source = getattr(self._orm._meta.fields_map.get(name), "source_field", None)
        return str(source) if source else f"{name}_id"

    async def list(self, query: ListQuery) -> Page[Any]:
        queryset = self._related(self._queryset(query)).order_by(*self._ordering(query))
        items = tuple(await queryset.limit(query.limit).offset(query.offset))
        return Page(
            items=items,
            total=await self.count(query),
            page=query.page,
            page_size=query.page_size,
        )

    async def count(self, query: ListQuery) -> int:
        queryset = self._queryset(query)
        if self._count_cap is not None:
            # Counting every row of a large table dominates the request.
            # Stopping at a cap lets the interface show "10 000+" instead.
            return len(await queryset.limit(self._count_cap).values_list("pk"))
        return int(await queryset.count())

    async def get(self, pk: PrimaryKey) -> Any | None:
        queryset = self._related(self._orm.all().using_db(self._uow.connection))
        with contextlib.suppress(DoesNotExist, ValueError):
            return await queryset.get(**{self._spec.primary_key[0]: pk[0]})
        return None

    async def require(self, pk: PrimaryKey) -> Any:
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
        # What the caller declared wins; the conventional names are the fallback
        # for a model that never said which of its columns its `__str__` is
        # built from -- the same rule, and the same list, as the other backend.
        names = [name for name in search_fields if name in target._meta.fields_map] or [
            name
            for name in ("name", "title", "label", "slug", "email", "username")
            if name in target._meta.fields_map
        ]

        queryset = target.all().using_db(self._uow.connection).limit(limit)
        if term and names:
            queryset = queryset.filter(
                Q(*[Q(**{f"{name}__icontains": term}) for name in names], join_type="OR")
            )

        return [RelatedChoice(value=obj.pk, label=str(obj)) for obj in await queryset]

    # -- writing ------------------------------------------------------------

    async def create(self, data: Mapping[str, Any]) -> Any:
        writable = self._writable(data)
        scalars, relations, many = await self._split(writable)

        obj = self._orm(**scalars, **relations)
        await self._save(obj)
        await self._set_many(obj, many)
        return obj

    async def update(self, obj: Any, data: Mapping[str, Any]) -> Any:
        writable = self._writable(data)
        scalars, relations, many = await self._split(writable)

        for name, value in {**scalars, **relations}.items():
            setattr(obj, name, value)
        await self._save(obj)
        await self._set_many(obj, many)
        return obj

    async def delete(self, obj: Any) -> None:
        await obj.delete(using_db=self._uow.connection)

    async def _save(self, obj: Any) -> None:
        """Write, turning a constraint violation into something a form can show.

        The database is the last line of validation and catches what the spec
        cannot: a unique index, a check constraint, a foreign key that no longer
        points anywhere. Left raw those surface as a 500 -- the person filling
        the form sees a stack trace instead of the field they have to change,
        and their input is gone. `AdapterError` is what the views already catch
        and re-render the form with.
        """
        try:
            await obj.save(using_db=self._uow.connection)
        except IntegrityError as exc:
            raise AdapterError(_constraint_message(exc)) from exc

    async def _split(
        self, data: Mapping[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any], _ManyValues]:
        """Sort a write into scalars, to-one relations and to-many relations.

        Three groups because Tortoise writes them three ways: scalars are
        attributes, a to-one is an instance assigned before the save, and a
        to-many can only be set once the row has a primary key to link against.
        """
        scalars: dict[str, Any] = {}
        relations: dict[str, Any] = {}
        many: _ManyValues = {}

        for name, value in data.items():
            field = self._spec.field(name)
            if not field.is_relation:
                scalars[name] = value
            elif field.type.is_multi_valued:
                items = value if isinstance(value, list | tuple | set) else [value]
                many[name] = [
                    await self._resolve(name, item) for item in items if item not in (None, "")
                ]
            else:
                relations[name] = None if value in (None, "") else await self._resolve(name, value)
        return scalars, relations, many

    async def _set_many(self, obj: Any, many: _ManyValues) -> None:
        """Replace each many-to-many, rather than adding to it.

        A form submits the whole selection, so anything not in it was
        deselected. Adding without clearing would make deselection impossible
        through the admin, which is how a tag nobody wants stays on a product
        for ever.
        """
        for name, items in many.items():
            manager = getattr(obj, name)
            await manager.clear(using_db=self._uow.connection)
            if items:
                await manager.add(*items, using_db=self._uow.connection)

    async def _resolve(self, field: str, value: Any) -> Any:
        """A related row from either an instance or a bare identity.

        Forms submit identities and programmatic callers pass objects; both have
        to work, so anything that is not already an instance is looked up.
        """
        target = self._relation_target(field)
        if isinstance(value, target):
            return value
        try:
            return await target.all().using_db(self._uow.connection).get(pk=value)
        except (DoesNotExist, ValueError):
            raise ValidationError(f"No {target.__name__} with key {value!r}.") from None

    def _relation_target(self, field: str) -> Any:
        return self._orm._meta.fields_map[field].related_model

    def _writable(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Discard anything the spec does not mark editable.

        This is the mass-assignment boundary. A request that posts
        `is_superuser=true` for a field the admin never rendered is dropped
        here, whichever endpoint it arrived through -- the same filter, and the
        same single source of truth, as the SQLAlchemy adapter.
        """
        allowed = {field.name for field in self._spec.editable_fields}
        return {name: value for name, value in data.items() if name in allowed}

    # -- bulk ---------------------------------------------------------------

    async def bulk_update(self, query: ListQuery, data: Mapping[str, Any]) -> int:
        writable = self._writable(data)
        if not writable:
            return 0
        # Scalars only: a to-many cannot be set by one statement, and quietly
        # dropping one would be worse than the action reporting no such field.
        scalars = {
            name: value
            for name, value in writable.items()
            if not self._spec.field(name).is_relation
        }
        if not scalars:
            return 0
        return int(await self._queryset(query).update(**scalars))

    async def bulk_delete(self, query: ListQuery) -> int:
        return int(await self._queryset(query).delete())

    # -- inspection ---------------------------------------------------------

    def primary_key_of(self, obj: Any) -> PrimaryKey:
        return (getattr(obj, self._spec.primary_key[0]),)

    def snapshot(self, obj: Any) -> dict[str, Any]:
        """Current values, with to-one relations reduced to their identity.

        Sensitive fields are omitted entirely rather than masked, so their
        values cannot reach an audit record even by accident.
        """
        state: dict[str, Any] = {}
        for field in self._spec:
            if field.sensitive or field.type.is_multi_valued:
                continue
            if field.is_relation:
                state[field.name] = getattr(obj, f"{field.name}_id", None)
            else:
                state[field.name] = getattr(obj, field.name, None)
        return state

    def label_for(self, obj: Any) -> str:
        text = str(obj)
        return text or f"{self._spec.verbose_name} {self.primary_key_of(obj)}"

    async def deletion_plan(self, objects: Sequence[Any]) -> DeletionPlan:
        """What deleting `objects` would do to everything pointing at them.

        Read from the *child* side, the same as the SQLAlchemy planner: every
        backward relation Tortoise resolved onto this model is a foreign key
        somewhere else, and its `on_delete` says what happens to those rows.
        Many-to-many is excluded on purpose -- only the association rows go, not
        the related objects.
        """
        if not objects:
            return DeletionPlan(related=())

        keys = [self.primary_key_of(obj)[0] for obj in objects]
        groups: list[RelatedRows] = []

        meta = self._orm._meta
        for name in sorted(set(getattr(meta, "backward_fk_fields", ()) or ())):
            field = meta.fields_map[name]
            target = field.related_model
            column = getattr(field, "relation_field", None) or f"{meta.db_table}_id"

            queryset = target.all().using_db(self._uow.connection).filter(**{f"{column}__in": keys})
            rows = list(await queryset.limit(DELETION_SAMPLE + 1))
            if not rows:
                continue

            effect = _effect_of(target, column)
            label = humanise(target.__name__)
            groups.append(
                RelatedRows(
                    model_key=self._resolve_key(target),
                    label=pluralise(label),
                    singular=label,
                    # Named from the *related* model's side, which is the side
                    # whose foreign key is at stake: "Products.category" reads
                    # unambiguously where "category" alone does not.
                    field=column.removesuffix("_id"),
                    effect=effect,
                    count=len(rows) if len(rows) <= DELETION_SAMPLE else await queryset.count(),
                    truncated=len(rows) > DELETION_SAMPLE,
                    samples=tuple(str(row) for row in rows[:DELETION_SAMPLE]),
                )
            )

        return DeletionPlan(related=tuple(groups))


#: Rows named in a deletion confirmation, matching the SQLAlchemy planner.
DELETION_SAMPLE = 5


def _effect_of(target: Any, column: str) -> DeletionEffect:
    """What happens to the rows pointing here when the parent goes.

    Read from the child's own field, because that is where Tortoise declares it
    -- and reported honestly: a nullable foreign key with no cascade is nulled,
    not deleted, and claiming otherwise would make the confirmation page lie
    about what the button does.
    """
    field = target._meta.fields_map.get(column.removesuffix("_id"))
    rule = str(getattr(field, "on_delete", "")).upper()
    if "CASCADE" in rule:
        return DeletionEffect.DELETE
    if "SET NULL" in rule or "SET_NULL" in rule:
        return DeletionEffect.CLEAR
    return DeletionEffect.PROTECT


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, tuple) else value


def _constraint_message(error: Exception) -> str:
    """What to tell somebody whose save the database refused.

    Only the first line, for the same reason as on the other side: the driver's
    text names the constraint and often the column, which is useful, but it also
    carries the whole failing row -- a screenful, possibly holding data the
    person should not see echoed back.
    """
    detail = str(error).strip().splitlines()
    first = detail[0].strip() if detail else ""
    return first or "The database rejected this change."
