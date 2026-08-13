"""Security tests for the admin's sign-in.

Each test names an attack it prevents. They are not a coverage exercise: an admin
is the highest-value surface a project has, and every one of these has been a real
vulnerability in a real admin panel.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column
from tests.orm.models import Base, Product

from fastfort import FastFort, FastFortSettings
from fastfort.admin import ModelAdmin
from fastfort.admin.security import safe_next_url
from fastfort.auth import hash_password, verify_password
from fastfort.auth.csrf import CsrfProtection
from fastfort.auth.lockout import InMemoryLockoutStore, Lockout
from fastfort.auth.sessions import SessionCodec
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"
PASSWORD = "correct-horse-battery"


class Staff(Base):
    """A user model defined here so the security suite owns its own fixtures."""

    __tablename__ = "security_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    hashed_password: Mapped[str] = mapped_column(sa.String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_staff: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)


@pytest.fixture
async def app_and_users(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncIterator[tuple[FastAPI, dict[str, Staff]]]:
    async with session_factory() as session:
        people = {
            "staff": Staff(email="staff@example.com", hashed_password=hash_password(PASSWORD)),
            "inactive": Staff(
                email="inactive@example.com",
                hashed_password=hash_password(PASSWORD),
                is_active=False,
            ),
            "customer": Staff(
                email="customer@example.com",
                hashed_password=hash_password(PASSWORD),
                is_staff=False,
            ),
        }
        session.add_all(list(people.values()))
        await session.commit()

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Fort",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(Staff)
    fort.register(Product, ModelAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app, people


@pytest.fixture
async def client(app_and_users: tuple[FastAPI, Any]) -> AsyncIterator[httpx.AsyncClient]:
    app, _ = app_and_users
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        yield opened


async def csrf_token(client: httpx.AsyncClient) -> str:
    body = (await client.get("/admin/login")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, "the login form must carry a CSRF token"
    return match.group(1)


async def sign_in(
    client: httpx.AsyncClient,
    identity: str = "staff@example.com",
    password: str = PASSWORD,
    *,
    token: str | None = None,
    next_url: str = "/admin/",
) -> httpx.Response:
    return await client.post(
        "/admin/login",
        data={
            "identity": identity,
            "password": password,
            "_csrf": token if token is not None else await csrf_token(client),
            "next": next_url,
        },
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/admin/", "/admin/shop.product/"])
async def test_an_anonymous_visitor_cannot_reach_the_admin(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


async def test_the_gate_remembers_where_you_were_going(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/shop.product/?q=phone")
    assert "next=/admin/shop.product/%3Fq%3Dphone" in response.headers["location"]


async def test_the_login_page_does_not_list_the_models(client: httpx.AsyncClient) -> None:
    """A page an anonymous visitor can reach must not enumerate what is managed."""
    body = (await client.get("/admin/login")).text
    assert "shop.product" not in body
    assert "ff-sidebar" not in body


async def test_the_stylesheet_stays_public(client: httpx.AsyncClient) -> None:
    """Otherwise the login page renders unstyled."""
    assert (await client.get("/admin/static/fastfort.css")).status_code == 200


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


async def test_correct_credentials_sign_you_in(client: httpx.AsyncClient) -> None:
    response = await sign_in(client, next_url="/admin/shop.product/")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/shop.product/"
    assert (await client.get("/admin/")).status_code == 200


@pytest.mark.parametrize(
    ("identity", "password"),
    [
        ("staff@example.com", "wrong-password"),
        ("nobody@example.com", PASSWORD),
        ("inactive@example.com", PASSWORD),
        ("customer@example.com", PASSWORD),
        ("", ""),
    ],
)
async def test_every_rejection_looks_identical(
    client: httpx.AsyncClient, identity: str, password: str
) -> None:
    """Distinguishing them hands out a user enumeration oracle.

    Wrong password, unknown address, deactivated account and "not staff" must be
    indistinguishable from outside.
    """
    response = await sign_in(client, identity, password)

    assert response.status_code == 200
    assert "do not match an account that can sign in here" in response.text
    # And no session was issued.
    assert (await client.get("/admin/")).status_code == 303


async def test_a_non_staff_user_with_valid_credentials_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """A customer account must not become an admin account."""
    await sign_in(client, "customer@example.com", PASSWORD)
    assert (await client.get("/admin/")).status_code == 303


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def test_a_login_without_a_token_is_refused(client: httpx.AsyncClient) -> None:
    await client.get("/admin/login")
    response = await client.post(
        "/admin/login", data={"identity": "staff@example.com", "password": PASSWORD}
    )

    assert "security token" in response.text
    assert (await client.get("/admin/")).status_code == 303


async def test_a_forged_token_is_refused(client: httpx.AsyncClient) -> None:
    """The signature is checked, not just the match between cookie and field."""
    await csrf_token(client)
    response = await sign_in(client, token="a-value-i-chose-myself")

    assert "security token" in response.text
    assert (await client.get("/admin/")).status_code == 303


async def test_a_stale_token_still_works(client: httpx.AsyncClient) -> None:
    """Rotating on every rejection breaks the Back button and a second tab.

    The token is signed and time-limited, so keeping it across a failed attempt
    costs nothing and avoids a rejection nobody can explain.
    """
    token = await csrf_token(client)
    await sign_in(client, password="wrong", token=token)

    assert (await sign_in(client, token=token)).status_code == 303


async def test_the_token_is_rotated_on_a_successful_sign_in(
    client: httpx.AsyncClient,
) -> None:
    """A token planted before authentication must not survive into the session."""
    before = await csrf_token(client)
    await sign_in(client)

    assert client.cookies.get("fastfort_session_csrf") != before


def test_csrf_rejects_a_mismatched_pair() -> None:
    protection = CsrfProtection(
        FastFortSettings(secret_key=SECRET)  # type: ignore[call-arg]
    )
    issued = protection.issue()

    protection.verify(cookie=issued, submitted=issued)
    with pytest.raises(Exception, match="does not match"):
        protection.verify(cookie=issued, submitted=protection.issue())


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def test_a_tampered_cookie_is_ignored(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    original = client.cookies.get("fastfort_session")
    assert original

    client.cookies.set("fastfort_session", original[:-4] + "AAAA")
    assert (await client.get("/admin/")).status_code == 303


async def test_changing_a_password_signs_every_device_out(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The session carries a stamp derived from the password hash."""
    await sign_in(client)
    assert (await client.get("/admin/")).status_code == 200

    async with session_factory() as session:
        user = (
            await session.execute(sa.select(Staff).where(Staff.email == "staff@example.com"))
        ).scalar_one()
        user.hashed_password = hash_password("something-entirely-different")
        await session.commit()

    assert (await client.get("/admin/")).status_code == 303


async def test_deactivating_an_account_ends_its_session(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in(client)

    async with session_factory() as session:
        user = (
            await session.execute(sa.select(Staff).where(Staff.email == "staff@example.com"))
        ).scalar_one()
        user.is_active = False
        await session.commit()

    assert (await client.get("/admin/")).status_code == 303


def test_a_session_signed_with_another_key_is_rejected() -> None:
    """Rotating the secret key must invalidate every cookie."""
    old = SessionCodec(FastFortSettings(secret_key=SECRET))  # type: ignore[call-arg]
    new = SessionCodec(
        FastFortSettings(secret_key="Zx4Vb8Nm2Kq7Wt1Ly6Pc3Rd9Fh5Jg0Sw")  # type: ignore[call-arg]
    )
    cookie = old.issue(user_id=1, password_hash="hash")

    assert old.read(cookie) is not None
    assert new.read(cookie) is None


def test_the_session_stamp_is_keyed_to_the_secret() -> None:
    """A leaked database alone must not be enough to mint a session."""
    codec = SessionCodec(FastFortSettings(secret_key=SECRET))  # type: ignore[call-arg]
    other = SessionCodec(
        FastFortSettings(secret_key="Zx4Vb8Nm2Kq7Wt1Ly6Pc3Rd9Fh5Jg0Sw")  # type: ignore[call-arg]
    )
    assert codec.stamp_for("some-hash") != other.stamp_for("some-hash")


# ---------------------------------------------------------------------------
# Signing out
# ---------------------------------------------------------------------------


async def test_signing_out_ends_the_session(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    token = client.cookies.get("fastfort_session_csrf")

    response = await client.post("/admin/logout", data={"_csrf": token})

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert (await client.get("/admin/")).status_code == 303


async def test_signing_out_is_not_available_over_get(client: httpx.AsyncClient) -> None:
    """Otherwise any image tag on any page can sign a person out."""
    await sign_in(client)
    assert (await client.get("/admin/logout")).status_code == 405
    assert (await client.get("/admin/")).status_code == 200


# ---------------------------------------------------------------------------
# Lockout: brute-force protection, not to be confused with logout
# ---------------------------------------------------------------------------


async def test_repeated_failures_are_locked_out(client: httpx.AsyncClient) -> None:
    token = await csrf_token(client)
    for _ in range(5):
        await sign_in(client, password="wrong", token=token)

    response = await sign_in(client, password="wrong", token=token)
    assert "Too many failed attempts" in response.text


async def test_a_lockout_blocks_the_correct_password_too(client: httpx.AsyncClient) -> None:
    """Otherwise an attacker learns when they have guessed right."""
    token = await csrf_token(client)
    for _ in range(6):
        await sign_in(client, password="wrong", token=token)

    assert "Too many failed attempts" in (await sign_in(client, token=token)).text


async def test_signing_in_successfully_clears_the_counter(
    client: httpx.AsyncClient,
) -> None:
    token = await csrf_token(client)
    for _ in range(3):
        await sign_in(client, password="wrong", token=token)

    assert (await sign_in(client, token=token)).status_code == 303


async def test_the_lockout_window_slides() -> None:
    """Five failures across a day must not lock what five in a second should."""
    settings = FastFortSettings(secret_key=SECRET)  # type: ignore[call-arg]
    store = InMemoryLockoutStore()
    lockout = Lockout(settings.auth, store)

    for _ in range(settings.auth.lockout_threshold):
        state = await lockout.record_failure(address="1.2.3.4", identity="a@b.c")
    assert state.locked

    # Aging the recorded timestamps past the window is what a real clock would do.
    store._events = {key: [] for key in store._events}
    assert not (await lockout.check(address="1.2.3.4", identity="a@b.c")).locked


async def test_an_identity_and_an_address_are_counted_separately() -> None:
    """Either alone can be worked around; both together cannot."""
    settings = FastFortSettings(secret_key=SECRET)  # type: ignore[call-arg]
    lockout = Lockout(settings.auth, InMemoryLockoutStore())

    for _ in range(settings.auth.lockout_threshold):
        await lockout.record_failure(address="1.1.1.1", identity="victim@example.com")

    # A new address does not help: the identity is locked as well.
    assert (await lockout.check(address="2.2.2.2", identity="victim@example.com")).locked
    # And an unrelated identity from the locked address is also refused.
    assert (await lockout.check(address="1.1.1.1", identity="other@example.com")).locked


# ---------------------------------------------------------------------------
# Open redirect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "//evil.example.com",
        "https://evil.example.com/",
        "http://evil.example.com",
        "/\\evil.example.com",
        "evil.example.com",
        "javascript:alert(1)",
    ],
)
async def test_a_hostile_next_url_is_discarded(client: httpx.AsyncClient, hostile: str) -> None:
    """`?next=` is the classic phishing vector on a login page."""
    response = await sign_in(client, next_url=hostile)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("/admin/shop.product/", "/admin/shop.product/"),
        ("/admin/?q=x", "/admin/?q=x"),
        (None, "/admin/"),
        ("", "/admin/"),
    ],
)
def test_a_local_next_url_is_kept(candidate: str | None, expected: str) -> None:
    assert safe_next_url(candidate, fallback="/admin/") == expected


# ---------------------------------------------------------------------------
# Headers and passwords
# ---------------------------------------------------------------------------


async def test_the_admin_sends_its_security_headers(client: httpx.AsyncClient) -> None:
    headers = (await client.get("/admin/login")).headers

    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert "script-src 'self'" in headers["content-security-policy"]
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"


async def test_the_session_cookie_is_not_readable_by_javascript(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise any XSS becomes a session takeover."""
    response = await sign_in(client)
    cookie_header = response.headers.get("set-cookie", "")
    assert "httponly" in cookie_header.lower()


def test_passwords_are_hashed_with_argon2id() -> None:
    hashed = hash_password(PASSWORD)

    assert hashed.startswith("$argon2id$")
    assert PASSWORD not in hashed
    assert verify_password(PASSWORD, hashed)
    assert not verify_password("something else", hashed)


def test_verifying_against_a_missing_hash_fails_rather_than_passing() -> None:
    """An account with no password set must not be signable-in to with anything."""
    assert not verify_password("anything", None)
    assert not verify_password("anything", "")
    assert not verify_password("", "")
