"""`ModelAdmin.list_editable`: editing cells without opening the form.

The whole table is one form with one Save button. A control per row posting on
its own would be one request per cell and nothing to submit it with when
scripting is off -- which is why Django's `list_editable` has the same shape,
and why these tests can drive it with a plain POST.
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
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.registry import default_model_key
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.introspect import introspect_model

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

_PRODUCT_SPEC = introspect_model(Product, key=default_model_key(Product))


class EditableProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "stock", "is_active")
    list_editable = ("stock", "is_active")


class PlainCategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


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
    fort.register(Product, EditableProductAdmin, key="shop.product")
    fort.register(Category, PlainCategoryAdmin, key="shop.category")
    fort.register(StaffUser, admin.ModelAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def post(client: httpx.AsyncClient, **data: Any) -> httpx.Response:
    body = (await client.get("/admin/shop.product/")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match
    data.setdefault("_csrf", match.group(1))
    return await client.post("/admin/shop.product/list-edit", data=data, follow_redirects=True)


async def a_product(session_factory: async_sessionmaker[AsyncSession]) -> Product:
    async with session_factory() as session:
        return (await session.execute(sa.select(Product).order_by(Product.id))).scalars().first()


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_a_column_not_on_the_list_cannot_be_edited_there() -> None:
    """Editing a cell nobody can see is not a feature."""

    class Bad(admin.ModelAdmin):
        list_display = ("id", "name")
        list_editable = ("stock",)

    with pytest.raises(ConfigurationError, match="list_display does not show"):
        Bad(_PRODUCT_SPEC)


def test_a_read_only_column_cannot_be_edited_in_place() -> None:
    class Bad(admin.ModelAdmin):
        list_display = ("id", "name", "stock")
        readonly_fields = ("stock",)
        list_editable = ("stock",)

    with pytest.raises(ConfigurationError, match="not writable"):
        Bad(_PRODUCT_SPEC)


def test_a_control_too_tall_for_a_cell_is_refused() -> None:
    class Bad(admin.ModelAdmin):
        list_display = ("id", "name", "attributes")
        list_editable = ("attributes",)

    with pytest.raises(ConfigurationError, match="list_editable names 'attributes'"):
        Bad(_PRODUCT_SPEC)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_editable_cells_render_as_controls(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text

    assert 'action="/admin/shop.product/list-edit"' in body
    # The name carries the row key and the column, so one form can say which
    # row each control belongs to.
    assert re.search(r'name="\d+-stock"', body)
    assert re.search(r'name="\d+-is_active"', body)


async def test_a_column_left_out_stays_plain_text(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text
    assert not re.search(r'name="\d+-name"', body)


async def test_a_list_with_nothing_editable_has_no_form(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.category/")).text
    assert "list-edit" not in body


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


async def test_a_cell_is_saved(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    product = await a_product(session_factory)

    response = await post(client, **{f"{product.id}-stock": "42"})
    assert response.status_code == 200

    async with session_factory() as session:
        assert (await session.get(Product, product.id)).stock == 42


async def test_a_row_nobody_edited_is_untouched(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Only rows whose controls actually arrived are written. A row that
    posted nothing must not be quietly rewritten with its own values."""
    async with session_factory() as session:
        rows = (await session.execute(sa.select(Product).order_by(Product.id))).scalars().all()
        edited, spared = rows[0].id, rows[-1].id
        before = rows[-1].stock

    await post(client, **{f"{edited}-stock": "7"})

    async with session_factory() as session:
        assert (await session.get(Product, spared)).stock == before


async def test_an_unticked_checkbox_means_false(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A checkbox posts nothing when unticked, so a row that carried one has
    to be told the absence means False -- otherwise the box could be ticked
    but never cleared."""
    async with session_factory() as session:
        product = (
            (await session.execute(sa.select(Product).where(Product.is_active.is_(True))))
            .scalars()
            .first()
        )
        product_id = product.id

    # `stock` arrives, `is_active` does not: the row was edited, the box was
    # cleared.
    await post(client, **{f"{product_id}-stock": "3"})

    async with session_factory() as session:
        assert (await session.get(Product, product_id)).is_active is False


async def test_a_column_never_named_cannot_be_written_by_posting_it(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The allow-list is `list_editable`, not the spec."""
    product = await a_product(session_factory)
    before = product.name

    await post(client, **{f"{product.id}-name": "Hijacked"})

    async with session_factory() as session:
        assert (await session.get(Product, product.id)).name == before


async def test_a_bad_value_writes_none_of_the_rows(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """All the rows or none: a table half-saved is worse than one that
    refused, because nothing on screen says which half."""
    async with session_factory() as session:
        rows = (await session.execute(sa.select(Product).order_by(Product.id))).scalars().all()
        good, bad = rows[0], rows[1]
        before = good.stock

    response = await post(client, **{f"{good.id}-stock": "11", f"{bad.id}-stock": "not-a-number"})
    assert "failed" in response.text.lower()

    async with session_factory() as session:
        assert (await session.get(Product, good.id)).stock == before


async def test_the_save_needs_a_csrf_token(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    product = await a_product(session_factory)
    before = product.stock

    await client.post(
        "/admin/shop.product/list-edit",
        data={f"{product.id}-stock": "999"},
        follow_redirects=True,
    )

    async with session_factory() as session:
        assert (await session.get(Product, product.id)).stock == before
