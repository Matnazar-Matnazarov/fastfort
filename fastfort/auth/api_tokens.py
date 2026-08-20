"""Personal access tokens: long-lived credentials for things that are not browsers.

`api.py` already issues JWTs from a password, which is the right shape for a
mobile client with a person behind it. It is the wrong shape for a cron job, a
deployment script or a partner integration: those have no password to type, no
way to refresh at three in the morning, and no person to notice when a refresh
fails. What they need is one long-lived string, revocable on its own, that
answers for itself.

Turned on the way every other FastFort feature that needs a table is::

    class ApiToken(ApiTokenMixin, Base):
        __tablename__ = "admin_api_token"


    fort.enable_api_tokens(ApiToken)

## Why SHA-256 and not Argon2

`passwords.py` uses Argon2, and using it here too would look consistent. It
would also be a mistake.

A work factor exists to make *guessing* expensive, and guessing is only a
threat when the secret is guessable -- a human password drawn from a small,
skewed distribution. These tokens are 32 bytes from `secrets.token_urlsafe`:
256 bits of uniform entropy, with no dictionary to try and no distribution to
exploit. There is nothing for a work factor to slow down.

What a work factor would do is cost every authenticated request the ~100ms
Argon2 is tuned to take, on the server, per call. A single-digit request rate
would saturate a core. So the digest here is a plain SHA-256 -- fast, and
sufficient precisely because the input is not a password.

The comparison is still constant-time. Not because a timing attack on a hash
lookup is plausible, but because the alternative is a `==` that somebody later
has to reason about.

## What is stored, and what is not

The secret exists in readable form exactly once: in the return value of
`issue`. It is never written to the database, never logged, and cannot be
recovered -- a token whose secret was lost is replaced, not looked up. What the
row keeps is the digest and a short prefix, and the prefix is there so a person
can match a row against the string in their configuration file without the row
being enough to authenticate with.

## What this does not decide

Scopes are stored and returned as the project wrote them. FastFort takes no
view on what `orders:read` means, because a scope vocabulary belongs to the API
being protected rather than to the admin that issues credentials for it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastfort.core.exceptions import ConfigurationError, ValidationError
from fastfort.core.registry import default_model_key
from fastfort.orm.coerce import coerce_filter_value
from fastfort.spec import Filter, FilterOperator, ListQuery

if TYPE_CHECKING:
    from fastfort.core.app import FastFort

__all__ = ["ApiTokens", "IssuedToken", "hash_token", "token_prefix"]

#: Bytes of entropy in a new secret. 32 gives a 43-character URL-safe string;
#: raising it costs nothing and gains nothing, because 256 bits is already
#: past the point where the number is the weakest thing in the system.
TOKEN_BYTES = 32

#: How much of the secret is kept in the clear. Long enough to identify a row
#: among a handful, far too short to be worth attacking: the remaining ~220
#: bits are still uniform.
PREFIX_LENGTH = 8

#: Columns the model must have. Checked when `enable_api_tokens` is called, so
#: a mistyped schema is a start-up error rather than a 500 on the first
#: authenticated request.
REQUIRED_FIELDS = ("token_hash", "user_key", "revoked_at", "expires_at")


def hash_token(secret: str) -> str:
    """The digest stored for a secret. See the module docstring for why this
    is SHA-256 and not a password hash."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def token_prefix(secret: str) -> str:
    return secret[:PREFIX_LENGTH]


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A token, and the one moment its secret is readable.

    `secret` is not on the row and cannot be fetched again. Whatever receives
    this is the last thing that can show it to anyone.
    """

    obj: Any
    secret: str


class ApiTokens:
    """Issue, resolve and revoke tokens against the project's own table."""

    def __init__(self, fort: FastFort, model: type[Any]) -> None:
        self.fort = fort
        self.model = model
        self.key = default_model_key(model)

    # -- configuration ------------------------------------------------------

    def check(self) -> None:
        """Fail now if the model cannot hold a token.

        Called from `enable_api_tokens`, so the sentence names the missing
        column while somebody is looking at the file that declares it.
        """
        spec = self.fort.backend.introspect(self.model, key=self.key)
        available = {field.name for field in spec}
        missing = [name for name in REQUIRED_FIELDS if name not in available]
        if missing:
            raise ConfigurationError(
                f"{self.model.__name__} cannot hold API tokens: it has no "
                f"{', '.join(missing)} column.",
                hint=(
                    "Give the model the columns of a token -- the quickest way "
                    "is to inherit fastfort.orm.sqlalchemy.ApiTokenMixin, which "
                    "declares all of them."
                ),
            )

    # -- issuing ------------------------------------------------------------

    async def issue(
        self,
        *,
        user: Any,
        name: str = "",
        scopes: str = "",
        expires_at: dt.datetime | None = None,
    ) -> IssuedToken:
        """Mint a token for `user` and return it with its readable secret.

        Runs in its own unit of work: a token is worth nothing until it is
        durable, and handing back a secret for a row that then rolled back
        would be handing back a credential that does not work.
        """
        secret = secrets.token_urlsafe(TOKEN_BYTES)

        async with self.fort.backend.unit_of_work() as uow:
            # The owner's key comes from the adapter rather than from an
            # attribute guess: a composite primary key joins on "~", which is
            # the same spelling the admin's URLs use.
            user_model = self.fort.user_config.model
            owner = self.fort.backend.adapter(user_model, uow, key=default_model_key(user_model))
            user_key = "~".join(str(part) for part in owner.primary_key_of(user))

            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            created = await adapter.create(
                {
                    "name": name,
                    "token_hash": hash_token(secret),
                    "prefix": token_prefix(secret),
                    "user_key": user_key,
                    "scopes": scopes,
                    "expires_at": expires_at,
                }
            )
            await uow.commit()

        return IssuedToken(obj=created, secret=secret)

    # -- resolving ----------------------------------------------------------

    async def resolve(self, secret: str, *, touch: bool = True) -> Any | None:
        """The account a secret belongs to, or `None`.

        `None` covers every refusal on purpose -- unknown, revoked, expired,
        or owned by an account that has since been deleted. Telling those apart
        in the response tells an attacker which of their guesses was a real
        token, and none of the four is something the caller can act on
        differently.
        """
        if not secret:
            return None

        digest = hash_token(secret)
        now = dt.datetime.now(dt.UTC)

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            # The digest is unique, so one row is the whole answer -- and the
            # filter goes through `Filter` rather than a hand-built query
            # because that is the only path an adapter offers, and it is the
            # same one the admin's list view takes.
            page = await adapter.list(
                ListQuery(
                    filters=(Filter("token_hash", FilterOperator.EXACT, digest),), page_size=1
                )
            )
            row = page.items[0] if page.items else None

            if row is None:
                return None
            # Constant-time even though the digest was the lookup key: the
            # alternative is a `==` somebody later has to reason about.
            if not secrets.compare_digest(str(row.token_hash), digest):
                return None
            if row.revoked_at is not None:
                return None
            if row.expires_at is not None and _aware(row.expires_at) <= now:
                return None

            user = await self._owner(row)
            if user is None:
                return None

            if touch:
                # Best-effort: a token that authenticated must not be refused
                # because recording that fact failed.
                await adapter.update(row, {"last_used_at": now})
                await uow.commit()

        return user

    async def revoke(self, obj: Any) -> bool:
        """Stop a token working, keeping the row.

        The row is fetched again rather than updated where it stands. Whatever
        is passed in came from a unit of work that has since closed -- `issue`
        commits and returns, so its instance is detached -- and writing through
        a detached instance updates nothing while reporting success. Its
        attributes are still readable, which is enough to find the live row.

        Returns whether there was still a row to revoke.
        """
        primary = tuple(
            getattr(obj, name)
            for name in self.fort.backend.introspect(self.model, key=self.key).primary_key
        )

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            row = await adapter.get(primary)
            if row is None:
                return False
            await adapter.update(row, {"revoked_at": dt.datetime.now(dt.UTC)})
            await uow.commit()
        return True

    # -- internals ----------------------------------------------------------

    async def _owner(self, row: Any) -> Any | None:
        """The account named by `user_key`, or `None` if it is gone."""
        user_model = self.fort.user_config.model
        key = default_model_key(user_model)
        spec = self.fort.backend.introspect(user_model, key=key)
        raw = str(getattr(row, "user_key", "") or "")
        if not raw:
            return None

        parts = raw.split("~")
        if len(parts) != len(spec.primary_key):
            return None

        try:
            primary = tuple(
                coerce_filter_value(spec.get(name), part, name)
                for name, part in zip(spec.primary_key, parts, strict=True)
            )
        except (ValidationError, ValueError, TypeError):
            # The column changed type under a stored key, which is a refusal
            # rather than a crash: the token simply no longer resolves.
            return None

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(user_model, uow, key=key)
            return await adapter.get(primary)


def _aware(value: dt.datetime) -> dt.datetime:
    """SQLite hands back naive datetimes even from a `timezone=True` column, so
    a comparison against an aware `now` would raise rather than answer."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
