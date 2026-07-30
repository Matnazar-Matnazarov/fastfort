"""CSRF protection for the admin's forms.

Signed double-submit. The token is minted server-side, stored in its own cookie
and echoed in the form; a request is accepted only when the two match and the
signature verifies.

`SameSite=Lax` already blocks the classic cross-site POST, so this is the second
layer rather than the only one. It is worth having because Lax is not honoured
identically everywhere, and because a subdomain that can set cookies on the
parent domain can defeat SameSite but not a signature.
"""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from itsdangerous import BadSignature, URLSafeTimedSerializer

from fastfort.core.exceptions import SecurityError

if TYPE_CHECKING:
    from fastfort.core.settings import FastFortSettings

__all__ = ["CsrfProtection"]

#: Domain separation from the session cookie, which uses the same secret.
SALT = "fastfort.admin.csrf"

#: A CSRF token outliving the page it was rendered into serves no purpose, but too
#: short a life turns a slowly filled form into a mysterious rejection.
MAX_AGE = 60 * 60 * 12

#: Methods that cannot change state, so they need no token.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CsrfProtection:
    """Issues and verifies CSRF tokens."""

    def __init__(self, settings: FastFortSettings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=SALT)

    @property
    def enabled(self) -> bool:
        return self._settings.security.csrf_enabled

    @property
    def cookie_name(self) -> str:
        return f"{self._settings.security.cookie_name}_csrf"

    @property
    def field_name(self) -> str:
        return self._settings.security.csrf_field_name

    @property
    def header_name(self) -> str:
        return self._settings.security.csrf_header_name

    def issue(self) -> str:
        """Mint a fresh token."""
        return str(self._serializer.dumps(secrets.token_urlsafe(16)))

    def ensure(self, cookie: str | None) -> str:
        """Reuse the token the browser already holds, or mint one.

        Rotating on every response looks safer and is not: a form rendered before
        the rotation carries a value that no longer matches, so pressing Back, or
        keeping a second tab open, produces a mysterious rejection. The token is
        signed and time-limited, so keeping it for the session costs nothing.

        It is still rotated deliberately on a successful sign-in, where a token
        planted before authentication would be a fixation vector.
        """
        if cookie:
            try:
                self._serializer.loads(cookie, max_age=MAX_AGE)
            except BadSignature:
                return self.issue()
            else:
                return cookie
        return self.issue()

    def verify(self, *, cookie: str | None, submitted: str | None) -> None:
        """Raise `SecurityError` unless the request carries a valid token pair.

        The signature is checked as well as the match. Without it, an attacker who
        can set a cookie on the domain could submit a value of their own choosing
        in both places and satisfy a naive comparison.
        """
        if not self.enabled:
            return

        if not cookie or not submitted:
            raise SecurityError(
                "This form is missing its security token.",
                hint="Reload the page and try again. If it keeps happening, check "
                "that cookies are not being blocked.",
            )

        # Compared before unsigning, so a mismatch costs nothing extra.
        if not hmac.compare_digest(cookie, submitted):
            raise SecurityError(
                "This form's security token does not match this session.",
                hint="Reload the page and submit it again.",
            )

        try:
            self._serializer.loads(cookie, max_age=MAX_AGE)
        except BadSignature as exc:
            raise SecurityError(
                "This form's security token is not valid.",
                hint="Reload the page and submit it again.",
            ) from exc

    def token_from(self, form: dict[str, str], headers: dict[str, str]) -> str | None:
        """Find the submitted token in a form body or a request header.

        The header form is what HTMX and fetch() use; the field is what a plain
        HTML form posts. Both are accepted so the admin works either way.
        """
        field = form.get(self.field_name)
        if field:
            return field
        lowered = {name.lower(): value for name, value in headers.items()}
        return lowered.get(self.header_name.lower())
