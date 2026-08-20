"""`ModelAdmin`: how one model is presented.

Django's vocabulary, because it is the one this audience already knows, but the
declarations are validated against the model spec at start-up rather than failing
on the first request. A typo in `list_display` should be a start-up error, not a
500 the first time someone opens that page.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from fastfort.core.exceptions import ConfigurationError
from fastfort.spec import FieldType, Filter, FilterOperator, SortSpec
from fastfort.ui.icons import icon_names, is_icon

from .widgets import WIDGET_NAMES, widget_for

if TYPE_CHECKING:
    from fastfort.spec import ModelSpec

__all__ = ["Action", "ModelAdmin", "action"]

#: The name of the delete action every model gets for free.
DELETE_ACTION = "delete"

#: Offered instead of `DELETE_ACTION` on the trash view of a model that
#: declared `soft_delete_field` -- see the option's own docstring.
RESTORE_ACTION = "restore"

#: Sets one field across every selected row. Offered only by a model that
#: named the fields it will accept -- see `ModelAdmin.bulk_editable`.
EDIT_ACTION = "edit"

#: Controls a bulk edit can set: one box holding one value that means the same
#: thing for every selected row. `readonly` is excluded for the obvious
#: reason, and everything absent -- files, geometry, JSON, multi-valued
#: relations -- is excluded because "the same value for forty rows" is not a
#: sentence those columns can complete.
BULK_EDIT_WIDGETS = frozenset(
    {
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
        "checkbox",
        "nullboolean",
        "select",
        "relation",
    }
)

#: Types `soft_delete_field` may name. Nullable is checked separately: the
#: marker's *value* is what says trashed or not, so a column that could never
#: be null could never say "not trashed" either.
_SOFT_DELETE_TYPES = frozenset({FieldType.BOOLEAN, FieldType.DATE, FieldType.DATETIME})


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

    #: The form's sections, in order. Without it every field lands in one
    #: unnamed group, which is what a short form wants; a model with twenty
    #: columns is a wall of controls until this is set::
    #:
    #:     fieldsets = (
    #:         (None, {"fields": ("name", "sku", "category")}),
    #:         ("Pricing", {"fields": ("price", "cost"),
    #:                      "description": "Shown to customers."}),
    #:         ("Logistics", {"fields": ("weight",), "collapsed": True}),
    #:     )
    #:
    #: A `None` title renders the section without a heading, for the opening
    #: group that needs no name. `collapsed` renders it shut -- as a
    #: `<details>`, so it still opens without script.
    #:
    #: Naming a field twice is an error, and so is omitting one the database
    #: requires: a form that cannot produce a saveable row should say so at
    #: start-up rather than at the first `NOT NULL` violation. Leaving out an
    #: optional field is allowed and is how a section list narrows a form.
    fieldsets: ClassVar[Sequence[tuple[str | None, Mapping[str, Any]]]] = ()

    #: A nullable boolean or date/datetime column that means "trashed" rather
    #: than gone. Set it and `DELETE_ACTION` stops calling `adapter.delete` --
    #: it writes the marker instead, `deletion_plan` is never consulted (a
    #: trashed row has not actually left the table, so nothing it referenced
    #: needs to cascade), and the list gains a trash view offering
    #: `RESTORE_ACTION` in place of delete.
    #:
    #: No new table, no new adapter method: a soft delete is a write to a
    #: column the project already owns, so it goes through `adapter.update`
    #: exactly like any other field. Excluded from create/edit forms
    #: automatically -- see `editable_field_names` -- because it is the
    #: deletion pipeline's column, not one a form should let someone set by
    #: hand.
    soft_delete_field: ClassVar[str | None] = None

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

    #: Whether the list offers uploading a file back in.
    #:
    #: Off by default, unlike `exportable`. Reading rows out is a permission the
    #: gate already grants; writing several thousand of them in one request is a
    #: different thing to hand somebody by accident, and a model whose rows have
    #: side effects -- an order that ships, a message that sends -- should say so
    #: deliberately rather than inherit it.
    importable: ClassVar[bool] = False

    #: Columns an import may set. Defaults to every editable field, minus the
    #: ones no file can carry: passwords, uploads, binary columns and anything
    #: the spec marks sensitive. Narrow it to make the accepted file smaller than
    #: the form -- a price list that may update prices but never the supplier.
    #:
    #: Never widens: `FieldSpec.editable` is checked first, so naming a
    #: read-only field here does not make it writable.
    import_fields: ClassVar[Sequence[str]] = ()

    #: Columns editable in place on the list, without opening the form::
    #:
    #:     list_display = ("id", "name", "stock", "is_active")
    #:     list_editable = ("stock", "is_active")
    #:
    #: The whole table becomes one form with one Save button, which is what
    #: makes it work with scripting off: a control per row posting on its own
    #: would be one request per cell and no way to submit any of them without
    #: script. Django's `list_editable` is the same shape for the same reason.
    #:
    #: A column has to be in `list_display` to be editable there -- editing a
    #: cell nobody can see is not a feature -- and takes the same narrow set of
    #: controls a bulk edit does, for the same reason: a map or an upload card
    #: in a table cell is worse than the form it saved a trip to.
    list_editable: ClassVar[Sequence[str]] = ()

    #: Fields a bulk edit may set across every selected row::
    #:
    #:     bulk_editable = ("status", "category", "is_active")
    #:
    #: Empty by default, which switches the action off. Opt-in rather than
    #: free, unlike `delete`: a delete announces itself and asks, while one
    #: mis-set column across forty rows is a silent change nobody sees until
    #: later. Naming the fields is also how a project keeps the destructive
    #: half of its schema out of a control that edits in bulk.
    bulk_editable: ClassVar[Sequence[str]] = ()

    #: Child models edited on this model's own form, in order::
    #:
    #:     class OrderLineInline(admin.TabularInline):
    #:         model = OrderLine
    #:
    #:     class OrderAdmin(admin.ModelAdmin):
    #:         inlines = (OrderLineInline,)
    #:
    #: Saved in the parent's transaction: a child that fails validation leaves
    #: the parent unwritten too, because half of an order is worse than none
    #: of it. See `admin/inlines.py`.
    inlines: ClassVar[Sequence[type[Any]]] = ()

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
            "import_fields",
        ):
            for name in getattr(self, attribute):
                if name not in known:
                    problems.append(f"{attribute} names {name!r}, which {self.spec.key} has no")

        for name in self.ordering:
            bare = name.removeprefix("-")
            if bare not in self.spec.sortable_fields:
                problems.append(f"ordering names {bare!r}, which is not sortable")

        for name in self.search_fields:
            if name not in known:
                continue
            field = self.spec.field(name)
            if not (field.is_text_like or field.is_identifier_like):
                problems.append(
                    f"search_fields names {name!r}, which is a {field.type.value} column. "
                    "Search matches text fields by substring and integer and UUID fields "
                    "exactly; there is no useful reading of a search term for this one. "
                    "Offer it through list_filter instead."
                )

        for name in self.list_filter:
            if name in known and not self.spec.field(name).filterable:
                problems.append(
                    f"list_filter names {name!r}; free-text and multi-valued fields "
                    "cannot be offered as a filter"
                )

        if self.icon is not None and not is_icon(self.icon):
            available = ", ".join(icon_names())
            problems.append(f"icon names {self.icon!r}, which is not one of: {available}")

        for name in self.list_editable:
            if name not in known:
                problems.append(f"list_editable names {name!r}, which {self.spec.key} has no")
                continue
            if name not in self.columns():
                problems.append(
                    f"list_editable names {name!r}, which list_display does not show. "
                    "A cell nobody can see cannot be edited in place."
                )
                continue
            if name not in self.editable_field_names():
                problems.append(
                    f"list_editable names {name!r}, which is not writable. It cannot widen "
                    "what FieldSpec.editable and readonly_fields already settled."
                )
                continue
            widget = widget_for(self.spec.field(name))
            if widget not in BULK_EDIT_WIDGETS:
                offered = ", ".join(sorted(BULK_EDIT_WIDGETS))
                problems.append(
                    f"list_editable names {name!r}, which draws a {widget!r} control. "
                    f"A table cell can hold: {offered}"
                )

        for name in self.bulk_editable:
            if name not in known:
                problems.append(f"bulk_editable names {name!r}, which {self.spec.key} has no")
                continue
            if name not in self.editable_field_names():
                problems.append(
                    f"bulk_editable names {name!r}, which is not writable. It cannot widen "
                    "what FieldSpec.editable and readonly_fields already settled."
                )
                continue
            widget = widget_for(self.spec.field(name))
            if widget not in BULK_EDIT_WIDGETS:
                offered = ", ".join(sorted(BULK_EDIT_WIDGETS))
                problems.append(
                    f"bulk_editable names {name!r}, which draws a {widget!r} control. "
                    f"A bulk edit sets one value for every selected row, which these can "
                    f"express: {offered}"
                )

        if self.fieldsets:
            seen: set[str] = set()
            for title, section in self.fieldsets:
                where = f"fieldsets section {title!r}" if title else "the unnamed fieldsets section"
                for name in section.get("fields", ()):
                    if name not in known:
                        problems.append(f"{where} names {name!r}, which {self.spec.key} has no")
                    elif name in seen:
                        problems.append(f"{where} names {name!r} a second time")
                    seen.add(name)

            # A field the database insists on, left out of every section, is a
            # form that cannot produce a saveable row -- and it fails at the
            # flush, with a constraint name, rather than here with a sentence.
            # Optional fields may be omitted freely: that is how a section list
            # narrows a form deliberately.
            for field in self.spec.editable_fields:
                if field.required and not field.has_db_default and field.name not in seen:
                    problems.append(
                        f"fieldsets leaves out {field.name!r}, which {self.spec.key} requires; "
                        "the form could never save a new row"
                    )

        if self.soft_delete_field is not None:
            if self.soft_delete_field not in known:
                problems.append(
                    f"soft_delete_field names {self.soft_delete_field!r}, "
                    f"which {self.spec.key} has no"
                )
            else:
                marker = self.spec.field(self.soft_delete_field)
                if marker.type not in _SOFT_DELETE_TYPES:
                    kinds = ", ".join(sorted(t.value for t in _SOFT_DELETE_TYPES))
                    problems.append(
                        f"soft_delete_field names {self.soft_delete_field!r}, which is a "
                        f"{marker.type.value} column; it must be one of: {kinds}"
                    )
                # A boolean marker is its own two states and needs nothing else --
                # `is_deleted = False` is a perfectly ordinary, usually
                # not-null column. A date/datetime marker's *nullability* is
                # what carries the second state: the moment it was trashed, or
                # nothing. Requiring null on a boolean would reject the
                # ordinary shape of that column for no reason connected to
                # what this option actually needs from it.
                elif marker.type is not FieldType.BOOLEAN and not marker.nullable:
                    problems.append(
                        f"soft_delete_field names {self.soft_delete_field!r}, which is not "
                        "nullable -- a date or datetime marker needs null to mean "
                        "'not trashed', so it has to be able to hold both"
                    )

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
        """Writable fields: the spec's allow-list minus anything marked read-only.

        `soft_delete_field` is excluded here too, and not by being added to
        `readonly_fields`: a read-only field still renders, in a box that
        says what the row will not accept. This one is the deletion
        pipeline's own bookkeeping and should not appear on the form at all.
        """
        locked = set(self.readonly_fields)
        if self.soft_delete_field is not None:
            locked.add(self.soft_delete_field)
        return frozenset(field.name for field in self.spec.editable_fields) - locked

    def soft_delete_filter(self, *, trashed: bool) -> Filter:
        """The condition that keeps trashed rows out of the ordinary list, or
        keeps only them on the trash view.

        A boolean marker is compared directly: `is_deleted = true` is what a
        SQL `WHERE` clause already means. A date/datetime marker is compared
        for nullness instead -- there is no fixed "trashed" timestamp to
        equal, only the presence of *some* value versus none -- which is the
        same reason `deletion_plan`'s own queries branch on column kind
        rather than trying to make every type answer to one comparison.
        """
        assert self.soft_delete_field is not None
        marker = self.spec.field(self.soft_delete_field)
        if marker.type is FieldType.BOOLEAN:
            return Filter(
                field=self.soft_delete_field,
                operator=FilterOperator.EXACT,
                value="true" if trashed else "false",
            )
        return Filter(
            field=self.soft_delete_field,
            operator=FilterOperator.ISNULL,
            value="false" if trashed else "true",
        )

    def soft_delete_values(self) -> tuple[Any, Any]:
        """`(trashed, restored)` for `soft_delete_field`.

        A boolean marker takes `True`/`False`. A date or datetime marker takes
        the moment it was trashed and `None` -- `deleted_at` reads as a
        sentence a bare flag never does, which is the usual reason a project
        reaches for one over the other.
        """
        assert self.soft_delete_field is not None
        marker = self.spec.field(self.soft_delete_field)
        if marker.type is FieldType.BOOLEAN:
            return True, False
        return datetime.now(UTC), None

    def action_specs(self) -> tuple[Action, ...]:
        """The bulk actions this model offers, in declaration order.

        `RESTORE_ACTION` is not named in `actions` the way a project's own
        `@admin.action` methods are -- it comes free the moment
        `soft_delete_field` is set, the same way `DELETE_ACTION` needs no
        method of its own. Which of the two a request actually sees (delete
        on the ordinary list, restore on the trash view) is decided in
        `list_view`, not here: this only says what exists.
        """
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
        if self.bulk_editable:
            found.append(Action(name=EDIT_ACTION, label="Edit selected", icon="edit"))
        if self.soft_delete_field is not None and DELETE_ACTION in self.actions:
            # Not `danger`, and so not confirmed either -- the bulk bar only
            # asks before a `danger` action, and a restore is the one action
            # here that undoes rather than causes the thing worth pausing
            # over. Asking "are you sure?" before an action whose entire
            # point is safety is a step nobody needed.
            found.append(Action(name=RESTORE_ACTION, label="Restore selected", icon="restore"))
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
            from fastfort.spec.geo import summarise

            return summarise(value)
        if field is None or not field.is_relation:
            return value
        if field.type.is_multi_valued:
            return [str(item) for item in value or ()]
        return None if value is None else str(value)

    def export_cell(self, obj: Any, column: str) -> Any:
        """The value for one cell of an exported file.

        The same as `cell` for almost everything, and deliberately different for
        a geometry. A list cell summarises one -- "Polygon · 14 points" -- which
        is the right thing to read in a table and a thing no importer can turn
        back into a polygon. A file is read by a program at least as often as by
        a person, and the promise import makes is that a file this wrote is a
        file it can take back; a column whose exported form cannot be parsed
        breaks that for the whole row.

        So a geometry exports as the same text its own form control shows: a
        point as "lat, lng" and everything else as EWKT.
        """
        field = self.spec.get(column)
        if field is not None and field.type is FieldType.GEOMETRY:
            from .values import render_value

            return render_value(getattr(obj, column, None), field)
        return self.cell(obj, column)
