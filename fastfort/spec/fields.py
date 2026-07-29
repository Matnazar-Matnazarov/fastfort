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

__all__ = ["Choice", "FieldSpec", "FieldType", "RelationSpec"]


class FieldType(StrEnum):
    """The set of field kinds the rest of FastFort knows how to render.

    Adapters map their native types onto these. Anything they cannot classify
    becomes `UNKNOWN`, which is displayed read-only rather than raising -- a model
    with one exotic column should still get a working admin page.
    """

    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    BIGINT = "bigint"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    TIME = "time"
    DURATION = "duration"
    UUID = "uuid"
    JSON = "json"
    ENUM = "enum"
    EMAIL = "email"
    URL = "url"
    PASSWORD = "password"  # noqa: S105 -- a field kind, not a credential
    FILE = "file"
    IMAGE = "image"
    ARRAY = "array"
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


_RELATION_TYPES = frozenset(
    {
        FieldType.FOREIGN_KEY,
        FieldType.ONE_TO_ONE,
        FieldType.MANY_TO_MANY,
        FieldType.REVERSE_FK,
    }
)

#: Types whose values are never rendered as plain text in a list column.
_NON_TEXT_TYPES = frozenset({FieldType.PASSWORD, FieldType.FILE, FieldType.IMAGE, FieldType.JSON})


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
    decimal_places: int | None = None
    choices: tuple[Choice, ...] = ()
    default: Any = None
    has_db_default: bool = False

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

    # -- derived properties -------------------------------------------------

    @property
    def is_relation(self) -> bool:
        return self.type.is_relation

    @property
    def is_text_like(self) -> bool:
        """Whether an icontains search over this field makes sense."""
        return self.type in {
            FieldType.STRING,
            FieldType.TEXT,
            FieldType.EMAIL,
            FieldType.URL,
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
            "choices": [choice.to_dict() for choice in self.choices],
            "default": jsonify(self.default),
            "has_db_default": self.has_db_default,
            "relation": self.relation.to_dict() if self.relation else None,
            "widget": self.widget,
            "placeholder": self.placeholder,
            "sortable": self.sortable,
            "searchable": self.searchable,
            "filterable": self.filterable,
            "sensitive": self.sensitive,
        }
