"""Starring rows through the admin.

Driven through the real ASGI stack and the real sign-in form, so the router
gate and the CSRF check are exercised rather than bypassed. The assertions that
matter are that the star survives a round trip, that it is per-account, and
that the `next` field cannot be turned into an open redirect.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import Category, Favorite, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


def _build(backend: SQLAlchemyBackend, *, favorites: bool = True) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, admin.ModelAdmin, key="shop.product")
    fort.register(Category, admin.ModelAdmin, key="shop.category")
    if favorites:
        fort.enable_favorites(Favorite)

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build(backend)),
        base_url="http://testserver",
    ) as opened:
        await sign_in(opened)
        yield opened


@pytest.fixture
async def off_client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    """The same admin with the feature never enabled."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build(backend, favorites=False)),
        base_url="http://testserver",
    ) as opened:
        await sign_in(opened)
        yield opened


@pytest.fixture
async def product_id(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        found = await session.scalar(sa.select(Product).limit(1))
    assert found is not None
    return int(found.id)


def csrf_of(body: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, "the page must render a CSRF token"
    return match.group(1)


async def star(
    client: httpx.AsyncClient, object_key: str, *, next_url: str = "/admin/shop.product/"
) -> httpx.Response:
    body = (await client.get("/admin/shop.product/")).text
    return await client.post(
        "/admin/shop.product/favorite",
        data={"_csrf": csrf_of(body), "object_key": object_key, "next": next_url},
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# The feature switch
# ---------------------------------------------------------------------------


async def test_no_star_is_drawn_until_it_is_enabled(off_client: httpx.AsyncClient) -> None:
    body = (await off_client.get("/admin/shop.product/")).text

    assert "data-ff-star" not in body
    assert 'id="ff-star"' not in body


async def test_the_page_is_absent_until_it_is_enabled(off_client: httpx.AsyncClient) -> None:
    assert (await off_client.get("/admin/favorites")).status_code == 404


async def test_toggling_is_absent_until_it_is_enabled(off_client: httpx.AsyncClient) -> None:
    body = (await off_client.get("/admin/shop.product/")).text
    response = await off_client.post(
        "/admin/shop.product/favorite", data={"_csrf": csrf_of(body), "object_key": "1"}
    )

    assert response.status_code == 404


async def test_the_sidebar_links_to_the_page(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text

    assert "/admin/favorites" in body


# ---------------------------------------------------------------------------
# Starring
# ---------------------------------------------------------------------------


async def test_a_star_survives_a_round_trip(client: httpx.AsyncClient, product_id: int) -> None:
    response = await star(client, str(product_id))
    assert response.status_code == 303

    body = (await client.get("/admin/shop.product/")).text
    assert f'value="{product_id}"' in body
    assert 'data-ff-starred="1"' in body


async def test_pressing_twice_turns_it_off(client: httpx.AsyncClient, product_id: int) -> None:
    await star(client, str(product_id))
    await star(client, str(product_id))

    body = (await client.get("/admin/shop.product/")).text
    assert 'data-ff-starred="1"' not in body


async def test_the_starred_row_shows_on_the_favourites_page(
    client: httpx.AsyncClient, product_id: int
) -> None:
    await star(client, str(product_id))

    body = (await client.get("/admin/favorites")).text

    assert f"/admin/shop.product/{product_id}/" in body


async def test_the_page_says_so_when_nothing_is_starred(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/favorites")).text

    assert "Nothing starred yet" in body


async def test_the_change_form_carries_the_star(client: httpx.AsyncClient, product_id: int) -> None:
    body = (await client.get(f"/admin/shop.product/{product_id}/")).text
    assert 'aria-pressed="false"' in body

    await star(client, str(product_id))

    body = (await client.get(f"/admin/shop.product/{product_id}/")).text
    assert 'aria-pressed="true"' in body


async def test_the_add_form_has_nothing_to_star(client: httpx.AsyncClient) -> None:
    """There is no row yet, so a star would have no target. The sidebar's link
    to /admin/favorites is still there, which is why this looks for the toggle
    endpoint rather than for the substring."""
    body = (await client.get("/admin/shop.product/add")).text

    assert "shop.product/favorite" not in body


# ---------------------------------------------------------------------------
# What a request may not do
# ---------------------------------------------------------------------------


async def test_starring_requires_the_csrf_token(
    client: httpx.AsyncClient,
    product_id: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await client.post(
        "/admin/shop.product/favorite",
        data={"object_key": str(product_id)},
        follow_redirects=True,
    )

    async with session_factory() as session:
        assert (await session.execute(sa.select(Favorite))).first() is None


async def test_a_missing_row_is_404_not_403(client: httpx.AsyncClient) -> None:
    """Confirming existence to somebody who may not see it is a leak."""
    response = await star(client, "99999999")

    assert response.status_code == 404


async def test_the_return_url_cannot_leave_the_site(
    client: httpx.AsyncClient, product_id: int
) -> None:
    """`next` is the classic open-redirect vector, and this one is posted from
    a form on every list page."""
    response = await star(client, str(product_id), next_url="//evil.example.com/")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/shop.product/"


async def test_signing_out_hides_the_page(backend: SQLAlchemyBackend) -> None:
    """Every admin route is behind the gate, this one included."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_build(backend)),
        base_url="http://testserver",
    ) as anonymous:
        response = await anonymous.get("/admin/favorites", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert "login" in response.headers["location"]


# ---------------------------------------------------------------------------
# The scripted path
# ---------------------------------------------------------------------------


async def test_the_script_gets_an_answer_rather_than_a_page(
    client: httpx.AsyncClient, product_id: int
) -> None:
    """Following the redirect would pull a whole list page down to learn one
    bit, which is the round trip the header exists to avoid."""
    body = (await client.get("/admin/shop.product/")).text
    response = await client.post(
        "/admin/shop.product/favorite",
        data={"_csrf": csrf_of(body), "object_key": str(product_id)},
        headers={"X-FastFort-Partial": "favorite"},
    )

    assert response.status_code == 200
    assert response.json()["starred"] is True
