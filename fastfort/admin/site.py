"""The admin's HTTP surface.

Everything is server-rendered. The list view sorts, searches, filters and
paginates through links and a GET form, so the admin works with JavaScript
disabled and every view is a URL that can be bookmarked or shared.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from fastfort._version import __version__
from fastfort.auth.service import AdminAuth
from fastfort.core.exceptions import (
    AdapterError,
    ObjectNotFound,
    RegistrationError,
    SecurityError,
    ValidationError,
)
from fastfort.spec import Choice, FieldType, ListQuery, SortSpec
from fastfort.ui.renderer import Renderer
from fastfort.ui.theming import Theme

from .auth_views import build_auth_router
from .forms import Form
from .messages import Message, Messages
from .options import ModelAdmin
from .security import make_guard

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

    def base_context(request: Request, current_key: str | None) -> dict[str, Any]:
        theme = Theme.from_settings(settings.ui)
        user = request.scope.get("fastfort_user")
        return {
            "user": user,
            "user_label": _user_label(fort, user),
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
            "breadcrumbs": (),
            "page_title": settings.project_name,
            "messages": notices.read(request),
            "request": request,
        }

    #: Static assets and the sign-in page stay reachable while signed out.
    public = APIRouter(tags=["fastfort-admin"])

    #: Everything else. The gate is a router-wide dependency rather than a call
    #: inside each view, because a view that forgets to check looks exactly like
    #: one that decided not to.
    router = APIRouter(
        tags=["fastfort-admin"], dependencies=[Depends(_remember(make_guard(auth, settings), auth))]
    )

    def page(request: Request, template: str, context: dict[str, Any]) -> HTMLResponse:
        """Render a page, then drop the message cookie.

        Cleared here rather than on read, so a banner appears exactly once: one
        that survives a refresh makes people wonder whether they saved twice.
        """
        response = HTMLResponse(renderer.render(template, **context))
        if context.get("messages"):
            notices.clear(response)
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

    @public.get("/static/fastfort.css", include_in_schema=False)
    async def stylesheet() -> Response:
        return Response(
            _bundled_css(),
            media_type="text/css",
            # Long-lived, because the URL changes with the package version in a
            # release. During development auto-reload matters more than caching.
            headers={"Cache-Control": "no-cache" if settings.debug else "public, max-age=86400"},
        )

    @public.get("/static/js/fastfort.js", include_in_schema=False)
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

        columns = tuple(model_admin.columns())
        context = base_context(request, model_key) | {
            "page_title": model_admin.title,
            "breadcrumbs": ({"label": model_admin.title, "url": None},),
            "admin": model_admin,
            "page": page_result,
            "query": query,
            "list_url": list_url(model_key),
            "add_url": f"{admin_url}/{model_key}/add",
            "columns": _column_headers(spec, columns, query, params, list_url(model_key)),
            "rows": _rows(
                model_admin, spec, page_result.items, columns, f"{admin_url}/{model_key}"
            ),
            "filters": _filter_controls(spec, model_admin, params),
            "ordering_param": ",".join(sort.as_token() for sort in query.ordering),
            "page_url": lambda number: _with_params(
                list_url(model_key), params, {"p": str(number)}
            ),
        }
        return page(request, "model/list.html", context)

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
    ) -> dict[str, tuple[Choice, ...]]:
        """Options for every relation field on the form.

        Bounded by `autocomplete_limit`: a dropdown over a million rows is not a
        usable control, and building it would stall the page. Once the limit is
        reached the field needs the autocomplete widget instead.
        """
        options: dict[str, tuple[Choice, ...]] = {}
        for field_spec in model_admin.spec:
            if not field_spec.is_relation or field_spec.type is FieldType.REVERSE_FK:
                continue
            found = await adapter.related_choices(
                field_spec.name, "", limit=settings.admin.autocomplete_limit
            )
            options[field_spec.name] = tuple(
                Choice(value=choice.value, label=choice.label or str(choice.value))
                for choice in found
            )
        return options

    def form_context(
        request: Request,
        model_key: str,
        model_admin: ModelAdmin,
        form: Form,
        *,
        instance: Any = None,
        label: str = "",
    ) -> dict[str, Any]:
        editing = instance is not None
        key = model_admin.spec.primary_key
        pk = tuple(getattr(instance, name) for name in key) if editing else ()
        return base_context(request, model_key) | {
            "page_title": label if editing else f"Add {model_admin.singular.lower()}",
            "breadcrumbs": (
                {"label": model_admin.title, "url": list_url(model_key)},
                {"label": label if editing else "Add", "url": None},
            ),
            "admin": model_admin,
            "form": form,
            "heading": label if editing else f"Add {model_admin.singular.lower()}",
            "subheading": model_admin.spec.key if editing else None,
            "list_url": list_url(model_key),
            "action_url": object_url(model_key, pk) if editing else f"{admin_url}/{model_key}/add",
            "delete_url": f"{object_url(model_key, pk)}delete" if editing else None,
            "submit_label": "Save changes" if editing else f"Create {model_admin.singular.lower()}",
            "version_token": None,
            "input_types": INPUT_TYPES,
        }

    @router.get("/{model_key}/add", response_class=HTMLResponse, name="fastfort:add")
    async def add_form(request: Request, model_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            form = Form(
                model_admin.spec,
                model_admin,
                relation_choices=await relation_choices(adapter, model_admin),
            )
        return page(request, "model/form.html", form_context(request, model_key, model_admin, form))

    @router.post("/{model_key}/add", name="fastfort:add-submit")
    async def add_submit(request: Request, model_key: str) -> Any:
        model_admin = _require_admin(admin_for, model_key)
        entry = fort.registry.entry_for_key(model_key)
        submitted = await _form_data(request)

        try:
            await verify_csrf(request)
        except SecurityError as exc:
            return redirect(f"{admin_url}/{model_key}/add", notices.danger(exc.message))

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            choices = await relation_choices(adapter, model_admin)
            form = Form(model_admin.spec, model_admin, relation_choices=choices)
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
            except (ValidationError, AdapterError) as exc:
                await uow.rollback()
                form.non_field_errors.append(exc.message)
                return page(
                    request,
                    "model/form.html",
                    form_context(request, model_key, model_admin, form),
                )

            label = adapter.label_for(created)
            key = adapter.primary_key_of(created)

        return redirect(
            object_url(model_key, key),
            notices.success(f"{model_admin.singular} \u201c{label}\u201d was created."),
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
            form = Form(
                model_admin.spec,
                model_admin,
                instance=instance,
                relation_choices=await relation_choices(adapter, model_admin),
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
        submitted = await _form_data(request)

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
            choices = await relation_choices(adapter, model_admin)
            form = Form(model_admin.spec, model_admin, instance=instance, relation_choices=choices)
            cleaned = form.bind(submitted)
            label = adapter.label_for(instance)

            if not form.is_valid:
                await uow.rollback()
                return page(
                    request,
                    "model/form.html",
                    form_context(
                        request, model_key, model_admin, form, instance=instance, label=label
                    ),
                )

            try:
                await adapter.update(instance, cleaned)
            except (ValidationError, AdapterError) as exc:
                await uow.rollback()
                form.non_field_errors.append(exc.message)
                return page(
                    request,
                    "model/form.html",
                    form_context(
                        request, model_key, model_admin, form, instance=instance, label=label
                    ),
                )

            label = adapter.label_for(instance)
            key = adapter.primary_key_of(instance)

        return redirect(
            object_url(model_key, key),
            notices.success(f"{model_admin.singular} \u201c{label}\u201d was saved."),
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

        return page(
            request,
            "model/delete.html",
            base_context(request, model_key)
            | {
                "page_title": f"Delete {label}",
                "breadcrumbs": (
                    {"label": model_admin.title, "url": list_url(model_key)},
                    {"label": label, "url": object_url(model_key, key)},
                    {"label": "Delete", "url": None},
                ),
                "admin": model_admin,
                "label": label,
                "cascades": _cascades(model_admin),
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

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(entry.model, uow, key=model_key)
            instance = await _require_object(adapter, parse_key(model_admin.spec, object_key))
            label = adapter.label_for(instance)
            try:
                await adapter.delete(instance)
            except AdapterError as exc:
                await uow.rollback()
                return redirect(
                    object_url(model_key, adapter.primary_key_of(instance)),
                    notices.danger(f"Could not delete this row: {exc.message}"),
                )

        return redirect(
            list_url(model_key),
            notices.success(f"{model_admin.singular} \u201c{label}\u201d was deleted."),
        )

    public.include_router(build_auth_router(fort, auth, renderer))
    public.include_router(router)
    return public


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------


#: HTML input types, keyed by widget name. Kept beside the widget map rather than
#: in the template, so adding a widget touches one file.
INPUT_TYPES = {
    "text": "text",
    "number": "number",
    "decimal": "number",
    "date": "date",
    "datetime": "datetime-local",
    "time": "time",
    "email": "email",
    "url": "url",
    "password": "password",
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


async def _form_data(request: Request) -> dict[str, Any]:
    """Read a submitted form, keeping repeated keys as lists.

    A multi-select posts the same name several times, and a plain dict would keep
    only the last value.
    """
    raw = await request.form()
    data: dict[str, Any] = {}
    for name in raw:
        values = raw.getlist(name)
        data[name] = [str(value) for value in values] if len(values) > 1 else str(values[0])
    return data


def _cascades(admin: ModelAdmin) -> tuple[str, ...]:
    """Relations whose rows go with this one.

    Naming them is the difference between a confirmation and a trap.
    """
    return tuple(
        field.label
        for field in admin.spec
        if field.relation is not None and field.relation.cascade_delete
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
    try:
        return fort.user_config.identity_of(user)
    except Exception:
        return "Account"


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
    admin: ModelAdmin,
    spec: ModelSpec,
    items: tuple[Any, ...],
    columns: tuple[str, ...],
    base: str,
) -> list[dict[str, Any]]:
    links = admin.link_columns()
    rows: list[dict[str, Any]] = []

    for obj in items:
        key = quote("~".join(str(getattr(obj, name)) for name in spec.primary_key), safe="")
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
        rows.append({"cells": cells, "edit_url": change_url, "delete_url": f"{change_url}delete"})

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
