"""JWT access and refresh tokens for a project's own API.

`AuthSettings` has described these since the first release -- `access_token_ttl`,
`refresh_token_ttl`, `rotate_refresh_tokens`, `revoke_family_on_reuse`,
`algorithm`, `issuer`, `audience` -- and nothing read any of them. This is the
implementation those settings were always the configuration for.

The admin itself does not use any of it. A browser session is a signed cookie
(`sessions.py`) and should stay one: a token in `localStorage` is readable by any
script that gets onto the page, and a token in a cookie is a session cookie with
extra steps. What this is for is the API a project builds *beside* its admin --
the mobile client, the integration, the service account -- where there is no
cookie jar and no CSRF story, and where writing "issue a JWT" from scratch means
writing the four things below from scratch as well.

## Rotation, and the thing rotation is for

A refresh token is long-lived by definition, which makes it the credential worth
stealing. Rotation means each refresh mints a new one and retires the one used,
so a stolen token is useful exactly until the real client refreshes -- and then
one of the two presents a token that has already been spent.

That replay is the *detection*. It cannot be told apart from an attacker using a
stolen token, so the safe reading is that it was one, and the answer is to revoke
the whole family: every token descended from the same original sign-in. The
legitimate client is signed out and has to authenticate again, which is a real
cost, and it is a much smaller cost than an attacker holding a valid session.

This is why the refresh side needs a store and the access side does not. An
access token is verified by its signature alone -- stateless, no round trip, no
shared state between workers -- and is short-lived precisely so that being unable
to revoke it does not matter for long. A refresh token has to be checked against
what has already been spent, and that is a fact about the server.

The default store keeps that in memory, which is right for one process and wrong
for several: two workers each holding half the families means a token spent on
one is still fresh on the other, and the replay detection above quietly stops
working. `fastfort check --deploy` says so, and `RefreshTokenStore` is a protocol
so a project can back it with Redis or a table.
"""

from __future__ import annotations

import hmac
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import jwt

from fastfort.core.exceptions import SecurityError

if TYPE_CHECKING:
    from fastfort.core.settings import FastFortSettings

__all__ = [
    "InMemoryRefreshTokenStore",
    "RefreshRecord",
    "RefreshTokenStore",
    "TokenPair",
    "TokenService",
]

#: Domain separation, the same reason `sessions.py` has a salt: a value minted as
#: a refresh token must never verify as an access token, even though both are
#: signed with the same key. Carried as the `typ` claim and checked on every
#: verification -- without it, handing an access token to `/refresh` would work,
#: which is a fifteen-minute credential silently promoted to a fortnight-long one.
ACCESS = "access"
REFRESH = "refresh"

#: What `TokenService` asks about the account behind a refresh token: its
#: current stamp (a keyed digest of the password hash) while it exists and is
#: active, `None` once it does not or is not. Supplied by `FastFort`, which
#: knows the user model; a bare `TokenService` has none and checks only the
#: token, which is what it always did.
AccountStamp = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class TokenPair:
    """What a sign-in or a refresh hands back."""

    access_token: str
    refresh_token: str
    expires_in: int
    # The RFC 6750 scheme name, not a credential -- hence the exemption.
    token_type: str = "Bearer"  # noqa: S105

    def to_dict(self) -> dict[str, Any]:
        """The OAuth 2 response shape, which every HTTP client already knows."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
        }


@dataclass(frozen=True, slots=True)
class RefreshRecord:
    """One issued refresh token, as the store remembers it."""

    #: This token's own id. What `spend` is asked about.
    jti: str
    #: The sign-in every token in this chain descends from. What gets revoked
    #: when a spent token is replayed.
    family: str
    subject: str
    expires_at: float


class RefreshTokenStore(Protocol):
    """Which refresh tokens are still live.

    Deliberately small. Everything about *why* a token is being retired lives in
    `TokenService`; this only has to answer honestly and atomically.
    """

    async def remember(self, record: RefreshRecord) -> None:
        """Record a newly issued refresh token as live."""
        ...

    async def spend(self, jti: str) -> RefreshRecord | None:
        """Retire `jti` and return it, or `None` if it was not live.

        One step, not a read then a write. Two refreshes arriving together must
        not both find the same token live and both succeed -- that is the exact
        race the rotation exists to notice.
        """
        ...

    async def revoke_family(self, family: str) -> int:
        """Retire every token descended from one sign-in. Returns how many."""
        ...

    async def revoke_subject(self, subject: str) -> int:
        """Retire everything belonging to one user, on every device."""
        ...


@dataclass
class InMemoryRefreshTokenStore:
    """Per-process families. Correct for one worker; see the module docstring."""

    _records: dict[str, RefreshRecord] = field(default_factory=dict)

    async def remember(self, record: RefreshRecord) -> None:
        # Expired entries are dropped here rather than on a timer: this is the
        # only method that runs on a schedule an attacker does not choose, and a
        # sweep on `spend` would walk the whole table on the hot path.
        now = time.time()
        if len(self._records) > 512:
            self._records = {
                jti: kept for jti, kept in self._records.items() if kept.expires_at > now
            }
        self._records[record.jti] = record

    async def spend(self, jti: str) -> RefreshRecord | None:
        record = self._records.pop(jti, None)
        if record is None or record.expires_at <= time.time():
            return None
        return record

    async def revoke_family(self, family: str) -> int:
        doomed = [jti for jti, record in self._records.items() if record.family == family]
        for jti in doomed:
            del self._records[jti]
        return len(doomed)

    async def revoke_subject(self, subject: str) -> int:
        doomed = [jti for jti, record in self._records.items() if record.subject == subject]
        for jti in doomed:
            del self._records[jti]
        return len(doomed)


class TokenService:
    """Issues, verifies and rotates the pair.

    `signing_key` and `verifying_key` are separate so that an asymmetric
    algorithm works without a second class. With HS256 they are the same secret;
    with RS256 or EdDSA the first is the private key and the second is the public
    one, which is what lets another service verify a token this one minted
    without being able to mint one itself.
    """

    def __init__(
        self,
        settings: FastFortSettings,
        *,
        store: RefreshTokenStore | None = None,
        signing_key: str | None = None,
        verifying_key: str | None = None,
        account_stamp: AccountStamp | None = None,
    ) -> None:
        self._settings = settings
        self._auth = settings.auth
        self._store = store or InMemoryRefreshTokenStore()
        self._account_stamp = account_stamp
        secret = settings.secret_key.get_secret_value()
        self._signing_key = signing_key or secret
        self._verifying_key = verifying_key or signing_key or secret

        if self._auth.algorithm.startswith(("RS", "ES", "Ed")) and signing_key is None:
            raise SecurityError(
                f"auth.algorithm is {self._auth.algorithm!r}, which needs a key pair.",
                hint=(
                    "Pass signing_key= (the private key) and verifying_key= (the public "
                    "key) to TokenService. The secret key signs cookies and is not an "
                    "asymmetric key."
                ),
            )

    # -- issuing ------------------------------------------------------------

    async def issue(self, subject: str, **claims: Any) -> TokenPair:
        """A fresh pair for `subject`, starting a new family.

        `claims` are merged into the *access* token only. A refresh token is a
        bearer credential that lives for weeks; anything put in it is a copy of
        something that may have changed by the time it is used, and a role read
        out of a fortnight-old token is a permission that was revoked a fortnight
        ago and still works.
        """
        stamp = await self._account_stamp(subject) if self._account_stamp else None
        return await self._mint(
            subject,
            family=uuid.uuid4().hex,
            claims=claims,
            signed_in_at=int(time.time()),
            stamp=stamp,
        )

    async def refresh(self, token: str) -> TokenPair:
        """Exchange a refresh token for a new pair, retiring the one presented.

        Raises `SecurityError` if the token is invalid, expired, or has already
        been spent -- and in that last case revokes everything descended from the
        same sign-in first. See the module docstring for why that is the right
        response to a replay rather than an overreaction to one.
        """
        payload = self._decode(token, expect=REFRESH)
        jti = str(payload.get("jti", ""))
        subject = str(payload.get("sub", ""))
        family = str(payload.get("fam", "")) or uuid.uuid4().hex
        # A token minted before 1.2 has no `ath`; its own `iat` is the closest
        # honest reading, and it means an upgrade signs no API client out.
        signed_in_at = int(payload.get("ath", payload.get("iat", 0)))

        # Before the store is consulted, so a refusal here spends nothing an
        # honest client might still need -- and before the rotation branch, so
        # turning rotation off gives up replay detection and nothing else.
        if signed_in_at + self._auth.effective_refresh_max_age <= time.time():
            await self._store.revoke_family(family)
            raise SecurityError(
                "This sign-in is too old to refresh.",
                hint="Sign in again with a password; refresh_token_max_age has passed.",
            )
        stamp = await self._check_account(subject, payload, family)

        if not self._auth.rotate_refresh_tokens:
            # No rotation means no spending, so there is nothing to replay and
            # nothing to detect. The token stays valid until it expires.
            return await self._mint(
                subject, family=family, claims={}, signed_in_at=signed_in_at, stamp=stamp
            )

        record = await self._store.spend(jti)
        if record is None:
            # Either already spent or never issued by this store. Both readings
            # are indistinguishable from theft, so both are treated as theft.
            if self._auth.revoke_family_on_reuse:
                await self._store.revoke_family(family)
            raise SecurityError(
                "That refresh token has already been used.",
                hint=(
                    "Every session for this account has been signed out, because a "
                    "token being presented twice is what a stolen one looks like. "
                    "Sign in again."
                ),
            )

        return await self._mint(
            record.subject,
            family=record.family,
            claims={},
            signed_in_at=signed_in_at,
            stamp=stamp,
        )

    async def _check_account(
        self, subject: str, payload: dict[str, Any], family: str
    ) -> str | None:
        """Refuse a refresh for an account that is gone, closed, or re-keyed.

        Rotation answers "has this token been spent?" and nothing else, so in
        1.1.1 a refresh token went on minting access tokens for an account that
        had been deactivated or deleted, and survived a password change -- the
        one thing a person does when they think a credential was stolen. The
        stamp is the same keyed digest of the password hash the browser session
        carries, so both kinds of session end on the same event, however the
        password was changed.

        The family is revoked rather than the request merely declined: an
        account reactivated tomorrow must not get yesterday's session back.
        """
        if self._account_stamp is None:
            return None
        current = await self._account_stamp(subject)
        issued = payload.get("stp")
        stale = current is None or (
            isinstance(issued, str) and not hmac.compare_digest(issued, current)
        )
        if stale:
            await self._store.revoke_family(family)
            # The same sentence for every reason. Saying "deactivated" or
            # "password changed" tells whoever holds a stolen token which it was.
            raise SecurityError(
                "That refresh token no longer identifies an active account.",
                hint="Sign in again.",
            )
        return current

    async def _mint(
        self,
        subject: str,
        *,
        family: str,
        claims: dict[str, Any],
        signed_in_at: int,
        stamp: str | None,
    ) -> TokenPair:
        now = int(time.time())
        access_ttl = self._auth.access_token_ttl
        # Capped at the chain's maximum age, so the last token of a chain
        # expires when the chain does rather than a full TTL past it.
        refresh_expires = min(
            now + self._auth.refresh_token_ttl,
            signed_in_at + self._auth.effective_refresh_max_age,
        )
        refresh_jti = uuid.uuid4().hex

        access = self._encode(
            {
                **claims,
                "sub": subject,
                "typ": ACCESS,
                "iat": now,
                "exp": now + access_ttl,
                # Its own id, so an access token can be named in an audit record
                # without the record having to hold the token itself.
                "jti": secrets.token_urlsafe(12),
            }
        )
        refresh_claims: dict[str, Any] = {
            "sub": subject,
            "typ": REFRESH,
            "iat": now,
            "exp": refresh_expires,
            "jti": refresh_jti,
            "fam": family,
            # When the password was last presented. Carried unchanged through
            # every rotation; see `refresh_token_max_age`.
            "ath": signed_in_at,
        }
        if stamp is not None:
            refresh_claims["stp"] = stamp
        refresh = self._encode(refresh_claims)

        if self._auth.rotate_refresh_tokens:
            await self._store.remember(
                RefreshRecord(
                    jti=refresh_jti,
                    family=family,
                    subject=subject,
                    expires_at=refresh_expires,
                )
            )

        return TokenPair(access_token=access, refresh_token=refresh, expires_in=access_ttl)

    # -- verifying ----------------------------------------------------------

    def verify(self, token: str) -> dict[str, Any]:
        """The claims in an access token, or `SecurityError`.

        Synchronous and stateless on purpose -- this is the call on the hot path
        of every authenticated request, and it must not become a round trip.
        """
        return self._decode(token, expect=ACCESS)

    # -- revoking -----------------------------------------------------------

    async def revoke(self, refresh_token: str) -> None:
        """Sign out the one session that token belongs to.

        Signing out does not fail on a token that is already invalid: the
        outcome the caller asked for is "this is not usable", and it is not.
        """
        try:
            payload = self._decode(refresh_token, expect=REFRESH)
        except SecurityError:
            return
        await self._store.revoke_family(str(payload.get("fam", "")))

    async def revoke_all(self, subject: str) -> int:
        """Sign `subject` out everywhere. For a password change, or a lost phone."""
        return await self._store.revoke_subject(subject)

    # -- the JWT itself -----------------------------------------------------

    def _encode(self, payload: dict[str, Any]) -> str:
        if self._auth.issuer:
            payload["iss"] = self._auth.issuer
        if self._auth.audience:
            payload["aud"] = self._auth.audience
        return jwt.encode(payload, self._signing_key, algorithm=self._auth.algorithm)

    def _decode(self, token: str, *, expect: str) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._verifying_key,
                # A list of exactly the one algorithm configured. Passing the
                # token's own `alg` header back to the decoder is the oldest JWT
                # vulnerability there is: `alg: none` verifies anything, and
                # `alg: HS256` against an RSA public key lets anyone holding that
                # public key -- which is public -- sign their own tokens.
                algorithms=[self._auth.algorithm],
                issuer=self._auth.issuer,
                audience=self._auth.audience,
                options={
                    "require": ["exp", "sub", "typ"],
                    "verify_aud": bool(self._auth.audience),
                },
            )
        except jwt.ExpiredSignatureError as error:
            raise SecurityError(
                "That token has expired.",
                hint="Use the refresh token to get a new one.",
            ) from error
        except jwt.InvalidTokenError as error:
            # One message for every other failure. Saying which check failed
            # tells whoever is holding a forged token how to make a better one.
            raise SecurityError("That token is not valid.") from error

        if payload.get("typ") != expect:
            raise SecurityError(
                f"That is not {'an access' if expect == ACCESS else 'a refresh'} token.",
                hint=(
                    "Access tokens authenticate a request; refresh tokens only "
                    "exchange for a new pair. They are not interchangeable."
                ),
            )
        return payload
