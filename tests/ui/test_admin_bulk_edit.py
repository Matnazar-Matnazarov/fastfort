"""`ModelAdmin.bulk_editable`: setting one field across the selected rows.

Opt-in, unlike delete. A delete announces itself and asks; one mis-set column
across forty rows is a silent change nobody notices until later, so a project
names the fields it will accept rather than getting the action for free.

The route is two steps -- an intermediate page that asks which field and what
value, then the write -- which is what makes it work with scripting off, and
is the same shape the delete confirmation already takes.
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


class BulkProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "stock", "is_active")
    bulk_editable = ("stock", "is_active", "status")


class PlainCategoryAdmin(admin.ModelAdmin):
    """Names nothing, so the action is not offered at all."""

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
    fort.register(Product, BulkProductAdmin, key="shop.product")
    fort.register(Category, PlainCategoryAdmin, key="shop.category")
    fort.register(StaffUser, admin.ModelAdmin, key="accounts.user")

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


async def post(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    data.setdefault("_csrf", await token(client, "/admin/shop.product/"))
    return await client.post(path, data=data, follow_redirects=True)


async def product_ids(session_factory: async_sessionmaker[AsyncSession]) -> list[int]:
    async with session_factory() as session:
        return [p.id for p in (await session.execute(sa.select(Product))).scalars().all()]


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_an_unknown_field_is_refused() -> None:
    class Bad(admin.ModelAdmin):
        bulk_editable = ("nope",)

    with pytest.raises(ConfigurationError, match="bulk_editable names 'nope'"):
        Bad(_PRODUCT_SPEC)


def test_a_read_only_field_cannot_be_made_bulk_editable() -> None:
    """It must not widen what `FieldSpec.editable` and `readonly_fields`
    already settled -- that boundary is deliberately singular."""

    class Bad(admin.ModelAdmin):
        readonly_fields = ("stock",)
        bulk_editable = ("stock",)

    with pytest.raises(ConfigurationError, match="not writable"):
        Bad(_PRODUCT_SPEC)


def test_a_control_that_cannot_hold_one_shared_value_is_refused() -> None:
    """ "The same value for forty rows" is not a sentence a JSON column can
    complete."""

    class Bad(admin.ModelAdmin):
        bulk_editable = ("attributes",)

    with pytest.raises(ConfigurationError, match="bulk_editable names 'attributes'"):
        Bad(_PRODUCT_SPEC)


# ---------------------------------------------------------------------------
# The action
# ---------------------------------------------------------------------------


async def test_the_action_is_offered_when_fields_are_named(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text
    assert 'value="edit"' in body


async def test_the_action_is_absent_when_nothing_is_named(client: httpx.AsyncClient) -> None:
    """Opt-in: a model that named no fields gets no button."""
    body = (await client.get("/admin/shop.category/")).text
    assert 'value="edit"' not in body


async def test_choosing_the_action_asks_which_field_and_what_value(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await product_ids(session_factory)

    response = await post(
        client, "/admin/shop.product/action", action="edit", keys=[str(ids[0]), str(ids[1])]
    )

    assert response.status_code == 200
    # Every declared field is offered, and only those.
    assert 'name="field"' in response.text
    assert 'value="stock"' in response.text
    assert 'value="is_active"' in response.text
    assert 'value="name"' not in response.text
    # The selection travels with the form rather than being re-queried.
    assert response.text.count('name="keys"') == 2


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------


async def test_a_field_is_set_across_every_selected_row(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = (await product_ids(session_factory))[:2]

    response = await post(
        client,
        "/admin/shop.product/bulk-edit",
        field="stock",
        value="77",
        keys=[str(i) for i in ids],
    )
    assert response.status_code == 200
    assert "were updated" in response.text

    async with session_factory() as session:
        for product_id in ids:
            assert (await session.get(Product, product_id)).stock == 77


async def test_rows_that_were_not_selected_are_untouched(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = await product_ids(session_factory)
    chosen, spared = ids[0], ids[-1]

    async with session_factory() as session:
        before = (await session.get(Product, spared)).stock

    await post(
        client, "/admin/shop.product/bulk-edit", field="stock", value="5", keys=[str(chosen)]
    )

    async with session_factory() as session:
        assert (await session.get(Product, spared)).stock == before


async def test_a_field_never_named_cannot_be_set_by_posting_it(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The allow-list is the declaration, not the spec. `name` is perfectly
    writable on the form and must still be refused here."""
    ids = (await product_ids(session_factory))[:1]

    async with session_factory() as session:
        before = (await session.get(Product, ids[0])).name

    response = await post(
        client, "/admin/shop.product/bulk-edit", field="name", value="Hijacked", keys=[str(ids[0])]
    )
    assert "cannot be edited in bulk" in response.text

    async with session_factory() as session:
        assert (await session.get(Product, ids[0])).name == before


async def test_an_unparseable_value_writes_nothing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = (await product_ids(session_factory))[:2]

    async with session_factory() as session:
        before = [(await session.get(Product, i)).stock for i in ids]

    await post(
        client,
        "/admin/shop.product/bulk-edit",
        field="stock",
        value="not-a-number",
        keys=[str(i) for i in ids],
    )

    async with session_factory() as session:
        after = [(await session.get(Product, i)).stock for i in ids]
    assert after == before


async def test_the_edit_needs_a_csrf_token(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    ids = (await product_ids(session_factory))[:1]

    async with session_factory() as session:
        before = (await session.get(Product, ids[0])).stock

    response = await client.post(
        "/admin/shop.product/bulk-edit",
        data={"field": "stock", "value": "999", "keys": str(ids[0])},
        follow_redirects=True,
    )
    assert response.status_code == 200

    async with session_factory() as session:
        assert (await session.get(Product, ids[0])).stock == before
