"""Where a save goes next: the buttons Django puts under every form.

"Save", "Save and add another", "Save and return to list" and, where the admin
asks for it, "Save as new". One posted name decides it (`_save`), so these tests
post that name the way a browser does when the button is pressed.
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


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price")
    save_as = True
    save_on_top = True


class CategoryAdmin(admin.ModelAdmin):
    """Without `save_as`: the copy button must be absent *and* refused."""


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
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, CategoryAdmin, key="shop.category")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def post(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    body = (await client.get(path)).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, f"{path} must render a CSRF token"
    data.setdefault("_csrf", match.group(1))
    return await client.post(path, data=data)


async def first_id(session_factory: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with session_factory() as session:
        return int((await session.execute(sa.select(sa.func.min(model.id)))).scalar_one())


# ---------------------------------------------------------------------------
# The buttons
# ---------------------------------------------------------------------------


async def test_the_add_form_offers_every_way_out(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/add")).text

    assert 'name="_save" value="addanother"' in body
    assert 'name="_save" value="list"' in body
    # Nothing to copy until the row exists.
    assert 'value="asnew"' not in body


async def test_the_plain_save_is_the_first_submit_button(client: httpx.AsyncClient) -> None:
    """Enter in a text box submits with the first submit button in the form. If
    that were "Save as new", pressing Enter on an edit would create a copy."""
    body = (await client.get("/admin/shop.product/1/")).text
    form = body[body.index('<form\n    class="ff-mt-4"') :]

    first = re.search(r'<button[^>]*type="submit"[^>]*>', form)
    assert first, "the form should have a submit button"
    assert 'value=""' in first.group(0)


async def test_save_on_top_repeats_the_buttons_above_the_form(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/")).text

    assert "ff-form-actions--top" in body
    assert body.count('value="addanother"') == 2


async def test_save_as_is_offered_only_where_the_admin_asks(client: httpx.AsyncClient) -> None:
    assert 'value="asnew"' in (await client.get("/admin/shop.product/1/")).text
    assert 'value="asnew"' not in (await client.get("/admin/shop.category/1/")).text


async def test_a_popup_offers_only_the_plain_save(client: httpx.AsyncClient) -> None:
    """A popup answers the field that opened it and closes; "add another" would
    leave it open on a blank form the opener is no longer waiting for."""
    body = (await client.get("/admin/shop.product/add?_popup=1&_field=product")).text

    assert 'value="addanother"' not in body
    assert 'value="list"' not in body


# ---------------------------------------------------------------------------
# Where each one goes
# ---------------------------------------------------------------------------


async def test_a_plain_save_stays_on_the_record(client: httpx.AsyncClient) -> None:
    response = await post(client, "/admin/shop.product/add", name="Plain", price="1.00")

    assert response.status_code == 303
    assert re.fullmatch(r"/admin/shop\.product/\d+/", response.headers["location"])


async def test_save_and_add_another_opens_a_blank_form(client: httpx.AsyncClient) -> None:
    response = await post(
        client, "/admin/shop.product/add", name="Another", price="1.00", _save="addanother"
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/shop.product/add"


async def test_save_and_return_to_list(client: httpx.AsyncClient) -> None:
    response = await post(
        client, "/admin/shop.product/1/", name="Listed", price="2.00", _save="list"
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/shop.product/"


async def test_an_unknown_choice_is_the_default_not_an_error(client: httpx.AsyncClient) -> None:
    """The write already happened; a stale tab deserves the record, not a 400."""
    response = await post(
        client, "/admin/shop.product/add", name="Odd", price="1.00", _save="sideways"
    )

    assert response.status_code == 303
    assert re.fullmatch(r"/admin/shop\.product/\d+/", response.headers["location"])


# ---------------------------------------------------------------------------
# Save as new
# ---------------------------------------------------------------------------


async def test_save_as_new_creates_a_copy_and_leaves_the_original(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    original = await first_id(session_factory, Product)
    async with session_factory() as session:
        before = (await session.get(Product, original)).name  # type: ignore[union-attr]

    response = await post(
        client, f"/admin/shop.product/{original}/", name="The Copy", price="3.00", _save="asnew"
    )

    assert response.status_code == 303
    copy_id = int(response.headers["location"].rstrip("/").rsplit("/", 1)[1])
    assert copy_id != original
    async with session_factory() as session:
        assert (await session.get(Product, original)).name == before  # type: ignore[union-attr]
        assert (await session.get(Product, copy_id)).name == "The Copy"  # type: ignore[union-attr]


async def test_save_as_new_is_refused_where_it_was_not_offered(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Posting `_save=asnew` by hand to an admin without `save_as` is an
    ordinary save, never a create the project did not agree to expose."""
    original = await first_id(session_factory, Category)
    async with session_factory() as session:
        count = (await session.execute(sa.select(sa.func.count(Category.id)))).scalar_one()

    response = await post(
        client, f"/admin/shop.category/{original}/", name="Renamed", _save="asnew"
    )

    assert response.status_code == 303
    assert response.headers["location"] == f"/admin/shop.category/{original}/"
    async with session_factory() as session:
        after = (await session.execute(sa.select(sa.func.count(Category.id)))).scalar_one()
        assert after == count
        assert (await session.get(Category, original)).name == "Renamed"  # type: ignore[union-attr]


def test_save_as_beside_inlines_is_a_start_up_error() -> None:
    class Lines(admin.TabularInline):
        model = Product

    class Copying(admin.ModelAdmin):
        save_as = True
        inlines = (Lines,)

    spec = introspect_model(Category, key=default_model_key(Category))
    with pytest.raises(ConfigurationError, match="save_as cannot be combined with inlines"):
        Copying(spec)
