"""The admin's HTTP surface.

Everything is server-rendered. The list view sorts, searches, filters and
paginates through links and a GET form, so the admin works with JavaScript
disabled and every view is a URL that can be bookmarked or shared.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.datastructures import UploadFile

from fastfort._version import __version__
from fastfort.auth.service import AdminAuth
from fastfort.core.exceptions import (
    AdapterError,
    ObjectNotFound,
    RegistrationError,
    SecurityError,
    ValidationError,
)
from fastfort.core.registry import default_model_key
from fastfort.i18n import Translator, is_rtl, negotiate_language
from fastfort.spec import Choice, DeletionPlan, FieldType, ListQuery, SortSpec
from fastfort.ui.compression import compress_asset
from fastfort.ui.renderer import Renderer
from fastfort.ui.theming import Theme

from .auth_views import build_auth_router
from .export import EXPORT_FORMATS, stream_csv, stream_json, stream_xlsx
from .files import UploadedFile
from .forms import Form
from .importing import (
    ImportFileError,
    Plan,
    RowError,
    build_plan,
    column_hint,
    importable_fields,
    read_table,
    sniff_format,
    template_rows,
)
from .insights import Series, build_series
from .messages import Message, MessageLevel, Messages
from .options import ModelAdmin
from .security import LANGUAGE_COOKIE, make_guard, safe_next_url, set_csrf_cookie
from .values import split_multi

if TYPE_CHECKING:
    from fastfort.core.app import FastFort
    from fastfort.core.settings import MediaSettings
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
    "05-admin.css",
    "06-widgets.css",
)

#: Every script this admin will serve. An allow-list, because the route reads a
#: file by a name the request chose.
#:
#: The first two load on every page. The other two are asked for only by a page
#: that renders a field needing them, which is what `_extra_scripts` decides:
#: the map editor and the structured-data editors together are about a third of
#: the front end's weight, and the great majority of admin pages have no
#: geometry, array or hstore column at all.
SCRIPTS = frozenset({"boot.js", "fastfort.js", "fastfort-geo.js", "fastfort-data.js"})

#: The bundle each widget needs, over and above the everyday one.
_WIDGET_SCRIPTS = {
    "geometry": "fastfort-geo.js",
    "tags": "fastfort-data.js",
    "keyvalue": "fastfort-data.js",
    "json": "fastfort-data.js",
    "inet": "fastfort-data.js",
    "mac": "fastfort-data.js",
}

#: Filterable types that ask for a lower and an upper bound rather than a list of
#: values, mapped to the input the two boxes use.
#:
#: A number, a time and a duration all have the same problem an exact-match
#: dropdown cannot solve: nobody filters a price list to exactly 19.99, they
#: filter it to under twenty. Dates are handled separately, above -- they get the
#: same two boxes plus the presets ("this month") that are what a date filter is
#: actually used for, and no equivalent exists for a price.
_BOUNDED_FILTER_TYPES = {
    FieldType.INTEGER: "number",
    FieldType.BIGINT: "number",
    FieldType.FLOAT: "number",
    FieldType.DECIMAL: "number",
    FieldType.MONEY: "number",
    FieldType.TIME: "time",
    # No native control, so the same text box the form's duration field starts
    # as -- and the same `HH:MM:SS` the parser reads.
    FieldType.DURATION: "text",
}

#: Types whose columns are right-aligned with tabular figures, so digits line up.
_NUMERIC_TYPES = frozenset(
    {FieldType.INTEGER, FieldType.BIGINT, FieldType.DECIMAL, FieldType.FLOAT}
)

#: The accent colours the appearance panel offers, as OKLCH hues.
#:
#: Hues rather than hex codes, because the whole palette is derived from one
#: number: picking a swatch moves `--ff-h` and every accent in the interface
#: follows, including the ones dark mode computes at its own lightness. A list of
#: hex values could not do that -- it would fix one shade and leave the other
#: eleven stops of the ramp still pointing at the old hue.
ACCENT_PRESETS: tuple[dict[str, Any], ...] = (
    {"name": "Violet", "hue": 285},
    {"name": "Indigo", "hue": 265},
    {"name": "Blue", "hue": 255},
    {"name": "Sky", "hue": 230},
    {"name": "Teal", "hue": 195},
    {"name": "Green", "hue": 150},
    {"name": "Lime", "hue": 130},
    {"name": "Amber", "hue": 85},
    {"name": "Orange", "hue": 55},
    {"name": "Red", "hue": 25},
    {"name": "Pink", "hue": 0},
    {"name": "Magenta", "hue": 330},
)


@lru_cache(maxsize=1)
def _read_css() -> str:
    """Concatenate the stylesheets once per process.

    Served as one response rather than six, and kept in memory because the files
    ship inside the wheel and never change at runtime.
    """
    css = Path(STATIC_DIR, "css")
    return "\n".join((css / name).read_text(encoding="utf-8") for name in CSS_SHEETS)


@lru_cache(maxsize=2)
def _read_js(name: str) -> str:
    return Path(STATIC_DIR, "js", name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _read_favicon() -> str:
    return Path(STATIC_DIR, "favicon.svg").read_text(encoding="utf-8")


def _bundled_css(*, debug: bool = False) -> str:
    """The stylesheet, re-read from disk while debugging.

    The cache is right in production -- these files cannot change without a new
    process -- but under `--reload` it means an edit to a stylesheet does nothing
    until the server is restarted, which is a long way to travel to discover
    that a rule was never being served. The response already declares itself
    uncacheable in debug; this is the other half of that promise.
    """
    if debug:
        _read_css.cache_clear()
    return _read_css()


def _bundled_js(name: str = "fastfort.js", *, debug: bool = False) -> str:
    if debug:
        _read_js.cache_clear()
    return _read_js(name)


def build_admin_router(fort: FastFort) -> APIRouter:
    """Build the router that `FastFort.mount` attaches under `admin.url`."""
    settings = fort.settings
    admin_url = settings.admin.url
    static_url = f"{admin_url}/static"
    renderer = Renderer(settings)
    auth = AdminAuth(fort)
    fort.auth = auth
    notices = Messages(settings)

    #: How composite primary keys travel in a URL. A single-column key is just its
    #: value; more than one is joined, which keeps one route shape for both.
    key_separator = "~"

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

    def translator_for(request: Request) -> Translator:
        """The language for this request.

        An explicit choice wins over the project's configuration, which wins over
        the browser. The browser comes last because a project that configured
        Uzbek should not show English merely because a laptop was bought abroad.
        """
        return Translator(
            negotiate_language(
                chosen=request.cookies.get(LANGUAGE_COOKIE),
                configured=settings.ui.language,
                accept=request.headers.get("accept-language", ""),
            ),
            project_dir=settings.ui.locale_dir,
        )

    def base_context(request: Request, current_key: str | None) -> dict[str, Any]:
        theme = Theme.from_settings(settings.ui)
        user = request.scope.get("fastfort_user")
        translator = translator_for(request)
        return {
            "_": translator,
            "language": translator.language,
            # Arabic reads right to left, and the whole layout follows from this
            # one attribute: the stylesheet is written in logical properties
            # throughout, so the sidebar, the table, the chevrons and every
            # popover turn around without a mirrored rule anywhere.
            "text_direction": "rtl" if is_rtl(translator.language) else "ltr",
            "languages": translator.choices(),
            "current_language": translator.choice,
            "language_url": f"{admin_url}/language",
            "user": user,
            "user_label": _user_label(fort, user),
            "user_identity": _user_identity(fort, user),
            "is_superuser": user is not None and fort.user_config.is_superuser(user),
            "logout_url": f"{admin_url}/logout",
            "csrf_field": auth.csrf.field_name,
            "csrf_token": request.scope.get("fastfort_csrf", ""),
            "settings": settings,
            "theme": theme,
            "stylesheets": theme.stylesheets(static_url),
            "static_url": static_url,
            "admin_url": admin_url,
            "version": __version__,
            "current_key": current_key,
            "nav": _navigation(fort, list_url),
            # What Ctrl+K searches. Sent with the page rather than fetched on
            # open: it is the sidebar's own contents, a few hundred bytes, and a
            # palette that has to wait for a request before it can answer is a
            # palette people stop reaching for.
            "palette": _palette_entries(fort, list_url, translator),
            "accents": ACCENT_PRESETS,
            "ui_text": _ui_text(translator),
            # Nothing beyond the everyday bundle, unless a view says otherwise.
            # Declared here rather than per page because the renderer runs with
            # StrictUndefined, so a template reading a name one view forgot to
            # set is an error, not an empty loop.
            "extra_scripts": (),
            "breadcrumbs": (),
            # Only a form opened from a relation field is a popup; every other
            # page gets the shell. Declared here so no template has to guard.
            "popup": False,
            "page_title": settings.project_name,
            "messages": notices.read(request),
            "request": request,
            # Where a language change should return to. The query string is part
            # of it: switching language from a filtered, sorted, paginated list
            # and landing on page one of an unfiltered list is a loss of work.
            "current_path": _current_path(request),
        }

    #: Static assets and the sign-in page stay reachable while signed out.
    public = APIRouter(tags=["fastfort-admin"])

    #: Everything else. The gate is a router-wide dependency rather than a call
    #: inside each view, because a view that forgets to check looks exactly like
    #: one that decided not to.
    router = APIRouter(
        tags=["fastfort-admin"], dependencies=[Depends(_remember(make_guard(auth, settings), auth))]
    )

    def _popup_result(
        request: Request,
        renderer_: Renderer,
        base_context_: Any,
        model_key: str,
        key: tuple[Any, ...],
        label: str,
    ) -> HTMLResponse:
        """What a popup returns after saving: the row it just created.

        A page carrying the new value in data attributes, which the script reads
        and hands to the window that opened it before closing. Data attributes
        rather than an inline script, because the admin's CSP is
        `script-src 'self'` with no nonce -- and keeping it that way is worth
        more than the convenience of a one-line `<script>`.
        """
        return HTMLResponse(
            renderer_.render(
                "popup_result.html",
                **base_context_(request, model_key)
                | {
                    "page_title": label,
                    "result_value": key_separator.join(str(part) for part in key),
                    "result_label": label,
                    "result_field": request.query_params.get("_field", ""),
                },
            )
        )

    def persist_csrf(request: Request, response: Response) -> None:
        """Write the CSRF cookie back whenever it does not match the page.

        The gate mints a token when the browser has none, or when the one it
        holds has expired, and puts it on the request scope for the templates.
        Nothing was writing it back, and the cookie is a session cookie -- so
        closing the browser and returning left every form on every page carrying
        a token with no cookie behind it. Each one then failed with "this form is
        missing its security token", and the advice it gives, reload and try
        again, could not help: the reload minted another token and dropped that
        one too. The only way out was to sign out and back in.
        """
        issued = request.scope.get("fastfort_csrf", "")
        if issued and request.cookies.get(auth.csrf.cookie_name) != issued:
            set_csrf_cookie(response, issued, settings, auth.csrf.cookie_name)

    def page(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
        """Render a page, then drop the message cookie.

        Cleared here rather than on read, so a banner appears exactly once: one
        that survives a refresh makes people wonder whether they saved twice.
        """
        response = HTMLResponse(renderer.render(template, **context))
        if context.get("messages"):
            notices.clear(response)
        persist_csrf(request, response)
        return response

    def redirect(to: str, *messages: Message) -> RedirectResponse:
        """A 303 carrying feedback for the page that follows.

        303 rather than 302 so the browser follows with GET and the form body
        cannot be resubmitted by a refresh.
        """
        response = RedirectResponse(to, status_code=303)
        notices.queue(response, *messages)
        return response

    async def verify_csrf(request: Request) -> None:
        form = {name: str(value) for name, value in (await request.form()).items()}
        auth.csrf.verify(
            cookie=request.cookies.get(auth.csrf.cookie_name),
            submitted=auth.csrf.token_from(form, dict(request.headers)),
        )

    # -- assets -------------------------------------------------------------

    def asset(request: Request, text: str, media_type: str) -> Response:
        """A static asset, compressed to whatever the browser reads.

        Brotli first, gzip after it, the bytes themselves for anything that asks
        for neither -- and `Vary`, without which a cache in front of this serves
        one browser's Brotli to another browser that cannot read it.

        Both are produced once per process and kept: these files ship in the
        wheel and cannot change while it is running, so this is not per-request
        work. Compression is off in debug, where the files *are* changing and
        the saving is on a request to localhost.
        """
        body, encoding = compress_asset(
            text,
            request.headers.get("accept-encoding", ""),
            enabled=not settings.debug,
        )
        headers = {
            # Long-lived, because the URL changes with the package version in a
            # release. During development auto-reload matters more than caching.
            "Cache-Control": "no-cache" if settings.debug else "public, max-age=86400",
            "Vary": "Accept-Encoding",
        }
        if encoding:
            headers["Content-Encoding"] = encoding
        return Response(body, media_type=media_type, headers=headers)

    @public.get("/static/fastfort.css", include_in_schema=False)
    async def stylesheet(request: Request) -> Response:
        return asset(request, _bundled_css(debug=settings.debug), "text/css")

    @public.post("/language", name="fastfort:language")
    async def set_language(request: Request) -> Any:
        """Remember a language choice and return to where the request came from.

        Public, because the sign-in page needs a language too. It changes nothing
        but a display preference, so a CSRF token would be ceremony without a
        threat to answer.
        """
        form = await request.form()
        chosen = negotiate_language(
            chosen=str(form.get("language", "")), configured=settings.ui.language
        )
        target = safe_next_url(str(form.get("next", "")), fallback=f"{admin_url}/")

        response = RedirectResponse(target, status_code=303)
        response.set_cookie(
            LANGUAGE_COOKIE,
            chosen,
            max_age=60 * 60 * 24 * 365,
            path=settings.security.cookie_path,
            samesite=settings.security.cookie_samesite,
            secure=settings.security.cookie_secure,
            httponly=False,
        )
        return response

    @public.get("/static/favicon.svg", include_in_schema=False)
    async def favicon(request: Request) -> Response:
        """The default admin icon, for projects that have not supplied one.

        A tab with no icon gets the browser's blank page glyph, which is what
        every other unremarkable tab has -- and an admin is a tab people keep
        open and hunt for. `UISettings.favicon_url` still wins.
        """
        return asset(request, _read_favicon(), "image/svg+xml")

    @public.get("/static/js/{name}", include_in_schema=False)
    async def script(request: Request, name: str) -> Response:
        # An allow-list, not a path join: `name` comes from the URL, and
        # anything that reads a file by a name a request chose is one `..` away
        # from serving the rest of the disk.
        if name not in SCRIPTS:
            raise HTTPException(status_code=404, detail="No such script.")
        return asset(request, _bundled_js(name, debug=settings.debug), "text/javascript")

    # -- uploaded files -------------------------------------------------------

    @router.get("/media/{path:path}", name="fastfort:media")
    async def media_file(path: str) -> Any:
        """A file uploaded through a file or image field.

        On `router`, not `public`: an upload is a record like any other, gated
        the same way as every other view here -- not a static mount, which has
        no notion of that gate at all.

        `path` is a URL a request chose, not a value FastFort generated, so it is
        resolved and checked against `media.root` rather than trusted outright.
        Without that, "../../etc/passwd" would be a perfectly ordinary thing for
        it to contain.
        """
        root = settings.media.root.resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="No such file.")
        return FileResponse(target)

    # -- dashboard ----------------------------------------------------------

    @router.get("/", response_class=HTMLResponse, name="fastfort:dashboard")
    async def dashboard(request: Request) -> HTMLResponse:
        models: list[dict[str, Any]] = []
        signups: Series | None = None

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

            # In the same unit of work as the counts above, so the whole
            # dashboard is one connection rather than two.
            days = settings.admin.dashboard_days
            user_key = ""
            if days and fort.has_user_model:
                user_model = fort.user_config.model
                registered = fort.registry.get(user_model)
                # The user model does not have to have an admin page of its own,
                # so fall back to the key introspection would derive anyway.
                user_key = registered.key if registered is not None else ""
                signups = await build_series(
                    fort.backend.adapter(user_model, uow, key=user_key or None),
                    fort.backend.introspect(
                        user_model, key=user_key or default_model_key(user_model)
                    ),
                    days=days,
                    field=settings.admin.signup_field,
                )

        context = base_context(request, None) | {
            "page_title": "Dashboard",
            "models": models,
            "dialect": fort.backend.dialect,
            "issues": fort.check(),
            "signups": signups if signups is not None and signups.field else None,
            # Only when the model has a page to link to.
            "signups_url": list_url(user_key) if signups is not None and user_key else "",
        }
        return page(request, "dashboard.html", context)

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
        params = _flat_params(request)

        query = ListQuery.from_params(
            params,
            sortable_fields=spec.sortable_fields,
            filterable_fields=model_admin.list_filter or spec.filterable_fields,
            spatial_fields=spec.spatial_fields,
            vector_fields=spec.vector_fields,
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
                page_result = await adapter.list(query)
            except ValidationError:
                # A malformed filter value in the URL should not 500; showing the
                # unfiltered list with the input cleared is the useful recovery.
                query = ListQuery(
                    search=query.search,
                    ordering=query.ordering,
                    page=1,
                    page_size=query.page_size,
                )
                page_result = await adapter.list(query)

            filter_controls = await _filter_controls(
                spec,
                model_admin,
                params,
                adapter,
                settings.admin.autocomplete_limit,
                translator_for(request),
                autocomplete_url=f"{admin_url}/{model_key}/autocomplete",
                target_search_fields={
                    name: _target_search_fields(admin_for, spec.field(name))
                    for name in model_admin.list_filter
                    if spec.get(name) is not None and spec.field(name).is_relation
                },
            )

        columns = tuple(model_admin.columns())
        context_translator = translator_for(request)

        def page_url(number: int) -> str:
            return _with_params(list_url(model_key), params, {"p": str(number)})

        # A live update asks for the results only. Both paths render the same
        # fragment from the same context, so they cannot drift apart.
        partial = request.headers.get("x-fastfort-partial") == "results"
        model_title = model_admin.title
        model_singular = model_admin.singular

        context = base_context(request, model_key) | {
            "page_title": model_title,
            "breadcrumbs": ({"label": model_title, "url": None},),
            # Resolved here rather than translated in the template: the name may
            # come from a per-language mapping the admin declared, in which case
            # there is nothing for the catalogue to look up.
            "model_title": model_title,
            "model_singular": model_singular,
            "admin": model_admin,
            "page": page_result,
            "query": query,
            "list_url": list_url(model_key),
            "add_url": f"{admin_url}/{model_key}/add",
            "columns": _column_headers(
                spec, columns, query, params, list_url(model_key), context_translator, model_admin
            ),
            "rows": _rows(
                model_admin, spec, page_result.items, columns, f"{admin_url}/{model_key}"
            ),
            "filters": filter_controls,
            # Named in the delete confirmation, so it says what else goes.
            "cascades": _cascades(model_admin),
            "active_filters": _active_filters(
                filter_controls, query, params, list_url(model_key), context_translator
            ),
            "actions": model_admin.action_specs(),
            "action_url": f"{admin_url}/{model_key}/action",
            # The export keeps the current search, filters and ordering, so the
            # file is the table on screen rather than the whole model.
            "exports": (
                [
                    {
                        "label": fmt.label,
                        "url": _with_params(
                            f"{admin_url}/{model_key}/export", params, {"format": name, "p": None}
                        ),
                    }
                    for name, fmt in EXPORT_FORMATS.items()
                ]
                if model_admin.exportable
                else []
            ),
            # No query string on it, unlike the export: an import is not a view
            # of anything, so carrying the list's filters onto it would suggest
            # the upload is somehow scoped by them.
            "import_url": (f"{admin_url}/{model_key}/import" if model_admin.importable else None),
            "ordering_param": ",".join(sort.as_token() for sort in query.ordering),
            "page_url": page_url,
            "page_links": _page_links(page_result, page_url),
            "page_sizes": [
                {
                    "size": size,
                    # Changing the size resets to page one: page 7 of 20-row
                    # pages is not page 7 of 100-row pages.
                    "url": _with_params(list_url(model_key), params, {"ps": str(size), "p": None}),
                    "current": size == query.page_size,
                }
                for size in _page_size_choices(settings.admin, query.page_size)
            ],
        }
        return page(request, "model/_results.html" if partial else "model/list.html", context)

    # -- export -------------------------------------------------------------

    @router.get("/{model_key}/export", name="fastfort:export")
    async def export_view(request: Request, model_key: str) -> Any:
        """The current view as a file.

        The whole result set, not the page on screen: someone exporting a
        filtered list wants the filter's answer, and paging through it by hand
        to reassemble the file is the thing the button exists to avoid. The same
        `ListQuery` the list view builds is reused so the file cannot disagree
        with the table it came from.

        Capped by `AdminSettings.export_limit`, because an export runs with no
        pagination and a mis-clicked export of ten million rows is a request that
        never finishes.
        """
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)
        spec = model_admin.spec

        chosen = request.query_params.get("format", "csv")
        if chosen not in EXPORT_FORMATS or not model_admin.exportable:
            raise HTTPException(status_code=404, detail="No such export format.")
        export_format = EXPORT_FORMATS[chosen]

        params = _flat_params(request)
        query = ListQuery.from_params(
            params,
            sortable_fields=spec.sortable_fields,
            filterable_fields=model_admin.list_filter or spec.filterable_fields,
            spatial_fields=spec.spatial_fields,
            vector_fields=spec.vector_fields,
            searchable=bool(model_admin.searchable()),
            default_ordering=model_admin.default_ordering(),
            page_size=settings.admin.export_chunk_size,
            max_page_size=settings.admin.export_chunk_size,
        )

        columns = tuple(model_admin.export_columns())
        translate = translator_for(request)
        headers = [
            translate(model_admin.field_label(name, _column_label(spec, name))) for name in columns
        ]

        rows = await _export_rows(
            fort, entry, model_admin, query, columns, settings.admin.export_limit
        )
        writer = {"csv": stream_csv, "json": stream_json}.get(chosen, stream_xlsx)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d")
        filename = f"{model_key}-{stamp}.{export_format.extension}"

        return StreamingResponse(
            writer(headers, rows),
            media_type=export_format.media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # -- import ---------------------------------------------------------------

    def _require_importable(model_key: str) -> ModelAdmin:
        model_admin = _require_admin(admin_for, model_key)
        if not model_admin.importable:
            # 404 rather than 403, for the same reason a missing row is: a model
            # that does not accept uploads should not confirm that it exists
            # here any more than it would anywhere else.
            raise HTTPException(status_code=404, detail="This model does not accept imports.")
        return model_admin

    def import_context(
        request: Request, model_key: str, model_admin: ModelAdmin, translate: Translator
    ) -> dict[str, Any]:
        return base_context(request, model_key) | {
            "page_title": translate("Import {name}", name=model_admin.title.lower()),
            "model_title": model_admin.title,
            "heading": translate("Import {name}", name=model_admin.title.lower()),
            "breadcrumbs": (
                {"label": model_admin.title, "url": list_url(model_key)},
                {"label": translate("Import"), "url": None},
            ),
            "list_url": list_url(model_key),
            "action_url": f"{admin_url}/{model_key}/import",
            "template_url": f"{admin_url}/{model_key}/import/template",
            "formats": list(EXPORT_FORMATS.values()),
            "columns": _import_columns(model_admin),
            "plan": None,
            "error": None,
            "error_tone": "danger",
            "filename": "",
            "checked_only": False,
        }

    @router.get("/{model_key}/import/template", name="fastfort:import-template")
    async def import_template(request: Request, model_key: str) -> Any:
        """A file with the right columns, and a row saying what each one wants.

        The hint row is the point. A duration, a range and a point have no
        spelling anybody guesses right the first time, and being told in the file
        beats being told by an error report afterwards. Every hint cell fails to
        parse, so a template uploaded unchanged reports itself as one bad row
        rather than importing a row of instructions.
        """
        model_admin = _require_importable(model_key)
        chosen = request.query_params.get("format", "csv")
        if chosen not in EXPORT_FORMATS:
            raise HTTPException(status_code=404, detail="No such template format.")
        template = EXPORT_FORMATS[chosen]

        translate = translator_for(request)
        headers, rows = template_rows(model_admin.spec, model_admin, translate)
        writer = {"csv": stream_csv, "json": stream_json}.get(chosen, stream_xlsx)
        filename = f"{model_key}-template.{template.extension}"

        return StreamingResponse(
            writer(headers, rows),
            media_type=template.media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @router.get("/{model_key}/import", response_class=HTMLResponse, name="fastfort:import")
    async def import_form(request: Request, model_key: str) -> Any:
        model_admin = _require_importable(model_key)
        return page(
            request,
            "model/import.html",
            import_context(request, model_key, model_admin, translator_for(request)),
        )

    @router.post("/{model_key}/import", name="fastfort:import-submit")
    async def import_submit(request: Request, model_key: str) -> Any:
        """Check a whole file, then write all of it or none of it.

        All-or-nothing on purpose. A half-applied spreadsheet is worse than a
        rejected one: the rejection is a list of line numbers somebody can act
        on, and the half-application is a data set nobody can tell apart from
        the one they meant to upload. One unit of work covers every row, so a
        failure on line nine hundred takes the first eight hundred and
        ninety-nine back out with it.
        """
        model_admin = _require_importable(model_key)
        entry = fort.registry.entry_for_key(model_key)
        translate = translator_for(request)
        submitted = await _form_data(request, settings.media)

        upload = submitted.get("file")
        context = import_context(request, model_key, model_admin, translate)

        if not isinstance(upload, UploadedFile) or not upload.chosen:
            context["error"] = translate("Choose a file to import.")
            return page(request, "model/import.html", context)

        try:
            kind = sniff_format(upload.filename, upload.content)
            headers, rows = read_table(upload.content, kind)
            plan = build_plan(headers, rows, model_admin.spec, model_admin)
        except ImportFileError as exc:
            context |= {"error": str(exc), "error_tone": exc.tone}
            return page(request, "model/import.html", context)

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            await _resolve_relations(
                adapter,
                model_admin,
                plan,
                lambda field: _target_search_fields(admin_for, field),
            )

            checking = str(submitted.get("check", "")) in ("1", "true", "on")
            written = 0
            if not plan.errors and not checking:
                written = await _apply_import(adapter, model_admin, plan)

            if plan.errors or checking:
                # Rolled back explicitly rather than left to the context
                # manager. On the checking path there is nothing to keep; on the
                # error path there may be several hundred rows that were written
                # before the failure, and taking them back out is the
                # all-or-nothing this feature promises. Everything the page
                # renders below was read before this point.
                await uow.rollback()
                context |= {
                    "plan": plan,
                    "filename": upload.filename,
                    "checked_only": checking and not plan.errors,
                }
                return page(request, "model/import.html", context)

        return redirect(
            list_url(model_key),
            Message(
                MessageLevel.SUCCESS,
                translate(
                    "Imported {count} rows from {name}.", count=written, name=upload.filename
                ),
            ),
        )

    # -- autocomplete -------------------------------------------------------

    @router.get("/{model_key}/autocomplete", name="fastfort:autocomplete")
    async def autocomplete(request: Request, model_key: str) -> Any:
        """Options for one relation field, searched by term.

        This is what makes a foreign key onto a large table usable. The
        alternative -- rendering every row as an `<option>` -- is several
        megabytes of HTML on a table of fifty thousand customers, which is the
        point at which a stock admin stops being usable at all.

        Behind the same gate as every other view: the labels it returns are rows
        of the target table, so an open endpoint here would leak the contents of
        a model to anyone who knew the URL.
        """
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        field_name = request.query_params.get("field", "")
        field_spec = model_admin.spec.get(field_name)
        if field_spec is None or not field_spec.is_relation:
            raise HTTPException(status_code=404, detail="No such relation field.")

        term = request.query_params.get("q", "")[:100]
        limit = settings.admin.autocomplete_limit

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            # One past the limit, so the answer can say whether it is complete
            # rather than silently cutting the list off at a round number.
            found = await adapter.related_choices(
                field_name,
                term,
                limit=limit + 1,
                search_fields=_target_search_fields(admin_for, field_spec),
            )

        return {
            "results": [
                {"value": choice.value, "label": choice.label or str(choice.value)}
                for choice in found[:limit]
            ],
            "more": len(found) > limit,
        }

    # -- create, change, delete ---------------------------------------------

    def object_url(model_key: str, key: tuple[Any, ...]) -> str:
        joined = key_separator.join(str(part) for part in key)
        return f"{admin_url}/{model_key}/{quote(joined, safe='')}/"

    def parse_key(spec: ModelSpec, raw: str) -> tuple[Any, ...]:
        parts = raw.split(key_separator) if len(spec.primary_key) > 1 else [raw]
        if len(parts) != len(spec.primary_key):
            raise HTTPException(status_code=404, detail="Malformed object key.")
        return tuple(parts)

    def form_relations(model_admin: ModelAdmin) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Relations a form has to have loaded before it can render.

        Reading `product.category` on a lazily loaded attribute raises
        MissingGreenlet under asyncio, so the eager loads cannot be left to the
        admin's `select_related` -- a project that did not declare it would get a
        500 on the edit page rather than a slow one.
        """
        to_one = tuple(
            field.name
            for field in model_admin.spec
            if field.is_relation and not field.type.is_multi_valued
        )
        to_many = tuple(
            field.name for field in model_admin.spec if field.type is FieldType.MANY_TO_MANY
        )
        return (
            tuple(dict.fromkeys((*model_admin.select_related, *to_one))),
            tuple(dict.fromkeys((*model_admin.prefetch_related, *to_many))),
        )

    async def relation_choices(
        adapter: Any, model_admin: ModelAdmin
    ) -> tuple[dict[str, tuple[Choice, ...]], frozenset[str]]:
        """Options for every relation field on the form, and which ones overflowed.

        Bounded by `autocomplete_limit`: a dropdown over a million rows is not a
        usable control, and building it would stall the page. One row past the
        limit is fetched deliberately -- it is the difference between "this is
        the whole list" and "there is more", and the second case is what turns
        the control into a searching one instead of a truncated one.
        """
        options: dict[str, tuple[Choice, ...]] = {}
        remote: set[str] = set()
        limit = settings.admin.autocomplete_limit

        for field_spec in model_admin.spec:
            if not field_spec.is_relation or field_spec.type is FieldType.REVERSE_FK:
                continue
            found = await adapter.related_choices(field_spec.name, "", limit=limit + 1)
            if len(found) > limit:
                remote.add(field_spec.name)
                continue
            options[field_spec.name] = tuple(
                Choice(value=choice.value, label=choice.label or str(choice.value))
                for choice in found
            )
        return options, frozenset(remote)

    def form_context(
        request: Request,
        model_key: str,
        model_admin: ModelAdmin,
        form: Form,
        *,
        instance: Any = None,
        label: str = "",
        pk: tuple[Any, ...] | None = None,
    ) -> dict[str, Any]:
        # A popup is the same form without the shell: it is opened from a field
        # on another form, so a sidebar full of other models is noise, and there
        # is nowhere for it to navigate to.
        popup = request.query_params.get(POPUP_PARAM) == "1"
        # The form has to post back to a URL that still says it is a popup.
        # Without this the save looked like any other, redirected to the change
        # page, and the window sat there open showing "… was created" while the
        # form that opened it waited for a value that never arrived.
        popup_query = ""
        if popup:
            popup_query = "?" + urlencode(
                {POPUP_PARAM: "1", "_field": request.query_params.get("_field", "")}
            )
        editing = instance is not None
        # Read off the instance only when the caller did not already have it. A
        # rollback expires every attribute in the session, so a view rendering
        # this page on an error path passes the key it read while the row was
        # still live -- reading it here instead is a synchronous refresh against
        # an async session, which crashes rather than returning a stale value.
        if pk is None:
            key = model_admin.spec.primary_key
            pk = tuple(getattr(instance, name) for name in key) if editing else ()

        # Built through the translator with a placeholder, not with an f-string.
        # An f-string produces a sentence the catalogue can never match, which is
        # how "Save changes" stayed English on an otherwise translated page while
        # the catalogue sat there carrying a translation for it.
        translate = translator_for(request)
        model_title = model_admin.title
        model_singular = model_admin.singular
        singular = model_singular.lower()
        heading = label if editing else translate("Add {name}", name=singular)

        return base_context(request, model_key) | {
            "page_title": heading,
            "model_title": model_title,
            "model_singular": model_singular,
            "breadcrumbs": (
                {"label": model_title, "url": list_url(model_key)},
                {"label": label if editing else "Add", "url": None},
            ),
            "admin": model_admin,
            "form": form,
            "heading": heading,
            "subheading": model_admin.spec.key if editing else None,
            "list_url": list_url(model_key),
            "action_url": (
                f"{object_url(model_key, pk)}{popup_query}"
                if editing
                else f"{admin_url}/{model_key}/add{popup_query}"
            ),
            "autocomplete_url": f"{admin_url}/{model_key}/autocomplete",
            "media_url": f"{admin_url}/media",
            # Where each relation points, so the control can offer the same
            # add/change/view buttons Django puts beside a foreign key. Absent
            # for a target with no admin of its own, which is normal: a model can
            # be pointed at without having a page.
            "relation_links": _relation_links(admin_for, model_admin, admin_url),
            "popup": popup,
            "cascades": _cascades(model_admin),
            "delete_url": f"{object_url(model_key, pk)}delete" if editing else None,
            "object_label": label if editing else "",
            "submit_label": (
                translate("Save changes") if editing else translate("Create {name}", name=singular)
            ),
            "version_token": None,
            "input_types": INPUT_TYPES,
            "extra_scripts": _extra_scripts(form),
        }

    @router.get("/{model_key}/add", response_class=HTMLResponse, name="fastfort:add")
    async def add_form(request: Request, model_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            choices, remote = await relation_choices(adapter, model_admin)
            form = Form(
                model_admin.spec,
                model_admin,
                relation_choices=choices,
                remote_relations=remote,
                media=settings.media,
            )
        return page(request, "model/form.html", form_context(request, model_key, model_admin, form))

    @router.post("/{model_key}/add", name="fastfort:add-submit")
    async def add_submit(request: Request, model_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)
        submitted = await _form_data(request, settings.media)

        try:
            await verify_csrf(request)
        except SecurityError as exc:
            return redirect(f"{admin_url}/{model_key}/add", notices.danger(exc.message))

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            choices, remote = await relation_choices(adapter, model_admin)
            form = Form(
                model_admin.spec,
                model_admin,
                relation_choices=choices,
                remote_relations=remote,
                auth_settings=settings.auth,
                media=settings.media,
            )
            cleaned = form.bind(submitted)

            if not form.is_valid:
                await uow.rollback()
                return page(
                    request,
                    "model/form.html",
                    form_context(request, model_key, model_admin, form),
                )

            try:
                created = await adapter.create(cleaned)
                label = adapter.label_for(created)
                key = adapter.primary_key_of(created)
                # Explicit rather than left to the context manager: a constraint
                # the database checks at commit has to be catchable here, or it
                # surfaces as a 500 on a save that looked like any other.
                await uow.commit()
            except (ValidationError, AdapterError) as exc:
                await uow.rollback()
                form.non_field_errors.append(exc.message)
                return page(
                    request,
                    "model/form.html",
                    form_context(request, model_key, model_admin, form),
                )

            # After the row is durable, not merely flushed: a file staged during
            # `bind()` must not be left on disk by a transaction that then rolls
            # back, which is a stored path pointing at nothing.
            form.commit_files()

        if request.query_params.get(POPUP_PARAM) == "1":
            return _popup_result(request, renderer, base_context, model_key, key, label)

        translate = translator_for(request)
        return redirect(
            object_url(model_key, key),
            notices.success(
                translate(
                    "{model} “{name}” was created.",
                    model=translate(model_admin.singular),
                    name=label,
                )
            ),
        )

    @router.get("/{model_key}/{object_key}/", response_class=HTMLResponse, name="fastfort:change")
    async def change_form(request: Request, model_key: str, object_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        eager, prefetch = form_relations(model_admin)
        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(
                entry.model,
                uow,
                key=model_key,
                select_related=eager,
                prefetch_related=prefetch,
            )
            instance = await _require_object(adapter, parse_key(model_admin.spec, object_key))
            choices, remote = await relation_choices(adapter, model_admin)
            form = Form(
                model_admin.spec,
                model_admin,
                instance=instance,
                relation_choices=choices,
                remote_relations=remote,
                auth_settings=settings.auth,
                media=settings.media,
            )
            label = adapter.label_for(instance)

        return page(
            request,
            "model/form.html",
            form_context(request, model_key, model_admin, form, instance=instance, label=label),
        )

    @router.post("/{model_key}/{object_key}/", name="fastfort:change-submit")
    async def change_submit(request: Request, model_key: str, object_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)
        submitted = await _form_data(request, settings.media)

        try:
            await verify_csrf(request)
        except SecurityError as exc:
            return redirect(f"{admin_url}/{model_key}/", notices.danger(exc.message))

        eager, prefetch = form_relations(model_admin)
        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(
                entry.model,
                uow,
                key=model_key,
                select_related=eager,
                prefetch_related=prefetch,
            )
            instance = await _require_object(adapter, parse_key(model_admin.spec, object_key))
            choices, remote = await relation_choices(adapter, model_admin)
            form = Form(
                model_admin.spec,
                model_admin,
                instance=instance,
                relation_choices=choices,
                remote_relations=remote,
                auth_settings=settings.auth,
                media=settings.media,
            )
            cleaned = form.bind(submitted)
            # Read while the row is live. A rollback expires every attribute on
            # every object in the session, `instance` included, and reading one
            # afterwards is a synchronous refresh against an async session
            # outside the greenlet bridge that makes those work -- a crash on
            # every edit that failed, which is the path that most needs to work.
            label = adapter.label_for(instance)
            key = adapter.primary_key_of(instance)

            def invalid() -> HTMLResponse:
                return page(
                    request,
                    "model/form.html",
                    form_context(
                        request,
                        model_key,
                        model_admin,
                        form,
                        instance=instance,
                        label=label,
                        pk=key,
                    ),
                )

            if not form.is_valid:
                response = invalid()
                await uow.rollback()
                return response

            try:
                await adapter.update(instance, cleaned)
                label = adapter.label_for(instance)
                key = adapter.primary_key_of(instance)
                # Explicit rather than left to the context manager: a constraint
                # the database checks at commit has to be catchable here, or it
                # surfaces as a 500 on a save that looked like any other. It
                # rolls back before it raises, which is why nothing below reads
                # the instance again.
                await uow.commit()
            except (ValidationError, AdapterError) as exc:
                form.non_field_errors.append(exc.message)
                response = invalid()
                await uow.rollback()
                return response

            # After the row is durable, not merely flushed: a file staged during
            # `bind()` must not be left on disk by a transaction that then rolls
            # back, which is a stored path pointing at nothing.
            form.commit_files()

        if request.query_params.get(POPUP_PARAM) == "1":
            return _popup_result(request, renderer, base_context, model_key, key, label)

        translate = translator_for(request)
        return redirect(
            object_url(model_key, key),
            notices.success(
                translate(
                    "{model} “{name}” was saved.",
                    model=translate(model_admin.singular),
                    name=label,
                )
            ),
        )

    @router.get(
        "/{model_key}/{object_key}/delete", response_class=HTMLResponse, name="fastfort:delete"
    )
    async def delete_confirm(request: Request, model_key: str, object_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            instance = await _require_object(adapter, parse_key(model_admin.spec, object_key))
            label = adapter.label_for(instance)
            key = adapter.primary_key_of(instance)
            # What else this takes with it, counted before anything is written.
            # "This cannot be undone" is not a confirmation on its own; the
            # question people have is what happens to the rows underneath.
            plan = await adapter.deletion_plan([instance])

        delete_translate = translator_for(request)
        delete_model_title = model_admin.title
        delete_model_singular = model_admin.singular

        return page(
            request,
            "model/delete.html",
            base_context(request, model_key)
            | {
                "page_title": delete_translate("Delete {name}", name=label),
                "model_title": delete_model_title,
                "model_singular": delete_model_singular,
                "breadcrumbs": (
                    {"label": delete_model_title, "url": list_url(model_key)},
                    {"label": label, "url": object_url(model_key, key)},
                    {"label": "Delete", "url": None},
                ),
                "admin": model_admin,
                "label": label,
                "cascades": _cascades(model_admin),
                "related": _related_groups(fort, plan, admin_url),
                "blocked": plan.blocked,
                "action_url": f"{object_url(model_key, key)}delete",
                "cancel_url": object_url(model_key, key),
            },
        )

    @router.post("/{model_key}/{object_key}/delete", name="fastfort:delete-submit")
    async def delete_submit(request: Request, model_key: str, object_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        try:
            await verify_csrf(request)
        except SecurityError as exc:
            return redirect(list_url(model_key), notices.danger(exc.message))

        translate = translator_for(request)

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            instance = await _require_object(adapter, parse_key(model_admin.spec, object_key))
            # Read before any write. A rollback expires every attribute on every
            # object in the session, and reading one afterwards is a synchronous
            # refresh against an async session -- a crash on the error path,
            # which is the path that most needs to work.
            label = adapter.label_for(instance)
            back = object_url(model_key, adapter.primary_key_of(instance))

            # Checked again here, not only on the confirmation page: the page may
            # have been open for an hour, and the row that protects this one may
            # have arrived since. Without it the delete fails inside the flush and
            # the person gets a constraint violation instead of a sentence.
            plan = await adapter.deletion_plan([instance])
            if plan.blocked:
                return redirect(back, notices.danger(_protected_message(fort, plan, translate)))

            try:
                await adapter.delete(instance)
                # Explicit, inside the guard: a constraint the database only
                # checks at commit would otherwise be raised on the way out of
                # this block, past every handler that could explain it.
                await uow.commit()
            except AdapterError as exc:
                await uow.rollback()
                return redirect(back, notices.danger(f"Could not delete this row: {exc.message}"))

        return redirect(
            list_url(model_key),
            notices.success(
                translate(
                    "{model} “{name}” was deleted.",
                    model=translate(model_admin.singular),
                    name=label,
                )
            ),
        )

    # -- bulk actions -------------------------------------------------------

    @router.post("/{model_key}/action", name="fastfort:action")
    async def run_action(request: Request, model_key: str) -> Any:
        """Run one bulk action over the selected rows.

        Without this, clearing out fifty rows is fifty confirmations. Rows are
        named by primary key in the body rather than by a filter in the URL: an
        action that re-ran the current query server-side would act on whatever
        matched at that moment, which is not necessarily what was on screen when
        the boxes were ticked.
        """
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)
        target = list_url(model_key)

        try:
            await verify_csrf(request)
        except SecurityError as exc:
            return redirect(target, notices.danger(exc.message))

        submitted = await request.form()
        name = str(submitted.get("action", ""))
        keys = [str(value) for value in submitted.getlist("keys")]

        if not keys:
            return redirect(target, notices.info("Nothing was selected."))

        offered = {spec.name for spec in model_admin.action_specs()}
        if name not in offered:
            return redirect(target, notices.danger("That action is not available here."))

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            objects = []
            for raw in keys:
                found = await adapter.get(parse_key(model_admin.spec, raw))
                if found is not None:
                    objects.append(found)

            if not objects:
                return redirect(target, notices.info("Those rows no longer exist."))

            try:
                if name == "delete":
                    # The same check the single-row delete makes. A bulk delete
                    # is where it matters most: half the rows going and the rest
                    # failing on a foreign key is the worst possible outcome, and
                    # it is what happens without this.
                    plan = await adapter.deletion_plan(objects)
                    if plan.blocked:
                        return redirect(
                            target,
                            notices.danger(_protected_message(fort, plan, translator_for(request))),
                        )
                    for obj in objects:
                        await adapter.delete(obj)
                    message = notices.success(
                        f"{len(objects)} {model_admin.title.lower()} were deleted."
                        if len(objects) != 1
                        else f"One {model_admin.singular.lower()} was deleted."
                    )
                else:
                    handler = model_admin.action_handler(name)
                    if handler is None:
                        return redirect(
                            target, notices.danger("That action is not available here.")
                        )
                    outcome = await handler(adapter, tuple(objects))
                    message = notices.success(str(outcome) if outcome else "Done.")
                # Inside the guard, so a constraint the database defers to commit
                # is reported as a failed action rather than raised on the way
                # out of this block, where nothing is left to explain it. An
                # action either applies to every selected row or to none.
                await uow.commit()
            except (AdapterError, ValidationError) as exc:
                await uow.rollback()
                return redirect(target, notices.danger(f"The action failed: {exc.message}"))

        return redirect(target, message)

    public.include_router(build_auth_router(fort, auth, renderer))
    public.include_router(router)
    return public


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


#: HTML input types, keyed by widget name. Kept beside the widget map rather than
#: in the template, so adding a widget touches one file.
#:
#: Only widgets the generic `<input>` branch in `_widgets.html` actually
#: renders belong here -- "tags", "keyvalue", "range" and "geometry" draw their
#: own markup and never reach this lookup. A name this dict does not carry
#: still renders: `generic_control` reads it with `.get(field.widget, "text")`,
#: which is the fix for the crash this dict used to cause (see the widget's own
#: comment in `widgets.py`) -- so a project's `register_widget()` name degrades
#: to a plain text box instead of a 500 until it ships a template partial.
INPUT_TYPES = {
    "text": "text",
    "number": "number",
    "decimal": "number",
    "money": "number",
    "date": "date",
    "datetime": "datetime-local",
    "time": "time",
    "duration": "text",
    "email": "email",
    "url": "url",
    "password": "password",
    "inet": "text",
    "mac": "text",
    "bits": "text",
}


def _require_admin(admin_for: Any, model_key: str) -> ModelAdmin:
    """Resolve a model key, or 404. A stale bookmark is not a server error."""
    try:
        return admin_for(model_key)  # type: ignore[no-any-return]
    except RegistrationError as exc:
        raise HTTPException(
            status_code=404, detail=f"No admin is registered for {model_key!r}."
        ) from exc


async def _require_object(adapter: Any, key: tuple[Any, ...]) -> Any:
    """Load a row, or 404.

    Deliberately 404 and not 403 when a row exists but is filtered out of the
    caller's queryset: confirming existence to someone who may not see it is an
    information leak.
    """
    try:
        instance = await adapter.get(key)
    except (ValidationError, ObjectNotFound) as exc:
        raise HTTPException(status_code=404, detail="No such object.") from exc
    if instance is None:
        raise HTTPException(status_code=404, detail="No such object.")
    return instance


async def _materialize(value: str | UploadFile, media: MediaSettings) -> str | UploadedFile:
    """A submitted value, read into memory once.

    A file input's value is an `UploadFile` -- a handle onto the request body,
    not the bytes themselves -- so it has to be read here, inside the one
    `async def` on this path. `Form.bind` is synchronous and knows nothing
    about `multipart/form-data`; by the time a value reaches it, "this field is
    a file" is `UploadedFile`, not a fact about the transport.

    Read one byte past `upload_limit` rather than the whole thing: enough to
    tell `Form.bind` the upload is over the limit without first having to buffer
    all of one that is -- a multi-gigabyte file must not sit fully in memory
    only to be rejected afterwards.
    """
    if isinstance(value, UploadFile):
        content = await value.read(media.upload_limit + 1)
        await value.close()
        return UploadedFile(filename=value.filename or "", content=content)
    return str(value)


async def _form_data(request: Request, media: MediaSettings) -> dict[str, Any]:
    """Read a submitted form, keeping repeated keys as lists.

    A multi-select posts the same name several times, and a plain dict would keep
    only the last value.
    """
    raw = await request.form()
    data: dict[str, Any] = {}
    for name in raw:
        values = [await _materialize(value, media) for value in raw.getlist(name)]
        data[name] = values if len(values) > 1 else values[0]
    return data


def _cascades(admin: ModelAdmin) -> tuple[str, ...]:
    """Relations whose rows go with this one, as the *spec* declares them.

    A hint, printed on the buttons that delete without leaving the page. It costs
    no query, which is why it is what the list and the form can afford; the
    counted, per-row answer is on the confirmation page, from `deletion_plan`.
    """
    return tuple(
        field.label
        for field in admin.spec
        if field.relation is not None and field.relation.cascade_delete
    )


def _related_groups(fort: FastFort, plan: DeletionPlan, admin_url: str) -> list[dict[str, Any]]:
    """A deletion plan as the confirmation page renders it.

    A related model's own admin has the better name for it -- read off the
    registered class rather than by instantiating one, which would cost an
    introspection per group. A model with no admin page keeps the name the
    adapter derived and gets no link, which is normal: plenty of tables are
    pointed at without having a page.
    """
    groups: list[dict[str, Any]] = []

    for row in plan.related:
        entry = fort.registry.get_by_key(row.model_key) if row.model_key else None
        groups.append(
            {
                "effect": row.effect.value,
                "title": _title_of(entry.admin, entry.key) if entry else row.label,
                "field": row.field,
                "count": row.count,
                # A count that stopped at the cap is shown as "1000+", never as a
                # total: a warning that understates what it is warning about is
                # worse than no number at all.
                "truncated": row.truncated,
                "samples": list(row.samples),
                "more": row.more,
                "url": f"{admin_url}/{row.model_key}/" if entry else "",
            }
        )

    return groups


def _protected_message(fort: FastFort, plan: DeletionPlan, translate: Translator) -> str:
    """Why a delete was refused, naming what is holding it.

    "Integrity error" tells nobody which row to go and fix. This names the model
    and how many of its rows require this one, which is the whole of what has to
    happen before the delete can go through.
    """
    named: list[str] = []
    for row in plan.protected:
        entry = fort.registry.get_by_key(row.model_key) if row.model_key else None
        # "1 products" is what naming the model only in the plural reads as, and
        # a count of one is the commonest case there is.
        if row.count == 1 and not row.truncated:
            name = _singular_of(entry.admin, entry.key) if entry else row.singular
            named.append(f"1 {name.lower()}")
            continue
        plural = _title_of(entry.admin, entry.key) if entry else row.label
        named.append(f"{row.count}{'+' if row.truncated else ''} {plural.lower()}")

    return translate(
        "This cannot be deleted while other records require it: {records}.",
        records=", ".join(named),
    )


def _remember(guard: Any, auth: AdminAuth) -> Any:
    """Wrap the gate so the signed-in user and a CSRF token reach the templates.

    Stored on the request scope rather than threaded through every view signature:
    the shell needs them on every page, and a view that does not care should not
    have to mention them.
    """

    async def dependency(request: Request) -> Any:
        user = await guard(request)
        request.scope["fastfort_user"] = user
        request.scope["fastfort_csrf"] = auth.csrf.ensure(
            request.cookies.get(auth.csrf.cookie_name)
        )
        return user

    return dependency


def _user_label(fort: FastFort, user: Any) -> str:
    """How to name the signed-in person in the corner of the page."""
    if user is None:
        return ""
    for attribute in ("full_name", "name", "display_name"):
        value = getattr(user, attribute, None)
        if isinstance(value, str) and value.strip():
            return value
    return _user_identity(fort, user) or "Account"


def _user_identity(fort: FastFort, user: Any) -> str:
    """What they signed in with, shown under their name in the account card.

    Empty when it is unavailable, so the card renders one line rather than a line
    and a gap.
    """
    if user is None:
        return ""
    try:
        return fort.user_config.identity_of(user)
    except Exception:
        return ""


def _flat_params(request: Request) -> dict[str, str]:
    """The query string as a plain mapping, joining repeated keys with commas.

    A checkbox group submits `status__in=new&status__in=paid`, and `dict()` over
    a multidict keeps only the last one -- so ticking three boxes would filter by
    one. Joining matches the form the query layer already parses for `__in`.
    """
    merged: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        merged.setdefault(key, []).append(value)
    return {key: ",".join(values) for key, values in merged.items()}


def _current_path(request: Request) -> str:
    """This request's path with its query string, for a round trip back to it."""
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


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
            "label": _group_of(entries[0].admin if entries else None, name),
            # Deliberately not "items": Jinja would resolve `group.items` to
            # dict.items and iterate a bound method.
            "entries": [
                {
                    "key": entry.key,
                    "title": _title_of(entry.admin, entry.key),
                    "url": list_url(entry.key),
                    # Read off the class rather than an instance: the sidebar is
                    # built on every page and instantiating a ModelAdmin needs the
                    # spec, which needs a round trip to the database.
                    "icon": getattr(entry.admin, "icon", None),
                }
                for entry in entries
            ],
        }
        for name, entries in fort.registry.grouped().items()
    ]


def _palette_entries(fort: FastFort, list_url: Any, translate: Translator) -> list[dict[str, Any]]:
    """Everything Ctrl+K can jump to."""
    return [
        {
            "label": _title_of(entry.admin, entry.key),
            "group": _group_of(entry.admin, name),
            "url": list_url(entry.key),
            "icon": getattr(entry.admin, "icon", None) or "list",
            "hint": entry.key,
        }
        for name, entries in fort.registry.grouped().items()
        for entry in entries
    ]


def _ui_text(translate: Translator) -> dict[str, str]:
    """Strings the browser-side widgets produce on their own.

    Handed over as data attributes rather than compiled into the script, because
    the script is one cached file for every language and the page is not.
    """
    return {
        "search": translate("Search…"),
        "no-results": translate("No results"),
        "selected": translate("selected"),
        "clear": translate("Clear"),
        "cancel": translate("Cancel"),
        "delete": translate("Delete"),
        "loading": translate("Loading…"),
        "choose": translate("Choose…"),
        "showing": translate("Showing the first {n}. Keep typing to narrow it down."),
        # The calendar draws its own month and weekday names from `Intl`, but
        # these are ours.
        "today": translate("Today"),
        # A datetime picker sets the clock too, and "Today" is the wrong word
        # for a button that does that.
        "now": translate("Now"),
        "done": translate("Done"),
        "previous": translate("Previous"),
        "next": translate("Next"),
        "days": translate("Days"),
        "hours": translate("Hours"),
        "minutes": translate("Minutes"),
        "seconds": translate("Seconds"),
        "zoom-in": translate("Zoom in"),
        "zoom-out": translate("Zoom out"),
        "my-location": translate("My location"),
        # The upload card. A file's size is rendered as a number and a unit the
        # script formats itself, because "MB" is one of the few strings that is
        # the same in every language the admin speaks.
        "choose-file": translate("Choose a file"),
        "or-drop-it": translate("or drop it here"),
        "replace": translate("Click to replace"),
        "remove": translate("Remove"),
        "undo": translate("Undo"),
        "too-large": translate("Too large"),
        "wrong-type": translate("Not an image"),
        # The key/value (HSTORE), tag (ARRAY) and JSON editors, which live in
        # `fastfort-data.js` rather than the everyday bundle. They read these
        # off `<html>` exactly like every widget above, because `t()` reaches
        # them through the kit the main script publishes -- one table of
        # strings for every bundle, not one per file.
        "add": translate("Add"),
        "key": translate("Key"),
        "value": translate("Value"),
        "format": translate("Format"),
        "from": translate("From"),
        "to": translate("To"),
        "bounds": translate("Bounds"),
        "invalid-address": translate("Invalid address"),
    }


def _extra_scripts(form: Form) -> tuple[str, ...]:
    """The bundles this form's controls need, beyond the everyday one.

    Read off the widgets actually rendered rather than off the model's fields:
    a geometry column a `ModelAdmin` marked read-only draws no map, and asking
    the browser for the map editor anyway is exactly the download this split
    exists to avoid.

    Ordered rather than a set, so the same page produces the same tags in the
    same order on every request -- a `<script>` list that reshuffles between
    responses defeats the browser cache it is trying to use.
    """
    wanted = {_WIDGET_SCRIPTS[f.widget] for f in form.fields if f.widget in _WIDGET_SCRIPTS}
    return tuple(name for name in ("fastfort-data.js", "fastfort-geo.js") if name in wanted)


def _title_of(admin: Any, key: str) -> str:
    """A sidebar label without needing the spec, which needs a database round trip."""
    declared = getattr(admin, "verbose_name_plural", None)
    if isinstance(declared, str) and declared:
        return declared
    return key.split(".", 1)[-1].replace("_", " ").title() + "s"


def _singular_of(admin: Any, key: str) -> str:
    """`_title_of`'s counterpart, for a sentence that names one row."""
    declared = getattr(admin, "verbose_name", None)
    if isinstance(declared, str) and declared:
        return declared
    return key.split(".", 1)[-1].replace("_", " ").title()


def _group_of(admin: Any, name: str) -> str:
    """A sidebar heading, honouring a declared `group_name`."""
    declared = getattr(admin, "group_name", None)
    if isinstance(declared, str) and declared:
        return declared
    return name.replace("_", " ").title()


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
    translate: Translator,
    admin: ModelAdmin | None = None,
) -> list[dict[str, Any]]:
    active = {sort.field: sort for sort in query.ordering}
    headers: list[dict[str, Any]] = []

    for name in columns:
        field = spec.get(name)
        # Translated here rather than in the template: a column header comes from
        # the project's model, so leaving it untranslated is what makes an admin
        # appear half in one language and half in another. A label the admin
        # declared per language wins; otherwise the catalogue gets a go at the
        # spec's own label.
        source = field.label if field else name.replace("_", " ").capitalize()
        label = admin.field_label(name, source) if admin is not None else source
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
    admin: ModelAdmin,
    spec: ModelSpec,
    items: tuple[Any, ...],
    columns: tuple[str, ...],
    base: str,
) -> list[dict[str, Any]]:
    links = admin.link_columns()
    rows: list[dict[str, Any]] = []

    for obj in items:
        raw_key = "~".join(str(getattr(obj, name)) for name in spec.primary_key)
        key = quote(raw_key, safe="")
        change_url = f"{base}/{key}/"
        cells = []
        for name in columns:
            field = spec.get(name)
            cells.append(
                {
                    "value": admin.cell(obj, name),
                    "boolean": field is not None and field.type is FieldType.BOOLEAN,
                    "numeric": field is not None and field.type in _NUMERIC_TYPES,
                    # The first column links to the row, which is how people
                    # expect to open a record from a table.
                    "url": change_url if name in links else None,
                }
            )
        rows.append(
            {
                "cells": cells,
                # Unquoted: this one goes into a form field, not a URL, and
                # quoting it there would make the action endpoint look up a key
                # that has been escaped twice.
                "key": raw_key,
                # Named in the delete confirmation, so it says what is about to
                # go rather than "this row".
                "label": str(obj),
                "edit_url": change_url,
                "delete_url": f"{change_url}delete",
            }
        )

    return rows


async def _filter_controls(
    spec: ModelSpec,
    admin: ModelAdmin,
    params: dict[str, str],
    adapter: Any,
    relation_limit: int = 20,
    translate: Translator | None = None,
    autocomplete_url: str = "",
    target_search_fields: dict[str, tuple[str, ...]] | None = None,
) -> list[dict[str, Any]]:
    """Build a control for each declared filter.

    Booleans and enumerations become a dropdown from what the spec already knows.
    Dates become a pair of bounds, because "created this month" is the question
    people actually ask of a date column. A relation becomes a dropdown of its
    target's rows -- and past the cap it becomes a searching one rather than
    disappearing, which is what it used to do: a filter that silently vanishes
    once a table grows is worse than no filter at all, because the person who set
    it up saw it working.
    """
    controls: list[dict[str, Any]] = []
    label_of = translate or Translator()
    target_search_fields = target_search_fields or {}

    for name in admin.list_filter:
        field = spec.get(name)
        if field is None:
            continue

        label = admin.field_label(name, field.label)

        if field.type in {FieldType.DATE, FieldType.DATETIME}:
            from_value = params.get(f"{name}__gte", "")
            to_value = params.get(f"{name}__lte", "")
            controls.append(
                {
                    "kind": "range",
                    "name": name,
                    "label": label,
                    "from_name": f"{name}__gte",
                    "to_name": f"{name}__lte",
                    "from_value": from_value,
                    "to_value": to_value,
                    "input_type": "date" if field.type is FieldType.DATE else "datetime-local",
                    # Nobody types two dates to ask "this month". The presets are
                    # what a date filter is used for; the two boxes are the
                    # escape hatch for the case they do not cover.
                    "presets": _date_presets(
                        name,
                        from_value,
                        to_value,
                        with_time=field.type is FieldType.DATETIME,
                        translate=label_of,
                    ),
                }
            )
            continue

        if field.type in _BOUNDED_FILTER_TYPES:
            # The same two-bound control the dates get, without the presets --
            # there is no "this month" for a price. Offered because the
            # alternative for a number is an exact match, and nobody has ever
            # wanted to filter a price list to exactly 19.99; they want under
            # twenty. `__gte`/`__lte` are what the query layer already reads.
            controls.append(
                {
                    "kind": "range",
                    "name": name,
                    "label": label,
                    "from_name": f"{name}__gte",
                    "to_name": f"{name}__lte",
                    "from_value": params.get(f"{name}__gte", ""),
                    "to_value": params.get(f"{name}__lte", ""),
                    "input_type": _BOUNDED_FILTER_TYPES[field.type],
                    "presets": [],
                }
            )
            continue

        # Every value filter is multi-valued. "Show me cancelled *and* refunded"
        # is the normal question, and answering it one value at a time means
        # running the report twice and adding up by hand. The control submits
        # `field__in`, which the query layer already understood; a legacy
        # single-value `?field=x` link still works and still shows as selected.
        chosen = _selected_values(params, name)
        options: tuple[tuple[str, str], ...]
        url = ""

        if field.type is FieldType.BOOLEAN:
            options = (("1", label_of("Yes")), ("0", label_of("No")))
        elif field.choices:
            options = tuple((str(choice.value), label_of(choice.label)) for choice in field.choices)
        elif field.is_relation and not field.type.is_multi_valued:
            found = await adapter.related_choices(
                name, "", limit=relation_limit + 1, search_fields=target_search_fields.get(name, ())
            )
            if len(found) > relation_limit:
                # Too many to send. The control searches the server instead, and
                # carries only the rows currently selected so that it can show
                # what is filtered rather than an empty box.
                url = f"{autocomplete_url}?field={quote(name)}"
                options = await _selected_relation_options(adapter, name, chosen)
            else:
                options = tuple(
                    (str(choice.value), choice.label or str(choice.value)) for choice in found
                )
        else:
            continue

        if not options and not url:
            continue

        controls.append(
            {
                # "search" renders as a picker backed by the autocomplete
                # endpoint; "choices" as a plain list, which needs no click to
                # read and no request to open.
                "kind": "search" if url else "choices",
                "name": name,
                "param": f"{name}__in",
                "label": label,
                "url": url,
                "choices": [
                    {"value": value, "label": label, "selected": value in chosen}
                    for value, label in options
                ],
            }
        )

    return controls


def _date_presets(
    name: str,
    from_value: str,
    to_value: str,
    *,
    with_time: bool,
    translate: Translator,
) -> list[dict[str, Any]]:
    """The ranges a date column is actually asked for.

    Bounds are inclusive on both ends, which is what `__gte`/`__lte` already
    mean and what "1–31 March" means to a person. A datetime column gets the
    same days, widened to cover the whole of the last one -- without that,
    "today" on a datetime silently excludes everything after midnight, which is
    everything.
    """
    # UTC rather than the server's local zone, because that is the zone the
    # column is stored in: `Form._parse` reads a naive datetime-local as UTC. A
    # "today" computed in a different zone from the one the values are in is off
    # by a day for part of every day.
    today = dt.datetime.now(dt.UTC).date()
    start_of_week = today - dt.timedelta(days=today.weekday())
    start_of_month = today.replace(day=1)
    last_month_end = start_of_month - dt.timedelta(days=1)

    def bounds(first: dt.date, last: dt.date) -> tuple[str, str]:
        if not with_time:
            return first.isoformat(), last.isoformat()
        return f"{first.isoformat()}T00:00", f"{last.isoformat()}T23:59"

    ranges: list[tuple[str, dt.date, dt.date]] = [
        ("Today", today, today),
        ("Yesterday", today - dt.timedelta(days=1), today - dt.timedelta(days=1)),
        ("Last 7 days", today - dt.timedelta(days=6), today),
        ("Last 30 days", today - dt.timedelta(days=29), today),
        ("This week", start_of_week, today),
        ("This month", start_of_month, today),
        ("Last month", last_month_end.replace(day=1), last_month_end),
        ("This year", today.replace(month=1, day=1), today),
    ]

    presets: list[dict[str, Any]] = []
    for label, first, last in ranges:
        low, high = bounds(first, last)
        presets.append(
            {
                "label": translate(label),
                "from_value": low,
                "to_value": high,
                "current": from_value == low and to_value == high,
            }
        )
    return presets


def _selected_values(params: dict[str, str], name: str) -> frozenset[str]:
    """Which values of one filter are currently applied.

    Reads both spellings: `field__in=a,b` is what the control submits, and
    `field=a` is what older links and bookmarks carry.
    """
    values = {value for value in params.get(f"{name}__in", "").split(",") if value}
    single = params.get(name, "")
    if single:
        values.add(single)
    return frozenset(values)


def _active_filters(
    controls: list[dict[str, Any]],
    query: ListQuery,
    params: dict[str, str],
    base: str,
    translate: Translator,
) -> list[dict[str, Any]]:
    """What is currently narrowing the list, stated in words, each undoable.

    Without this the only way to know a filter is on is to read every control,
    and the commonest support question about any admin is "why can I not find
    this row" answered by a filter someone left set three days ago.
    """
    chips: list[dict[str, Any]] = []

    def remove(*names: str) -> str:
        return _with_params(base, params, dict.fromkeys((*names, "p")))

    if query.search:
        chips.append(
            {
                "field": translate("Search"),
                "value": query.search,
                "remove_url": remove("q"),
            }
        )

    for control in controls:
        if control["kind"] == "range":
            bounds = [value for value in (control["from_value"], control["to_value"]) if value]
            if bounds:
                chips.append(
                    {
                        "field": control["label"],
                        "value": " – ".join(bounds),
                        "remove_url": remove(control["from_name"], control["to_name"]),
                    }
                )
            continue

        # One chip per selected value, not one per filter. "Status: 3 selected"
        # tells nobody which three, and removing the lot is a blunter undo than
        # anyone wants.
        for choice in control["choices"]:
            if not choice["selected"]:
                continue
            keep = [
                other["value"]
                for other in control["choices"]
                if other["selected"] and other["value"] != choice["value"]
            ]
            chips.append(
                {
                    "field": control["label"],
                    "value": choice["label"],
                    # Both spellings are dropped: the legacy single-value form
                    # and the multi-value one, or removing one would leave the
                    # other still filtering.
                    "remove_url": _with_params(
                        base,
                        params,
                        {
                            control["name"]: None,
                            control["param"]: ",".join(keep) if keep else None,
                            "p": None,
                        },
                    ),
                }
            )

    return chips


#: Marks a form as opened from a relation field on another form.
POPUP_PARAM = "_popup"


def _relation_links(
    admin_for: Any, model_admin: ModelAdmin, admin_url: str
) -> dict[str, dict[str, str]]:
    """Admin URLs for the target of each relation field.

    This is what lets a foreign key carry the buttons Django puts beside one:
    add a related row without leaving the form you are filling in, or open the
    one already chosen. A target that is not registered gets no entry, and the
    template renders the plain picker.
    """
    links: dict[str, dict[str, str]] = {}

    for field_spec in model_admin.spec:
        if not field_spec.is_relation or field_spec.relation is None:
            continue
        if field_spec.type is FieldType.REVERSE_FK:
            continue
        target = field_spec.relation.target
        try:
            admin_for(target)
        except (RegistrationError, KeyError):
            continue
        links[field_spec.name] = {
            "add": f"{admin_url}/{target}/add",
            "base": f"{admin_url}/{target}",
        }

    return links


def _target_search_fields(admin_for: Any, field_spec: Any) -> tuple[str, ...]:
    """`search_fields` declared by the admin of the relation's target.

    An unregistered target is normal -- a model can be pointed at without having
    a page of its own -- so a miss falls back to the adapter's own guess rather
    than being an error.
    """
    if field_spec.relation is None:
        return ()
    try:
        return tuple(admin_for(field_spec.relation.target).searchable())
    except (RegistrationError, KeyError):
        return ()


def _column_label(spec: ModelSpec, name: str) -> str:
    field = spec.get(name)
    return field.label if field else name.replace("_", " ").capitalize()


def _import_columns(admin: ModelAdmin) -> list[dict[str, Any]]:
    """What the page lists as acceptable columns, with each one's shape.

    The same list the downloadable template is built from, so the page and the
    file cannot disagree about what the import will take.
    """
    return [
        {
            "name": admin.field_label(field.name, field.label),
            "required": field.required,
            "hint": column_hint(field),
        }
        for field in importable_fields(admin.spec, admin)
    ]


async def _resolve_relations(
    adapter: Any,
    admin: ModelAdmin,
    plan: Plan,
    search_fields_of: Callable[[Any], tuple[str, ...]],
) -> None:
    """Turn every relation cell into an identity, or into an error naming the row.

    This is the half `importing.py` cannot do: "is there a category called
    Phones" is a question only the database answers. A cell may hold the
    target's primary key or the label the export wrote for it -- both, because a
    file edited by a person carries names and a file produced by a machine
    carries ids, and refusing either would make one of those two workflows
    impossible.

    Every distinct value is looked up once and cached. A price list of three
    thousand rows across nine categories is nine queries this way and three
    thousand without, and it is the same nine answers either way.
    """
    seen: dict[tuple[str, str], Any] = {}

    for row in plan.rows:
        for name, text in row.relations.items():
            field = admin.spec.field(name)
            if not text:
                # Blank clears the link. A required relation is caught by the
                # database, which is the authority on whether NULL is allowed.
                row.values[name] = [] if field.type.is_multi_valued else None
                continue

            # `split_multi` rather than a comma split: a JSON export writes a
            # real array for a many-valued cell, and splitting `["new", "sale"]`
            # on commas produces two fragments that match nothing.
            wanted = split_multi(text) if field.type.is_multi_valued else [text]
            resolved: list[Any] = []
            for item in wanted:
                if not item:
                    continue
                if (name, item) in seen:
                    cached = seen[(name, item)]
                else:
                    cached = await _lookup_related(adapter, admin, name, item, search_fields_of)
                    seen[(name, item)] = cached
                if cached is _MISSING:
                    plan.errors.append(
                        RowError(
                            row.line,
                            admin.field_label(name, field.label),
                            f"No {field.label.lower()} matches this.",
                            item,
                        )
                    )
                else:
                    resolved.append(cached)

            row.values[name] = (
                resolved if field.type.is_multi_valued else (resolved[0] if resolved else None)
            )


#: A lookup that found nothing, cached so the same missing name is not asked
#: for once per row. `None` cannot stand in for it -- `None` is what a blank
#: cell legitimately means.
_MISSING = object()


async def _lookup_related(
    adapter: Any,
    admin: ModelAdmin,
    name: str,
    text: str,
    search_fields_of: Callable[[Any], tuple[str, ...]],
) -> Any:
    """One relation cell, by the target's own identity or by its label.

    Both, because a file edited by a person carries names and a file produced by
    a machine carries ids, and refusing either would make one of those two
    workflows impossible.

    A value that could be an identity is tried as one first, but only if it
    matches exactly -- a category genuinely called "12" must not be shadowed by
    whichever row happens to have id 12. An ambiguous name resolves to nothing
    rather than to the first match: picking one of two "Phones" categories on
    the person's behalf is a silent wrong answer, and the error names the cell.
    """
    field = admin.spec.field(name)
    matches = await adapter.related_choices(
        name, text, limit=2, search_fields=search_fields_of(field)
    )

    exact = [choice for choice in matches if (choice.label or "").strip().lower() == text.lower()]
    if len(exact) == 1:
        return exact[0].value
    if len(matches) == 1 and not exact:
        return matches[0].value
    if matches:
        # Two rows called the same thing. Picking one on the person's behalf is
        # a silent wrong answer, so the cell is reported instead.
        return _MISSING

    # Nothing matched by label. It may still be an identity -- `related_choices`
    # searches the target's *text* columns, so an id never matches there even
    # when the row exists. Handed through as-is for the adapter to resolve,
    # which is the same path a form's dropdown value takes; a key that does not
    # exist comes back from the write as a `ValidationError` and is reported
    # against this row's line by `_apply_import`.
    if text.isdigit() or _looks_like_uuid(text):
        return text
    return _MISSING


def _looks_like_uuid(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


async def _apply_import(adapter: Any, admin: ModelAdmin, plan: Plan) -> int:
    """Write every row, updating where the file carried a key and creating where
    it did not.

    The second half of the validation, and the only half that can see the
    database: a relation given as an identity is resolved here, and a unique
    index or a check constraint has its say here too. Anything that goes wrong
    becomes a `RowError` against the line it came from rather than a request
    that fails with one sentence about the first problem -- which is the same
    report the file pass produces, so the page has one table and not two.

    The caller rolls back when this reports anything. Rows written before the
    failure are part of the same unit of work and go back out with it, which is
    the all-or-nothing this feature promises.

    An unknown key is an error rather than an insert with that key: a file whose
    id column holds a typo would otherwise create a row nobody asked for and
    leave the one that was meant to change untouched.
    """
    written = 0
    for row in plan.rows:
        try:
            if row.key is None:
                await adapter.create(row.values)
            else:
                existing = await adapter.get(row.key)
                if existing is None:
                    plan.errors.append(
                        RowError(
                            row.line,
                            admin.spec.primary_key[0],
                            "No row with this key to update.",
                            str(row.key[0]),
                        )
                    )
                    continue
                await adapter.update(existing, row.values)
        except (ValidationError, AdapterError) as exc:
            plan.errors.append(RowError(row.line, admin.title, str(exc)))
            continue
        written += 1
    return written


async def _export_rows(
    fort: FastFort,
    entry: Any,
    admin: ModelAdmin,
    query: ListQuery,
    columns: tuple[str, ...],
    limit: int,
) -> list[list[Any]]:
    """Every matching row, read a page at a time.

    Collected rather than yielded lazily because the session that produced them
    closes with its unit of work, and a generator handed to the response would
    still be reading from it after the request had let it go. Bounded by `limit`,
    which is what makes holding them defensible.
    """
    collected: list[list[Any]] = []

    async with fort.backend.unit_of_work() as uow:
        adapter = fort.backend.adapter(
            entry.model,
            uow,
            key=admin.spec.key,
            search_fields=admin.searchable(),
            select_related=tuple(admin.select_related),
            prefetch_related=tuple(admin.prefetch_related),
        )
        # Always from the first page. `query` carries whatever `?p=` the list
        # was on, and honouring it made an export started from page two skip
        # everything before it -- a silently short file, which is the worst kind.
        page = 1
        while len(collected) < limit:
            result = await adapter.list(
                ListQuery(
                    search=query.search,
                    filters=query.filters,
                    ordering=query.ordering,
                    page=page,
                    page_size=query.page_size,
                )
            )
            if not result.items:
                break
            for obj in result.items:
                # `export_cell`, not `cell`: a list summarises a geometry and a
                # file has to carry one an importer can read back.
                collected.append([admin.export_cell(obj, name) for name in columns])
                if len(collected) >= limit:
                    break
            page += 1

    return collected


def _page_size_choices(admin_settings: Any, active: int) -> list[int]:
    """The sizes the control offers, always including the one in force.

    A menu that cannot express the current state is a menu that looks broken:
    without this, a project configured to 25 rows would open a size control whose
    every option is wrong.
    """
    offered = {
        size for size in admin_settings.page_size_choices if size <= admin_settings.max_page_size
    }
    offered.add(min(active, admin_settings.max_page_size))
    return sorted(offered)


def _page_links(page_result: Any, page_url: Any) -> list[dict[str, Any]]:
    """Numbered pages with gaps, rather than only Previous and Next.

    Reaching page 12 by pressing Next eleven times is not navigation. The window
    is the ends plus the neighbours of the current page, so the control stays the
    same width whether there are four pages or four hundred.
    """
    total = page_result.pages
    current = page_result.page
    if total <= 1:
        return []

    wanted = {1, total, current, current - 1, current + 1}
    if current <= 3:
        wanted |= {2, 3}
    if current >= total - 2:
        wanted |= {total - 1, total - 2}

    numbers = sorted(number for number in wanted if 1 <= number <= total)
    links: list[dict[str, Any]] = []
    previous = 0
    for number in numbers:
        if previous and number - previous > 1:
            links.append({"gap": True})
        links.append(
            {"gap": False, "number": number, "url": page_url(number), "current": number == current}
        )
        previous = number
    return links


async def _selected_relation_options(
    adapter: Any, name: str, chosen: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    """The options a searching relation filter starts with: the active ones.

    Looked up by term rather than by key because that is the only lookup the
    adapter protocol offers; a miss falls back to showing the raw value, which
    is still better than showing nothing.
    """
    if not chosen:
        return ()
    labels: dict[str, str] = {}
    try:
        found = await adapter.related_choices(name, "", limit=200)
    except (ValidationError, AdapterError):
        found = []
    for choice in found:
        labels[str(choice.value)] = choice.label or str(choice.value)
    return tuple((value, labels.get(value, value)) for value in sorted(chosen))
