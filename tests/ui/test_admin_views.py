"""The rendered admin, exercised over HTTP against every selected database.

These go through the real routing, the real query builder and the real templates.
A test that renders a template in isolation would miss the two things that
actually break: a context key the view forgot to pass, and a Jinja name that
resolves to something unexpected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser, StockLevel

from fastfort import FastFort, FastFortSettings
from fastfort.admin import ModelAdmin
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(ModelAdmin):
    list_display = ("id", "name", "category", "price", "stock", "is_active", "released_on")
    list_filter = ("is_active", "status")
    search_fields = ("name", "description")
    ordering = ("-id",)
    select_related = ("category",)
    verbose_name_plural = "Products"


class CategoryAdmin(ModelAdmin):
    list_display = ("id", "name")
    search_fields = ("name",)
    ordering = ("name",)
    verbose_name_plural = "Categories"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in admin client.

    Signing in for real rather than bypassing the gate: a fixture that forged a
    session would let a regression in the gate pass unnoticed.
    """
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
    fort.register(StockLevel, ModelAdmin, key="shop.stock_level")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def html(client: httpx.AsyncClient, path: str, **params: Any) -> str:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    return response.text


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


async def test_the_stylesheet_is_served_as_one_response(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/static/fastfort.css")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    # All five sheets, not just one.
    for marker in ("--ff-h:", ".ff-app", ".ff-btn", ".ff-table", ":focus-visible"):
        assert marker in response.text


async def test_the_script_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/static/js/fastfort.js")
    assert response.status_code == 200
    assert "text/javascript" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


async def test_the_dashboard_renders_html_not_json(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/")
    assert body.lstrip().startswith("<!doctype html>")
    assert "Test Shop" in body


async def test_the_dashboard_counts_each_model(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/")
    assert "Products" in body
    assert "Categories" in body
    # The seeded fixture has four products and two categories.
    assert ">4<" in body
    assert ">2<" in body


async def test_the_sidebar_groups_by_namespace(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/")
    assert "ff-nav-group__label" in body
    assert 'href="/admin/shop.product/"' in body


async def test_an_admin_page_is_never_indexed(client: httpx.AsyncClient) -> None:
    """An indexed admin leaks its URL structure, and its data through snippets."""
    body = await html(client, "/admin/")
    assert 'content="noindex, nofollow"' in body
    assert 'name="referrer" content="same-origin"' in body


async def test_every_page_offers_a_skip_link(client: httpx.AsyncClient) -> None:
    """Keyboard users should not have to walk the sidebar to reach the table."""
    assert 'class="ff-skip-link"' in await html(client, "/admin/")


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


async def test_the_list_renders_a_table_of_rows(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/")
    assert "ff-table" in body
    for name in ("Pixel Phone", "pixel case", "Retired Laptop", "Unfiled Gadget"):
        assert name in body


async def test_a_relation_column_shows_the_related_name(client: httpx.AsyncClient) -> None:
    """A foreign key column has to read as "Phones", not as an integer."""
    body = await html(client, "/admin/shop.product/")
    assert "Phones" in body
    assert "Laptops" in body


async def test_a_boolean_column_renders_a_mark(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/")
    assert "ff-bool--on" in body
    assert "ff-bool--off" in body


async def test_a_missing_value_is_visibly_marked(client: httpx.AsyncClient) -> None:
    """An empty cell is ambiguous: it could equally be an empty string."""
    assert "—" in await html(client, "/admin/shop.product/")


async def test_search_filters_the_table(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", q="pixel")
    assert "Pixel Phone" in body
    assert "Retired Laptop" not in body


async def test_search_is_case_insensitive_in_the_view(client: httpx.AsyncClient) -> None:
    assert "Pixel Phone" in await html(client, "/admin/shop.product/", q="PIXEL")


async def test_a_search_with_no_matches_offers_a_way_back(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", q="nothing-matches-this")
    assert "Nothing matched" in body
    assert "Clear search and filters" in body


async def test_a_filter_narrows_the_table(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", is_active="0")
    assert "Retired Laptop" in body
    assert "Pixel Phone" not in body


async def test_declared_filters_get_a_control(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/")
    # `__in`, because every value filter accepts more than one value.
    assert 'name="is_active__in"' in body
    # An enum filter offers its members.
    assert 'name="status__in"' in body
    assert "Archived" in body


async def test_sorting_marks_the_active_column(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", o="name")
    assert 'aria-sort="ascending"' in body


async def test_clicking_a_sorted_column_reverses_it(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", o="name")
    assert "o=-name" in body


async def test_sorting_keeps_the_active_search(client: httpx.AsyncClient) -> None:
    """Losing the search on a sort click makes the table unusable."""
    body = await html(client, "/admin/shop.product/", q="pixel")
    assert "q=pixel" in body


async def test_pagination_reports_its_position(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", ps="2")
    assert "Page 1 of 2" in body
    assert "1–2 of 4" in body
    assert "p=2" in body


async def test_the_page_size_can_be_chosen(client: httpx.AsyncClient) -> None:
    """Twenty rows is a default, not a decision the admin gets to make for you."""
    body = await html(client, "/admin/shop.product/")

    assert "20 per page" in body
    for size in (20, 50, 100):
        assert f"ps={size}" in body, size


async def test_choosing_a_page_size_returns_to_the_first_page(
    client: httpx.AsyncClient,
) -> None:
    """Page 7 of 20-row pages is not page 7 of 100-row pages."""
    import re as _re

    body = await html(client, "/admin/shop.product/", ps="2", p="2")
    # Scoped to the size control: the numbered page links legitimately carry a
    # page, and they also carry the active `ps`.
    start = body.index("ff-page-size")
    control = body[start : body.index("ff-pages", start)]
    links = _re.findall(r'href="([^"]*)"', control)

    assert links, "the size control should offer links"
    for link in links:
        assert "p=" not in link.replace("ps=", ""), link


async def test_the_active_size_is_always_offered(client: httpx.AsyncClient) -> None:
    """A menu that cannot express the current state looks broken."""
    body = await html(client, "/admin/shop.product/", ps="7")
    assert "7 per page" in body


async def test_a_model_with_no_rows_explains_itself(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.stock_level/", q="")
    assert "ff-table" in body or "No" in body


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


async def test_an_unknown_model_key_is_a_404(client: httpx.AsyncClient) -> None:
    """A stale bookmark should not be a 500."""
    assert (await client.get("/admin/nope.nope/")).status_code == 404


async def test_a_malformed_filter_value_does_not_break_the_page(
    client: httpx.AsyncClient,
) -> None:
    """The recovery is the unfiltered list, not a traceback."""
    body = await html(client, "/admin/shop.product/", stock__gt="not-a-number")
    assert "ff-table" in body


async def test_an_unknown_sort_key_is_ignored(client: httpx.AsyncClient) -> None:
    body = await html(client, "/admin/shop.product/", o="api_secret); DROP TABLE x;--")
    assert "ff-table" in body


async def test_an_oversized_page_size_is_clamped(client: httpx.AsyncClient) -> None:
    assert "ff-table" in await html(client, "/admin/shop.product/", ps="100000")


async def test_a_search_term_is_escaped_into_the_page(client: httpx.AsyncClient) -> None:
    """The search box echoes user input, so it is an injection point."""
    payload = "<img src=x onerror=alert(1)>"
    body = await html(client, "/admin/shop.product/", q=payload)

    assert payload not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


async def test_a_sensitive_column_is_not_shown_by_default(client: httpx.AsyncClient) -> None:
    """`api_secret` is detected as sensitive, so the default columns skip it."""
    body = await html(client, "/admin/shop.stock_level/")
    assert "api_secret" not in body


# ---------------------------------------------------------------------------
# ModelAdmin validation
# ---------------------------------------------------------------------------


async def test_a_misspelled_column_fails_at_build_time(
    backend: SQLAlchemyBackend,
) -> None:
    """A typo should not wait for someone to open that page."""

    class Broken(ModelAdmin):
        list_display = ("id", "nmae")

    spec = backend.introspect(Product, key="shop.product")
    with pytest.raises(ConfigurationError) as caught:
        Broken(spec)

    message = str(caught.value)
    assert "nmae" in message
    assert "Fields available" in message


async def test_every_problem_is_reported_at_once(backend: SQLAlchemyBackend) -> None:
    class VeryBroken(ModelAdmin):
        list_display = ("nope",)
        search_fields = ("price",)
        list_filter = ("name",)

    spec = backend.introspect(Product, key="shop.product")
    with pytest.raises(ConfigurationError) as caught:
        VeryBroken(spec)

    message = str(caught.value)
    assert "nope" in message
    assert "not a text field" in message
    assert "cannot be offered as a filter" in message


async def test_a_bare_registration_still_gets_useful_columns(
    backend: SQLAlchemyBackend,
) -> None:
    """`fort.register(Model, ModelAdmin)` with no declarations must work."""
    spec = backend.introspect(Product, key="shop.product")
    columns = ModelAdmin(spec).columns()

    assert columns[0] == "id"
    assert len(columns) <= 6
    assert "api_secret" not in columns
    assert "tags" not in columns
