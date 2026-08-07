"""Turning a `ListQuery` into a SQLAlchemy statement.

Field names arriving here have already been checked against the model spec by
`ListQuery.from_params`, so this module never sees an attribute the model does
not have. What it still has to do is coerce string values to the column's type
and keep the SQL identical across the three supported databases.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.orm import InstrumentedAttribute, joinedload, selectinload

from fastfort.core.exceptions import ValidationError
from fastfort.spec import FieldSpec, Filter, FilterOperator, ListQuery, ModelSpec

from ..coerce import as_bool, coerce_filter_value
from .dialects import (
    DialectProfile,
    icontains,
    order_term,
    spatial_condition,
    vector_distance,
)

__all__ = ["QueryBuilder"]

#: Strings accepted as boolean true in a query string.
_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSY = frozenset({"0", "false", "f", "no", "n", "off", ""})

#: Separator for traversing one relation, as in ``category__name``.
_PATH_SEPARATOR = "__"

#: Where the search term has to appear, per string operator.
_TEXT_OPERATORS = {
    FilterOperator.ICONTAINS: "anywhere",
    FilterOperator.ISTARTSWITH: "start",
    FilterOperator.IENDSWITH: "end",
}


class QueryBuilder:
    """Builds SELECT statements for one model."""

    def __init__(
        self,
        model: type,
        spec: ModelSpec,
        profile: DialectProfile,
        *,
        search_fields: Sequence[str] = (),
        select_related: Sequence[str] = (),
        prefetch_related: Sequence[str] = (),
    ) -> None:
        self.model = model
        self.spec = spec
        self.profile = profile
        self.search_fields = tuple(search_fields) or spec.searchable_fields
        self.select_related = tuple(select_related)
        self.prefetch_related = tuple(prefetch_related)

    # -- public -------------------------------------------------------------

    def select(self, query: ListQuery) -> sa.Select[Any]:
        """A statement returning one page of rows, with relations preloaded."""
        statement = self._filtered(sa.select(self.model), query)
        statement = self._ordered(statement, query)
        statement = statement.limit(self._page_limit(query)).offset(query.offset)
        return self._with_eager_loads(statement)

    def _page_limit(self, query: ListQuery) -> int:
        """How many rows this page may hold.

        The page size, unless a similarity search asked for fewer. `k` is "the
        nearest ten", and the tenth is the last row there is -- so page two of
        a twenty-row list has nothing on it, and the window has to close rather
        than paging on into rows the search already ruled out.

        Never negative: an offset past `k` is a page beyond the end, and a
        negative limit is a database error rather than an empty page.
        """
        if query.vector is None or query.vector.limit is None:
            return query.limit
        return max(0, min(query.limit, query.vector.limit - query.offset))

    def count(self, query: ListQuery) -> sa.Select[Any]:
        """A statement returning the number of matching rows.

        Built from a subquery so that any joins the filters needed cannot
        multiply the count.
        """
        inner = self._filtered(sa.select(*self._primary_key_columns()), query)
        if query.vector is not None and query.vector.limit is not None:
            # The paginator counts what the list can actually reach. Without
            # this it promised twenty pages of a `k=10` search and nineteen of
            # them were empty.
            inner = self._ordered(inner, query).limit(query.vector.limit)
        return sa.select(sa.func.count()).select_from(inner.subquery())

    def key_select(self, query: ListQuery) -> sa.Select[Any]:
        """A statement returning only the primary keys of matching rows.

        Bulk actions work from this rather than from whole objects, so a large
        selection never has to be materialised.
        """
        return self._filtered(sa.select(*self._primary_key_columns()), query)

    def by_primary_key(self, values: tuple[Any, ...]) -> sa.Select[Any]:
        columns = self._primary_key_columns()
        if len(values) != len(columns):
            raise ValidationError(
                f"{self.spec.key} has a {len(columns)}-column primary key, "
                f"but {len(values)} value(s) were supplied."
            )
        # Coerced against the column type before comparing. PostgreSQL refuses
        # `integer = character varying` outright, where SQLite and MySQL quietly
        # cast -- so a key arriving from a URL as a string works on two databases
        # and fails on the third unless it is converted here.
        coerced = tuple(
            self._coerce(self.spec.get(name), str(value), name)
            for name, value in zip(self.spec.primary_key, values, strict=True)
        )
        statement: sa.Select[Any] = sa.select(self.model).where(
            sa.and_(*(column == value for column, value in zip(columns, coerced, strict=True)))
        )
        return self._with_eager_loads(statement)

    # -- clauses ------------------------------------------------------------

    def _filtered(self, statement: sa.Select[Any], query: ListQuery) -> sa.Select[Any]:
        joined: set[str] = set()

        for condition in query.filters:
            statement = self._join_for(statement, condition.field, joined)
            statement = statement.where(self._condition(condition))

        for spatial in query.spatial:
            # `None` where the database has no PostGIS. A saved link carrying a
            # spatial filter is still a perfectly good request for a list page,
            # so the condition is dropped rather than the page.
            predicate = spatial_condition(
                self._attribute(spatial.field),
                spatial.operator.value,
                spatial.geometry,
                spatial.distance,
                self.profile,
            )
            if predicate is not None:
                statement = statement.where(predicate)

        # A similarity search narrowed by a maximum distance. The *ordering*
        # half is in `_ordered`, because that is what a nearest-neighbour search
        # mostly is; this is only the "and no further than" part.
        if query.vector is not None and query.vector.within is not None:
            distance = self._vector_distance(query)
            if distance is not None:
                statement = statement.where(distance <= query.vector.within)

        if query.search and self.search_fields:
            clauses: list[sa.ColumnElement[bool]] = []
            for name in self.search_fields:
                statement = self._join_for(statement, name, joined)
                clauses.append(icontains(self._attribute(name), query.search, self.profile))
            statement = statement.where(sa.or_(*clauses))

        return statement

    def _ordered(self, statement: sa.Select[Any], query: ListQuery) -> sa.Select[Any]:
        terms: list[Any] = []
        joined: set[str] = set()

        # Nearest first, and before every other term. A similarity search is an
        # ordering, and one asked for alongside "sort by name" means "the
        # nearest, and break ties by name" -- not "by name, and break ties by
        # similarity", which would return the alphabet.
        if query.vector is not None:
            distance = self._vector_distance(query)
            if distance is not None:
                terms.append(distance.asc())

        for sort in query.ordering:
            statement = self._join_for(statement, sort.field, joined)
            terms.extend(
                order_term(
                    self._attribute(sort.field),
                    descending=sort.descending,
                    profile=self.profile,
                )
            )

        # Without a tiebreaker, two rows with equal sort keys can swap places
        # between pages and one of them is never shown.
        for name in self.spec.primary_key:
            terms.append(self._attribute(name).asc())

        return statement.order_by(*terms)

    def _vector_distance(self, query: ListQuery) -> Any:
        """The distance expression for this query's similarity search.

        Built once and used by both halves -- the ordering and the optional
        `within` bound -- because they have to agree. Two expressions built
        separately could drift apart on the metric and rank by one measure
        while filtering by another.
        """
        search = query.vector
        if search is None:
            return None
        field = self.spec.get(search.field)
        kind = field.vector.kind if field is not None and field.vector else "vector"
        return vector_distance(
            self._attribute(search.field),
            search.vector,
            search.metric.value,
            self.profile,
            kind,
        )

    def _with_eager_loads(self, statement: sa.Select[Any]) -> sa.Select[Any]:
        """Preload the relations the caller declared.

        Without this, rendering a list of N rows that shows a related name costs
        N extra queries. `joinedload` suits to-one relations; to-many uses
        `selectinload`, which keeps the row count of the main query intact.
        """
        options = [joinedload(self._attribute(name)) for name in self.select_related]
        options += [selectinload(self._attribute(name)) for name in self.prefetch_related]
        return statement.options(*options) if options else statement

    # -- resolution ---------------------------------------------------------

    def _primary_key_columns(self) -> tuple[InstrumentedAttribute[Any], ...]:
        return tuple(self._attribute(name) for name in self.spec.primary_key)

    def _foreign_key_attribute(self, name: str) -> InstrumentedAttribute[Any]:
        """The local column backing a to-one relation."""
        relation = self._own_attribute(self.model, name)
        mapper = cast("sa.orm.Mapper[Any]", sa.inspect(self.model))
        for column in relation.property.local_columns:
            prop = mapper.get_property_by_column(column)
            if prop is not None:
                return self._own_attribute(self.model, prop.key)
        raise ValidationError(f"{name!r} has no local column to filter on.")

    def _attribute(self, path: str) -> InstrumentedAttribute[Any]:
        """Resolve ``name`` or ``relation__name`` to a mapped attribute."""
        head, separator, tail = path.partition(_PATH_SEPARATOR)
        if not separator:
            return self._own_attribute(self.model, path)

        relation = self._own_attribute(self.model, head)
        target = relation.property.mapper.class_
        return self._own_attribute(target, tail)

    @staticmethod
    def _own_attribute(model: type, name: str) -> InstrumentedAttribute[Any]:
        attribute = getattr(model, name, None)
        if not isinstance(attribute, InstrumentedAttribute):
            raise ValidationError(f"{model.__name__} has no mapped attribute {name!r}.")
        return attribute

    def _join_for(self, statement: sa.Select[Any], path: str, joined: set[str]) -> sa.Select[Any]:
        """Join the relation a dotted path traverses, at most once per statement."""
        head, separator, _ = path.partition(_PATH_SEPARATOR)
        if not separator or head in joined:
            return statement
        joined.add(head)
        return statement.join(self._own_attribute(self.model, head))

    # -- conditions ---------------------------------------------------------

    def _condition(self, condition: Filter) -> sa.ColumnElement[bool]:
        field = self._field_spec(condition.field)
        # A to-one relation is filtered through its foreign key column: comparing
        # the relationship itself to a bare identity is not what `==` means to
        # the ORM, and the identity is what a relation dropdown submits.
        attribute = (
            self._foreign_key_attribute(condition.field)
            if field is not None and field.is_relation and not field.type.is_multi_valued
            else self._attribute(condition.field)
        )
        operator = condition.operator

        if operator is FilterOperator.ISNULL:
            return attribute.is_(None) if _as_bool(condition.value) else attribute.is_not(None)

        if operator is FilterOperator.IN:
            values = [self._coerce(field, item, condition.field) for item in _as_tuple(condition)]
            return attribute.in_(values)

        if operator is FilterOperator.RANGE:
            bounds: tuple[str, ...] = _as_tuple(condition)
            if len(bounds) != 2:
                raise ValidationError(
                    f"Filter {condition.field!r} needs exactly two values, got {len(bounds)}.",
                    field_errors={condition.field: ["Provide a lower and an upper bound."]},
                )
            low, high = (self._coerce(field, item, condition.field) for item in bounds)
            return attribute.between(low, high)

        if operator in _TEXT_OPERATORS:
            return icontains(
                attribute, _as_str(condition), self.profile, anchor=_TEXT_OPERATORS[operator]
            )

        if operator is FilterOperator.IEXACT:
            return sa.func.lower(attribute) == sa.func.lower(_as_str(condition))

        value = self._coerce(field, _as_str(condition), condition.field)
        comparisons: dict[FilterOperator, Callable[[Any], Any]] = {
            FilterOperator.EXACT: attribute.__eq__,
            FilterOperator.NE: attribute.__ne__,
            FilterOperator.LT: attribute.__lt__,
            FilterOperator.LTE: attribute.__le__,
            FilterOperator.GT: attribute.__gt__,
            FilterOperator.GTE: attribute.__ge__,
        }
        return cast("sa.ColumnElement[bool]", comparisons[operator](value))

    def _field_spec(self, path: str) -> FieldSpec | None:
        """The spec for a local field; relation paths have no local spec."""
        return self.spec.get(path)

    def _coerce(self, field: FieldSpec | None, raw: str, path: str) -> Any:
        """Convert a query-string value to the column's Python type.

        Shared with the Tortoise backend, because it is about the column rather
        than about either ORM -- and two copies would drift into filters that
        quietly match different rows on the two backends.
        """
        return coerce_filter_value(field, raw, path)


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _as_bool(value: str | tuple[str, ...]) -> bool:
    return as_bool(value[0] if isinstance(value, tuple) else value)


def _as_str(condition: Filter) -> str:
    return condition.value[0] if isinstance(condition.value, tuple) else condition.value


def _as_tuple(condition: Filter) -> tuple[str, ...]:
    return condition.value if isinstance(condition.value, tuple) else (condition.value,)
