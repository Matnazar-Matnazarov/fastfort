"""`date_hierarchy`: the year -> month -> day bar above a list.

The seeded products were released on 2026-01-15 and 2026-03-01, and two have no
release date at all -- which is what checks that an empty value neither ends
the bar early nor shows up as a year.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.hierarchy import Window, read_window
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.registry import default_model_key
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.introspect import introspect_model

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "released_on")
    search_fields = ("name",)
    # Narrowed on purpose: the drill must still work on a column the filter
    # panel does not offer.
    list_filter = ("is_active",)
    date_hierarchy = "released_on"


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

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def bar(client: httpx.AsyncClient, path: str = "/admin/shop.product/") -> tuple[str, str]:
    """The bar's markup and the page body, both unescaped."""
    response = await client.get(path)
    assert response.status_code == 200, response.text
    body = html.unescape(response.text)
    found = re.search(r'<nav class="ff-drilldown".*?</nav>', body, re.S)
    assert found, "the list should draw a date bar"
    return found.group(0), body


def links(markup: str) -> list[str]:
    return re.findall(r'class="ff-drilldown__link" href="[^"]*">([^<]+)<', markup)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


async def test_the_top_level_offers_the_years_with_data(client: httpx.AsyncClient) -> None:
    markup, _ = await bar(client)

    assert links(markup) == ["2026"]
    assert "All dates" in markup


async def test_a_year_offers_only_the_months_its_data_spans(client: httpx.AsyncClient) -> None:
    markup, body = await bar(
        client, "/admin/shop.product/?released_on__gte=2026-01-01&released_on__lt=2027-01-01"
    )

    assert links(markup) == ["January", "February", "March"]
    assert "Pixel Phone" in body
    assert "Retired Laptop" not in body  # no release date, so not in any year


async def test_a_month_offers_its_days_and_filters_the_rows(client: httpx.AsyncClient) -> None:
    markup, body = await bar(
        client, "/admin/shop.product/?released_on__gte=2026-03-01&released_on__lt=2026-04-01"
    )

    assert links(markup) == ["1"]
    assert "March 2026" in markup
    assert "pixel case" in body
    assert "Pixel Phone" not in body


async def test_a_day_is_the_bottom(client: httpx.AsyncClient) -> None:
    markup, _ = await bar(
        client, "/admin/shop.product/?released_on__gte=2026-01-15&released_on__lt=2026-01-16"
    )

    assert links(markup) == []
    assert 'aria-current="page">15<' in markup


async def test_the_links_keep_the_search(client: httpx.AsyncClient) -> None:
    markup, _ = await bar(client, "/admin/shop.product/?q=pixel")

    assert "q=pixel" in markup


async def test_the_toolbar_carries_the_drill_into_a_search(client: httpx.AsyncClient) -> None:
    """Without the hidden inputs a search typed inside March searched every year."""
    _, body = await bar(
        client, "/admin/shop.product/?released_on__gte=2026-03-01&released_on__lt=2026-04-01"
    )

    assert '<input type="hidden" name="released_on__gte" value="2026-03-01" />' in body
    assert '<input type="hidden" name="released_on__lt" value="2026-04-01" />' in body


async def test_the_bar_is_part_of_the_live_fragment(client: httpx.AsyncClient) -> None:
    response = await client.get(
        "/admin/shop.product/?q=pixel", headers={"X-FastFort-Partial": "results"}
    )

    assert 'class="ff-drilldown"' in response.text


async def test_the_bar_speaks_the_page_language(client: httpx.AsyncClient) -> None:
    await client.post("/admin/language", data={"language": "uz", "next": "/admin/"})
    markup, _ = await bar(
        client, "/admin/shop.product/?released_on__gte=2026-01-01&released_on__lt=2027-01-01"
    )

    assert links(markup) == ["Yanvar", "Fevral", "Mart"]
    assert "Barcha sanalar" in markup


# ---------------------------------------------------------------------------
# What is and is not a drill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gte", "lt", "level"),
    [
        ("2026-01-01", "2027-01-01", "year"),
        ("2026-12-01", "2027-01-01", "month"),
        ("2026-02-28", "2026-03-01", "day"),
    ],
)
def test_a_span_the_bar_built_is_read_back(gte: str, lt: str, level: str) -> None:
    window = read_window({"d__gte": gte, "d__lt": lt}, "d")

    assert window == Window(dt.date.fromisoformat(gte), dt.date.fromisoformat(lt))
    assert window.level == level


@pytest.mark.parametrize(
    "params",
    [
        {"d__gte": "2026-01-01"},
        {"d__gte": "2026-01-03", "d__lt": "2026-01-12"},
        {"d__gte": "2026-03-15", "d__lt": "2026-04-15"},
        {"d__gte": "nonsense", "d__lt": "2026-01-01"},
    ],
)
def test_any_other_range_is_not_mistaken_for_a_level(params: dict[str, str]) -> None:
    """A range someone typed shows the top level rather than a crumb that lies."""
    assert read_window(params, "d") is None


@pytest.mark.parametrize("field", ["name", "nope"])
def test_date_hierarchy_must_name_a_date_column(field: str) -> None:
    class Wrong(admin.ModelAdmin):
        date_hierarchy = field

    spec = introspect_model(Product, key=default_model_key(Product))
    with pytest.raises(ConfigurationError, match="date_hierarchy names"):
        Wrong(spec)
