"""`ModelAdmin.soft_delete_field`: deleting without a second table.

A trashed row never leaves the database -- `adapter.delete` is never called for
a model that declares this option, only `adapter.update` on the marker column
the project already owns. These tests are therefore about visibility (which
list a row appears on) and about what a request is allowed to reach (restore
only from the trash, delete only from the ordinary list), not about a new
adapter surface, because there is not one.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.introspect import introspect_model

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class TrashableProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price", "stock")
    soft_delete_field = "deleted_at"


class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "is_staff")


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
    fort.register(Product, TrashableProductAdmin, key="shop.trashable_product")
    fort.register(Category, admin.ModelAdmin, key="shop.category")
    fort.register(StaffUser, UserAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def token(client: httpx.AsyncClient, path: str) -> str:
    body = (await client.get(path)).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, f"{path} must render a CSRF token"
    return match.group(1)


async def submit(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    data.setdefault("_csrf", await token(client, path))
    return await client.post(path, data=data, follow_redirects=True)


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


#: Built once, since every declaration test asks the same spec the same
#: kind of question and none of them mutate it.
_PRODUCT_SPEC = introspect_model(Product, key="shop.product")


def test_soft_delete_field_must_name_a_real_field() -> None:
    class BadAdmin(admin.ModelAdmin):
        soft_delete_field = "not_a_field"

    with pytest.raises(ConfigurationError, match="soft_delete_field names 'not_a_field'"):
        BadAdmin(_PRODUCT_SPEC)


def test_soft_delete_field_must_be_boolean_or_date_like() -> None:
    class BadAdmin(admin.ModelAdmin):
        soft_delete_field = "name"  # a String column

    with pytest.raises(ConfigurationError, match="soft_delete_field names 'name'"):
        BadAdmin(_PRODUCT_SPEC)


def test_a_non_nullable_datetime_marker_is_rejected() -> None:
    class BadAdmin(admin.ModelAdmin):
        # created_at is DateTime but not nullable.
        soft_delete_field = "created_at"

    with pytest.raises(ConfigurationError, match="not nullable"):
        BadAdmin(_PRODUCT_SPEC)


def test_a_non_nullable_boolean_marker_is_accepted() -> None:
    """Unlike a date marker, a boolean one needs no null state -- False already
    means "not trashed"."""

    class OkAdmin(admin.ModelAdmin):
        soft_delete_field = "is_active"  # Boolean, not nullable

    OkAdmin(_PRODUCT_SPEC)  # does not raise


async def test_the_marker_field_is_excluded_from_the_form(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/add")).text
    assert "deleted_at" not in body


# ---------------------------------------------------------------------------
# The ordinary list
# ---------------------------------------------------------------------------


async def test_the_ordinary_list_shows_untrashed_rows(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/")).text
    assert "Pixel Phone" in body


async def test_the_ordinary_list_offers_a_link_to_the_trash(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/")).text
    assert 'href="/admin/shop.trashable_product/?trashed=1"' in body


# ---------------------------------------------------------------------------
# Deleting is trashing
# ---------------------------------------------------------------------------


async def test_deleting_a_row_keeps_it_in_the_database(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = Product(name="Doomed Widget", price=Decimal("9.99"), stock=1)
        session.add(product)
        await session.commit()
        product_id = product.id

    response = await submit(client, f"/admin/shop.trashable_product/{product_id}/delete")
    assert response.status_code == 200
    assert "was moved to trash" in response.text

    async with session_factory() as session:
        still_there = await session.get(Product, product_id)
        assert still_there is not None
        assert still_there.deleted_at is not None


async def test_a_trashed_row_leaves_the_ordinary_list(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = Product(name="Doomed Widget Two", price=Decimal("9.99"), stock=1)
        session.add(product)
        await session.commit()
        product_id = product.id

    await submit(client, f"/admin/shop.trashable_product/{product_id}/delete")

    body = (await client.get("/admin/shop.trashable_product/")).text
    assert "Doomed Widget Two" not in body


async def test_a_trashed_row_appears_on_the_trash_view(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = Product(name="Doomed Widget Three", price=Decimal("9.99"), stock=1)
        session.add(product)
        await session.commit()
        product_id = product.id

    await submit(client, f"/admin/shop.trashable_product/{product_id}/delete")

    body = (await client.get("/admin/shop.trashable_product/?trashed=1")).text
    assert "Doomed Widget Three" in body


async def test_the_trash_view_does_not_show_untrashed_rows(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/?trashed=1")).text
    assert "Pixel Phone" not in body


async def test_the_confirmation_page_does_not_warn_about_cascades(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Nothing points at this product, but the admin still declares relations
    that would cascade under a real delete -- the point is that a soft delete
    does not ask `deletion_plan` at all, so this warning cannot appear even
    when a project's own model would otherwise draw one."""
    async with session_factory() as session:
        product = Product(name="Doomed Widget Four", price=Decimal("9.99"), stock=1)
        session.add(product)
        await session.commit()
        product_id = product.id

    body = (await client.get(f"/admin/shop.trashable_product/{product_id}/delete")).text
    assert "will be deleted too" not in body
    assert "restored from Trash" in body


# ---------------------------------------------------------------------------
# Restoring
# ---------------------------------------------------------------------------


async def test_restoring_a_row_returns_it_to_the_ordinary_list(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = Product(name="Reprieved Widget", price=Decimal("9.99"), stock=1)
        session.add(product)
        await session.commit()
        product_id = product.id

    await submit(client, f"/admin/shop.trashable_product/{product_id}/delete")

    # `/action` is POST-only, so its own CSRF field can't be scraped by a GET
    # the way `submit()` normally does -- the bulk bar's hidden field on the
    # list page it posts from carries the same token.
    response = await client.post(
        "/admin/shop.trashable_product/action",
        data={
            "_csrf": await token(client, "/admin/shop.trashable_product/?trashed=1"),
            "action": "restore",
            "keys": str(product_id),
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "was restored" in response.text

    body = (await client.get("/admin/shop.trashable_product/")).text
    assert "Reprieved Widget" in body

    async with session_factory() as session:
        restored = await session.get(Product, product_id)
        assert restored is not None
        assert restored.deleted_at is None


async def test_the_trash_view_offers_restore_not_delete(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/?trashed=1")).text
    assert 'value="restore"' in body
    assert 'value="delete"' not in body


async def test_the_ordinary_list_offers_delete_not_restore(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.trashable_product/")).text
    assert 'value="delete"' in body
    assert 'value="restore"' not in body
