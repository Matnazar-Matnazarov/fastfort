"""Turning a SQLAlchemy model into a `ModelSpec`.

This is the only place in FastFort that reads SQLAlchemy's mapper metadata.
Everything downstream sees the canonical spec, which is what keeps the admin and
the UI portable across ORMs.

An unrecognised column type becomes `FieldType.UNKNOWN` rather than an error: one
exotic column should degrade to a read-only cell, not take the whole admin page
down with it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.interfaces import MANYTOMANY, MANYTOONE, ONETOMANY

from fastfort.core.exceptions import AdapterError
from fastfort.core.registry import default_model_key
from fastfort.spec import FieldSpec, FieldType, ModelSpec, RelationSpec

from .types import classify

__all__ = ["humanise", "introspect_model", "is_sqlalchemy_model", "pluralise"]

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
        FieldType.FLOAT,
        FieldType.DECIMAL,
        FieldType.MONEY,
        FieldType.TIME,
        FieldType.DURATION,
        FieldType.FOREIGN_KEY,
        FieldType.ONE_TO_ONE,
        FieldType.UUID,
    }
)

#: A simple `<column> >= <number>` style comparison, the only shape of CHECK
#: constraint this reads as a bound. Anything else (an OR, a second column, a
#: function call) is left alone rather than guessed at -- a wrong bound would
#: reject a value the database was going to accept anyway.
_CHECK_BOUND_RE = re.compile(
    r'(?:"(?P<quoted>[^"]+)"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))'
    r"\s*(?P<op>>=|<=|>|<)\s*"
    r"(?P<value>-?\d+(?:\.\d+)?)"
)


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

    verbose = humanise(model.__name__)
    return ModelSpec(
        key=key,
        name=model.__name__,
        verbose_name=verbose,
        verbose_name_plural=pluralise(verbose),
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
    classification = classify(column)
    field_type = classification.type
    is_pk = column.primary_key

    # A key the database generates cannot be supplied by a form; offering the box
    # invites someone to collide with an existing row. `column.identity` is
    # Postgres's `GENERATED ... AS IDENTITY`, which `column.computed` (a
    # generated *expression* column) does not cover on its own -- without this,
    # `mapped_column(Identity(), primary_key=True)` was offered as an editable
    # box, same trap as an autoincrementing key with no `Identity()` at all.
    generated = (
        bool(column.computed)
        or column.identity is not None
        or (is_pk and _is_autoincrement(mapper, column))
    )
    has_default = column.default is not None or column.server_default is not None

    # A UUID the application mints is an identity, not a value someone fills in.
    # Offering a box for it invites a typo into a column other rows point at, and
    # there is nothing useful a person could type there instead -- the same
    # reasoning that already keeps a generated primary key off the form.
    if field_type is FieldType.UUID and has_default:
        generated = True

    max_length = getattr(column.type, "length", None)
    scale = getattr(column.type, "scale", None)
    decimal_places = scale if isinstance(scale, int) else None
    digits = getattr(column.type, "precision", None)
    precision = digits if isinstance(digits, int) else None

    min_value, max_value = _check_bounds(column)
    if max_value is None and precision is not None and decimal_places is not None:
        # Nothing in a CHECK constraint said so, but `Numeric(precision, scale)`
        # itself is a hard ceiling: the database rejects anything larger, so
        # catching it in the browser turns a 500 into a field-level message.
        max_value = Decimal(10) ** (precision - decimal_places) - Decimal(10) ** -decimal_places

    widget = classification.widget
    if widget is None and _is_long_text(field_type, max_length):
        widget = "textarea"

    # A classification that hands back `widget="readonly"` (a raster, a search
    # vector, an OID -- see `types.py`) is saying the column cannot be written
    # back at all, not just that it looks best undrawn. Without folding that
    # into `generated` too, `editable` stayed `True` for it: `_writable()` is
    # documented as *the* mass-assignment boundary with "deliberately no
    # second flag that could disagree with it" (`CLAUDE.md`), so a form field
    # nobody can see was still one a hand-crafted POST could reach -- not a
    # data breach (the column's real type rejects the string `values.py`
    # would hand it), but exactly the "control that 500s on save" Phase 2's
    # write path exists to avoid.
    generated = generated or widget == "readonly"

    return FieldSpec(
        name=name,
        type=field_type,
        label=humanise(name),
        help_text=column.doc,
        required=not column.nullable and not has_default and not generated,
        nullable=bool(column.nullable),
        editable=not generated,
        unique=bool(column.unique),
        primary_key=is_pk,
        max_length=max_length if isinstance(max_length, int) else None,
        min_value=min_value,
        max_value=max_value,
        decimal_places=decimal_places,
        precision=precision,
        choices=classification.choices,
        default=_static_default(column),
        has_db_default=has_default,
        item=classification.item,
        geometry=classification.geometry,
        vector=classification.vector,
        bounds=classification.bounds,
        searchable=field_type in {FieldType.STRING, FieldType.TEXT, FieldType.EMAIL},
        filterable=field_type in _FILTERABLE_TYPES,
        widget=widget,
        sensitive=_looks_sensitive(name),
    )


def _check_bounds(column: sa.Column[Any]) -> tuple[Decimal | None, Decimal | None]:
    """Read `min_value`/`max_value` off a simple CHECK constraint naming this column.

    Walks every `CheckConstraint` on the table looking for a plain `<column> >=
    <number>` (or `>`, `<=`, `<`) comparison; anything with more than one
    comparison, another column, or a function call does not match and is
    skipped, per `_CHECK_BOUND_RE`'s docstring above.

    A strict `> 0` is recorded as an inclusive `>= 0`, because the spec has no
    exclusive bound. That errs towards accepting one value the database will
    reject rather than rejecting one it would have taken -- the same direction
    every other guess in this function leans, since the database remains the
    authority and a false rejection is the failure nobody can work around.

    `getattr` for the constraints: a class mapped onto a subquery rather than a
    table has columns whose `.table` is a selectable with no constraints at all,
    and an exotic mapping must not be able to take the whole admin page down.
    """
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    for constraint in getattr(column.table, "constraints", ()):
        if not isinstance(constraint, sa.CheckConstraint):
            continue
        match = _CHECK_BOUND_RE.fullmatch(str(constraint.sqltext).strip())
        if match is None:
            continue
        if (match["quoted"] or match["bare"]) != column.name:
            continue
        value = Decimal(match["value"])
        if match["op"] in (">=", ">"):
            min_value = value
        else:
            max_value = value
    return min_value, max_value


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
        label=humanise(rel.key),
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
        # A unique foreign key is a one-to-one, whatever SQLAlchemy calls the
        # direction. The parent side already reports ONETOMANY-not-uselist as
        # one, so without this the same relationship was two different kinds
        # depending on which model's page you were looking at.
        return FieldType.ONE_TO_ONE if _is_unique(rel) else FieldType.FOREIGN_KEY
    if rel.direction is ONETOMANY:
        return FieldType.REVERSE_FK if rel.uselist else FieldType.ONE_TO_ONE
    return FieldType.UNKNOWN


def _is_unique(rel: RelationshipProperty[Any]) -> bool:
    """Whether every local column behind a to-one relation is unique.

    Read from the columns and from the table's own UNIQUE constraints, because
    `Column(unique=True)` and `UniqueConstraint(...)` are the same statement
    said two ways and a project picks whichever suits. A composite key counts
    only when one constraint covers all of it -- two separately unique columns
    do not make the pair unique.
    """
    local = list(rel.local_columns)
    if not local:
        return False
    if all(column.unique or column.primary_key for column in local):
        return True

    names = {column.name for column in local}
    table = local[0].table
    return any(
        isinstance(constraint, sa.UniqueConstraint)
        and {column.name for column in constraint.columns} == names
        for constraint in getattr(table, "constraints", ())
    )


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def humanise(name: str) -> str:
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


def pluralise(word: str) -> str:
    """Good enough for English labels; anything else is set explicitly."""
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return f"{word}es"
    if word.endswith("y") and len(word) > 1 and word[-2].lower() not in "aeiou":
        return f"{word[:-1]}ies"
    return f"{word}s"
