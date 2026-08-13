"""Canonical description of a single model field.

`FieldSpec` is what an ORM adapter produces and what every other layer consumes.
Nothing above this module is allowed to look at a SQLAlchemy column or a Tortoise
field, which is what keeps the admin and the UI portable across ORMs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ._json import jsonify

__all__ = [
    "Choice",
    "FieldSpec",
    "FieldType",
    "GeometrySpec",
    "RangeSpec",
    "RelationSpec",
    "VectorSpec",
]


class FieldType(StrEnum):
    """The set of field kinds the rest of FastFort knows how to render.

    Adapters map their native types onto these. Anything they cannot classify
    becomes `UNKNOWN`, which is displayed read-only rather than raising -- a model
    with one exotic column should still get a working admin page.

    Deliberately no ``SMALLINT``: a small integer is an `INTEGER` with a narrower
    bound, and the bound (``min_value``/``max_value``) is what a form needs to
    validate against, not a second type to branch on everywhere else.
    """

    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    BIGINT = "bigint"
    FLOAT = "float"
    DECIMAL = "decimal"
    MONEY = "money"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    DURATION = "duration"
    UUID = "uuid"
    JSON = "json"
    HSTORE = "hstore"
    ENUM = "enum"
    EMAIL = "email"
    URL = "url"
    PASSWORD = "password"  # noqa: S105 -- a field kind, not a credential
    FILE = "file"
    IMAGE = "image"
    BINARY = "binary"
    INET = "inet"
    MACADDR = "macaddr"
    BITS = "bits"
    SEARCH_VECTOR = "search_vector"
    ARRAY = "array"
    RANGE = "range"
    MULTIRANGE = "multirange"
    GEOMETRY = "geometry"
    VECTOR = "vector"
    FOREIGN_KEY = "foreign_key"
    ONE_TO_ONE = "one_to_one"
    MANY_TO_MANY = "many_to_many"
    REVERSE_FK = "reverse_fk"
    UNKNOWN = "unknown"

    @property
    def is_relation(self) -> bool:
        return self in _RELATION_TYPES

    @property
    def is_multi_valued(self) -> bool:
        """True when a single value of this field is a collection of objects."""
        return self in {FieldType.MANY_TO_MANY, FieldType.REVERSE_FK}

    @property
    def is_spatial(self) -> bool:
        """True for a geometry/geography column, mirroring `is_relation`.

        A single member today, but later phases (the map widget, spatial
        filters) branch on this rather than on ``self is FieldType.GEOMETRY``
        directly, so a second spatial kind would only need adding here.
        """
        return self is FieldType.GEOMETRY


_RELATION_TYPES = frozenset(
    {
        FieldType.FOREIGN_KEY,
        FieldType.ONE_TO_ONE,
        FieldType.MANY_TO_MANY,
        FieldType.REVERSE_FK,
    }
)

#: Types whose values are never rendered as plain text in a list column.
_NON_TEXT_TYPES = frozenset(
    {
        FieldType.PASSWORD,
        FieldType.FILE,
        FieldType.IMAGE,
        FieldType.JSON,
        FieldType.GEOMETRY,
        FieldType.HSTORE,
        FieldType.BINARY,
        FieldType.SEARCH_VECTOR,
        FieldType.RANGE,
        FieldType.MULTIRANGE,
        FieldType.VECTOR,
    }
)


@dataclass(frozen=True, slots=True)
class Choice:
    """One option of an enumerated field."""

    value: Any
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"value": jsonify(self.value), "label": self.label}


@dataclass(frozen=True, slots=True)
class RelationSpec:
    """Where a relation field points and how it behaves."""

    #: Registry key of the related model, e.g. ``"shop.category"``.
    target: str
    #: Attribute on the target model that this relation resolves against.
    to_field: str = "id"
    #: True for to-many relations.
    is_list: bool = False
    #: Reverse accessor on the target model, when the ORM exposes one.
    related_name: str | None = None
    #: Deleting the target cascades to this row. Surfaced in delete confirmations.
    cascade_delete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "to_field": self.to_field,
            "is_list": self.is_list,
            "related_name": self.related_name,
            "cascade_delete": self.cascade_delete,
        }


@dataclass(frozen=True, slots=True)
class VectorSpec:
    """How a `VECTOR` field is shaped: how many numbers, and of what.

    Plain data, like every other attachment here -- pgvector is an ORM-layer
    concern and this only carries what that concern reported.
    """

    #: How many numbers one value holds. `None` when the column did not say,
    #: which pgvector allows and which means the admin cannot check a length.
    dimensions: int | None = None
    #: `vector`, `halfvec`, `sparsevec` or `bit`. They differ in storage and in
    #: which distance operators apply, not in what the admin draws.
    kind: str = "vector"

    def to_dict(self) -> dict[str, Any]:
        return {"dimensions": self.dimensions, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class GeometrySpec:
    """How a `GEOMETRY` field is shaped, for the map widget and validation.

    Plain data, same as `RelationSpec` -- PostGIS and GeoAlchemy2 are ORM-layer
    concerns; this dataclass only carries what those concerns produced.
    """

    #: POINT, LINESTRING, POLYGON, MULTIPOINT, MULTILINESTRING, MULTIPOLYGON,
    #: GEOMETRYCOLLECTION, or the untyped GEOMETRY.
    kind: str = "GEOMETRY"
    srid: int = 4326
    #: True for a PostGIS `geography` column rather than `geometry`.
    geography: bool = False
    dimension: int = 2
    spatial_index: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "srid": self.srid,
            "geography": self.geography,
            "dimension": self.dimension,
            "spatial_index": self.spatial_index,
        }


@dataclass(frozen=True, slots=True)
class RangeSpec:
    """How a `RANGE` or `MULTIRANGE` field's endpoints are typed."""

    #: The type of each endpoint, e.g. `FieldType.INTEGER` for an `int4range`.
    bound: FieldType = FieldType.INTEGER
    #: True when this is a multirange (several disjoint ranges in one value)
    #: rather than a single range -- must agree with the field's own type.
    multi: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"bound": self.bound.value, "multi": self.multi}


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Everything FastFort knows about one field of one model.

    The security-relevant attribute is `editable`: it is the single source of truth
    for mass-assignment protection. A submitted value for a field whose spec says
    ``editable=False`` is discarded, no matter which form or endpoint it arrived
    through. There is deliberately no second flag that could disagree with it.
    """

    name: str
    type: FieldType
    label: str
    help_text: str | None = None

    # --- constraints -------------------------------------------------------
    required: bool = False
    nullable: bool = True
    editable: bool = True
    unique: bool = False
    primary_key: bool = False
    max_length: int | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    #: The scale: digits after the point, e.g. `2` for a price in cents.
    #: Paired with `precision` below -- a `Numeric(10, 2)` carries `precision=10,
    #: decimal_places=2`, and a form needs both to size the box and validate it.
    decimal_places: int | None = None
    #: The total digit count of a fixed-point number, paired with `decimal_places`
    #: (the scale) above. `None` means the database places no limit.
    precision: int | None = None
    choices: tuple[Choice, ...] = ()
    default: Any = None
    has_db_default: bool = False

    # --- structured values ---------------------------------------------------
    #: The element spec of an `ARRAY`. Recursive by design -- `ARRAY(ARRAY(...))`
    #: needs it, and it is how `ARRAY(Integer)` gets per-entry validation and
    #: `ARRAY(Enum)` gets its choices. `None` on a bare `ARRAY` degrades to a
    #: list of strings rather than blocking the column.
    item: FieldSpec | None = None
    geometry: GeometrySpec | None = None
    #: Set for a `VECTOR` column: how many numbers it holds.
    vector: VectorSpec | None = None
    bounds: RangeSpec | None = None

    # --- relations ---------------------------------------------------------
    relation: RelationSpec | None = None

    # --- presentation ------------------------------------------------------
    #: Overrides the widget the type would normally select.
    widget: str | None = None
    placeholder: str | None = None
    sortable: bool = True
    searchable: bool = False
    filterable: bool = False

    # --- privacy -----------------------------------------------------------
    #: Values of sensitive fields are masked in audit records and never echoed
    #: back into a form. Password columns are the obvious case.
    sensitive: bool = field(default=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("FieldSpec.name must not be empty")
        if self.type.is_relation and self.relation is None:
            raise ValueError(f"{self.name!r} is a relation field but has no RelationSpec")
        if self.relation is not None and not self.type.is_relation:
            raise ValueError(f"{self.name!r} has a RelationSpec but type is {self.type.value!r}")
        if self.required and self.nullable and not self.has_db_default:
            # A required-but-nullable column is almost always an adapter bug, and
            # it produces forms that reject values the database would accept.
            raise ValueError(f"{self.name!r} cannot be both required and nullable")
        if self.item is not None and self.type is not FieldType.ARRAY:
            raise ValueError(f"{self.name!r} has an item spec but type is {self.type.value!r}")
        if self.geometry is not None and self.type is not FieldType.GEOMETRY:
            raise ValueError(f"{self.name!r} has a GeometrySpec but type is {self.type.value!r}")
        if self.vector is not None and self.type is not FieldType.VECTOR:
            raise ValueError(f"{self.name!r} has a VectorSpec but type is {self.type.value!r}")
        if self.bounds is not None:
            if self.type not in (FieldType.RANGE, FieldType.MULTIRANGE):
                raise ValueError(f"{self.name!r} has a RangeSpec but type is {self.type.value!r}")
            if self.bounds.multi != (self.type is FieldType.MULTIRANGE):
                raise ValueError(
                    f"{self.name!r} bounds.multi={self.bounds.multi!r} disagrees with "
                    f"type {self.type.value!r}"
                )

    # -- derived properties -------------------------------------------------

    @property
    def is_relation(self) -> bool:
        return self.type.is_relation

    @property
    def is_text_like(self) -> bool:
        """Whether an icontains search over this field makes sense.

        `INET` and `MACADDR` are deliberately excluded even though they read like
        text: PostgreSQL has no `LIKE` for `inet`, so `icontains` against one
        fails with "operator does not exist" -- a search box that 500s is worse
        than one that silently skips the column.
        """
        return self.type in {
            FieldType.STRING,
            FieldType.TEXT,
            FieldType.EMAIL,
            FieldType.URL,
        }

    @property
    def is_identifier_like(self) -> bool:
        """Whether searching this field by an exact value makes sense.

        The other half of what a search box is for. `search_fields` used to accept
        text columns only, which meant the one thing everybody actually types into
        an admin's search box -- a row's id, off a support ticket or an invoice --
        was the one thing it could not find, and `search_fields = ("id", "name")`
        was a configuration error rather than the obvious line it looks like.

        Exact rather than `icontains`, because these have no substrings worth
        matching: an id of `42` should find row 42, not rows 142, 420 and 3428.
        That also keeps it index-friendly, where `LIKE '%42%'` on a cast column
        is a full scan of a table whose id column is the best index on it.
        """
        return self.type in {
            FieldType.INTEGER,
            FieldType.BIGINT,
            FieldType.UUID,
        }

    @property
    def displayable(self) -> bool:
        """Whether the raw value can be rendered directly in a list column."""
        return self.type not in _NON_TEXT_TYPES

    def replace(self, **changes: Any) -> FieldSpec:
        """Return a copy with `changes` applied.

        Specs are immutable, so admin-level overrides (a custom label, a field
        forced read-only) produce a new spec instead of mutating the one the
        adapter derived from the model.
        """
        from dataclasses import replace as _replace

        return _replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type.value,
            "label": self.label,
            "help_text": self.help_text,
            "required": self.required,
            "nullable": self.nullable,
            "editable": self.editable,
            "unique": self.unique,
            "primary_key": self.primary_key,
            "max_length": self.max_length,
            "min_value": jsonify(self.min_value),
            "max_value": jsonify(self.max_value),
            "decimal_places": self.decimal_places,
            "precision": self.precision,
            "choices": [choice.to_dict() for choice in self.choices],
            "default": jsonify(self.default),
            "has_db_default": self.has_db_default,
            "item": self.item.to_dict() if self.item is not None else None,
            "geometry": self.geometry.to_dict() if self.geometry is not None else None,
            "vector": self.vector.to_dict() if self.vector is not None else None,
            "bounds": self.bounds.to_dict() if self.bounds is not None else None,
            "relation": self.relation.to_dict() if self.relation else None,
            "widget": self.widget,
            "placeholder": self.placeholder,
            "sortable": self.sortable,
            "searchable": self.searchable,
            "filterable": self.filterable,
            "sensitive": self.sensitive,
        }
