"""The admin's HTTP surface.

Everything is server-rendered. The list view sorts, searches, filters and
paginates through links and a GET form, so the admin works with JavaScript
disabled and every view is a URL that can be bookmarked or shared.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from fastfort import __version__
from fastfort.core.exceptions import RegistrationError, ValidationError
from fastfort.spec import FieldType, ListQuery, SortSpec
from fastfort.ui.renderer import Renderer
from fastfort.ui.theming import Theme

from .options import ModelAdmin

if TYPE_CHECKING:
    from fastfort.core.app import FastFort
    from fastfort.spec import ModelSpec

__all__ = ["build_admin_router"]

STATIC_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"

#: Load order matters: tokens must be declared before anything reads them.
CSS_SHEETS = (
    "00-reset.css",
    "01-tokens.css",
    "02-base.css",
    "03-layout.css",
    "04-components.css",
)

#: Types whose columns are right-aligned with tabular figures, so digits line up.
_NUMERIC_TYPES = frozenset(
    {FieldType.INTEGER, FieldType.BIGINT, FieldType.DECIMAL, FieldType.FLOAT}
)


@lru_cache(maxsize=1)
def _bundled_css() -> str:
    """Concatenate the stylesheets once per process.

    Served as one response rather than five, and kept in memory because the files
    ship inside the wheel and never change at runtime.
    """
    css = Path(STATIC_DIR, "css")
    return "\n".join((css / name).read_text(encoding="utf-8") for name in CSS_SHEETS)


@lru_cache(maxsize=1)
def _bundled_js() -> str:
    return Path(STATIC_DIR, "js", "fastfort.js").read_text(encoding="utf-8")


def build_admin_router(fort: FastFort) -> APIRouter:
    """Build the router that `FastFort.mount` attaches under `admin.url`."""
    settings = fort.settings
    admin_url = settings.admin.url
    static_url = f"{admin_url}/static"
    renderer = Renderer(settings)

    #: ModelAdmin instances, built once. Instantiating validates the declarations
    #: against the spec, so a typo surfaces here rather than on a request.
    resolved: dict[str, ModelAdmin] = {}

    def admin_for(key: str) -> ModelAdmin:
        if key not in resolved:
            entry = fort.registry.entry_for_key(key)
            spec = fort.backend.introspect(entry.model, key=key)
            resolved[key] = _instantiate(entry.admin, spec)
        return resolved[key]

    def list_url(key: str) -> str:
        return f"{admin_url}/{key}/"

    def base_context(request: Request, current_key: str | None) -> dict[str, Any]:
        theme = Theme.from_settings(settings.ui)
        return {
            "settings": settings,
            "theme": theme,
            "stylesheets": theme.stylesheets(static_url),
            "static_url": static_url,
            "admin_url": admin_url,
            "version": __version__,
            "current_key": current_key,
            "nav": _navigation(fort, list_url),
            "breadcrumbs": (),
            "page_title": settings.project_name,
            "request": request,
        }

    router = APIRouter(tags=["fastfort-admin"])

    # -- assets -------------------------------------------------------------

    @router.get("/static/fastfort.css", include_in_schema=False)
    async def stylesheet() -> Response:
        return Response(
            _bundled_css(),
            media_type="text/css",
            # Long-lived, because the URL changes with the package version in a
            # release. During development auto-reload matters more than caching.
            headers={"Cache-Control": "no-cache" if settings.debug else "public, max-age=86400"},
        )

    @router.get("/static/js/fastfort.js", include_in_schema=False)
    async def script() -> Response:
        return Response(
            _bundled_js(),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache" if settings.debug else "public, max-age=86400"},
        )

    # -- dashboard ----------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, name="fastfort:dashboard")
    async def dashboard(request: Request) -> HTMLResponse:
        models: list[dict[str, Any]] = []

        async with fort.backend.unit_of_work() as uow:
            for entry in fort.registry:
                model_admin = admin_for(entry.key)
                adapter = fort.backend.adapter(entry.model, uow, key=entry.key)
                models.append(
                    {
                        "key": entry.key,
                        "title": model_admin.title,
                        "group": entry.key.split(".", 1)[0],
                        "url": list_url(entry.key),
                        "count": await adapter.count(ListQuery()),
                    }
                )

        context = base_context(request, None) | {
            "page_title": "Dashboard",
            "models": models,
            "dialect": fort.backend.dialect,
            "issues": fort.check(),
        }
        return HTMLResponse(renderer.render("dashboard.html", **context))

    # -- list ---------------------------------------------------------------

    @router.get("/{model_key}/", response_class=HTMLResponse, name="fastfort:list")
    async def list_view(request: Request, model_key: str) -> HTMLResponse:
        try:
            model_admin = admin_for(model_key)
        except RegistrationError as exc:
            # An unknown key is a 404, not a 500: it is usually a stale bookmark.
            raise HTTPException(
                status_code=404, detail=f"No admin is registered for {model_key!r}."
            ) from exc

        entry = fort.registry.entry_for_key(model_key)
        spec = model_admin.spec
        params = dict(request.query_params)

        query = ListQuery.from_params(
            params,
            sortable_fields=spec.sortable_fields,
            filterable_fields=model_admin.list_filter or spec.filterable_fields,
            searchable=bool(model_admin.searchable()),
            default_ordering=model_admin.default_ordering(),
            page_size=model_admin.page_size(settings.admin.page_size),
            max_page_size=settings.admin.max_page_size,
        )

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(
                entry.model,
                uow,
                key=model_key,
                search_fields=model_admin.searchable(),
                select_related=tuple(model_admin.select_related),
                prefetch_related=tuple(model_admin.prefetch_related),
            )
            try:
                page = await adapter.list(query)
            except ValidationError:
                # A malformed filter value in the URL should not 500; showing the
                # unfiltered list with the input cleared is the useful recovery.
                query = ListQuery(
                    search=query.search,
                    ordering=query.ordering,
                    page=1,
                    page_size=query.page_size,
                )
                page = await adapter.list(query)

        columns = tuple(model_admin.columns())
        context = base_context(request, model_key) | {
            "page_title": model_admin.title,
            "breadcrumbs": ({"label": model_admin.title, "url": None},),
            "admin": model_admin,
            "page": page,
            "query": query,
            "list_url": list_url(model_key),
            "columns": _column_headers(spec, columns, query, params, list_url(model_key)),
            "rows": _rows(model_admin, spec, page.items, columns),
            "filters": _filter_controls(spec, model_admin, params),
            "ordering_param": ",".join(sort.as_token() for sort in query.ordering),
            "page_url": lambda number: _with_params(
                list_url(model_key), params, {"p": str(number)}
            ),
        }
        return HTMLResponse(renderer.render("model/list.html", **context))

    return router


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


def _instantiate(admin: Any, spec: ModelSpec) -> ModelAdmin:
    """Accept either a ModelAdmin subclass or an already-built instance."""
    if isinstance(admin, ModelAdmin):
        return admin
    if isinstance(admin, type) and issubclass(admin, ModelAdmin):
        return admin(spec)
    # A registration that predates ModelAdmin still gets a working list view
    # rather than an error, using the spec's own defaults.
    return ModelAdmin(spec)


def _navigation(fort: FastFort, list_url: Any) -> list[dict[str, Any]]:
    """The sidebar, grouped by the namespace half of each registry key."""
    return [
        {
            "label": name.replace("_", " ").title(),
            # Deliberately not "items": Jinja would resolve `group.items` to
            # dict.items and iterate a bound method.
            "entries": [
                {
                    "key": entry.key,
                    "title": _title_of(entry.admin, entry.key),
                    "url": list_url(entry.key),
                }
                for entry in entries
            ],
        }
        for name, entries in fort.registry.grouped().items()
    ]


def _title_of(admin: Any, key: str) -> str:
    """A sidebar label without needing the spec, which needs a database round trip."""
    declared = getattr(admin, "verbose_name_plural", None)
    if isinstance(declared, str) and declared:
        return declared
    return key.split(".", 1)[-1].replace("_", " ").title() + "s"


def _with_params(base: str, existing: dict[str, str], changes: dict[str, str | None]) -> str:
    """Build a URL that keeps the current view and changes one thing.

    Sorting must not drop the active search, and paging must not drop the sort.
    """
    merged = {key: value for key, value in existing.items() if value != ""}
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return f"{base}?{urlencode(merged)}" if merged else base


def _column_headers(
    spec: ModelSpec,
    columns: tuple[str, ...],
    query: ListQuery,
    params: dict[str, str],
    base: str,
) -> list[dict[str, Any]]:
    active = {sort.field: sort for sort in query.ordering}
    headers: list[dict[str, Any]] = []

    for name in columns:
        field = spec.get(name)
        label = field.label if field else name.replace("_", " ").title()
        sortable = name in spec.sortable_fields

        state = "none"
        if name in active:
            state = "descending" if active[name].descending else "ascending"

        sort_url = None
        if sortable:
            # Clicking a sorted column reverses it; clicking a new one sorts
            # ascending. Paging resets, because page 7 of a different order is
            # meaningless.
            token = SortSpec(name, descending=state == "ascending").as_token()
            sort_url = _with_params(base, params, {"o": token, "p": None})

        headers.append({"name": name, "label": label, "sort_state": state, "sort_url": sort_url})

    return headers


def _rows(
    admin: ModelAdmin, spec: ModelSpec, items: tuple[Any, ...], columns: tuple[str, ...]
) -> list[dict[str, Any]]:
    links = admin.link_columns()
    rows: list[dict[str, Any]] = []

    for obj in items:
        cells = []
        for name in columns:
            field = spec.get(name)
            cells.append(
                {
                    "value": admin.cell(obj, name),
                    "boolean": field is not None and field.type is FieldType.BOOLEAN,
                    "numeric": field is not None and field.type in _NUMERIC_TYPES,
                    # Detail views arrive in a later stage. The link columns are
                    # already resolved, so only this value changes then.
                    "url": None,
                    "is_link": name in links,
                }
            )
        rows.append({"cells": cells})

    return rows


def _filter_controls(
    spec: ModelSpec, admin: ModelAdmin, params: dict[str, str]
) -> list[dict[str, Any]]:
    """Dropdowns for the declared filters.

    Only fields with a known, small set of values get a control. A foreign key
    would need its target's rows, which is a query per filter, so it waits for
    the autocomplete widget.
    """
    controls: list[dict[str, Any]] = []

    for name in admin.list_filter:
        field = spec.get(name)
        if field is None:
            continue

        options: tuple[tuple[str, str], ...]
        if field.type is FieldType.BOOLEAN:
            options = (("1", "Yes"), ("0", "No"))
        elif field.choices:
            options = tuple((str(choice.value), choice.label) for choice in field.choices)
        else:
            continue

        current = params.get(name, "")
        controls.append(
            {
                "name": name,
                "label": field.label,
                "choices": [
                    {"value": value, "label": label, "selected": current == value}
                    for value, label in options
                ],
            }
        )

    return controls
