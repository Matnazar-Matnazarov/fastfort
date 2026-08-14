"""A record of who signed in, from where, and on what.

Turned on with one call, against a table the project owns::

    fort.record_sign_ins(SignInRecord)

Four decisions are worth stating, because each one is the opposite of what the
obvious implementation does.

*The project owns the table.* FastFort ships no migrations and creates nothing,
so the model is the project's -- `SignInRecordMixin` in
`fastfort.orm.sqlalchemy` supplies the columns for SQLAlchemy, and a Tortoise
project declares the same field names. The columns are checked when
`record_sign_ins` is called rather than on the first sign-in, so a mistyped
schema is a start-up error rather than a surprise in the middle of the night.

*There is no foreign key.* The user is stored as text -- the identity they typed
and their primary key as a string. An audit trail that cascades away when the
account is deleted is not an audit trail, and "who deleted this account" is
exactly the question these rows exist to answer.

*Failures are recorded too, by default.* A log of successes tells you nothing
about the night somebody tried four hundred passwords.

*Writing a record can never fail a sign-in.* It runs in its own transaction, and
if that transaction fails the exception is logged rather than raised: a missing
audit table must not be able to lock everyone out of the admin.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from fastfort.core.exceptions import ConfigurationError
from fastfort.core.hooks import Hook
from fastfort.core.registry import default_model_key

from .addresses import client_address
from .devices import read_device

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastfort.core.app import FastFort

__all__ = ["SignIn", "SignInRecorder"]

#: The first and only logger in the package. Everything else here surfaces a
#: problem as a value the caller can see, which is the better habit -- but the
#: one thing this must never do is interrupt a sign-in, so a failed write has
#: nowhere else to go.
logger = logging.getLogger("fastfort.auth")

#: Columns a project's table has to have, because a record without them is not
#: one. Everything else is written only if the model declares it, so a project
#: that wants nothing but the address and the time can have exactly that.
REQUIRED_FIELDS = ("at", "successful", "address")


@dataclass(frozen=True, slots=True)
class SignIn:
    """One attempt, as it will be stored.

    Field names are the column names. That is the whole mapping: there is no
    configuration for it, because a second way to spell `address` is a second
    thing that can disagree.
    """

    at: dt.datetime
    successful: bool
    address: str
    identity: str = ""
    user_key: str = ""
    browser: str = ""
    platform: str = ""
    kind: str = ""
    user_agent: str = ""
    location: str = ""


class SignInRecorder:
    """Writes a `SignIn` for every sign-in, and by default for every refusal."""

    def __init__(
        self,
        fort: FastFort,
        model: type,
        *,
        failures: bool = True,
        locate: Callable[[str], str] | None = None,
    ) -> None:
        self.fort = fort
        self.model = model
        self.failures = failures
        self.locate = locate
        self.key = default_model_key(model)
        self._writable: frozenset[str] = frozenset()

    # -- configuration ------------------------------------------------------

    def check(self) -> None:
        """Fail now if the model cannot hold a record.

        Called from `record_sign_ins`, so the sentence names the missing column
        while somebody is looking at the file that declares it.
        """
        spec = self.fort.backend.introspect(self.model, key=self.key)
        available = {field.name for field in spec}
        missing = [name for name in REQUIRED_FIELDS if name not in available]
        if missing:
            raise ConfigurationError(
                f"{self.model.__name__} cannot record sign-ins: it has no "
                f"{', '.join(missing)} column.",
                hint=(
                    "Give the model the columns of a sign-in record -- the "
                    "quickest way is to inherit fastfort.orm.sqlalchemy."
                    "SignInRecordMixin, which declares all of them."
                ),
            )
        # Only the columns that exist are written, so a narrower table is a
        # supported choice rather than a KeyError on the first sign-in.
        self._writable = frozenset(available & set(_FIELD_NAMES))

    def attach(self) -> None:
        self.fort.hooks.add(Hook.USER_LOGGED_IN, self.on_signed_in)
        if self.failures:
            self.fort.hooks.add(Hook.LOGIN_FAILED, self.on_failed)

    # -- listeners ----------------------------------------------------------

    async def on_signed_in(self, *, user: Any = None, request: Any = None, **_: Any) -> None:
        config = self.fort.user_config
        await self.write(
            self.build(
                request,
                successful=True,
                identity=config.identity_of(user) if user is not None else "",
                user_key=str(getattr(user, config.id_field, "")) if user is not None else "",
            )
        )

    async def on_failed(self, *, identity: str = "", request: Any = None, **_: Any) -> None:
        # The identity is stored as typed. It is not proof of an account, and a
        # password typed into the identity box by mistake is why this column is
        # worth reviewing rather than trusting.
        await self.write(self.build(request, successful=False, identity=identity))

    # -- writing ------------------------------------------------------------

    def build(self, request: Any, *, successful: bool, identity: str, user_key: str = "") -> SignIn:
        headers = dict(request.headers) if request is not None else {}
        # The same reading of the address the lockout and the rate limiter use.
        # Two answers to "who is this" would mean one of them is wrong, and the
        # address in the log has to be the one the defences acted on.
        address = (
            client_address(
                request.scope,
                forwarded_depth=self.fort.settings.security.effective_forwarded_depth,
            )
            if request is not None
            else ""
        )
        device = read_device(headers, address=address)
        return SignIn(
            at=dt.datetime.now(dt.UTC),
            successful=successful,
            address=address,
            identity=identity[:255],
            user_key=user_key,
            browser=device.browser,
            platform=device.platform,
            kind=device.kind,
            user_agent=device.user_agent,
            location=self._locate(address),
        )

    async def write(self, record: SignIn) -> None:
        """Store one record, in a transaction of its own.

        Its own, because the request's has already been decided by the time this
        runs -- and because a rolled-back sign-in should still leave the attempt
        in the log.
        """
        data = {name: value for name, value in asdict(record).items() if name in self._writable}
        try:
            async with self.fort.backend.unit_of_work() as uow:
                adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
                await adapter.create(data)
        except Exception:
            # Never re-raised. This runs inside the sign-in request, and an
            # audit table that is missing, full or locked must not be able to
            # keep the people who could fix it out of the admin.
            logger.exception("Could not record a sign-in for %s", record.identity or "an account")

    def _locate(self, address: str) -> str:
        """Where the address is, if the project said how to find out.

        FastFort ships no GeoIP database and calls no service: bundling one
        would be a hundred megabytes of data going stale in a wheel, and calling
        one would hand every administrator's address to a third party from
        inside the login handler. A project that has a database passes a
        function.
        """
        if not (self.locate and address):
            return ""
        try:
            return str(self.locate(address))[:120]
        except Exception:
            logger.exception("A locate() callback failed for %s", address)
            return ""


#: Every field a record can carry, taken from the dataclass so the two cannot
#: drift apart.
_FIELD_NAMES = tuple(SignIn.__dataclass_fields__)
