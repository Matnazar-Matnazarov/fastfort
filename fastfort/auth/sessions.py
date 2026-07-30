"""The admin's session cookie.

A signed cookie rather than a server-side session table. The admin needs to know
who you are and nothing else, and a signed cookie has no fixation problem to
solve: there is no identifier to plant before login, because the cookie is only
ever minted after a successful one.

What a stateless cookie cannot do by itself is expire early. So the payload
carries a stamp derived from the user's password hash: changing a password
changes the stamp, which invalidates every cookie already issued for that
account, on every device.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

if TYPE_CHECKING:
    from fastfort.core.settings import FastFortSettings

__all__ = ["AdminSession", "SessionCodec"]

#: Domain separation: a value signed for a session must never verify as a CSRF
#: token, even though both use the same secret key.
SALT = "fastfort.admin.session"

#: Length of the password stamp. Twelve base16 characters is plenty to detect a
#: change, and keeps the cookie small.
_STAMP_LENGTH = 12


@dataclass(frozen=True, slots=True)
class AdminSession:
    """Who the cookie says you are."""

    user_id: str
    stamp: str

    def to_payload(self) -> dict[str, str]:
        return {"uid": self.user_id, "st": self.stamp}


class SessionCodec:
    """Signs and verifies admin session cookies."""

    def __init__(self, settings: FastFortSettings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=SALT)

    # -- stamps -------------------------------------------------------------

    def stamp_for(self, password_hash: str) -> str:
        """Derive the invalidation stamp from a stored password hash.

        Keyed with the secret so the stamp cannot be computed from a leaked
        database alone, and truncated because only equality matters.
        """
        digest = hmac.new(
            self._settings.secret_key.get_secret_value().encode(),
            (password_hash or "").encode(),
            hashlib.sha256,
        ).hexdigest()
        return digest[:_STAMP_LENGTH]

    # -- cookies ------------------------------------------------------------

    def issue(self, *, user_id: Any, password_hash: str) -> str:
        session = AdminSession(user_id=str(user_id), stamp=self.stamp_for(password_hash))
        return str(self._serializer.dumps(session.to_payload()))

    def read(self, raw: str | None) -> AdminSession | None:
        """Return the session a cookie carries, or None if it is not usable.

        Every failure -- tampered, expired, from an older secret key, malformed --
        collapses to None. The admin's response is the same in all of those cases:
        show the login page.
        """
        if not raw:
            return None
        try:
            payload = self._serializer.loads(raw, max_age=self._settings.auth.session_ttl)
        except (BadSignature, SignatureExpired, Exception):
            return None

        if not isinstance(payload, dict):
            return None
        user_id = payload.get("uid")
        stamp = payload.get("st")
        if not isinstance(user_id, str) or not isinstance(stamp, str):
            return None
        return AdminSession(user_id=user_id, stamp=stamp)

    def matches(self, session: AdminSession, password_hash: str) -> bool:
        """Whether the cookie's stamp still matches the account.

        Compared in constant time; the stamp is a keyed digest, so a timing
        oracle on it would be a slow path to forging one.
        """
        return hmac.compare_digest(session.stamp, self.stamp_for(password_hash))
