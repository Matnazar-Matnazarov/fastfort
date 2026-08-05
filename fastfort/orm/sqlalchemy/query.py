"""Turning a `ListQuery` into a SQLAlchemy statement.

Field names arriving here have already been checked against the model spec by
`ListQuery.from_params`, so this module never sees an attribute the model does
not have. What it still has to do is coerce string values to the column's type
and keep the SQL identical across the three supported databases.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.orm import InstrumentedAttribute, joinedload, selectinload

from fastfort.core.exceptions import ValidationError
from fastfort.spec import FieldSpec, FieldType, Filter, FilterOperator, ListQuery, ModelSpec

from .dialects import DialectProfile, icontains, order_term

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
        statement = statement.limit(query.limit).offset(query.offset)
        return self._with_eager_loads(statement)

    def count(self, query: ListQuery) -> sa.Select[Any]:
        """A statement returning the number of matching rows.

        Built from a subquery so that any joins the filters needed cannot
        multiply the count.
        """
        inner = self._filtered(sa.select(*self._primary_key_columns()), query).subquery()
        return sa.select(sa.func.count()).select_from(inner)

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
        """Convert a query-string value to the column's Python type."""
        field_type = field.type if field is not None else FieldType.STRING

        # A relation is filtered by the identity of its target, which is the
        # value the related-object dropdown submits.
        if field_type.is_relation:
            return _coerce_scalar(raw, path)

        try:
            coercer = _COERCERS.get(field_type)
            return raw if coercer is None else coercer(raw)
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise ValidationError(
                f"{raw!r} is not a valid value for {path!r}.",
                field_errors={path: [f"Expected {field_type.value}."]},
            ) from exc


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------


def _as_bool(value: str | tuple[str, ...]) -> bool:
    text = (value[0] if isinstance(value, tuple) else value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ValidationError(f"{text!r} is not a boolean value.")


def _as_str(condition: Filter) -> str:
    return condition.value[0] if isinstance(condition.value, tuple) else condition.value


def _as_tuple(condition: Filter) -> tuple[str, ...]:
    return condition.value if isinstance(condition.value, tuple) else (condition.value,)


def _coerce_scalar(raw: str, path: str) -> Any:
    """Best-effort identity coercion for a relation target's key."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return uuid.UUID(raw)
    except ValueError:
        return raw


def _coerce_datetime(raw: str) -> dt.datetime:
    """Parse an ISO 8601 datetime, tolerating a trailing ``Z``."""
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _coerce_duration(raw: str) -> dt.timedelta:
    """`HH:MM:SS`, `MM:SS` or `Nd HH:MM:SS` into a timedelta.

    A near-twin of `parse_duration` in `fastfort/admin/values.py`, and
    deliberately not shared with it: `fastfort/orm/` may import the spec layer
    but not the admin layer, and inverting that to save fifteen lines would put
    the ORM adapter downstream of the web layer it exists to be independent of.

    It exists at all because `DURATION` is offered as a filter, and without a
    coercer the bound reaches the database as the string it arrived as --
    which PostgreSQL refuses against an `interval` column, so the filter that
    looked available was a 500 waiting to be clicked.
    """
    text = raw.strip()
    days = 0
    if "d" in text:
        head, _, text = text.partition("d")
        days = int(head.strip())
    parts = text.strip().split(":") if text.strip() else ["0"]
    if len(parts) > 3:
        raise ValueError(f"{raw!r} is not a duration")
    numbers = [float(part) for part in parts]
    # Right-aligned, so "90" is ninety seconds rather than ninety hours -- the
    # same rule the form's box follows.
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    return dt.timedelta(days=days, hours=numbers[0], minutes=numbers[1], seconds=numbers[2])


_COERCERS: dict[FieldType, Callable[[str], Any]] = {
    FieldType.DURATION: _coerce_duration,
    FieldType.INTEGER: int,
    FieldType.BIGINT: int,
    FieldType.FLOAT: float,
    FieldType.DECIMAL: Decimal,
    FieldType.BOOLEAN: _as_bool,
    FieldType.DATE: dt.date.fromisoformat,
    FieldType.DATETIME: _coerce_datetime,
    FieldType.TIME: dt.time.fromisoformat,
    FieldType.UUID: uuid.UUID,
}
