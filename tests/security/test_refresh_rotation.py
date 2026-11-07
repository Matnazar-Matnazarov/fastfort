"""Refresh-token rotation, against the account it was issued for.

Rotation alone answers one question -- has this exact token been spent? -- and
1.1.1 asked nothing else. Three consequences, each a test below:

* a refresh token kept working for an account that had been **deactivated or
  deleted**, minting fresh access tokens for anybody who checked them with
  `fort.tokens.verify` rather than through `bearer_user`;
* it kept working after the **password was changed**, which is the one thing a
  person does when they think a credential has been stolen -- the browser
  sessions were signed out by the password stamp and the API sessions were not;
* and the chain had **no end**: each rotation set a fresh expiry, so a stolen
  token refreshed by the thief before the real client next refreshed lived for
  as long as the thief kept refreshing it.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import FastAPI
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.orm.models import StaffUser

from fastfort import FastFort, FastFortSettings
from fastfort.auth.tokens import InMemoryRefreshTokenStore, RefreshRecord, TokenService
from fastfort.core.exceptions import SecurityError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


def make_fort(backend: SQLAlchemyBackend, **auth: Any) -> FastFort:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            auth={"api_enabled": True, **auth},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    return fort


@pytest.fixture
async def fort(backend: SQLAlchemyBackend, staff_user: StaffUser) -> FastFort:
    return make_fort(backend)


@pytest.fixture
async def client(fort: FastFort) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    fort.mount(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        yield opened


async def sign_in(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/auth/token", data={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def refresh(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/auth/refresh", json={"refresh_token": token})


async def update_user(fort: FastFort, user: StaffUser, **values: Any) -> None:
    async with fort.backend.unit_of_work() as uow:
        adapter = fort.backend.adapter(StaffUser, uow)
        row = await adapter.require((user.id,))
        await adapter.update(row, values)


# ---------------------------------------------------------------------------
# The account behind the token
# ---------------------------------------------------------------------------


async def test_a_deactivated_account_cannot_refresh(
    fort: FastFort, client: httpx.AsyncClient, staff_user: StaffUser
) -> None:
    tokens = await sign_in(client)
    await update_user(fort, staff_user, is_active=False)

    assert (await refresh(client, tokens["refresh_token"])).status_code == 401


async def test_a_deleted_account_cannot_refresh(
    fort: FastFort, client: httpx.AsyncClient, staff_user: StaffUser
) -> None:
    tokens = await sign_in(client)
    async with fort.backend.unit_of_work() as uow:
        adapter = fort.backend.adapter(StaffUser, uow)
        await adapter.delete(await adapter.require((staff_user.id,)))

    assert (await refresh(client, tokens["refresh_token"])).status_code == 401


async def test_a_password_change_signs_api_sessions_out(
    fort: FastFort, client: httpx.AsyncClient, staff_user: StaffUser
) -> None:
    """However the password changed: here through the adapter, which is the path
    the admin's own form takes, not through `set_password`."""
    tokens = await sign_in(client)
    await update_user(fort, staff_user, hashed_password="$argon2id$v=19$changed")

    assert (await refresh(client, tokens["refresh_token"])).status_code == 401


async def test_a_refused_refresh_retires_the_family(
    fort: FastFort, client: httpx.AsyncClient, staff_user: StaffUser
) -> None:
    """Reactivating the account must not bring the old session back: the
    family was revoked when the refresh was refused, not merely declined."""
    tokens = await sign_in(client)
    await update_user(fort, staff_user, is_active=False)
    assert (await refresh(client, tokens["refresh_token"])).status_code == 401

    await update_user(fort, staff_user, is_active=True)
    assert (await refresh(client, tokens["refresh_token"])).status_code == 401


async def test_an_ordinary_refresh_still_works(client: httpx.AsyncClient) -> None:
    tokens = await sign_in(client)
    for _ in range(3):
        response = await refresh(client, tokens["refresh_token"])
        assert response.status_code == 200, response.text
        tokens = response.json()


async def test_without_rotation_the_account_is_still_checked(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Turning rotation off gives up replay detection, not the account check."""
    fort = make_fort(backend, rotate_refresh_tokens=False)
    app = FastAPI()
    fort.mount(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        tokens = await sign_in(opened)
        await update_user(fort, staff_user, is_active=False)

        assert (await refresh(opened, tokens["refresh_token"])).status_code == 401


# ---------------------------------------------------------------------------
# An end to the chain
# ---------------------------------------------------------------------------


def _claims(token: str) -> dict[str, Any]:
    return dict(jwt.decode(token, options={"verify_signature": False}))


async def test_a_rotated_token_keeps_the_original_sign_in_time() -> None:
    service = TokenService(FastFortSettings(secret_key=SECRET))  # type: ignore[call-arg]
    first = await service.issue("1")
    second = await service.refresh(first.refresh_token)

    assert _claims(second.refresh_token)["ath"] == _claims(first.refresh_token)["ath"]


def _signed(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, SECRET, algorithm="HS256")


async def test_no_rotation_outlives_the_maximum_age() -> None:
    """Tokens crafted at the right age rather than by patching the clock: PyJWT
    checks `iat` and `exp` against the real one, so a patched `time.time` would
    only test that PyJWT disagrees with it."""
    settings = FastFortSettings(  # type: ignore[call-arg]
        secret_key=SECRET,
        auth={"refresh_token_ttl": 3600, "refresh_token_max_age": 7200},  # type: ignore[arg-type]
    )
    store = InMemoryRefreshTokenStore()
    service = TokenService(settings, store=store)
    now = int(time.time())

    # Signed in 100 minutes ago: one more rotation is allowed, but the token it
    # mints may not outlive the two hours since sign-in, though its TTL would.
    await store.remember(RefreshRecord("j1", "f1", "1", now + 3000))
    young = _signed(
        {
            "sub": "1",
            "typ": "refresh",
            "iat": now - 60,
            "exp": now + 3000,
            "jti": "j1",
            "fam": "f1",
            "ath": now - 6000,
        }
    )
    renewed = await service.refresh(young)
    assert _claims(renewed.refresh_token)["exp"] <= now - 6000 + 7200

    # Signed in more than two hours ago: refused, however fresh the token is,
    # and the family goes with it.
    await store.remember(RefreshRecord("j2", "f2", "1", now + 3000))
    old = _signed(
        {
            "sub": "1",
            "typ": "refresh",
            "iat": now - 60,
            "exp": now + 3000,
            "jti": "j2",
            "fam": "f2",
            "ath": now - 7300,
        }
    )
    with pytest.raises(SecurityError, match="too old"):
        await service.refresh(old)
    assert await store.spend("j2") is None


def test_the_maximum_age_cannot_be_shorter_than_one_token() -> None:
    settings = FastFortSettings(  # type: ignore[call-arg]
        secret_key=SECRET,
        auth={"refresh_token_ttl": 7200, "refresh_token_max_age": 3600},  # type: ignore[arg-type]
    )

    assert settings.auth.effective_refresh_max_age == 7200


async def test_a_token_from_1_1_1_without_a_sign_in_time_is_still_accepted() -> None:
    """Tokens minted before the upgrade carry no `ath`. They are read as starting
    at their own `iat`, so an upgrade does not sign every API client out."""
    service = TokenService(FastFortSettings(secret_key=SECRET))  # type: ignore[call-arg]
    pair = await service.issue("1")
    claims = _claims(pair.refresh_token)
    del claims["ath"]
    legacy = jwt.encode(claims, SECRET, algorithm="HS256")

    renewed = await service.refresh(legacy)
    assert _claims(renewed.refresh_token)["ath"] == claims["iat"]
