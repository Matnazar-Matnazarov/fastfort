"""Minting and revoking tokens through the admin.

The assertion that matters is that the secret appears exactly once and is
reachable from nowhere else -- not from the list, not from the row's own form,
not from a second visit to the page that showed it.
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
from tests.conftest import sign_in
from tests.orm.models import ApiToken, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.auth.api_tokens import hash_token
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.enable_api_tokens(ApiToken)
    fort.register(ApiToken, admin.ApiTokenAdmin, key="auth.token")
    fort.register(Product, admin.ModelAdmin, key="shop.product")
    fort.register(StaffUser, admin.ModelAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def mint(client: httpx.AsyncClient, **data: Any) -> httpx.Response:
    body = (await client.get("/admin/auth.token/add")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, "the minting page must render a CSRF token"
    data.setdefault("_csrf", match.group(1))
    return await client.post("/admin/auth.token/add", data=data, follow_redirects=True)


def shown_secret(body: str) -> str:
    match = re.search(r'id="ff-token-secret"[^>]*?value="([^"]+)"', body, re.S)
    assert match, "the page must show the secret once"
    return match.group(1)


# ---------------------------------------------------------------------------
# The minting page
# ---------------------------------------------------------------------------


async def test_the_add_view_is_the_minting_page(client: httpx.AsyncClient) -> None:
    """A secret has to be generated rather than typed, so this one model's add
    view answers with a different page."""
    body = (await client.get("/admin/auth.token/add")).text

    assert 'name="name"' in body
    assert 'name="scopes"' in body
    assert 'name="expires_at"' in body
    # Never a control for the digest: a form that took one would be a form for
    # forging a credential.
    assert 'name="token_hash"' not in body


async def test_another_model_keeps_the_ordinary_add_form(client: httpx.AsyncClient) -> None:
    """The branch is by model identity, not by something a second table with
    the same columns could trip."""
    body = (await client.get("/admin/shop.product/add")).text
    assert "ff-token-secret" not in body


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


async def test_minting_shows_the_secret_once(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await mint(client, name="deploy")
    secret = shown_secret(response.text)

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    assert row is not None
    assert row.name == "deploy"
    assert row.token_hash == hash_token(secret)


async def test_the_secret_is_never_shown_again(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The list and the row's own form both have to be free of it, and so does
    a second visit to the page that showed it."""
    secret = shown_secret((await mint(client, name="deploy")).text)

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    assert secret not in (await client.get("/admin/auth.token/")).text
    assert secret not in (await client.get(f"/admin/auth.token/{row.id}/")).text
    assert secret not in (await client.get("/admin/auth.token/add")).text


async def test_refreshing_the_minting_page_does_not_re_show_it(
    client: httpx.AsyncClient,
) -> None:
    """It renders rather than redirects precisely so the reveal has no URL to
    return to."""
    secret = shown_secret((await mint(client, name="deploy")).text)
    assert secret not in (await client.get("/admin/auth.token/add")).text


async def test_scopes_and_expiry_are_stored(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await mint(client, name="reader", scopes="orders:read", expires_at="2030-01-01T00:00")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    assert row.scopes == "orders:read"
    assert row.expires_at is not None
    assert row.expires_at.year == 2030


async def test_an_unreadable_expiry_mints_nothing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await mint(client, name="bad", expires_at="whenever")

    assert "not a date" in response.text
    assert "ff-token-secret" not in response.text

    async with session_factory() as session:
        assert (await session.execute(sa.select(ApiToken))).first() is None


async def test_minting_needs_a_csrf_token(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await client.post("/admin/auth.token/add", data={"name": "forged"}, follow_redirects=True)

    async with session_factory() as session:
        assert (await session.execute(sa.select(ApiToken))).first() is None


# ---------------------------------------------------------------------------
# The list, and revoking
# ---------------------------------------------------------------------------


async def test_the_list_shows_the_prefix_and_not_the_digest(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The prefix is what lets somebody match a row against the string in their
    configuration file; the digest is nobody's business."""
    secret = shown_secret((await mint(client, name="deploy")).text)

    body = (await client.get("/admin/auth.token/")).text
    assert secret[:8] in body
    assert hash_token(secret) not in body


async def test_a_token_can_be_revoked_from_the_list(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await mint(client, name="deploy")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    body = (await client.get("/admin/auth.token/")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match
    await client.post(
        "/admin/auth.token/action",
        data={"_csrf": match.group(1), "action": "revoke", "keys": str(row.id)},
        follow_redirects=True,
    )

    async with session_factory() as session:
        after = await session.get(ApiToken, row.id)

    assert after is not None, "revoking keeps the row"
    assert after.revoked_at is not None


async def test_the_digest_is_not_editable_on_the_form(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`readonly_fields` covers everything the mint decided. A writable
    `token_hash` would let somebody paste a digest they had chosen."""
    await mint(client, name="deploy")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    body = (await client.get(f"/admin/auth.token/{row.id}/")).text
    assert 'name="token_hash"' not in body
    # The one thing still worth changing later.
    assert 'name="name"' in body
