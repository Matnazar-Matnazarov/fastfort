"""Pluggable classification of a SQLAlchemy column onto a `FieldType`.

Before this module existed, teaching FastFort a new column type meant editing an
if/elif chain inside the library itself -- a project with its own SQLAlchemy type
(a `TypeDecorator`, a vendor extension) had no way in short of a fork. `classify`
walks a list of `TypeRule`s and returns the first match; `register_type` is how a
project joins that list, either ahead of the built-ins (`first=True`, to override
one) or behind them (the default, so the rule only fires for a column nothing
built in -- or registered earlier -- already recognised).

Nothing here raises: an unclassifiable column becomes `Classification(FieldType.
UNKNOWN)` rather than an error, because one exotic column must degrade to a
read-only row instead of taking the whole admin page down with it -- the same
invariant `introspect.py` documents for the layer above this one.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum as _enum
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql.ranges import AbstractMultiRange, AbstractSingleRange

from fastfort.spec import Choice, FieldSpec, FieldType, GeometrySpec, RangeSpec

__all__ = ["Classification", "TypeRule", "classify", "register_type"]


@dataclass(frozen=True, slots=True)
class Classification:
    """What a `TypeRule` reports about one column."""

    type: FieldType
    choices: tuple[Choice, ...] = ()
    item: FieldSpec | None = None
    geometry: GeometrySpec | None = None
    bounds: RangeSpec | None = None
    #: Overrides the widget the type would normally select. Used by the
    #: read-only types (a raster, a search vector) so the page names what the
    #: column is instead of calling it unknown.
    widget: str | None = None


#: A rule inspects a column and either classifies it or declines by returning
#: `None`, in which case the next rule gets a turn.
TypeRule = Callable[[sa.Column[Any]], "Classification | None"]


def _humanise(name: str) -> str:
    """`introspect.py` owns `humanise`; imported lazily so the two modules can
    import each other -- `introspect.py` reaches for `classify` at its own
    top level, so this side of the pair has to wait until call time."""
    from .introspect import humanise

    return humanise(name)


# ---------------------------------------------------------------------------
# 1. Spatial -- first, because GeoAlchemy2 types are `UserDefinedType` and would
#    otherwise fall through every `isinstance` check below.
# ---------------------------------------------------------------------------


def _check_spatial(type_: Any) -> Classification | None:
    """Detected by where the type class lives, never by `isinstance`, because the
    library must not import GeoAlchemy2 just to notice that a project does."""
    if type(type_).__module__.split(".", 1)[0] != "geoalchemy2":
        return None
    dialect_name = getattr(type(type_), "name", "")
    if dialect_name == "raster":
        # Not editable through a form -- an honest read-only row beats either
        # refusing the column or offering a box nothing can parse back.
        return Classification(FieldType.BINARY, widget="readonly")
    return Classification(
        FieldType.GEOMETRY,
        geometry=GeometrySpec(
            kind=getattr(type_, "geometry_type", None) or "GEOMETRY",
            srid=getattr(type_, "srid", 4326),
            geography=dialect_name == "geography",
            dimension=getattr(type_, "dimension", 2),
            spatial_index=bool(getattr(type_, "spatial_index", True)),
        ),
    )


# ---------------------------------------------------------------------------
# 2. sa.Enum
# ---------------------------------------------------------------------------


def _enum_choices(column_type: sa.Enum) -> tuple[Choice, ...]:
    native = column_type.enum_class
    if native is not None and issubclass(native, _enum.Enum):
        return tuple(Choice(value=member.value, label=_humanise(member.name)) for member in native)
    return tuple(Choice(value=item, label=_humanise(item)) for item in column_type.enums)


def _check_enum(type_: Any) -> Classification | None:
    if not isinstance(type_, sa.Enum):
        return None
    return Classification(FieldType.ENUM, choices=_enum_choices(type_))


# ---------------------------------------------------------------------------
# 3. Ranges -- `AbstractMultiRange` and `AbstractSingleRange` are siblings under
#    `AbstractRange` in SQLAlchemy 2.0.51 (verified against the installed
#    version, not assumed), so which is checked first does not matter; only
#    that both are checked before the endpoint type is read off the class name.
# ---------------------------------------------------------------------------

#: `INT4RANGE` -> `"INT4"`, `TSTZMULTIRANGE` -> `"TSTZ"`, etc.
_RANGE_BOUNDS: dict[str, FieldType] = {
    "INT4": FieldType.INTEGER,
    "INT8": FieldType.BIGINT,
    "NUM": FieldType.DECIMAL,
    "DATE": FieldType.DATE,
    "TS": FieldType.DATETIME,
    "TSTZ": FieldType.DATETIME,
}


def _range_bound(type_: Any) -> FieldType:
    name = type(type_).__name__.removesuffix("MULTIRANGE").removesuffix("RANGE")
    return _RANGE_BOUNDS.get(name, FieldType.STRING)


def _check_range(type_: Any) -> Classification | None:
    if isinstance(type_, AbstractMultiRange):
        return Classification(
            FieldType.MULTIRANGE, bounds=RangeSpec(_range_bound(type_), multi=True)
        )
    if isinstance(type_, AbstractSingleRange):
        return Classification(FieldType.RANGE, bounds=RangeSpec(_range_bound(type_), multi=False))
    return None


# ---------------------------------------------------------------------------
# 4. CITEXT before Text -- the whole point of ordering. `postgresql.CITEXT`
#    subclasses `TEXT`, so without a dedicated, earlier rule it would match the
#    core `Text` check below and render as a 4-row textarea; it is a
#    case-insensitive `VARCHAR` and belongs in a text input.
# ---------------------------------------------------------------------------


def _check_citext(type_: Any) -> Classification | None:
    if not isinstance(type_, pg.CITEXT):
        return None
    return Classification(FieldType.STRING)


# ---------------------------------------------------------------------------
# 5. PostgreSQL scalars whose `python_type` raises `NotImplementedError`
#    (verified against the installed SQLAlchemy version) -- without a rule here
#    each one falls all the way through to `UNKNOWN`.
# ---------------------------------------------------------------------------


def _check_pg_scalar(type_: Any) -> Classification | None:
    if isinstance(type_, pg.INET | pg.CIDR):
        return Classification(FieldType.INET)
    if isinstance(type_, pg.MACADDR | pg.MACADDR8):
        return Classification(FieldType.MACADDR)
    if isinstance(type_, pg.HSTORE):
        return Classification(FieldType.HSTORE)
    if isinstance(type_, pg.MONEY):
        return Classification(FieldType.MONEY)
    if isinstance(type_, pg.BIT):  # covers VARBIT too -- it is BIT(varying=True)
        return Classification(FieldType.BITS)
    if isinstance(type_, pg.TSVECTOR):
        return Classification(FieldType.SEARCH_VECTOR, widget="readonly")
    if isinstance(type_, pg.OID):
        return Classification(FieldType.INTEGER, widget="readonly")
    return None


# ---------------------------------------------------------------------------
# 6. sa.ARRAY (and postgresql.ARRAY, which subclasses it)
# ---------------------------------------------------------------------------


def _array_item_spec(base_name: str, item_type: Any, depth: int = 1) -> FieldSpec:
    """The element spec for one level of `ARRAY`.

    SQLAlchemy's own `ARRAY` never nests -- multi-dimensionality is a
    `dimensions=` argument on one instance, not `ARRAY(ARRAY(...))`, and its
    constructor raises if asked to. A project's own array-like type could still
    hand back a nested one here, so this recurses defensively rather than
    assuming `item_type` is always a leaf.
    """
    item_name = base_name + "[]" * depth
    label = _humanise(base_name)
    if isinstance(item_type, sa.ARRAY):
        nested = _array_item_spec(base_name, item_type.item_type, depth + 1)
        return FieldSpec(name=item_name, type=FieldType.ARRAY, label=label, item=nested)
    classification = _classify_type(item_type)
    max_length = getattr(item_type, "length", None)
    return FieldSpec(
        name=item_name,
        type=classification.type,
        label=label,
        choices=classification.choices,
        max_length=max_length if isinstance(max_length, int) else None,
    )


def _rule_array(column: sa.Column[Any]) -> Classification | None:
    """Needs the column, not just its type -- the item spec's name is built from
    `column.name`, which a bare `TypeEngine` does not carry."""
    if not isinstance(column.type, sa.ARRAY):
        return None
    return Classification(
        FieldType.ARRAY, item=_array_item_spec(column.name, column.type.item_type)
    )


# ---------------------------------------------------------------------------
# 7. sa.LargeBinary
# ---------------------------------------------------------------------------


def _check_largebinary(type_: Any) -> Classification | None:
    if not isinstance(type_, sa.LargeBinary):
        return None
    return Classification(FieldType.BINARY, widget="readonly")


# ---------------------------------------------------------------------------
# 8. The existing core rules, unchanged in behaviour.
# ---------------------------------------------------------------------------


def _check_core(type_: Any) -> Classification | None:
    if isinstance(type_, sa.Boolean):
        return Classification(FieldType.BOOLEAN)
    if isinstance(type_, sa.BigInteger):
        return Classification(FieldType.BIGINT)
    if isinstance(type_, sa.Integer):
        return Classification(FieldType.INTEGER)
    if isinstance(type_, sa.Numeric) and not isinstance(type_, sa.Float):
        return Classification(FieldType.DECIMAL)
    if isinstance(type_, sa.Float):
        return Classification(FieldType.FLOAT)
    if isinstance(type_, sa.DateTime):
        return Classification(FieldType.DATETIME)
    if isinstance(type_, sa.Date):
        return Classification(FieldType.DATE)
    if isinstance(type_, sa.Time):
        return Classification(FieldType.TIME)
    if isinstance(type_, sa.Interval):
        return Classification(FieldType.DURATION)
    if isinstance(type_, sa.Uuid):
        return Classification(FieldType.UUID)
    if isinstance(type_, sa.JSON):
        return Classification(FieldType.JSON)
    if isinstance(type_, sa.Text):
        return Classification(FieldType.TEXT)
    if isinstance(type_, sa.String):
        return Classification(FieldType.STRING)
    return None


# ---------------------------------------------------------------------------
# 9. The `_PYTHON_TYPE_MAP` fallback, unchanged.
# ---------------------------------------------------------------------------

#: Python types SQLAlchemy reports for a column, mapped onto our vocabulary.
#: Consulted after every concrete SQL type above, as a last resort.
_PYTHON_TYPE_MAP: dict[type, FieldType] = {
    bool: FieldType.BOOLEAN,
    int: FieldType.INTEGER,
    float: FieldType.FLOAT,
    str: FieldType.STRING,
    bytes: FieldType.UNKNOWN,
    decimal.Decimal: FieldType.DECIMAL,
    dt.date: FieldType.DATE,
    dt.datetime: FieldType.DATETIME,
    dt.time: FieldType.TIME,
    dt.timedelta: FieldType.DURATION,
    uuid.UUID: FieldType.UUID,
    dict: FieldType.JSON,
    list: FieldType.JSON,
}


def _check_python_map(type_: Any) -> Classification | None:
    """Declines rather than answering `UNKNOWN`.

    Both failure modes -- `python_type` raising, and a Python type nothing here
    maps -- return `None` so that a rule appended by a project still gets a turn.
    Answering `Classification(UNKNOWN)` here would make the whole append path
    dead for the case it exists for: a `TypeDecorator` over some domain class
    reports that class from `python_type`, which is exactly the kind of type this
    map does not know. `classify()` supplies `UNKNOWN` at the end either way, so
    nothing about the outcome changes for a project that registered nothing.
    """
    try:
        python_type = type_.python_type
    except NotImplementedError:
        return None
    found = _PYTHON_TYPE_MAP.get(python_type)
    return None if found is None else Classification(found)


def _classify_type(type_: Any) -> Classification:
    """Classify a bare SQLAlchemy type, independent of any column.

    `column.type.item_type` is a type, not a column, so an `ARRAY`'s element
    goes through here rather than through the column-level registry below --
    there is no column to hand a project's rule in the first place.
    """
    for check in (
        _check_spatial,
        _check_enum,
        _check_range,
        _check_citext,
        _check_pg_scalar,
        _check_largebinary,
        _check_core,
    ):
        result = check(type_)
        if result is not None:
            return result
    return _check_python_map(type_) or Classification(FieldType.UNKNOWN)


def _column_rule(check: Callable[[Any], Classification | None]) -> TypeRule:
    """Adapt a type-only check into a column-level rule for the registry."""

    def rule(column: sa.Column[Any]) -> Classification | None:
        return check(column.type)

    return rule


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

#: Built-ins in brief-numbered order. None of these are an unconditional catch
#: -all -- each declines (`None`) when it does not recognise the type, which is
#: what lets a plain `register_type(rule)` (no `first=True`) ever run at all: it
#: is appended after every entry here, so it only fires for a column nothing
#: above it -- built in or previously registered -- already claimed.
_RULES: list[TypeRule] = [
    _column_rule(_check_spatial),
    _column_rule(_check_enum),
    _column_rule(_check_range),
    _column_rule(_check_citext),
    _column_rule(_check_pg_scalar),
    _rule_array,
    _column_rule(_check_largebinary),
    _column_rule(_check_core),
    _column_rule(_check_python_map),
]


def classify(column: sa.Column[Any]) -> Classification:
    """Classify `column` by trying each registered rule in turn.

    Nothing here raises: a column no rule recognises becomes
    `Classification(FieldType.UNKNOWN)`, which the admin renders as a read-only
    cell rather than refusing to build the page.
    """
    for rule in _RULES:
        result = rule(column)
        if result is not None:
            return result
    return Classification(FieldType.UNKNOWN)


def register_type(rule: TypeRule, *, first: bool = False) -> None:
    """Add a classification rule.

    `first=True` inserts ahead of every built-in, including spatial detection,
    so a project can override one. Otherwise the rule is appended and only ever
    fires for a column nothing built in -- or registered earlier -- recognised.
    """
    if first:
        _RULES.insert(0, rule)
    else:
        _RULES.append(rule)
