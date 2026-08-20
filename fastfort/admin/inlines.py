"""Editing a model's children on its own page.

The gap this closes: a project that models an order and its lines registers
both and gets two unrelated list views. Adding three lines to an order means
three round trips through a separate page, choosing the parent from a dropdown
each time. `ModelAdmin.inlines` puts the children on the parent's form, saved
in the parent's own transaction.

Nothing here is a new adapter method. A child row is created, updated and
deleted through `adapter.create`, `adapter.update` and `adapter.delete`, and
the existing children are fetched with a `ListQuery` carrying one equality
filter on the foreign key -- all of which both backends already implement, and
`tests/orm/test_conformance.py` already holds them to.

**A tabular inline takes a deliberately narrow set of controls.** Text,
numbers, booleans, dates, enums and to-one relations render usefully in a
table cell. A map, an upload card, a rich-text editor or a key/value grid does
not: it is taller than the row that holds it and worse than the full form it
was meant to save a trip to. Naming one is a start-up error rather than a
cramped control, which is the same trade `list_filter` makes for free text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, ClassVar

from fastfort.core.exceptions import ConfigurationError
from fastfort.spec import Choice, FieldType, Filter, FilterOperator, ListQuery

from .values import parse_value, render_value
from .widgets import widget_for

if TYPE_CHECKING:
    from fastfort.spec import FieldSpec, ModelSpec

__all__ = ["InlineAdmin", "InlineCell", "InlineRow", "InlineSet", "TabularInline"]

#: Controls that read well inside a table row: everything that renders as a
#: single box, plus the two that render as one small control. Anything outside
#: this list is refused at declaration time -- see the module docstring.
#:
#: `decimal` and `money` are the reason this list is spelled out rather than
#: guessed at: the canonical inline is an order and its lines, and a line has
#: a price. A list that covered only `text` and `number` would have refused
#: the example the feature exists for.
INLINE_WIDGETS = frozenset(
    {
        # One `<input>`, whatever its type attribute turns out to be.
        "text",
        "number",
        "decimal",
        "money",
        "date",
        "datetime",
        "time",
        "duration",
        "email",
        "url",
        "inet",
        "mac",
        "bits",
        "color",
        # Small controls that still fit a cell.
        "checkbox",
        "nullboolean",
        "select",
        "relation",
        "readonly",
    }
)

#: Separates the parts of an inline control's name: `lines-0-quantity`.
#:
#: A hyphen rather than `__`, which `ListQuery.from_params` reads as a filter
#: operator -- an inline posting `lines__0__quantity` into a page that also
#: accepts filters is a collision waiting for the one project that puts a form
#: and a list on the same URL.
SEPARATOR = "-"

#: Suffixes on the two controls that are about the row rather than about a
#: column of it.
PK_SUFFIX = "_pk"
DELETE_SUFFIX = "_delete"


class InlineAdmin:
    """How one child model is presented on its parent's form."""

    #: The child model class. Required.
    model: ClassVar[Any] = None

    #: The foreign key on the child that points at the parent. Inferred when
    #: the child has exactly one relation to the parent, which is the usual
    #: case; naming it is only necessary when there are two.
    fk_name: ClassVar[str | None] = None

    #: Columns to offer. Defaults to every editable field except the foreign
    #: key back to the parent -- which is set by the relation itself and would
    #: be a dropdown inviting someone to move the row to another parent.
    fields: ClassVar[Sequence[str]] = ()

    #: Blank rows offered for adding. With scripting off these are the only way
    #: to add a child, so the default is one rather than zero.
    extra: ClassVar[int] = 1

    #: A ceiling on how many children the form will accept. `None` is no limit.
    max_num: ClassVar[int | None] = None

    #: Whether each row gets a delete box.
    can_delete: ClassVar[bool] = True

    #: Heading above the table. Derived from the model name when unset.
    verbose_name_plural: ClassVar[str | None] = None

    def __init__(self, spec: ModelSpec, parent: ModelSpec) -> None:
        self.spec = spec
        self.parent = parent
        self.fk = self._resolve_fk()
        self.columns = self._resolve_columns()

    # -- declaration --------------------------------------------------------

    def _resolve_fk(self) -> str:
        """Which column on the child points back at the parent."""
        candidates = [
            field.name
            for field in self.spec
            if field.is_relation
            and field.type is not FieldType.REVERSE_FK
            and field.relation is not None
            and field.relation.target == self.parent.key
        ]

        if self.fk_name is not None:
            if self.fk_name not in {field.name for field in self.spec}:
                raise ConfigurationError(
                    f"{type(self).__name__}.fk_name names {self.fk_name!r}, "
                    f"which {self.spec.key} has no",
                    hint=f"Fields on {self.spec.key}: {', '.join(f.name for f in self.spec)}",
                )
            return self.fk_name

        if not candidates:
            raise ConfigurationError(
                f"{type(self).__name__} names {self.spec.key}, which has no foreign key "
                f"to {self.parent.key}",
                hint="Add the relation to the child model, or set fk_name if it is "
                "named in a way introspection cannot follow.",
            )
        if len(candidates) > 1:
            raise ConfigurationError(
                f"{type(self).__name__} names {self.spec.key}, which points at "
                f"{self.parent.key} through more than one field: {', '.join(candidates)}",
                hint="Set fk_name to the one this inline is about.",
            )
        return candidates[0]

    def _resolve_columns(self) -> tuple[FieldSpec, ...]:
        """The child's columns this inline edits, validated for the table."""
        known = {field.name: field for field in self.spec}
        problems: list[str] = []

        if self.fields:
            chosen: list[FieldSpec] = []
            for name in self.fields:
                if name not in known:
                    problems.append(f"fields names {name!r}, which {self.spec.key} has no")
                    continue
                chosen.append(known[name])

            # Named explicitly, so a control that cannot live in a row is a
            # mistake to report rather than a column to quietly drop: the
            # project asked for this one by name.
            for field in chosen:
                widget = widget_for(field)
                if widget not in INLINE_WIDGETS:
                    offered = ", ".join(sorted(INLINE_WIDGETS))
                    problems.append(
                        f"fields names {field.name!r}, which draws a {widget!r} control. "
                        f"A table row can hold: {offered}. Edit this one on the child's "
                        "own form instead."
                    )
        else:
            # Nothing was named, so nothing was asked for that cannot be given.
            # A column too tall for a row is skipped rather than refused --
            # otherwise one JSON column anywhere on the child would make the
            # default configuration impossible, and every project with one
            # would have to spell out `fields` to say what it already meant.
            #
            # The foreign key goes too: it is set by the relation, and a
            # dropdown for it in every row is an invitation to move a line to
            # a different parent by accident.
            chosen = [
                field
                for field in self.spec.editable_fields
                if field.name != self.fk
                and not (field.primary_key and not field.editable)
                and widget_for(field) in INLINE_WIDGETS
            ]
            if not chosen:
                problems.append(
                    f"{self.spec.key} has no column that fits a table row, so this inline "
                    "would render an empty one"
                )

        if problems:
            listed = "\n".join(f"  - {problem}" for problem in problems)
            raise ConfigurationError(
                f"{type(self).__name__} is misconfigured:\n{listed}",
                hint=f"Fields available on {self.spec.key}: {', '.join(sorted(known))}",
            )
        return tuple(chosen)

    # -- resolved options ---------------------------------------------------

    @property
    def prefix(self) -> str:
        """The form-name namespace for this inline's controls.

        Taken from the child's registry key rather than from the relation, so
        two inlines over the same parent cannot collide.
        """
        return self.spec.key.replace(".", "_")

    @property
    def title(self) -> str:
        return self.verbose_name_plural or self.spec.verbose_name_plural


class TabularInline(InlineAdmin):
    """Children as rows in a table. The shape a line item wants."""


# ---------------------------------------------------------------------------
# What a request renders
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InlineCell:
    """One control inside an inline row."""

    name: str
    label: str
    value: str
    widget: str
    spec: FieldSpec
    choices: tuple[Choice, ...] = ()
    errors: list[str] = dataclass_field(default_factory=list)

    @property
    def required(self) -> bool:
        return self.spec.required and not self.spec.has_db_default


@dataclass(slots=True)
class InlineRow:
    """One child, existing or blank."""

    index: int
    #: The child's primary key, joined by `~` for a composite one. Empty for a
    #: row that does not exist yet.
    pk: str
    cells: tuple[InlineCell, ...]
    #: Ticked for removal. Survives a failed save so the box stays ticked when
    #: the form comes back with an error somewhere else.
    deleted: bool = False

    @property
    def is_new(self) -> bool:
        return not self.pk

    @property
    def has_errors(self) -> bool:
        return any(cell.errors for cell in self.cells)


@dataclass(slots=True)
class InlineSet:
    """One inline's rows, ready to render."""

    inline: InlineAdmin
    rows: list[InlineRow]
    #: Relation options, loaded once per request and shared by every row --
    #: the difference between one query and forty on an order with forty
    #: lines. Held on the set rather than read back off a row, because an
    #: inline with `extra = 0` and no children has no row to read them from.
    choices: dict[str, tuple[Choice, ...]] = dataclass_field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return self.inline.prefix

    @property
    def title(self) -> str:
        return self.inline.title

    @property
    def columns(self) -> tuple[FieldSpec, ...]:
        return self.inline.columns

    @property
    def can_delete(self) -> bool:
        return self.inline.can_delete

    @property
    def has_errors(self) -> bool:
        return any(row.has_errors for row in self.rows)

    def blank_row(self, index: int) -> InlineRow:
        """A row with nothing in it, for the template's "add another" source."""
        return _row(self.inline, index, pk="", values={}, choices=self.choices)


def _row(
    inline: InlineAdmin,
    index: int,
    *,
    pk: str,
    values: Mapping[str, Any],
    choices: Mapping[str, tuple[Choice, ...]],
) -> InlineRow:
    cells = tuple(
        InlineCell(
            name=f"{inline.prefix}{SEPARATOR}{index}{SEPARATOR}{field.name}",
            label=field.label,
            value=values.get(field.name, ""),
            widget=widget_for(field),
            spec=field,
            choices=choices.get(field.name, ()),
        )
        for field in inline.columns
    )
    return InlineRow(index=index, pk=pk, cells=cells)


async def build_inline_set(
    inline: InlineAdmin,
    *,
    adapter: Any,
    parent_pk: tuple[Any, ...] | None,
    choice_limit: int,
) -> InlineSet:
    """This inline's existing children, plus its blank rows.

    `parent_pk` is `None` on the add form, where there is no parent yet and so
    no children to find -- the set is `extra` blank rows and nothing else.
    """
    choices: dict[str, tuple[Choice, ...]] = {}
    for field in inline.columns:
        if field.is_relation and field.type is not FieldType.REVERSE_FK:
            found = await adapter.related_choices(field.name, "", limit=choice_limit)
            choices[field.name] = tuple(
                Choice(value=option.value, label=option.label or str(option.value))
                for option in found
            )

    rows: list[InlineRow] = []
    if parent_pk is not None:
        existing = await adapter.list(
            ListQuery(
                filters=(
                    Filter(
                        field=inline.fk,
                        operator=FilterOperator.EXACT,
                        value=str(parent_pk[0]),
                    ),
                ),
                page_size=inline.max_num or 100,
            )
        )
        for index, child in enumerate(existing.items):
            rows.append(
                _row(
                    inline,
                    index,
                    pk=_key_of(adapter, child),
                    values={
                        field.name: render_value(getattr(child, field.name, None), field)
                        for field in inline.columns
                    },
                    choices=choices,
                )
            )

    for offset in range(inline.extra):
        rows.append(_row(inline, len(rows) + offset, pk="", values={}, choices=choices))

    return InlineSet(inline=inline, rows=rows, choices=choices)


def _key_of(adapter: Any, obj: Any) -> str:
    return "~".join(str(part) for part in adapter.primary_key_of(obj))


@dataclass(slots=True)
class InlinePlan:
    """What a submitted inline asks to be written, once it has parsed."""

    inline: InlineAdmin
    created: list[dict[str, Any]]
    updated: list[tuple[str, dict[str, Any]]]
    deleted: list[str]


def bind_inline_set(inline_set: InlineSet, data: Mapping[str, Any]) -> tuple[InlineSet, InlinePlan]:
    """Read one inline out of a submitted form.

    Returns the set rebuilt from what was typed -- so a form that comes back
    with an error still shows every value -- alongside the writes it asked
    for. Nothing is written here; that is `apply_inline_set`, which runs
    inside the parent's own transaction.

    A blank row is not an empty child: a row whose every column was left alone
    is somebody who did not use it, and creating a row of nulls from it is the
    bug this check exists to prevent.
    """
    prefix = inline_set.prefix
    choices = inline_set.choices
    plan = InlinePlan(inline=inline_set.inline, created=[], updated=[], deleted=[])

    indices = sorted(_submitted_indices(prefix, data))
    rows: list[InlineRow] = []

    for position, index in enumerate(indices):
        base = f"{prefix}{SEPARATOR}{index}{SEPARATOR}"
        pk = str(data.get(f"{base}{PK_SUFFIX}", "") or "")
        raw = {
            field.name: str(data.get(f"{base}{field.name}", "") or "")
            for field in inline_set.columns
        }
        deleted = bool(data.get(f"{base}{DELETE_SUFFIX}"))

        row = _row(inline_set.inline, position, pk=pk, values=raw, choices=choices)
        row.deleted = deleted
        rows.append(row)

        if deleted:
            if pk:
                plan.deleted.append(pk)
            continue

        # A checkbox posts nothing when unticked, so "untouched" cannot be read
        # off the raw values for a boolean column. Only the others count
        # towards deciding whether a new row was used at all.
        typed = any(
            raw[field.name]
            for field in inline_set.columns
            if widget_for(field) not in ("checkbox", "nullboolean")
        )
        if not pk and not typed:
            continue

        cleaned: dict[str, Any] = {}
        for cell, field in zip(row.cells, inline_set.columns, strict=True):
            widget = widget_for(field)
            if widget == "readonly":
                continue
            if widget == "checkbox":
                cleaned[field.name] = bool(data.get(f"{base}{field.name}"))
                continue
            try:
                cleaned[field.name] = parse_value(raw[field.name], field)
            except ValueError as exc:
                cell.errors.append(str(exc))

        if row.has_errors:
            continue
        if pk:
            plan.updated.append((pk, cleaned))
        else:
            plan.created.append(cleaned)

    # The blank rows the next render offers. Without these a form that came
    # back with an error would lose its "add another" slot.
    for offset in range(inline_set.inline.extra):
        rows.append(_row(inline_set.inline, len(rows) + offset, pk="", values={}, choices=choices))

    return InlineSet(inline=inline_set.inline, rows=rows, choices=choices), plan


def _submitted_indices(prefix: str, data: Mapping[str, Any]) -> set[int]:
    """Which row numbers this submission carried.

    Read off the keys rather than from a hidden count. A management field
    saying "there are four rows" is a second source of truth for something the
    request already states, and the failure it produces -- a row silently
    dropped because the count was stale -- is invisible in the response.
    """
    found: set[int] = set()
    head = f"{prefix}{SEPARATOR}"
    for key in data:
        if not key.startswith(head):
            continue
        rest = key[len(head) :]
        number, _, remainder = rest.partition(SEPARATOR)
        if remainder and number.isdigit():
            found.add(int(number))
    return found


async def apply_inline_set(plan: InlinePlan, *, adapter: Any, parent: Any, spec: ModelSpec) -> None:
    """Write one inline's rows, inside the caller's transaction.

    The parent is passed rather than its key: on the add form the parent was
    created moments ago in this same unit of work, and assigning the object is
    what lets the adapter set the foreign key without the row having to be
    flushed and read back first.
    """
    for pk in plan.deleted:
        existing = await adapter.get(_parse_key(spec, pk))
        if existing is not None:
            await adapter.delete(existing)

    for pk, values in plan.updated:
        existing = await adapter.get(_parse_key(spec, pk))
        if existing is not None:
            await adapter.update(existing, values)

    for values in plan.created:
        await adapter.create({**values, plan.inline.fk: parent})


def _parse_key(spec: ModelSpec, raw: str) -> tuple[Any, ...]:
    """A primary key out of its URL form, coerced to the column's type."""
    parts = raw.split("~")
    return tuple(
        parse_value(part, spec.field(name))
        for part, name in zip(parts, spec.primary_key, strict=False)
    )
