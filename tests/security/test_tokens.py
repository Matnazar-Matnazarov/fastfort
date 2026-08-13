"""JWT access and refresh tokens, and the endpoints that hand them out.

`AuthSettings` described all of this from the first release and nothing read any
of it, so these are the tests that make the settings true.

The ones worth reading are the refusals. A token layer that issues and verifies
is half an hour's work; what takes the other half is being sure a refresh token
cannot be used as an access token, that `alg: none` is not accepted, that a
replayed refresh token takes the whole family down with it, and that a
deactivated account stops working before its access token expires.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
from fastapi import Depends, FastAPI
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.auth import bearer_user
from fastfort.auth.tokens import InMemoryRefreshTokenStore, TokenService
from fastfort.core.exceptions import SecurityError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"


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
    fort.register(Product, ProductAdmin, key="shop.product")
    return fort


def build(backend: SQLAlchemyBackend, **auth: Any) -> FastAPI:
    fort = make_fort(backend, **auth)
    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as opened:
        yield opened


@pytest.fixture
def service() -> TokenService:
    return TokenService(FastFortSettings(secret_key=SECRET))  # type: ignore[call-arg]


async def get_tokens(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/auth/token", data={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


async def test_a_password_buys_a_pair_of_tokens(client: httpx.AsyncClient) -> None:
    body = await get_tokens(client)

    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["access_token"] != body["refresh_token"]


async def test_the_response_is_the_shape_every_client_already_knows(
    client: httpx.AsyncClient,
) -> None:
    """OAuth 2's field names, so a generated client and a `curl` example from any
    other API both work without translation."""
    assert set(await get_tokens(client)) == {
        "access_token",
        "refresh_token",
        "token_type",
        "expires_in",
    }


async def test_a_wrong_password_is_a_401_that_says_nothing(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/auth/token", data={"identity": ADMIN_EMAIL, "password": "not-it"}
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    # The same message an unknown address gets: whether the account exists is not
    # something an unauthenticated request is entitled to learn.
    unknown = await client.post(
        "/auth/token", data={"identity": "nobody@example.com", "password": "not-it"}
    )
    assert unknown.json()["detail"] == response.json()["detail"]


async def test_the_api_is_not_mounted_unless_it_is_asked_for(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Adding public endpoints to somebody else's application is not a decision a
    library gets to make quietly."""
    fort = FastFort(
        FastFortSettings(secret_key=SECRET),  # type: ignore[call-arg]
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        assert (await opened.post("/auth/token", data={})).status_code == 404


async def test_me_reports_who_the_access_token_is(client: httpx.AsyncClient) -> None:
    tokens = await get_tokens(client)
    response = await client.get(
        "/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert response.status_code == 200
    assert response.json()["identity"] == ADMIN_EMAIL


async def test_me_publishes_three_fields_and_no_more(client: httpx.AsyncClient) -> None:
    """A project's user model is its own. A framework guessing which of its
    columns are safe to publish is one that eventually publishes a reset token."""
    tokens = await get_tokens(client)
    body = (
        await client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    ).json()
    assert set(body) == {"id", "identity", "is_staff"}


async def test_no_token_is_a_401_not_a_403(client: httpx.AsyncClient) -> None:
    """403 is "you may not", which sends a client looking for a permission
    problem. "You did not say who you are" is 401."""
    response = await client.get("/auth/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# Refresh, rotation, and what a replay means
# ---------------------------------------------------------------------------


async def test_a_refresh_token_buys_a_new_pair(client: httpx.AsyncClient) -> None:
    first = await get_tokens(client)
    response = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})

    assert response.status_code == 200
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"], "rotation means a new one each time"


async def test_a_spent_refresh_token_stops_working(client: httpx.AsyncClient) -> None:
    first = await get_tokens(client)
    await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})

    replay = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401


async def test_a_replay_signs_the_whole_family_out(client: httpx.AsyncClient) -> None:
    """The important one, and the reason rotation is worth the bookkeeping.

    A token presented twice cannot be told apart from a stolen one being used
    alongside the real client, so it is treated as one. The legitimate holder is
    signed out and has to authenticate again -- a real cost, and a much smaller
    one than an attacker keeping a live session.
    """
    first = await get_tokens(client)
    second = (
        await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    ).json()

    # The attacker replays the token they stole.
    replayed = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replayed.status_code == 401

    # And the real client's *current* token, which was still perfectly valid a
    # moment ago, is now gone too. That is the point.
    honest = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert honest.status_code == 401


async def test_logging_out_retires_the_family(client: httpx.AsyncClient) -> None:
    tokens = await get_tokens(client)

    assert (
        await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 204
    assert (
        await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    ).status_code == 401


async def test_logging_out_twice_is_not_an_error(client: httpx.AsyncClient) -> None:
    """The caller asked for the token to stop working, and it does not work. An
    error would only tell somebody holding a stolen one whether it was live."""
    tokens = await get_tokens(client)
    for _ in range(2):
        response = await client.post(
            "/auth/logout", json={"refresh_token": tokens["refresh_token"]}
        )
        assert response.status_code == 204


async def test_rotation_can_be_turned_off(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Without rotation there is nothing spent and therefore nothing to replay:
    the token stays usable until it expires. Worth having as an option, and worth
    a deployment warning, which `deployment_issues` already gives it."""
    app = build(backend, rotate_refresh_tokens=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        tokens = await get_tokens(opened)
        for _ in range(3):
            again = await opened.post(
                "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            assert again.status_code == 200


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------


async def test_a_refresh_token_is_not_an_access_token(service: TokenService) -> None:
    """Without the `typ` claim, handing a refresh token to an authenticated route
    would work -- a fortnight-long credential silently accepted where a
    fifteen-minute one was meant."""
    pair = await service.issue("7")

    with pytest.raises(SecurityError, match="not an access token"):
        service.verify(pair.refresh_token)


async def test_an_access_token_cannot_be_refreshed(service: TokenService) -> None:
    """And the other direction: exchanging a short token for a long one would
    make the short lifetime decorative."""
    pair = await service.issue("7")

    with pytest.raises(SecurityError, match="not a refresh token"):
        await service.refresh(pair.access_token)


async def test_the_none_algorithm_is_refused(service: TokenService) -> None:
    """The oldest JWT vulnerability there is: a decoder that believes the token's
    own `alg` header accepts a token signed with nothing at all."""
    forged = jwt.encode(
        {"sub": "7", "typ": "access", "exp": int(time.time()) + 3600},
        key="",
        algorithm="none",
    )
    with pytest.raises(SecurityError):
        service.verify(forged)


async def test_a_token_signed_with_another_key_is_refused(service: TokenService) -> None:
    forged = jwt.encode(
        {"sub": "7", "typ": "access", "exp": int(time.time()) + 3600},
        key="a-completely-different-signing-key",
        algorithm="HS256",
    )
    with pytest.raises(SecurityError):
        service.verify(forged)


async def test_an_expired_token_says_so(backend: SQLAlchemyBackend) -> None:
    """Distinguished from every other failure on purpose: "expired" is the one
    case where the client's correct response is to refresh rather than to give
    up, and it leaks nothing -- the expiry is in the token, in plain sight."""
    service = TokenService(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            auth={"access_token_ttl": 60},  # type: ignore[arg-type]
        )
    )
    stale = jwt.encode(
        {"sub": "7", "typ": "access", "exp": int(time.time()) - 1},
        key=SECRET,
        algorithm="HS256",
    )
    with pytest.raises(SecurityError, match="expired"):
        service.verify(stale)


async def test_claims_ride_on_the_access_token_only(service: TokenService) -> None:
    """A refresh token lives for weeks. A role copied into one is a permission
    that was revoked a fortnight ago and still works."""
    pair = await service.issue("7", role="editor")

    assert service.verify(pair.access_token)["role"] == "editor"
    assert "role" not in jwt.decode(pair.refresh_token, SECRET, algorithms=["HS256"])


async def test_the_issuer_and_audience_are_checked_when_set() -> None:
    settings = FastFortSettings(  # type: ignore[call-arg]
        secret_key=SECRET,
        auth={"issuer": "shop", "audience": "shop-api"},  # type: ignore[arg-type]
    )
    service = TokenService(settings)
    pair = await service.issue("7")
    assert service.verify(pair.access_token)["iss"] == "shop"

    other = TokenService(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            auth={"issuer": "somewhere-else", "audience": "shop-api"},  # type: ignore[arg-type]
        )
    )
    with pytest.raises(SecurityError):
        other.verify(pair.access_token)


def test_an_asymmetric_algorithm_without_a_key_pair_says_what_is_missing() -> None:
    """The secret key signs cookies. It is not an RSA private key, and failing at
    the first token rather than at start-up would be a much worse place to find
    that out."""
    with pytest.raises(SecurityError, match="key pair"):
        TokenService(
            FastFortSettings(  # type: ignore[call-arg]
                secret_key=SECRET,
                auth={"algorithm": "RS256"},  # type: ignore[arg-type]
            )
        )


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------


async def test_the_dependency_hands_a_route_the_user_row(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Not a claims dict. A route that has to look the row up itself has been
    handed half an answer."""
    fort = make_fort(backend)
    app = FastAPI()
    fort.mount(app)

    # A default value rather than `Annotated[...]`, because this route is defined
    # inside a function: this module has `from __future__ import annotations`, so
    # FastAPI resolves the annotation string against module globals and `fort` is
    # not one. In a project's own module-level route the `Annotated` form is fine.
    current_user = bearer_user(fort)

    @app.get("/whoami")
    async def whoami(user: Any = Depends(current_user)) -> dict[str, str]:
        return {"email": user.email}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        tokens = await get_tokens(opened)
        response = await opened.get(
            "/whoami", headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        assert response.status_code == 200
        assert response.json() == {"email": ADMIN_EMAIL}


async def test_a_deactivated_account_stops_working_before_its_token_expires(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """A signature check alone would keep letting them in for the rest of the
    token's life, which is fifteen minutes of an account that was closed."""
    fort = make_fort(backend)
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        tokens = await get_tokens(opened)
        header = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert (await opened.get("/auth/me", headers=header)).status_code == 200

        async with fort.backend.unit_of_work() as uow:
            adapter = fort.backend.adapter(StaffUser, uow)
            row = await adapter.require((staff_user.id,))
            await adapter.update(row, {"is_active": False})

        assert (await opened.get("/auth/me", headers=header)).status_code == 401


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_the_refresh_store_can_be_replaced(backend: SQLAlchemyBackend) -> None:
    """The default is per-process. Two workers each holding half the families
    means a token spent on one is still live on the other, and the replay
    detection quietly stops working."""
    store = InMemoryRefreshTokenStore()
    fort = make_fort(backend)
    fort.configure_tokens(store=store)

    await fort.tokens.issue("7")
    assert len(store._records) == 1


async def test_the_token_service_cannot_be_reconfigured_once_built(
    backend: SQLAlchemyBackend,
) -> None:
    from fastfort.core.exceptions import ConfigurationError

    fort = make_fort(backend)
    _ = fort.tokens

    with pytest.raises(ConfigurationError, match="before the first use"):
        fort.configure_tokens(store=InMemoryRefreshTokenStore())


async def test_the_token_endpoint_is_metered_like_the_sign_in_form(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Same Argon2 hash, different door. Metering one and not the other is a lock
    on one of two entrances."""
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            auth={"api_enabled": True},  # type: ignore[arg-type]
            rate_limit={"enabled": True, "login_per_minute": 5, "burst": 1.0},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        statuses = [
            (
                await opened.post(
                    "/auth/token", data={"identity": ADMIN_EMAIL, "password": f"no-{attempt}"}
                )
            ).status_code
            for attempt in range(12)
        ]

    assert 429 in statuses, statuses
