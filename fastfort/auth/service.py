"""Signing in and out of the admin.

The two rules that shape this module:

*Failures are indistinguishable.* Wrong password, unknown address, inactive
account, not a staff member -- all produce the same message and the same amount
of work. Telling them apart hands an attacker a user enumeration oracle for free.

*The cost is paid either way.* A password is verified even when no user was found,
against a fixed dummy hash, so response time does not reveal which addresses are
registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastfort.core.exceptions import SecurityError
from fastfort.core.hooks import Hook
from fastfort.spec import Filter, FilterOperator, ListQuery

from .csrf import CsrfProtection
from .lockout import Lockout, LockoutStore
from .passwords import hash_password, needs_rehash, verify_password
from .sessions import SessionCodec

if TYPE_CHECKING:
    from starlette.requests import Request

    from fastfort.core.app import FastFort

__all__ = ["AdminAuth", "AuthResult"]

#: One message for every failure mode. Anything more specific is an oracle.
GENERIC_FAILURE = "Those credentials do not match an account that can use the admin."


@dataclass(frozen=True, slots=True)
class AuthResult:
    """A successful sign-in."""

    user: Any
    session_cookie: str


class AdminAuth:
    """Authentication for the admin, built over whatever backend is configured."""

    def __init__(self, fort: FastFort, *, lockout_store: LockoutStore | None = None) -> None:
        self._fort = fort
        self.sessions = SessionCodec(fort.settings)
        self.csrf = CsrfProtection(fort.settings)
        self.lockout = Lockout(fort.settings.auth, lockout_store)

    # -- signing in ---------------------------------------------------------

    async def authenticate(
        self, *, identity: str, password: str, address: str, request: Request | None = None
    ) -> AuthResult:
        """Verify credentials, or raise `SecurityError` with a generic message."""
        state = await self.lockout.check(address=address, identity=identity)
        if state.locked:
            raise SecurityError(
                "Too many failed attempts. Try again shortly.",
                hint=f"Wait about {state.retry_after} seconds before trying again.",
            )

        user = await self._find_user(identity)
        config = self._fort.user_config

        # Verified unconditionally: skipping it when `user is None` is what turns
        # response time into a list of registered addresses.
        stored_hash = config.password_hash_of(user) if user is not None else ""
        correct = verify_password(password, stored_hash)

        allowed = user is not None and correct and config.is_active(user) and config.is_staff(user)
        if not allowed:
            await self.lockout.record_failure(address=address, identity=identity)
            await self._fort.hooks.emit(
                Hook.LOGIN_FAILED, request=request, identity=identity, reason="rejected"
            )
            raise SecurityError(GENERIC_FAILURE)

        await self.lockout.record_success(address=address, identity=identity)

        # Raising the cost parameters should upgrade accounts as people sign in,
        # rather than never.
        if needs_rehash(stored_hash):
            await self._store_password_hash(user, hash_password(password))
            stored_hash = config.password_hash_of(user)

        await self._fort.hooks.emit(Hook.USER_LOGGED_IN, request=request, user=user)
        return AuthResult(
            user=user,
            session_cookie=self.sessions.issue(
                user_id=getattr(user, config.id_field), password_hash=stored_hash
            ),
        )

    # -- reading the current user -------------------------------------------

    async def current_user(self, request: Request) -> Any | None:
        """The signed-in user, or None.

        Returns None for a tampered, expired or stale cookie alike: the response
        in every case is the login page.
        """
        session = self.sessions.read(request.cookies.get(self._fort.settings.security.cookie_name))
        if session is None:
            return None

        user = await self._get_user(session.user_id)
        if user is None:
            return None

        config = self._fort.user_config
        if not (config.is_active(user) and config.is_staff(user)):
            return None

        # The stamp is what makes a password change log every device out.
        if not self.sessions.matches(session, config.password_hash_of(user)):
            return None

        return user

    async def set_password(self, user: Any, password: str) -> None:
        """Store a new password hash, invalidating every existing session."""
        await self._store_password_hash(user, hash_password(password))
        await self._fort.hooks.emit(Hook.PASSWORD_CHANGED, request=None, user=user)

    # -- request helpers ----------------------------------------------------

    def client_address(self, request: Request) -> str:
        """The address to rate-limit against.

        `X-Forwarded-For` is only trusted when the deployment says a proxy sets
        it. Trusting it unconditionally would let any client send a fresh address
        per attempt and never be locked out.
        """
        if self._fort.settings.security.trust_forwarded_for:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        client = request.client
        return client.host if client else "unknown"

    # -- storage ------------------------------------------------------------

    async def _find_user(self, identity: str) -> Any | None:
        """Look a user up by the configured identity field."""
        config = self._fort.user_config
        cleaned = identity.strip()
        if not cleaned:
            return None

        async with self._fort.backend.unit_of_work() as uow:
            adapter = self._fort.backend.adapter(config.model, uow, key="fastfort.user")
            page = await adapter.list(
                ListQuery(
                    filters=(Filter(config.identity_field, FilterOperator.IEXACT, cleaned),),
                    page_size=2,
                )
            )
        # Exactly one match, or nothing. Two would mean the identity column is not
        # unique, which is a data problem we must not resolve by guessing.
        return page.items[0] if len(page.items) == 1 else None

    async def _get_user(self, user_id: str) -> Any | None:
        config = self._fort.user_config
        async with self._fort.backend.unit_of_work() as uow:
            adapter = self._fort.backend.adapter(config.model, uow, key="fastfort.user")
            spec = adapter.spec
            try:
                coerced = _coerce_key(user_id, spec.field(config.id_field))
            except KeyError:
                return None
            return await adapter.get((coerced,))

    async def _store_password_hash(self, user: Any, hashed: str) -> None:
        config = self._fort.user_config
        async with self._fort.backend.unit_of_work() as uow:
            adapter = self._fort.backend.adapter(config.model, uow, key="fastfort.user")
            merged = await adapter.get((getattr(user, config.id_field),))
            if merged is not None:
                setattr(merged, config.password_field, hashed)
        setattr(user, config.password_field, hashed)


def _coerce_key(raw: str, field: Any) -> Any:
    """Turn a cookie's string user id back into the column's type."""
    from fastfort.spec import FieldType

    if field.type in {FieldType.INTEGER, FieldType.BIGINT}:
        try:
            return int(raw)
        except ValueError:
            return raw
    if field.type is FieldType.UUID:
        import uuid

        try:
            return uuid.UUID(raw)
        except ValueError:
            return raw
    return raw
