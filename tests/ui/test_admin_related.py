"""The buttons beside a foreign key, and the popup they open.

Django has had this for twenty years and it is the thing people miss first:
without it, "the category does not exist yet" means abandoning the half-filled
form you are on, creating the category, and starting again.

The contract has three halves. The form has to know where a relation points; the
target's form has to render without the shell when opened as a popup; and saving
it has to hand the new row back rather than redirecting somewhere the opener
cannot see.
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
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "category")
    verbose_name_plural = "Products"


class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Categories"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
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


async def body_of(client: httpx.AsyncClient, path: str, **kwargs: Any) -> str:
    response = await client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    return response.text


async def token(client: httpx.AsyncClient, path: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', await body_of(client, path))
    assert match
    return match.group(1)


# ---------------------------------------------------------------------------
# Knowing where a relation points
# ---------------------------------------------------------------------------


def test_a_relation_names_the_key_its_target_is_registered_under(
    backend: SQLAlchemyBackend,
) -> None:
    """Introspection derives a key from the model's module by default.

    Nothing told the backend about the registry, so `Product.category` came back
    as `tests.orm.category` -- a key nothing could look up. Every feature that
    resolves a relation to the admin behind it was quietly doing nothing as a
    result: the autocomplete fell back to guessing which columns to search, and
    the buttons beside a foreign key never appeared at all.
    """
    fort = FastFort(
        FastFortSettings(secret_key=SECRET),  # type: ignore[call-arg]
        backend=backend,
    )
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, CategoryAdmin, key="shop.category")

    spec = fort.backend.introspect(Product, key="shop.product")
    targets = {
        field.name: field.relation.target
        for field in spec
        if field.is_relation and field.relation is not None
    }

    assert targets["category"] == "shop.category"


def test_an_unregistered_target_keeps_the_derived_key(backend: SQLAlchemyBackend) -> None:
    """Pointing at a model with no admin page of its own is perfectly normal."""
    fort = FastFort(
        FastFortSettings(secret_key=SECRET),  # type: ignore[call-arg]
        backend=backend,
    )
    fort.register(Product, ProductAdmin, key="shop.product")

    spec = fort.backend.introspect(Product, key="shop.product")
    target = next(
        field.relation.target
        for field in spec
        if field.name == "category" and field.relation is not None
    )

    assert target != "shop.category"


# ---------------------------------------------------------------------------
# The buttons
# ---------------------------------------------------------------------------


async def test_a_relation_field_carries_add_and_open_buttons(
    client: httpx.AsyncClient,
) -> None:
    body = await body_of(client, "/admin/shop.product/1/")

    assert 'data-ff-related="category"' in body
    assert 'data-ff-related-add="/admin/shop.category/add"' in body
    assert 'data-ff-related-base="/admin/shop.category"' in body
    for action in ("add", "change", "view"):
        assert f'data-ff-related-action="{action}"' in body, action


async def test_the_buttons_are_script_only(client: httpx.AsyncClient) -> None:
    """A popup that hands its result back has no meaning without script, and the
    picker itself works fine without them."""
    body = await body_of(client, "/admin/shop.product/1/")
    group = re.search(r'<div\s+class="([^"]*)"\s+data-ff-related="category"', body)

    assert group, "the button group should be findable"
    assert "ff-js-only" in group.group(1)


# ---------------------------------------------------------------------------
# The popup
# ---------------------------------------------------------------------------


async def test_a_popup_form_has_no_shell(client: httpx.AsyncClient) -> None:
    """It was opened from a field on another form: a sidebar listing every other
    model is noise, and there is nowhere for it to navigate to."""
    body = await body_of(client, "/admin/shop.category/add", params={"_popup": "1"})

    assert "ff-sidebar" not in body
    assert "ff-popup" in body
    # It is still the same form.
    assert "ff-form-grid" in body


async def test_the_same_form_keeps_its_shell_without_the_flag(
    client: httpx.AsyncClient,
) -> None:
    body = await body_of(client, "/admin/shop.category/add")
    assert "ff-sidebar" in body


async def test_a_popup_form_posts_back_to_a_popup_url(client: httpx.AsyncClient) -> None:
    """Otherwise the save is an ordinary one and the window never closes.

    The endpoint test below posts to the URL directly, so it cannot see this:
    the form has to *render* an action that still carries the flag, or the
    round trip through the browser breaks while the endpoint stays correct.
    """
    body = await body_of(
        client, "/admin/shop.category/add", params={"_popup": "1", "_field": "category"}
    )
    action = re.search(r'<form[^>]*\baction="([^"]+)"', body)

    assert action, "the form should have an action"
    assert "_popup=1" in action.group(1)
    assert "_field=category" in action.group(1)


async def test_saving_in_a_popup_returns_the_new_row(client: httpx.AsyncClient) -> None:
    """Rather than redirecting somewhere the window that opened it cannot see."""
    path = "/admin/shop.category/add?_popup=1&_field=category"
    response = await client.post(
        path,
        data={"name": "Made In A Popup", "_csrf": await token(client, path)},
    )

    assert response.status_code == 200
    assert 'data-ff-label="Made In A Popup"' in response.text
    assert 'data-ff-field="category"' in response.text
    assert re.search(r'data-ff-value="\d+"', response.text)


async def test_the_popup_result_carries_no_inline_script(
    client: httpx.AsyncClient,
) -> None:
    """The admin's CSP is `script-src 'self'` with no nonce, so the value is
    handed over in data attributes and the shipped script reads them."""
    path = "/admin/shop.category/add?_popup=1&_field=category"
    response = await client.post(
        path, data={"name": "No Inline", "_csrf": await token(client, path)}
    )

    assert not re.search(r"<script(?![^>]*\bsrc=)", response.text)


async def test_cancel_is_marked_to_close_the_popup(client: httpx.AsyncClient) -> None:
    """A plain link would navigate the popup window to the parent's list
    instead of closing it, leaving an abandoned form's window open on a page
    nothing points back to."""
    body = await body_of(client, "/admin/shop.category/add", params={"_popup": "1"})
    cancel = re.search(r"<a[^>]*>\s*Cancel\s*</a>", body)

    assert cancel, "the form should have a Cancel link"
    assert "data-ff-popup-cancel" in cancel.group(0)


async def test_cancel_is_a_plain_link_outside_a_popup(client: httpx.AsyncClient) -> None:
    """Outside a popup there is no window to close, so the marker would only
    ever do nothing -- Cancel stays the ordinary link back to the list."""
    body = await body_of(client, "/admin/shop.category/add")
    cancel = re.search(r"<a[^>]*>\s*Cancel\s*</a>", body)

    assert cancel, "the form should have a Cancel link"
    assert "data-ff-popup-cancel" not in cancel.group(0)


async def test_saving_outside_a_popup_still_redirects(client: httpx.AsyncClient) -> None:
    path = "/admin/shop.category/add"
    response = await client.post(
        path, data={"name": "Ordinary Save", "_csrf": await token(client, path)}
    )

    assert response.status_code == 303
