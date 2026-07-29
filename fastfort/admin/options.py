"""`ModelAdmin`: how one model is presented.

Django's vocabulary, because it is the one this audience already knows, but the
declarations are validated against the model spec at start-up rather than failing
on the first request. A typo in `list_display` should be a start-up error, not a
500 the first time someone opens that page.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from fastfort.core.exceptions import ConfigurationError
from fastfort.spec import FieldType, SortSpec

if TYPE_CHECKING:
    from fastfort.spec import ModelSpec

__all__ = ["ModelAdmin"]


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

    #: Overrides for the sidebar. Derived from the model name when unset.
    verbose_name: ClassVar[str | None] = None
    verbose_name_plural: ClassVar[str | None] = None
    icon: ClassVar[str | None] = None

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

    def editable_field_names(self) -> frozenset[str]:
        """Writable fields: the spec's allow-list minus anything marked read-only."""
        return frozenset(field.name for field in self.spec.editable_fields) - frozenset(
            self.readonly_fields
        )

    def cell(self, obj: Any, column: str) -> Any:
        """The raw value for one cell.

        Relations are rendered through the related object's own string form, which
        is what makes a foreign key column readable instead of an integer.
        """
        value = getattr(obj, column, None)
        field = self.spec.get(column)
        if field is None or not field.is_relation:
            return value
        if field.type.is_multi_valued:
            return [str(item) for item in value or ()]
        return None if value is None else str(value)
