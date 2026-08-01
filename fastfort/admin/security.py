"""The gate in front of every admin route, and the headers on every response.

Access control lives here rather than inside each view, because a view that
forgets to check is indistinguishable from one that decided not to. There is one
gate, and it is applied to the router as a whole.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastfort.core.settings import FastFortSettings

__all__ = [
    "LANGUAGE_COOKIE",
    "LoginRequired",
    "SecurityHeadersMiddleware",
    "clear_session_cookie",
    "make_guard",
    "safe_next_url",
    "set_csrf_cookie",
    "set_session_cookie",
]

#: Where a chosen language is remembered. A cookie rather than a column, so it
#: works before anyone has signed in -- the sign-in page needs a language too.
#: Named here because both the shell and the sign-in page read it, and a second
#: literal spelling of it is a bug waiting to happen.
LANGUAGE_COOKIE = "ff_language"


class LoginRequired(Exception):
    """Raised by the gate; turned into a redirect by a handler on the app.

    An exception rather than a returned response, so a view cannot accidentally
    continue after the gate has decided the answer is no.
    """

    def __init__(self, login_url: str, next_url: str | None = None) -> None:
        self.login_url = login_url
        self.next_url = next_url
        super().__init__("Authentication is required.")

    def to_response(self) -> RedirectResponse:
        target = self.login_url
        if self.next_url:
            # Only "/" stays literal. Leaving "?", "=" or "&" unescaped would let
            # a target's own query string split into separate parameters and the
            # redirect would lose half of where the person was going.
            target = f"{target}?next={quote(self.next_url, safe='/')}"
        # 303: the browser must follow with GET even if the blocked request was a
        # POST, otherwise it would re-post the body to the login page.
        return RedirectResponse(target, status_code=303)


def safe_next_url(candidate: str | None, *, fallback: str) -> str:
    """Return `candidate` only if it is a path on this site.

    Post-login redirects are the classic open-redirect vector: `?next=//evil.com`
    reads as a path but browsers treat it as a host. Anything with a scheme, a
    host, or a leading double slash is discarded rather than sanitised, because
    the safe fallback is always available.
    """
    if not candidate:
        return fallback
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return fallback

    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return fallback
    if not candidate.startswith("/"):
        return fallback
    return candidate


class SecurityHeadersMiddleware:
    """Adds the headers a browser needs in order to defend the admin.

    Scoped to the admin's own paths: a framework has no business changing the
    headers of the application it is mounted into.
    """

    def __init__(self, app: ASGIApp, *, settings: FastFortSettings) -> None:
        self.app = app
        self.settings = settings
        self._prefix = settings.admin.url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self._prefix):
            await self.app(scope, receive, send)
            return

        if not self.settings.security.security_headers:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                for name, value in self._headers():
                    headers.append((name.encode("latin-1"), value.encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_with_headers)

    def _headers(self) -> list[tuple[str, str]]:
        # 'unsafe-inline' for styles only: the theme is applied as an inline
        # `style` attribute on <html>, whose contents are numbers validated by
        # `Theme`. Scripts get no such allowance -- they are all external files.
        csp = (
            "default-src 'none'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        headers = [
            ("content-security-policy", csp),
            # Belt and braces with frame-ancestors, for older browsers.
            ("x-frame-options", "DENY"),
            ("x-content-type-options", "nosniff"),
            ("referrer-policy", "same-origin"),
            ("cross-origin-opener-policy", "same-origin"),
        ]
        if self.settings.security.hsts_seconds:
            headers.append(
                (
                    "strict-transport-security",
                    f"max-age={self.settings.security.hsts_seconds}; includeSubDomains",
                )
            )
        return headers


def set_session_cookie(response: Response, value: str, settings: FastFortSettings) -> None:
    """Attach the admin session cookie with the configured protections."""
    security = settings.security
    response.set_cookie(
        security.cookie_name,
        value,
        max_age=settings.auth.session_ttl,
        path=security.cookie_path,
        domain=security.cookie_domain,
        secure=security.cookie_secure,
        httponly=security.cookie_httponly,
        samesite=security.cookie_samesite,
    )


def clear_session_cookie(response: Response, settings: FastFortSettings) -> None:
    security = settings.security
    response.delete_cookie(
        security.cookie_name, path=security.cookie_path, domain=security.cookie_domain
    )


def set_csrf_cookie(response: Response, value: str, settings: FastFortSettings, name: str) -> None:
    """Attach the CSRF cookie.

    Readable by JavaScript on purpose -- `httponly=False` -- because the double
    submit needs the page to be able to echo it back in a header.
    """
    security = settings.security
    response.set_cookie(
        name,
        value,
        path=security.cookie_path,
        domain=security.cookie_domain,
        secure=security.cookie_secure,
        httponly=False,
        samesite=security.cookie_samesite,
    )


def make_guard(auth: Any, settings: FastFortSettings) -> Any:
    """Build the router-wide dependency that rejects anonymous requests.

    A factory over the auth object rather than a lookup in `request.scope`: the
    dependency then has no hidden requirement on middleware having run first.
    """
    login_url = f"{settings.admin.url}/login"

    async def guard(request: Request) -> Any:
        user = await auth.current_user(request)
        if user is None:
            target = request.url.path
            if request.url.query:
                target = f"{target}?{request.url.query}"
            raise LoginRequired(login_url, next_url=target)
        return user

    return guard
