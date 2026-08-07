"""Turning a Tortoise model into a `ModelSpec`.

The counterpart of `orm/sqlalchemy/introspect.py`, and the reason the spec layer
exists at all: everything above `fastfort/orm/` reads a `ModelSpec` and nothing
else, so a second ORM is a new module here rather than a change anywhere in the
admin, the forms or the UI.

Tortoise keeps what this needs on `Model._meta`. `fields_map` holds every field
including the concrete column behind a foreign key, which is why the relations
are subtracted from it: a `ForeignKeyField("models.Category")` shows up twice,
once as `category` and once as `category_id`, and offering both would give the
admin a "Category" dropdown and a raw integer box for the same thing.

An unrecognised field becomes `FieldType.UNKNOWN` rather than an error, exactly
as on the SQLAlchemy side: one exotic column should degrade to a read-only row,
not take the whole page down.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid
from collections.abc import Callable
from typing import Any, cast

from tortoise import Model
from tortoise.fields import Field
from tortoise.fields.relational import (
    BackwardFKRelation,
    BackwardOneToOneRelation,
    ForeignKeyFieldInstance,
    ManyToManyFieldInstance,
    OneToOneFieldInstance,
    RelationalField,
)

from fastfort.core.exceptions import AdapterError
from fastfort.core.registry import default_model_key
from fastfort.spec import Choice, FieldSpec, FieldType, ModelSpec, RelationSpec

from ..sqlalchemy.introspect import humanise, pluralise

__all__ = ["introspect_model", "is_tortoise_model"]

#: Length past which a CharField is treated as prose and gets a textarea. The
#: same threshold the SQLAlchemy side uses, so the two agree about what "long"
#: means and a model ported between them renders the same.
_TEXTAREA_THRESHOLD = 512

#: Field types worth offering as a list filter. Free text is excluded for the
#: same reason as on the other side: filtering on it produces a dropdown with
#: thousands of entries and no useful facets.
_FILTERABLE_TYPES = frozenset(
    {
        FieldType.BOOLEAN,
        FieldType.ENUM,
        FieldType.DATE,
        FieldType.DATETIME,
        FieldType.TIME,
        FieldType.DURATION,
        FieldType.INTEGER,
        FieldType.BIGINT,
        FieldType.FLOAT,
        FieldType.DECIMAL,
        FieldType.UUID,
        FieldType.FOREIGN_KEY,
        FieldType.ONE_TO_ONE,
    }
)

#: What Tortoise reports in `Field.field_type` -- the Python type a value has --
#: mapped onto our vocabulary. Consulted after the concrete field classes below,
#: as a last resort, mirroring `_PYTHON_TYPE_MAP` on the SQLAlchemy side.
_PYTHON_TYPE_MAP: dict[type, FieldType] = {
    bool: FieldType.BOOLEAN,
    int: FieldType.INTEGER,
    float: FieldType.FLOAT,
    str: FieldType.STRING,
    bytes: FieldType.BINARY,
    decimal.Decimal: FieldType.DECIMAL,
    dt.date: FieldType.DATE,
    dt.datetime: FieldType.DATETIME,
    dt.time: FieldType.TIME,
    dt.timedelta: FieldType.DURATION,
    uuid.UUID: FieldType.UUID,
    dict: FieldType.JSON,
    list: FieldType.JSON,
}


def is_tortoise_model(model: type) -> bool:
    """Whether `model` is a Tortoise model that has been initialised.

    `_meta` exists on the class from the moment it is declared, but the relation
    fields on it are only resolved once `Tortoise.init_models` (or `init`) has
    run -- before that a `ForeignKeyField` still holds the string
    `"models.Category"` and has no `related_model` to read. Checking for the
    resolved form is what turns "you forgot to initialise Tortoise" into a
    start-up error instead of an `AttributeError` deep in the introspector.
    """
    return isinstance(model, type) and issubclass(model, Model) and hasattr(model, "_meta")


def introspect_model(
    model: type,
    *,
    key: str,
    resolve_key: Callable[[type], str] = default_model_key,
) -> ModelSpec:
    """Derive the canonical description of a Tortoise model.

    `resolve_key` turns a related model class into its registry key; it is
    injected so that a project which overrides a model's key still gets
    relations pointing at the right place -- same contract as the SQLAlchemy
    introspector, because the layer above cannot tell the two apart.
    """
    if not is_tortoise_model(model):
        raise AdapterError(
            f"{model.__name__} is not a Tortoise model.",
            hint="Register a `tortoise.Model` subclass, or use the backend that owns this class.",
        )

    meta = cast("Any", model)._meta
    relation_names = _relation_names(meta)
    source_columns = _columns_behind_relations(meta)

    fields: list[FieldSpec] = []
    for name, field in meta.fields_map.items():
        if name in relation_names:
            continue
        # The concrete column behind a foreign key. Represented by its relation
        # instead, so the admin offers "Category" rather than a raw integer.
        if name in source_columns:
            continue
        fields.append(_column_field(name, field, meta))

    fields.extend(
        _relation_field(name, meta.fields_map[name], resolve_key)
        for name in relation_names
        if name in meta.fields_map
    )

    verbose = humanise(model.__name__)
    return ModelSpec(
        key=key,
        name=model.__name__,
        verbose_name=verbose,
        verbose_name_plural=pluralise(verbose),
        fields=tuple(fields),
        # Tortoise supports exactly one primary key per model, so this is always
        # a one-element tuple -- but it stays a tuple because the layer above
        # treats every key as one and a composite key is not a special case
        # there.
        primary_key=(meta.pk_attr,),
    )


def _relation_names(meta: Any) -> set[str]:
    """Every relation on the model, in declaration order where Tortoise keeps it.

    Five separate sets on `_meta`, because Tortoise tracks each direction
    apart: the forward ones a form can write, and the backward ones that belong
    on the other model's page.
    """
    names: set[str] = set()
    for attribute in (
        "fk_fields",
        "o2o_fields",
        "m2m_fields",
        "backward_fk_fields",
        "backward_o2o_fields",
    ):
        names |= set(getattr(meta, attribute, ()) or ())
    return names


def _columns_behind_relations(meta: Any) -> set[str]:
    """`category_id` for every `category`.

    Read from each relation's `source_field` rather than by appending `_id`,
    because a project may name the column itself and guessing would leave the
    real one showing as a stray integer box.
    """
    columns: set[str] = set()
    for name in set(getattr(meta, "fk_fields", ()) or ()) | set(
        getattr(meta, "o2o_fields", ()) or ()
    ):
        field = meta.fields_map.get(name)
        source = getattr(field, "source_field", None)
        if source:
            columns.add(str(source))
    return columns


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


def _column_field(name: str, field: Field[Any], meta: Any) -> FieldSpec:
    field_type, choices = _classify(field)
    is_pk = bool(getattr(field, "pk", False)) or name == meta.pk_attr

    # A value the database or the ORM supplies cannot be typed into a form.
    # `generated` is Tortoise's own flag for an autoincrementing key;
    # `auto_now`/`auto_now_add` are timestamps it sets on every save, and a box
    # for one is a box whose value is thrown away.
    generated = bool(
        getattr(field, "generated", False)
        or getattr(field, "auto_now", False)
        or getattr(field, "auto_now_add", False)
    )
    default = getattr(field, "default", None)
    has_default = default is not None or generated

    max_length = getattr(field, "max_length", None)
    precision = getattr(field, "max_digits", None)
    scale = getattr(field, "decimal_places", None)

    # Tortoise writes its own `description` for an enum field -- "DRAFT:
    # draft\nLIVE: live" -- which is the enum restated under a dropdown that
    # already lists it. Help text is for what a person could not otherwise
    # work out.
    described = getattr(field, "description", None)
    help_text = None if field_type is FieldType.ENUM else (described or None)

    return FieldSpec(
        name=name,
        type=field_type,
        label=humanise(name),
        help_text=help_text,
        required=not field.null and not has_default and not generated,
        nullable=bool(field.null),
        editable=not generated,
        unique=bool(getattr(field, "unique", False)),
        primary_key=is_pk,
        max_length=max_length if isinstance(max_length, int) else None,
        precision=precision if isinstance(precision, int) else None,
        decimal_places=scale if isinstance(scale, int) else None,
        choices=choices,
        # Only a plain value. A callable default is evaluated per row, and
        # rendering `<function uuid4>` into a form field helps nobody.
        default=None if callable(default) else default,
        has_db_default=has_default,
        searchable=field_type in {FieldType.STRING, FieldType.TEXT, FieldType.EMAIL},
        filterable=field_type in _FILTERABLE_TYPES,
        widget="textarea" if _is_long_text(field_type, max_length) else None,
        sensitive=_looks_sensitive(name),
    )


def _classify(field: Field[Any]) -> tuple[FieldType, tuple[Choice, ...]]:
    """Map a Tortoise field onto a `FieldType` and its choices.

    By class name rather than by `isinstance`, for the fields that have one.
    Tortoise's hierarchy is shallow and its names are stable, and reading the
    name means a field a later version adds falls through to the Python-type
    map instead of matching the wrong branch of an `isinstance` chain --
    `BigIntField` is not a subclass of `IntField`, but `SmallIntField` is.
    """
    enum_type = getattr(field, "enum_type", None)
    if enum_type is not None and isinstance(enum_type, type) and issubclass(enum_type, enum.Enum):
        return FieldType.ENUM, tuple(
            Choice(value=member.value, label=humanise(member.name)) for member in enum_type
        )

    found = _FIELD_CLASSES.get(type(field).__name__)
    if found is not None:
        return found, ()

    python_type = getattr(field, "field_type", None)
    if isinstance(python_type, type):
        return _PYTHON_TYPE_MAP.get(python_type, FieldType.UNKNOWN), ()
    return FieldType.UNKNOWN, ()


#: Tortoise's own field classes, by name. Everything it ships as of 1.1.
_FIELD_CLASSES: dict[str, FieldType] = {
    "IntField": FieldType.INTEGER,
    "SmallIntField": FieldType.INTEGER,
    "BigIntField": FieldType.BIGINT,
    "FloatField": FieldType.FLOAT,
    "DecimalField": FieldType.DECIMAL,
    "BooleanField": FieldType.BOOLEAN,
    "CharField": FieldType.STRING,
    "TextField": FieldType.TEXT,
    "DateField": FieldType.DATE,
    "DatetimeField": FieldType.DATETIME,
    "TimeField": FieldType.TIME,
    "TimeDeltaField": FieldType.DURATION,
    "UUIDField": FieldType.UUID,
    "JSONField": FieldType.JSON,
    "BinaryField": FieldType.BINARY,
}


def _is_long_text(field_type: FieldType, max_length: object) -> bool:
    if field_type is FieldType.TEXT:
        return True
    return field_type is FieldType.STRING and (
        max_length is None or (isinstance(max_length, int) and max_length > _TEXTAREA_THRESHOLD)
    )


def _looks_sensitive(name: str) -> bool:
    """Mark obviously secret columns so their values never reach a log or a form.

    Name-based and therefore imperfect, and deliberately the same list as the
    SQLAlchemy side: a model ported from one ORM to the other must not quietly
    start echoing a token back into a form.
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
    name: str, field: RelationalField[Any], resolve_key: Callable[[type], str]
) -> FieldSpec:
    field_type = _relation_type(field)
    target = field.related_model
    backward = isinstance(field, BackwardFKRelation | BackwardOneToOneRelation)

    #: Whether the foreign key sits on this model. Only then is there a column
    #: here that an `ORDER BY` or a `WHERE` can name.
    local_key = not backward and not field_type.is_multi_valued

    return FieldSpec(
        name=name,
        type=field_type,
        label=humanise(name),
        required=False,
        nullable=True,
        # A backward relation is not writable from this side: it is the other
        # model's foreign key, and editing it belongs on that model's page.
        editable=not backward,
        # Sorting or filtering by a to-many relation multiplies rows, so the
        # spec refuses it rather than letting a list page silently duplicate.
        # A backward one-to-one is refused for a second reason: its key is on
        # the other table, so Tortoise answers "Filtering by relation is not
        # possible" -- a 500 from one click on the column header.
        sortable=local_key,
        filterable=local_key and field_type in _FILTERABLE_TYPES,
        relation=RelationSpec(
            target=resolve_key(target),
            to_field=str(getattr(target._meta, "pk_attr", "id")),
            is_list=field_type.is_multi_valued,
            related_name=_related_name(field),
            # Tortoise declares the rule on the *field*, and `CASCADE` there
            # means "delete me when the target goes", which is what the
            # deletion plan reports.
            cascade_delete=str(getattr(field, "on_delete", "")).upper() == "CASCADE",
        ),
    )


def _relation_type(field: RelationalField[Any]) -> FieldType:
    if isinstance(field, ManyToManyFieldInstance):
        return FieldType.MANY_TO_MANY
    if isinstance(field, OneToOneFieldInstance | BackwardOneToOneRelation):
        return FieldType.ONE_TO_ONE
    if isinstance(field, ForeignKeyFieldInstance):
        return FieldType.FOREIGN_KEY
    if isinstance(field, BackwardFKRelation):
        return FieldType.REVERSE_FK
    return FieldType.UNKNOWN


def _related_name(field: RelationalField[Any]) -> str | None:
    related = getattr(field, "related_name", None)
    return str(related) if isinstance(related, str) and related else None
