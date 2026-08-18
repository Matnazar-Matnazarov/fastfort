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
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
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
