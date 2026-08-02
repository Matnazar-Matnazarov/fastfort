"""Bulk actions, the autocomplete endpoint and the active-filter chips.

These are the three parts of the list view that are not plain links, so each one
is a place where the browser and the server have to agree about a contract. The
tests below pin the contract rather than the markup.
"""

from __future__ import annotations

import html
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

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "category", "is_active")
    list_filter = ("is_active", "category")
    search_fields = ("name",)
    ordering = ("-id",)
    select_related = ("category",)
    actions = ("delete", "archive")
    verbose_name_plural = "Products"

    @admin.action("Archive", icon="box")
    async def archive(self, adapter: Any, objects: tuple[Any, ...]) -> str:
        for product in objects:
            await adapter.update(product, {"is_active": False})
        return f"{len(objects)} archived."


class LockedAdmin(admin.ModelAdmin):
    """A model whose rows must never be removed in bulk."""

    actions = ()
    verbose_name_plural = "Categories"


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
    fort.register(Category, LockedAdmin, key="shop.category")

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
    return html.unescape(response.text)


async def token(client: httpx.AsyncClient, path: str = "/admin/shop.product/") -> str:
    """The CSRF token as the page carries it.

    Read from the rendered form rather than from the cookie, because the cookie
    holds the signed half and the field holds the half a form has to send back.
    """
    match = re.search(r'name="_csrf" value="([^"]+)"', await body_of(client, path))
    assert match, "the page carries no CSRF token"
    return match.group(1)


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


async def test_the_action_bar_is_offered_when_actions_exist(
    client: httpx.AsyncClient,
) -> None:
    body = await body_of(client, "/admin/shop.product/")

    assert 'id="ff-bulkbar"' in body
    assert 'value="delete"' in body
    assert 'value="archive"' in body
    # A checkbox column only makes sense when there is something to do with it.
    assert "data-ff-select-row" in body


async def test_a_model_with_no_actions_has_no_checkbox_column(
    client: httpx.AsyncClient,
) -> None:
    """`actions = ()` is how a model says its rows are not bulk-editable."""
    body = await body_of(client, "/admin/shop.category/")

    assert 'id="ff-bulkbar"' not in body
    assert "data-ff-select-row" not in body


async def test_delete_removes_every_selected_row(client: httpx.AsyncClient) -> None:
    before = await body_of(client, "/admin/shop.product/")
    assert "Pixel Phone" in before

    response = await client.post(
        "/admin/shop.product/action",
        data={"action": "delete", "keys": ["1", "2"], "_csrf": await token(client)},
    )
    assert response.status_code == 303

    after = await body_of(client, "/admin/shop.product/")
    assert "Pixel Phone" not in after


async def test_a_custom_action_runs_and_reports(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/admin/shop.product/action",
        data={"action": "archive", "keys": ["1"], "_csrf": await token(client)},
    )
    assert response.status_code == 303

    after = await body_of(client, "/admin/shop.product/")
    assert "1 archived." in after


async def test_an_action_not_offered_is_refused(client: httpx.AsyncClient) -> None:
    """The check is against `actions`, not against what happens to be a method.

    Otherwise removing an action from the list would leave it reachable by
    anyone willing to post its name.
    """
    response = await client.post(
        "/admin/shop.category/action",
        data={
            "action": "delete",
            "keys": ["1"],
            "_csrf": await token(client, "/admin/shop.category/"),
        },
    )
    assert response.status_code == 303

    # The category is still there.
    assert "Phones" in await body_of(client, "/admin/shop.category/")


async def test_an_action_without_a_token_is_refused(client: httpx.AsyncClient) -> None:
    """A bulk delete is exactly the request a forged form would want to make."""
    response = await client.post(
        "/admin/shop.product/action",
        data={"action": "delete", "keys": ["1"]},
    )
    assert response.status_code == 303

    assert "Pixel Phone" in await body_of(client, "/admin/shop.product/")


async def test_an_action_over_nothing_says_so(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/admin/shop.product/action",
        data={"action": "delete", "_csrf": await token(client)},
    )
    assert response.status_code == 303
    assert "Nothing was selected." in await body_of(client, "/admin/shop.product/")


def test_an_action_naming_no_method_fails_at_declaration(
    backend: SQLAlchemyBackend,
) -> None:
    """A typo in `actions` is a start-up error, not a 500 on the first click."""

    class Broken(admin.ModelAdmin):
        actions = ("delete", "archve")

    spec = backend.introspect(Product, key="shop.product")
    with pytest.raises(ConfigurationError, match="archve"):
        Broken(spec)


def test_an_action_missing_its_decorator_fails_at_declaration(
    backend: SQLAlchemyBackend,
) -> None:
    class Undecorated(admin.ModelAdmin):
        actions = ("delete", "archive")

        async def archive(self, adapter: Any, objects: tuple[Any, ...]) -> str:
            return "done"

    spec = backend.introspect(Product, key="shop.product")
    with pytest.raises(ConfigurationError, match=re.escape("@admin.action")):
        Undecorated(spec)


# ---------------------------------------------------------------------------
# The CSRF cookie
# ---------------------------------------------------------------------------


async def test_a_page_restores_a_missing_csrf_cookie(client: httpx.AsyncClient) -> None:
    """Losing the cookie must not leave every form on every page dead.

    The gate mints a token when the browser has none and hands it to the
    templates. Nothing wrote it back, and it is a session cookie -- so closing
    the browser and returning rendered pages whose forms all carried a token with
    no cookie behind it. Every one of them failed, and the advice the failure
    gives, reload and try again, could not help: the reload minted another token
    and dropped that one too.
    """
    client.cookies.delete("fastfort_session_csrf")

    response = await client.get("/admin/shop.product/")
    assert response.status_code == 200

    restored = client.cookies.get("fastfort_session_csrf")
    assert restored, "the page must set the cookie it just rendered a token for"

    # And the token in the page is the one now in the jar, so a post works.
    assert restored == await token(client)
    posted = await client.post(
        "/admin/shop.product/action",
        data={"action": "archive", "keys": ["1"], "_csrf": restored},
    )
    assert posted.status_code == 303
    assert "1 archived." in await body_of(client, "/admin/shop.product/")


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------


async def test_autocomplete_returns_matching_rows(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/admin/shop.product/autocomplete", params={"field": "category", "q": "Pho"}
    )
    assert response.status_code == 200

    payload = response.json()
    assert [choice["label"] for choice in payload["results"]] == ["Phones"]
    assert payload["more"] is False


async def test_autocomplete_rejects_a_field_that_is_not_a_relation(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise it is a way to read any column of any model as a list."""
    response = await client.get(
        "/admin/shop.product/autocomplete", params={"field": "name", "q": ""}
    )
    assert response.status_code == 404


async def test_autocomplete_needs_a_session(backend: SQLAlchemyBackend) -> None:
    """It returns rows of the target table, so it sits behind the same gate."""
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        response = await anonymous.get(
            "/admin/shop.product/autocomplete", params={"field": "category"}
        )

    assert response.status_code in {302, 303, 401, 403}


# ---------------------------------------------------------------------------
# Active filters
# ---------------------------------------------------------------------------


async def test_an_active_filter_becomes_a_removable_chip(
    client: httpx.AsyncClient,
) -> None:
    """A filter left set three days ago is the commonest support question."""
    body = await body_of(client, "/admin/shop.product/", params={"category": "1"})

    assert "ff-filter-chip" in body
    assert "Filtered by" in body
    # The chip's link is the current view minus that one filter.
    assert "Phones" in body


async def test_a_search_term_is_a_chip_too(client: httpx.AsyncClient) -> None:
    body = await body_of(client, "/admin/shop.product/", params={"q": "pixel"})

    assert "ff-filter-chip" in body
    assert "pixel" in body


async def test_an_unfiltered_list_shows_no_chips(client: httpx.AsyncClient) -> None:
    body = await body_of(client, "/admin/shop.product/")
    assert "ff-filter-chip" not in body
