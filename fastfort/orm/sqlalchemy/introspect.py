"""Turning a SQLAlchemy model into a `ModelSpec`.

This is the only place in FastFort that reads SQLAlchemy's mapper metadata.
Everything downstream sees the canonical spec, which is what keeps the admin and
the UI portable across ORMs.

An unrecognised column type becomes `FieldType.UNKNOWN` rather than an error: one
exotic column should degrade to a read-only cell, not take the whole admin page
down with it.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import re
import uuid
from collections.abc import Callable
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.interfaces import MANYTOMANY, MANYTOONE, ONETOMANY

from fastfort.core.exceptions import AdapterError
from fastfort.core.registry import default_model_key
from fastfort.spec import Choice, FieldSpec, FieldType, ModelSpec, RelationSpec

__all__ = ["introspect_model", "is_sqlalchemy_model"]

#: Length past which a VARCHAR is treated as prose and gets a textarea.
_TEXTAREA_THRESHOLD = 512

#: Field types worth offering as a list filter. Free text is excluded: filtering
#: on it produces a dropdown with thousands of entries and no useful facets.
_FILTERABLE_TYPES = frozenset(
    {
        FieldType.BOOLEAN,
        FieldType.ENUM,
        FieldType.DATE,
        FieldType.DATETIME,
        FieldType.INTEGER,
        FieldType.BIGINT,
        FieldType.FOREIGN_KEY,
        FieldType.ONE_TO_ONE,
        FieldType.UUID,
    }
)

#: Python types SQLAlchemy reports for a column, mapped onto our vocabulary.
#: Consulted after the concrete SQL types below, as a last resort.
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


def is_sqlalchemy_model(model: type) -> bool:
    """Whether `model` is a mapped SQLAlchemy class."""
    try:
        sa_inspect(model)
    except Exception:  # any failure to inspect means "not a mapped class"
        return False
    return True


def introspect_model(
    model: type,
    *,
    key: str,
    resolve_key: Callable[[type], str] = default_model_key,
) -> ModelSpec:
    """Derive the canonical description of a mapped SQLAlchemy class.

    `resolve_key` turns a related model class into its registry key; it is
    injected so that a project which overrides a model's key still gets relations
    pointing at the right place.
    """
    if not is_sqlalchemy_model(model):
        raise AdapterError(
            f"{model.__name__} is not a mapped SQLAlchemy class.",
            hint="Register a declarative model, or use the backend that owns this class.",
        )

    mapper: Mapper[Any] = sa_inspect(model)
    relation_columns = _columns_used_by_relations(mapper)

    fields: list[FieldSpec] = [
        _column_field(mapper, name, column)
        for name, column in _mapped_columns(mapper)
        # A foreign key column is represented by its relationship instead, so the
        # admin offers "Category" rather than a raw "category_id" integer box.
        if name not in relation_columns
    ]
    fields.extend(_relation_field(rel, resolve_key) for rel in mapper.relationships)

    primary_key = tuple(
        prop.key
        for column in mapper.primary_key
        if (prop := mapper.get_property_by_column(column)) is not None
    )

    verbose = _humanise(model.__name__)
    return ModelSpec(
        key=key,
        name=model.__name__,
        verbose_name=verbose,
        verbose_name_plural=_pluralise(verbose),
        fields=tuple(fields),
        primary_key=primary_key,
    )


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def _mapped_columns(mapper: Mapper[Any]) -> list[tuple[str, sa.Column[Any]]]:
    """Column properties in declaration order, keyed by attribute name."""
    columns: list[tuple[str, sa.Column[Any]]] = []
    for prop in mapper.column_attrs:
        column = prop.columns[0]
        if isinstance(column, sa.Column):
            columns.append((prop.key, column))
    return columns


def _columns_used_by_relations(mapper: Mapper[Any]) -> frozenset[str]:
    """Attribute names of the local columns that back a many-to-one relation."""
    names: set[str] = set()
    for rel in mapper.relationships:
        if rel.direction is not MANYTOONE:
            continue
        for column in rel.local_columns:
            prop = mapper.get_property_by_column(column)
            if prop is not None and column not in mapper.primary_key:
                names.add(prop.key)
    return frozenset(names)


def _column_field(mapper: Mapper[Any], name: str, column: sa.Column[Any]) -> FieldSpec:
    field_type, choices = _classify(column)
    is_pk = column.primary_key

    # A key the database generates cannot be supplied by a form; offering the box
    # invites someone to collide with an existing row.
    generated = bool(column.computed) or (is_pk and _is_autoincrement(mapper, column))
    has_default = column.default is not None or column.server_default is not None

    max_length = getattr(column.type, "length", None)
    precision = getattr(column.type, "scale", None)

    return FieldSpec(
        name=name,
        type=field_type,
        label=_humanise(name),
        help_text=column.doc,
        required=not column.nullable and not has_default and not generated,
        nullable=bool(column.nullable),
        editable=not generated,
        unique=bool(column.unique),
        primary_key=is_pk,
        max_length=max_length if isinstance(max_length, int) else None,
        decimal_places=precision if isinstance(precision, int) else None,
        choices=choices,
        default=_static_default(column),
        has_db_default=has_default,
        searchable=field_type in {FieldType.STRING, FieldType.TEXT, FieldType.EMAIL},
        filterable=field_type in _FILTERABLE_TYPES,
        widget="textarea" if _is_long_text(field_type, max_length) else None,
        sensitive=_looks_sensitive(name),
    )


def _classify(column: sa.Column[Any]) -> tuple[FieldType, tuple[Choice, ...]]:
    """Map a SQLAlchemy column type onto a `FieldType` and its choices."""
    column_type = column.type

    if isinstance(column_type, sa.Enum):
        return FieldType.ENUM, _enum_choices(column_type)
    if isinstance(column_type, sa.Boolean):
        return FieldType.BOOLEAN, ()
    if isinstance(column_type, sa.BigInteger):
        return FieldType.BIGINT, ()
    if isinstance(column_type, sa.Integer):
        return FieldType.INTEGER, ()
    if isinstance(column_type, sa.Numeric) and not isinstance(column_type, sa.Float):
        return FieldType.DECIMAL, ()
    if isinstance(column_type, sa.Float):
        return FieldType.FLOAT, ()
    if isinstance(column_type, sa.DateTime):
        return FieldType.DATETIME, ()
    if isinstance(column_type, sa.Date):
        return FieldType.DATE, ()
    if isinstance(column_type, sa.Time):
        return FieldType.TIME, ()
    if isinstance(column_type, sa.Interval):
        return FieldType.DURATION, ()
    if isinstance(column_type, sa.Uuid):
        return FieldType.UUID, ()
    if isinstance(column_type, sa.JSON):
        return FieldType.JSON, ()
    if isinstance(column_type, sa.Text):
        return FieldType.TEXT, ()
    if isinstance(column_type, sa.String):
        return FieldType.STRING, ()
    if isinstance(column_type, sa.ARRAY):
        return FieldType.ARRAY, ()

    try:
        python_type = column_type.python_type
    except NotImplementedError:
        return FieldType.UNKNOWN, ()
    return _PYTHON_TYPE_MAP.get(python_type, FieldType.UNKNOWN), ()


def _enum_choices(column_type: sa.Enum) -> tuple[Choice, ...]:
    native = column_type.enum_class
    if native is not None and issubclass(native, enum.Enum):
        return tuple(Choice(value=member.value, label=_humanise(member.name)) for member in native)
    return tuple(Choice(value=item, label=_humanise(item)) for item in column_type.enums)


def _is_autoincrement(mapper: Mapper[Any], column: sa.Column[Any]) -> bool:
    """Whether the database will supply this primary key on insert."""
    if column.autoincrement is True:
        return True
    if column.autoincrement == "auto":
        # SQLAlchemy's own rule: a single-column integer primary key with no
        # explicit default is auto-incrementing.
        return (
            len(mapper.primary_key) == 1
            and isinstance(column.type, sa.Integer)
            and column.default is None
            and column.server_default is None
            and not column.foreign_keys
        )
    return False


def _static_default(column: sa.Column[Any]) -> Any:
    """The column default, when it is a plain value rather than a callable."""
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        return None
    return getattr(default, "arg", None)


def _is_long_text(field_type: FieldType, max_length: object) -> bool:
    if field_type is FieldType.TEXT:
        return True
    return field_type is FieldType.STRING and (
        max_length is None or (isinstance(max_length, int) and max_length > _TEXTAREA_THRESHOLD)
    )


def _looks_sensitive(name: str) -> bool:
    """Mark obviously secret columns so their values never reach a log or a form.

    Name-based and therefore imperfect; it is a safety net under an explicit
    declaration, not a replacement for one.
    """
    lowered = name.lower()
    return any(
        marker in lowered
        for marker in ("password", "secret", "token", "api_key", "private_key", "salt")
    )


# ---------------------------------------------------------------------------
# Relations
# ---------------------------------------------------------------------------


def _relation_field(
    rel: RelationshipProperty[Any], resolve_key: Callable[[type], str]
) -> FieldSpec:
    target = rel.mapper.class_
    field_type = _relation_type(rel)

    to_field = "id"
    if rel.local_remote_pairs:
        remote = rel.local_remote_pairs[0][1]
        to_field = getattr(remote, "key", None) or to_field

    return FieldSpec(
        name=rel.key,
        type=field_type,
        label=_humanise(rel.key),
        required=False,
        nullable=True,
        editable=not rel.viewonly,
        # Sorting or filtering by a to-many relation multiplies rows, so the
        # spec refuses it rather than letting a list page silently duplicate.
        sortable=not field_type.is_multi_valued,
        filterable=field_type in _FILTERABLE_TYPES,
        relation=RelationSpec(
            target=resolve_key(target),
            to_field=to_field,
            is_list=bool(rel.uselist),
            related_name=rel.back_populates
            or (rel.backref if isinstance(rel.backref, str) else None),
            cascade_delete="delete" in rel.cascade,
        ),
    )


def _relation_type(rel: RelationshipProperty[Any]) -> FieldType:
    if rel.direction is MANYTOMANY:
        return FieldType.MANY_TO_MANY
    if rel.direction is MANYTOONE:
        return FieldType.FOREIGN_KEY
    if rel.direction is ONETOMANY:
        return FieldType.REVERSE_FK if rel.uselist else FieldType.ONE_TO_ONE
    return FieldType.UNKNOWN


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def _humanise(name: str) -> str:
    """``created_at``, ``CreatedAt`` and ``CREATED_AT`` all become ``Created at``."""
    # SHOUTING_CASE carries no word boundaries for the camel pattern to find, so
    # it is folded first. Enum members are the usual source.
    if name.isupper():
        name = name.lower()
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    spaced = spaced.replace("_", " ").strip()
    if not spaced:
        return name
    return spaced[0].upper() + spaced[1:]


def _pluralise(word: str) -> str:
    """Good enough for English labels; anything else is set explicitly."""
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and len(word) > 1 and word[-2].lower() not in "aeiou":
        return f"{word[:-1]}ies"
    return f"{word}s"
