"""`ModelAdmin.inlines`: children edited on the parent's page.

`Category` and `Product` are already a parent and its children in the shared
test models, so nothing new is added here -- which is also the point: an
inline is meant to work over relations a project already has.

The transactional assertions matter most. A child that fails to parse must
leave the parent unwritten too, because half an order is worse than none of
it, and that is the one thing a formset is easy to get wrong.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser, Tag

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.inlines import InlineAdmin
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.registry import default_model_key
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.introspect import introspect_model

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

# Introspected under the derived keys, because that is what a relation's
# `target` resolves to without a `FastFort` installing its own resolver --
# and an inline matches the child's foreign key against the parent's key.
# The app path below goes through the backend, where the resolver makes the
# registered key (`shop.category`) the one both sides see.
_CATEGORY_SPEC = introspect_model(Category, key=default_model_key(Category))
_PRODUCT_SPEC = introspect_model(Product, key=default_model_key(Product))
_TAG_SPEC = introspect_model(Tag, key=default_model_key(Tag))


class ProductInline(admin.TabularInline):
    model = Product
    # Narrowed deliberately: `description` is a textarea and `warranty` a
    # duration, neither of which belongs in a table cell.
    fields = ("name", "price", "stock")
    extra = 1


class CategoryWithProducts(admin.ModelAdmin):
    list_display = ("id", "name")
    inlines = (ProductInline,)


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
    fort.register(Category, CategoryWithProducts, key="shop.category")
    fort.register(StaffUser, admin.ModelAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def submit(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    body = (await client.get(path)).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, f"{path} must render a CSRF token"
    data.setdefault("_csrf", match.group(1))
    return await client.post(path, data=data, follow_redirects=True)


async def category_id(session_factory: async_sessionmaker[AsyncSession], name: str) -> int:
    async with session_factory() as session:
        found = (
            await session.execute(sa.select(Category).where(Category.name == name))
        ).scalar_one()
        return found.id


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_the_foreign_key_is_inferred() -> None:
    """`Product` points at `Category` exactly once, so nothing has to say so."""
    inline = ProductInline(_PRODUCT_SPEC, _CATEGORY_SPEC)
    assert inline.fk == "category"


def test_a_child_with_no_relation_to_the_parent_is_refused() -> None:
    class TagInline(admin.TabularInline):
        model = Tag

    with pytest.raises(ConfigurationError, match="no foreign key"):
        TagInline(_TAG_SPEC, _CATEGORY_SPEC)


def test_an_unknown_fk_name_is_refused() -> None:
    class Wrong(admin.TabularInline):
        model = Product
        fk_name = "nope"

    with pytest.raises(ConfigurationError, match="fk_name names 'nope'"):
        Wrong(_PRODUCT_SPEC, _CATEGORY_SPEC)


def test_a_control_too_tall_for_a_table_cell_is_refused() -> None:
    """A textarea in a row is worse than the full form it saved a trip to, so
    it is a start-up error rather than a cramped control."""

    class TooWide(admin.TabularInline):
        model = Product
        fields = ("name", "description")

    with pytest.raises(ConfigurationError, match="textarea"):
        TooWide(_PRODUCT_SPEC, _CATEGORY_SPEC)


def test_a_price_column_is_allowed() -> None:
    """The canonical inline is an order and its lines, and a line has a price.
    A widget list that refused `money`/`decimal` would refuse the example the
    feature exists for."""

    class WithPrice(admin.TabularInline):
        model = Product
        fields = ("name", "price")

    inline = WithPrice(_PRODUCT_SPEC, _CATEGORY_SPEC)
    assert [field.name for field in inline.columns] == ["name", "price"]


def test_the_foreign_key_is_not_offered_as_a_column() -> None:
    """A dropdown for it in every row is an invitation to move a line to a
    different parent by accident."""

    class Defaulted(admin.TabularInline):
        model = Product

    # Left to its default, `fields` covers the editable columns -- and the one
    # pointing back at the parent is not among them.
    inline_columns = {field.name for field in Defaulted(_PRODUCT_SPEC, _CATEGORY_SPEC).columns}
    assert "category" not in inline_columns


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_existing_children_render_as_rows(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    phones = await category_id(session_factory, "Phones")
    body = (await client.get(f"/admin/shop.category/{phones}/")).text

    # `Product` is deliberately not registered: an inline child does not have
    # to be, so its key is the derived `orm.product` and the prefix follows.
    assert 'data-ff-inline="orm_product"' in body
    # The two seeded phones, each with its primary key carried so a save can
    # tell an edit from an addition.
    assert body.count("-_pk") >= 2
    assert "Pixel Phone" in body


async def test_a_blank_row_is_offered_without_script(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`extra` defaults to one precisely so that adding a child does not
    require the "Add another" button, which is script-only."""
    phones = await category_id(session_factory, "Phones")
    body = (await client.get(f"/admin/shop.category/{phones}/")).text

    rows = re.findall(r'name="orm_product-(\d+)-name"', body)
    assert len(rows) >= 3, rows  # two seeded children plus the blank one


async def test_the_add_form_offers_inline_rows_too(client: httpx.AsyncClient) -> None:
    """There is no parent yet, so there are no children -- just the blanks."""
    body = (await client.get("/admin/shop.category/add")).text

    assert 'data-ff-inline="orm_product"' in body
    assert 'name="orm_product-0-name"' in body
    assert "-_pk" not in body


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


async def test_a_child_can_be_added_from_the_parents_form(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    laptops = await category_id(session_factory, "Laptops")

    response = await submit(
        client,
        f"/admin/shop.category/{laptops}/",
        name="Laptops",
        **{
            "orm_product-0-name": "Inline Laptop",
            "orm_product-0-price": "1500.00",
            "orm_product-0-stock": "4",
        },
    )
    assert response.status_code == 200

    async with session_factory() as session:
        found = (
            await session.execute(sa.select(Product).where(Product.name == "Inline Laptop"))
        ).scalar_one()
        assert found.category_id == laptops
        assert str(found.price) == "1500.00"


async def test_a_row_left_untouched_creates_nothing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A blank row is somebody who did not use it, not an empty child."""
    laptops = await category_id(session_factory, "Laptops")

    async with session_factory() as session:
        before = len((await session.execute(sa.select(Product))).scalars().all())

    await submit(client, f"/admin/shop.category/{laptops}/", name="Laptops")

    async with session_factory() as session:
        after = len((await session.execute(sa.select(Product))).scalars().all())
    assert after == before


async def test_an_existing_child_can_be_edited(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = (
            await session.execute(sa.select(Product).where(Product.name == "Pixel Phone"))
        ).scalar_one()
        product_id, parent = product.id, product.category_id

    await submit(
        client,
        f"/admin/shop.category/{parent}/",
        name="Phones",
        **{
            "orm_product-0-_pk": str(product_id),
            "orm_product-0-name": "Pixel Phone Renamed",
            "orm_product-0-price": "799.00",
            "orm_product-0-stock": "10",
        },
    )

    async with session_factory() as session:
        assert (await session.get(Product, product_id)).name == "Pixel Phone Renamed"


async def test_a_ticked_row_is_deleted(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        doomed = Product(name="Doomed Child", price=Decimal("1.00"), stock=1)
        parent = (
            await session.execute(sa.select(Category).where(Category.name == "Laptops"))
        ).scalar_one()
        doomed.category_id = parent.id
        session.add(doomed)
        await session.commit()
        doomed_id, parent_id = doomed.id, parent.id

    await submit(
        client,
        f"/admin/shop.category/{parent_id}/",
        name="Laptops",
        **{
            "orm_product-0-_pk": str(doomed_id),
            "orm_product-0-name": "Doomed Child",
            "orm_product-0-price": "1.00",
            "orm_product-0-stock": "1",
            "orm_product-0-_delete": "on",
        },
    )

    async with session_factory() as session:
        assert await session.get(Product, doomed_id) is None


async def test_a_child_that_fails_to_parse_writes_nothing_at_all(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The transactional assertion: a bad child leaves the parent unwritten
    too. Half a save is the failure a formset is easiest to get wrong."""
    laptops = await category_id(session_factory, "Laptops")

    response = await submit(
        client,
        f"/admin/shop.category/{laptops}/",
        name="Renamed But Should Not Stick",
        **{
            "orm_product-0-name": "Bad Child",
            "orm_product-0-price": "not-a-number",
            "orm_product-0-stock": "1",
        },
    )

    # The form comes back rather than redirecting, with the typed values intact.
    assert response.status_code == 200
    assert "Bad Child" in response.text

    async with session_factory() as session:
        # The parent kept its old name...
        assert (await session.get(Category, laptops)).name == "Laptops"
        # ...and the child was never created.
        assert (
            await session.execute(sa.select(Product).where(Product.name == "Bad Child"))
        ).first() is None


def test_the_inline_base_is_exported() -> None:
    """`admin.TabularInline` is the public name; `InlineAdmin` is its base."""
    assert issubclass(admin.TabularInline, InlineAdmin)
