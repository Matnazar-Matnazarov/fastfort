"""`ModelAdmin.fieldsets`: the form in named sections.

A form with no `fieldsets` renders exactly as it always did -- one card, one
grid, every field. That is asserted here too, because the section machinery
runs for the ungrouped form as well (`Form.sections` returns a single unnamed
section) and a regression there would touch every form in every project.
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

_PRODUCT_SPEC = introspect_model(Product, key="shop.product")


class SectionedProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price")
    fieldsets = (
        (None, {"fields": ("name", "description")}),
        ("Pricing", {"fields": ("price", "stock"), "description": "What it costs."}),
        ("Logistics", {"fields": ("weight", "released_on"), "collapsed": True}),
    )


class PlainCategoryAdmin(admin.ModelAdmin):
    """Declares no `fieldsets`, so its form takes the single-section path."""

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
    fort.register(Product, SectionedProductAdmin, key="shop.sectioned")
    fort.register(Category, PlainCategoryAdmin, key="shop.category")
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


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


def test_a_section_cannot_name_a_field_the_model_has_no() -> None:
    class BadAdmin(admin.ModelAdmin):
        fieldsets = ((None, {"fields": ("name", "nope")}),)

    with pytest.raises(ConfigurationError, match="names 'nope'"):
        BadAdmin(_PRODUCT_SPEC)


def test_a_field_named_twice_is_an_error() -> None:
    """Two controls posting one name means the second silently wins."""

    class BadAdmin(admin.ModelAdmin):
        fieldsets = (
            (None, {"fields": ("name", "price")}),
            ("Pricing", {"fields": ("price",)}),
        )

    with pytest.raises(ConfigurationError, match="a second time"):
        BadAdmin(_PRODUCT_SPEC)


def test_leaving_out_a_required_field_is_an_error() -> None:
    """Otherwise the form renders, saves, and fails at the NOT NULL constraint
    with a name nobody outside the database recognises."""

    class BadAdmin(admin.ModelAdmin):
        fieldsets = ((None, {"fields": ("description",)}),)

    with pytest.raises(ConfigurationError, match="leaves out 'name'"):
        BadAdmin(_PRODUCT_SPEC)


def test_leaving_out_an_optional_field_is_allowed() -> None:
    """Narrowing a form is what a section list is for. `description`,
    `weight` and the rest are all nullable, so dropping them is a choice
    rather than a mistake."""

    class NarrowAdmin(admin.ModelAdmin):
        fieldsets = ((None, {"fields": ("name",)}),)

    NarrowAdmin(_PRODUCT_SPEC)  # does not raise


def test_the_error_names_every_problem_at_once() -> None:
    class BadAdmin(admin.ModelAdmin):
        fieldsets = ((None, {"fields": ("name", "nope", "alsonope")}),)

    with pytest.raises(ConfigurationError) as caught:
        BadAdmin(_PRODUCT_SPEC)

    assert "'nope'" in str(caught.value)
    assert "'alsonope'" in str(caught.value)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


async def test_each_section_renders_with_its_heading(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.sectioned/add")).text

    assert "ff-fieldset__title" in body
    assert "Pricing" in body
    assert "Logistics" in body
    assert "What it costs." in body


async def test_a_collapsed_section_is_shut_but_still_openable(client: httpx.AsyncClient) -> None:
    """`<details>` without `open`. It unfolds on click with no script at all,
    which is the reason it is a `<details>` rather than a class."""
    body = (await client.get("/admin/shop.sectioned/add")).text

    sections = re.findall(r"<details class=\"ff-card ff-fieldset\"[^>]*>", body)
    assert len(sections) == 2, sections
    # Pricing is open, Logistics is not.
    assert sum("open" in tag for tag in sections) == 1


async def test_the_unnamed_section_renders_without_a_heading(client: httpx.AsyncClient) -> None:
    """The opening group needs no name, and a `<summary>` saying nothing is
    worse than no summary."""
    body = (await client.get("/admin/shop.sectioned/add")).text

    opening = body.split("ff-fieldset__title")[0]
    assert 'id="ff-name"' in opening
    assert 'id="ff-description"' in opening


async def test_a_form_with_no_fieldsets_is_one_card(client: httpx.AsyncClient) -> None:
    """The ungrouped form still goes through `Form.sections`, so this is the
    assertion that the single-section path did not change what it renders."""
    body = (await client.get("/admin/shop.category/add")).text

    # Scoped to the fieldset class rather than to `<details>` itself: the
    # sidebar's account menu is a `<details>` too, on every page.
    assert "ff-fieldset__summary" not in body
    assert "ff-fieldset" not in body
    assert 'id="ff-name"' in body


async def test_fields_render_in_the_declared_order_not_the_spec_order(
    client: httpx.AsyncClient,
) -> None:
    """`stock` is declared after `price` in the Pricing section but comes
    before `weight` on the model, so spec order and section order disagree
    here on purpose."""
    body = (await client.get("/admin/shop.sectioned/add")).text

    assert body.index('id="ff-price"') < body.index('id="ff-stock"')
    assert body.index('id="ff-stock"') < body.index('id="ff-weight"')


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


async def test_a_sectioned_form_saves_every_section(client: httpx.AsyncClient) -> None:
    """Sections are a layout, not a boundary: one form, one POST, and a field
    inside a collapsed section is written like any other."""
    response = await submit(
        client,
        "/admin/shop.sectioned/add",
        name="Sectioned Widget",
        price="12.50",
        stock="3",
        weight="1.5",
    )

    assert response.status_code == 200
    assert "was created" in response.text

    listed = (await client.get("/admin/shop.sectioned/")).text
    assert "Sectioned Widget" in listed
