"""`ModelAdmin`: how one model is presented.

Django's vocabulary, because it is the one this audience already knows, but the
declarations are validated against the model spec at start-up rather than failing
on the first request. A typo in `list_display` should be a start-up error, not a
500 the first time someone opens that page.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from fastfort.core.exceptions import ConfigurationError
from fastfort.spec import FieldType, SortSpec
from fastfort.ui.icons import icon_names, is_icon

from .widgets import WIDGET_NAMES

if TYPE_CHECKING:
    from fastfort.spec import ModelSpec

__all__ = ["Action", "ModelAdmin", "action"]

#: The name of the delete action every model gets for free.
DELETE_ACTION = "delete"


@dataclass(frozen=True, slots=True)
class Action:
    """One entry in the bulk-action menu above the list."""

    name: str
    label: str
    icon: str | None = None
    #: Asks for confirmation before running, and is drawn in the danger colour.
    #: Anything that cannot be undone should say so here.
    danger: bool = False
    #: What the confirmation asks. `{count}` is substituted.
    confirm: str | None = None


def action(
    label: str,
    *,
    icon: str | None = None,
    danger: bool = False,
    confirm: str | None = None,
) -> Callable[[Any], Any]:
    """Mark a `ModelAdmin` method as a bulk action.

    The method receives the adapter and the selected rows, and returns the
    message to show::

        @admin.action("Mark as shipped", icon="truck")
        async def mark_shipped(self, adapter, objects):
            for order in objects:
                await adapter.update(order, {"status": "shipped"})
            return f"{len(objects)} orders marked as shipped."

    Declared with a decorator rather than by naming methods in a list, so the
    label and the code that implements it cannot drift apart.
    """

    def decorate(method: Any) -> Any:
        method.ff_action = Action(
            name=method.__name__, label=label, icon=icon, danger=danger, confirm=confirm
        )
        return method

    return decorate


class ModelAdmin:
    """Declarative presentation options for one registered model.

    Subclassed per model::

        @admin.register(Product)
        class ProductAdmin(admin.ModelAdmin):
            list_display = ("id", "name", "price", "is_active")
            search_fields = ("name", "description")
            ordering = ("-created_at",)
    """

    #: Columns of the list view, in order. Empty means "the primary key plus the
    #: first few readable fields", which makes a bare registration useful.
    list_display: ClassVar[Sequence[str]] = ()

    #: Columns that link to the detail view. Defaults to the first column.
    list_display_links: ClassVar[Sequence[str]] = ()

    #: Fields offered in the filter panel. Restricted to what the spec marks
    #: filterable, so free text cannot become a dropdown of ten thousand values.
    list_filter: ClassVar[Sequence[str]] = ()

    #: Fields the search box covers. Without any, the box is not rendered --
    #: better than a box that silently matches nothing.
    search_fields: ClassVar[Sequence[str]] = ()

    #: Default ordering, Django-style: a leading "-" means descending.
    ordering: ClassVar[Sequence[str]] = ()

    list_per_page: ClassVar[int | None] = None

    #: Relations to preload. Without these a list showing a related name costs
    #: one extra query per row.
    select_related: ClassVar[Sequence[str]] = ()
    prefetch_related: ClassVar[Sequence[str]] = ()

    #: Fields shown but never written. Merged into the spec's own allow-list.
    readonly_fields: ClassVar[Sequence[str]] = ()

    #: Columns that store a password hash. Their control takes a new password and
    #: a confirmation, hashes it, and leaves the stored value alone when both are
    #: blank. Detected from the spec when not declared, because a text box that
    #: expects someone to paste an Argon2 hash is not a usable control.
    password_fields: ClassVar[Sequence[str]] = ()

    #: Overrides for the sidebar. Derived from the model name when unset.
    #:
    #: Not translated. A model's name is the project's word for its own domain,
    #: and FastFort has no business guessing it in nine languages -- the same
    #: reason Django does not translate your model names either. FastFort
    #: translates its own interface: the buttons, the filters, the messages.
    verbose_name: ClassVar[str | None] = None
    verbose_name_plural: ClassVar[str | None] = None

    #: The sidebar group this model sits under. Without it the namespace half of
    #: the registry key is used.
    group_name: ClassVar[str | None] = None

    #: Labels for individual fields, keyed by field name. Overrides what the
    #: adapter derived from the column.
    field_labels: ClassVar[Mapping[str, str]] = {}

    #: A name from `fastfort.ui.icons`, drawn beside the sidebar entry. Checked at
    #: declaration time, so a typo is an error rather than a silently blank slot.
    icon: ClassVar[str | None] = None

    #: Which control renders a field, overriding what its type would choose.
    #:
    #: Keyed by field name or by `FieldType`, so a project can retype one column
    #: or every column of a kind::
    #:
    #:     formfield_overrides = {
    #:         "brand_colour": "color",
    #:         "description": "richtext",
    #:         FieldType.TEXT: "richtext",
    #:     }
    #:
    #: A name beats a type when both match. Names are checked against the spec at
    #: declaration time; an unknown widget name is a start-up error rather than a
    #: field that silently renders read-only.
    formfield_overrides: ClassVar[Mapping[str | FieldType, str]] = {}

    #: Whether the list offers a download of the current view. Off for a model
    #: whose rows should not leave the admin in a file.
    exportable: ClassVar[bool] = True

    #: Columns an export contains. Defaults to what the list shows, so the file
    #: matches the table it came from; widen it to include columns that are on
    #: the record but too many to put in a table.
    export_fields: ClassVar[Sequence[str]] = ()

    #: Bulk actions offered once rows are selected. `"delete"` is built in and
    #: enabled by default; anything else is the name of a method carrying
    #: `@admin.action`. Set to `()` to offer none, which is how a model whose rows
    #: must never be removed in bulk says so.
    actions: ClassVar[Sequence[str]] = (DELETE_ACTION,)

    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec
        self._validate()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} for {self.spec.key}>"

    # -- validation ---------------------------------------------------------

    def _validate(self) -> None:
        """Check every declared field name against the spec.

        Collects all the problems before raising, so one run of `fastfort check`
        reports every typo rather than the first.
        """
        problems: list[str] = []
        known = {field.name for field in self.spec}

        for attribute in (
            "list_display",
            "list_display_links",
            "list_filter",
            "search_fields",
            "select_related",
            "prefetch_related",
            "readonly_fields",
            "password_fields",
            "export_fields",
        ):
            for name in getattr(self, attribute):
                if name not in known:
                    problems.append(f"{attribute} names {name!r}, which {self.spec.key} has no")

        for name in self.ordering:
            bare = name.removeprefix("-")
            if bare not in self.spec.sortable_fields:
                problems.append(f"ordering names {bare!r}, which is not sortable")

        for name in self.search_fields:
            if name in known and not self.spec.field(name).is_text_like:
                problems.append(f"search_fields names {name!r}, which is not a text field")

        for name in self.list_filter:
            if name in known and not self.spec.field(name).filterable:
                problems.append(
                    f"list_filter names {name!r}; free-text and multi-valued fields "
                    "cannot be offered as a filter"
                )

        if self.icon is not None and not is_icon(self.icon):
            available = ", ".join(icon_names())
            problems.append(f"icon names {self.icon!r}, which is not one of: {available}")

        for name in self.actions:
            if name == DELETE_ACTION:
                continue
            handler = getattr(self, name, None)
            if handler is None:
                problems.append(f"actions names {name!r}, which is not a method on this admin")
            elif not hasattr(handler, "ff_action"):
                problems.append(f"actions names {name!r}, which is missing the @admin.action mark")

        for name in self.field_labels:
            if name not in known:
                problems.append(f"field_labels names {name!r}, which {self.spec.key} has no")

        for key, widget in self.formfield_overrides.items():
            if isinstance(key, str) and key not in known:
                problems.append(f"formfield_overrides names {key!r}, which {self.spec.key} has no")
            if widget not in WIDGET_NAMES:
                offered = ", ".join(sorted(WIDGET_NAMES))
                problems.append(
                    f"formfield_overrides maps {key!r} to {widget!r}, "
                    f"which is not a widget. Available: {offered}"
                )

        if problems:
            listed = "\n".join(f"  - {problem}" for problem in problems)
            available = ", ".join(sorted(known))
            raise ConfigurationError(
                f"{type(self).__name__} is misconfigured:\n{listed}",
                hint=f"Fields available on {self.spec.key}: {available}",
            )

    # -- resolved options ---------------------------------------------------

    @property
    def title(self) -> str:
        return self.verbose_name_plural or self.spec.verbose_name_plural

    @property
    def singular(self) -> str:
        return self.verbose_name or self.spec.verbose_name

    def widget_override(self, spec: Any) -> str | None:
        """The control this admin insists on for a field, if it named one.

        A field name wins over a type, because the narrower declaration is the
        one that was written about this column in particular.
        """
        by_name = self.formfield_overrides.get(spec.name)
        if by_name is not None:
            return by_name
        return self.formfield_overrides.get(spec.type)

    def field_label(self, name: str, fallback: str) -> str:
        """A field's label, honouring an override the admin declared."""
        return self.field_labels.get(name) or fallback

    def columns(self) -> tuple[str, ...]:
        """The list columns, falling back to something useful."""
        if self.list_display:
            return tuple(self.list_display)

        # A bare registration should still show a readable table, so the primary
        # key comes first and then whatever scalar fields exist, capped so a wide
        # model does not produce a hundred columns.
        chosen = list(self.spec.primary_key)
        for field in self.spec:
            if len(chosen) >= 6:
                break
            if field.name in chosen or field.sensitive:
                continue
            if field.type in {FieldType.JSON, FieldType.FILE, FieldType.IMAGE}:
                continue
            if field.type.is_multi_valued:
                continue
            chosen.append(field.name)
        return tuple(chosen)

    def export_columns(self) -> tuple[str, ...]:
        return tuple(self.export_fields) if self.export_fields else self.columns()

    def link_columns(self) -> frozenset[str]:
        if self.list_display_links:
            return frozenset(self.list_display_links)
        columns = self.columns()
        return frozenset(columns[:1])

    def default_ordering(self) -> tuple[SortSpec, ...]:
        if self.ordering:
            return tuple(
                SortSpec(name.removeprefix("-"), descending=name.startswith("-"))
                for name in self.ordering
            )
        # Newest first is the useful default for an admin, and the primary key is
        # the only column guaranteed to exist.
        return tuple(SortSpec(name, descending=True) for name in self.spec.primary_key)

    def page_size(self, fallback: int) -> int:
        return self.list_per_page or fallback

    def searchable(self) -> tuple[str, ...]:
        return tuple(self.search_fields)

    def password_field_names(self) -> frozenset[str]:
        """Fields to render as a password control.

        Declared names win. Otherwise a field is treated as a password when the
        adapter typed it that way, or when it is a sensitive column whose name
        says so -- which is how a plain `String` column called `hashed_password`
        is picked up without the project having to say anything.
        """
        if self.password_fields:
            return frozenset(self.password_fields)
        return frozenset(
            field.name
            for field in self.spec
            if field.type is FieldType.PASSWORD
            or (field.sensitive and "password" in field.name.lower())
        )

    def editable_field_names(self) -> frozenset[str]:
        """Writable fields: the spec's allow-list minus anything marked read-only."""
        return frozenset(field.name for field in self.spec.editable_fields) - frozenset(
            self.readonly_fields
        )

    def action_specs(self) -> tuple[Action, ...]:
        """The bulk actions this model offers, in declaration order."""
        found: list[Action] = []
        for name in self.actions:
            if name == DELETE_ACTION:
                found.append(
                    Action(
                        name=DELETE_ACTION,
                        label="Delete selected",
                        icon="trash",
                        danger=True,
                        confirm="Delete {count} rows?",
                    )
                )
                continue
            handler = getattr(self, name, None)
            declared = getattr(handler, "ff_action", None)
            if declared is not None:
                found.append(declared)
        return tuple(found)

    def action_handler(self, name: str) -> Any:
        """The callable behind an action name, or None when it is not offered.

        Checked against `actions` rather than resolved by `getattr` alone: a
        method that happens to carry the mark but was left out of the list must
        not be reachable by posting its name.
        """
        if name not in self.actions or name == DELETE_ACTION:
            return None
        handler = getattr(self, name, None)
        return handler if hasattr(handler, "ff_action") else None

    def cell(self, obj: Any, column: str) -> Any:
        """The raw value for one cell.

        Relations are rendered through the related object's own string form, which
        is what makes a foreign key column readable instead of an integer.
        """
        value = getattr(obj, column, None)
        field = self.spec.get(column)
        if field is not None and field.type is FieldType.GEOMETRY and value is not None:
            # Otherwise the cell is a WKB hex blob, which reads as corruption.
            from .geo import summarise

            return summarise(value)
        if field is None or not field.is_relation:
            return value
        if field.type.is_multi_valued:
            return [str(item) for item in value or ()]
        return None if value is None else str(value)
