"""Sign-in and sign-out.

Two separate things live here, and their names are similar enough to be worth
distinguishing:

*Logout* is the person leaving. `POST /logout` clears the session cookie.

*Lockout* is brute-force protection. After a configured number of failed attempts
an address or an identity is refused for a growing delay, so a password list
cannot be worked through. It lives in `fastfort.auth.lockout`.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fastfort._version import __version__
from fastfort.core.exceptions import SecurityError
from fastfort.core.hooks import Hook
from fastfort.i18n import Translator, is_rtl, negotiate_language
from fastfort.spec import FieldType
from fastfort.ui.theming import Theme

from .security import (
    LANGUAGE_COOKIE,
    clear_session_cookie,
    safe_next_url,
    set_csrf_cookie,
    set_session_cookie,
)

if TYPE_CHECKING:
    from fastfort.auth.service import AdminAuth
    from fastfort.core.app import FastFort
    from fastfort.ui.renderer import Renderer

__all__ = ["build_auth_router"]


def build_auth_router(fort: FastFort, auth: AdminAuth, renderer: Renderer) -> APIRouter:
    """Routes that must stay reachable while signed out."""
    settings = fort.settings
    admin_url = settings.admin.url
    login_url = f"{admin_url}/login"
    # Versioned, exactly as `site.py` builds it. Moving the version into the
    # path is what makes the year-long `immutable` cache safe, and this file was
    # missed when that landed -- so the sign-in page went on asking for
    # `/admin/static/fastfort.css`, the one address whose bytes change
    # underneath it. It is not a stale-stylesheet bug, because that address
    # answers `no-cache` and is revalidated; it is the sign-in page paying a
    # conditional request for its CSS and its script on every single load, for
    # ever, while every other page in the admin pays none.
    static_url = f"{admin_url}/static/{__version__}"

    router = APIRouter(tags=["fastfort-auth"])

    def page(
        request: Request,
        *,
        csrf_token: str,
        next_url: str,
        identity: str = "",
        error: str | None = None,
        error_hint: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        theme = Theme.from_settings(settings.ui)
        identity_field = _identity_field(fort)
        translator = Translator(
            negotiate_language(
                chosen=request.cookies.get(LANGUAGE_COOKIE),
                configured=settings.ui.language,
                accept=request.headers.get("accept-language", ""),
            ),
            # The same project catalogue the rest of the admin uses. Without it
            # the sign-in page is the one screen that ignores a project's own
            # translations, which reads as the language switcher being broken.
            project_dir=settings.ui.locale_dir,
        )
        body = renderer.render(
            "auth/login.html",
            settings=settings,
            theme=theme,
            _=translator,
            language=translator.language,
            # The sign-in page is not built on the shell, so it needs telling
            # separately. Without it, Arabic turned the whole admin around and
            # left the one page an anonymous visitor sees laid out the other way.
            text_direction="rtl" if is_rtl(translator.language) else "ltr",
            languages=translator.choices(),
            current_language=translator.choice,
            language_url=f"{admin_url}/language",
            # Switching language must land back on the sign-in page, keeping the
            # `?next=` that says where the person was headed.
            current_path=f"{login_url}?next={quote(next_url, safe='/')}" if next_url else login_url,
            stylesheets=theme.stylesheets(static_url),
            static_url=static_url,
            version=__version__,
            login_url=login_url,
            next_url=next_url,
            identity=identity,
            identity_label=identity_field[0],
            identity_type=identity_field[1],
            csrf_field=auth.csrf.field_name,
            csrf_token=csrf_token,
            error=error,
            error_hint=error_hint,
            # Worth saying out loud on the one page that collects a password.
            insecure_transport=request.url.scheme == "http"
            and request.url.hostname not in {"localhost", "127.0.0.1", "testserver"},
        )
        response = HTMLResponse(body, status_code=status_code)
        set_csrf_cookie(response, csrf_token, settings, auth.csrf.cookie_name)
        return response

    @router.get("/login", response_class=HTMLResponse, name="fastfort:login")
    async def login_form(request: Request) -> Any:
        # Already signed in: sending someone back to a login form they do not need
        # is how people end up with two tabs disagreeing about who they are.
        if await auth.current_user(request) is not None:
            return RedirectResponse(f"{admin_url}/", status_code=303)

        return page(
            request,
            csrf_token=auth.csrf.ensure(request.cookies.get(auth.csrf.cookie_name)),
            next_url=safe_next_url(request.query_params.get("next"), fallback=f"{admin_url}/"),
        )

    @router.post("/login", name="fastfort:login-submit")
    async def login_submit(request: Request) -> Any:
        form = dict(await request.form())
        identity = str(form.get("identity", ""))
        password = str(form.get("password", ""))
        next_url = safe_next_url(str(form.get("next", "")), fallback=f"{admin_url}/")
        # Kept, not rotated, when re-rendering a rejected form: the browser may
        # still hold pages carrying this value.
        held_token = auth.csrf.ensure(request.cookies.get(auth.csrf.cookie_name))

        def rejected(error: str, hint: str | None = None) -> HTMLResponse:
            # 200, not 401: this is a rendered form, and a 401 invites the browser
            # to pop its own basic-auth dialog.
            return page(
                request,
                csrf_token=held_token,
                next_url=next_url,
                identity=identity,
                error=error,
                error_hint=hint,
            )

        try:
            auth.csrf.verify(
                cookie=request.cookies.get(auth.csrf.cookie_name),
                submitted=auth.csrf.token_from(
                    {k: str(v) for k, v in form.items()}, dict(request.headers)
                ),
            )
        except SecurityError as exc:
            return rejected(exc.message, exc.hint)

        try:
            result = await auth.authenticate(
                identity=identity,
                password=password,
                address=auth.client_address(request),
                request=request,
            )
        except SecurityError as exc:
            return rejected(exc.message, exc.hint)

        # 303 so the browser follows with GET and the password is not left in a
        # resubmittable POST.
        response = RedirectResponse(next_url, status_code=303)
        set_session_cookie(response, result.session_cookie, settings)
        # Rotated here and only here: a CSRF token planted before authentication
        # would otherwise survive into the authenticated session.
        set_csrf_cookie(response, auth.csrf.issue(), settings, auth.csrf.cookie_name)
        return response

    @router.post("/logout", name="fastfort:logout")
    async def logout(request: Request) -> Any:
        """Sign out.

        POST only. A GET logout can be triggered by any image tag on any page,
        which is a nuisance rather than a breach, but a pointless one to allow.
        """
        user = await auth.current_user(request)

        # A failed token on the way out is not worth blocking: the safe outcome
        # is to clear the session either way.
        with contextlib.suppress(SecurityError):
            auth.csrf.verify(
                cookie=request.cookies.get(auth.csrf.cookie_name),
                submitted=auth.csrf.token_from(
                    {k: str(v) for k, v in (await request.form()).items()},
                    dict(request.headers),
                ),
            )

        if user is not None:
            await fort.hooks.emit(Hook.USER_LOGGED_OUT, request=request, user=user)

        response = RedirectResponse(login_url, status_code=303)
        clear_session_cookie(response, settings)
        return response

    return router


def _identity_field(fort: FastFort) -> tuple[str, str]:
    """The label and input type for whatever the project signs in with.

    A project keyed on `username` should not be shown a field labelled Email that
    rejects its own usernames for not containing an @.
    """
    config = fort.user_config
    name = config.identity_field
    label = name.replace("_", " ").capitalize()

    try:
        spec = fort.backend.introspect(config.model, key="fastfort.user")
        field = spec.get(name)
    except Exception:
        field = None

    if field is not None:
        label = field.label
        if field.type is FieldType.EMAIL or "email" in name:
            return label, "email"
    return label, "email" if "email" in name else "text"
