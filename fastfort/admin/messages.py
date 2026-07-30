"""Flash messages: "Product saved", "3 rows deleted".

Carried in a short-lived signed cookie rather than a server-side store, because
the admin has no other server-side session state and adding one for a sentence of
feedback would be the wrong trade.

They are read once and cleared. A "saved" banner that survives a refresh makes
people wonder whether they saved twice.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from itsdangerous import BadSignature, URLSafeTimedSerializer

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

    from fastfort.core.settings import FastFortSettings

__all__ = ["Message", "MessageLevel", "Messages"]

#: Domain separation from the session and CSRF cookies, which share the key.
SALT = "fastfort.admin.messages"

#: A redirect follows within milliseconds. Anything longer just means a stale
#: banner appears on some unrelated page later.
MAX_AGE = 60

#: Bounded so a loop that queues messages cannot grow a cookie past the 4 KB
#: browsers accept, which would silently drop the whole cookie.
MAX_MESSAGES = 8
MAX_LENGTH = 240


class MessageLevel(StrEnum):
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"


@dataclass(frozen=True, slots=True)
class Message:
    level: MessageLevel
    text: str

    def to_pair(self) -> list[str]:
        return [self.level.value, self.text]


class Messages:
    """Queues messages onto a response and reads them off a request."""

    def __init__(self, settings: FastFortSettings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt=SALT)

    @property
    def cookie_name(self) -> str:
        return f"{self._settings.security.cookie_name}_messages"

    def read(self, request: Request) -> tuple[Message, ...]:
        """Messages carried by this request.

        A tampered or expired cookie yields nothing. There is no security
        consequence either way -- but rendering attacker-chosen text inside our
        own chrome would make a convincing phishing surface, so it is signed.
        """
        raw = request.cookies.get(self.cookie_name)
        if not raw:
            return ()
        try:
            payload = self._serializer.loads(raw, max_age=MAX_AGE)
        except (BadSignature, json.JSONDecodeError):
            return ()

        if not isinstance(payload, list):
            return ()

        messages: list[Message] = []
        for item in payload[:MAX_MESSAGES]:
            if not (isinstance(item, list) and len(item) == 2):
                continue
            level, text = item
            try:
                messages.append(
                    Message(level=MessageLevel(str(level)), text=str(text)[:MAX_LENGTH])
                )
            except ValueError:
                continue
        return tuple(messages)

    def queue(self, response: Response, *messages: Message) -> None:
        """Attach messages to a response, for the page that follows it."""
        if not messages:
            return
        payload = [message.to_pair() for message in messages[:MAX_MESSAGES]]
        security = self._settings.security
        response.set_cookie(
            self.cookie_name,
            self._serializer.dumps(payload),
            max_age=MAX_AGE,
            path=security.cookie_path,
            domain=security.cookie_domain,
            secure=security.cookie_secure,
            httponly=True,
            samesite=security.cookie_samesite,
        )

    def clear(self, response: Response) -> None:
        """Drop the cookie, so a banner is shown exactly once."""
        security = self._settings.security
        response.delete_cookie(
            self.cookie_name, path=security.cookie_path, domain=security.cookie_domain
        )

    # -- shorthands ---------------------------------------------------------

    @staticmethod
    def success(text: str) -> Message:
        return Message(MessageLevel.SUCCESS, text[:MAX_LENGTH])

    @staticmethod
    def info(text: str) -> Message:
        return Message(MessageLevel.INFO, text[:MAX_LENGTH])

    @staticmethod
    def warning(text: str) -> Message:
        return Message(MessageLevel.WARNING, text[:MAX_LENGTH])

    @staticmethod
    def danger(text: str) -> Message:
        return Message(MessageLevel.DANGER, text[:MAX_LENGTH])
