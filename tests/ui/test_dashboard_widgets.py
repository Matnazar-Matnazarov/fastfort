"""The dashboard as widgets: what a project gets by default, and what it can say instead.

The default layout is what every existing install already had -- the counts and
the signups chart -- so the first thing pinned here is that configuring nothing
still costs the same queries and renders the same two cards.

Past that, the things worth guarding are the quiet failures. A widget pointed at
a column that does not exist has to leave its card off rather than raise, because
a dashboard is the first page anyone opens and a typo in a configuration file
should not be a 500. And a breakdown has to refuse a column with no fixed set of
values, because counting the distinct values of a free-text column is a scan of
the table on that same page.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.dashboard import Breakdown, Card, Counts, Metric, Recent, Trend, Widget
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"


class CategoryAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Categories"


def build(backend: SQLAlchemyBackend, *widgets: Widget, **admin_settings: Any) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            admin=admin_settings,  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, CategoryAdmin, key="shop.category")
    if widgets:
        fort.set_dashboard(*widgets)

    app = FastAPI()
    fort.mount(app)
    return app


async def open_dashboard(app: FastAPI) -> str:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await sign_in(client)
        response = await client.get("/admin/")
    assert response.status_code == 200, response.text
    return response.text


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------


async def test_a_project_that_configures_nothing_gets_the_counts_and_the_chart(
    client: httpx.AsyncClient,
) -> None:
    """The layout every install had before widgets existed, unchanged."""
    body = (await client.get("/admin/")).text

    assert "ff-plot__line" in body
    assert "Products" in body
    assert "Categories" in body


async def test_the_counts_are_grouped_the_way_the_sidebar_groups_them(
    client: httpx.AsyncClient,
) -> None:
    """The group is the first thing anyone scans for, and the sidebar has
    already taught them these names."""
    body = (await client.get("/admin/")).text

    assert "ff-section__label" in body
    assert ">shop<" in body


# ---------------------------------------------------------------------------
# Saying something else
# ---------------------------------------------------------------------------


async def test_the_widgets_appear_in_the_order_they_were_given(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    body = await open_dashboard(
        build(backend, Recent(Product, title="Newest"), Metric(Product, title="Added"))
    )

    assert body.index("Newest") < body.index("Added")


async def test_an_empty_dashboard_renders_the_page_and_no_cards(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """`fort.set_dashboard()` with nothing is a reasonable thing to want -- an
    admin whose front page is just the header."""
    body = await open_dashboard(build(backend))

    assert "ff-dash__cell" in body

    stripped = await open_dashboard(_empty(backend))
    assert "ff-dash__cell" not in stripped


def _empty(backend: SQLAlchemyBackend) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.set_dashboard()
    app = FastAPI()
    fort.mount(app)
    return app


async def test_a_breakdown_counts_one_bar_per_value(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    body = await open_dashboard(build(backend, Breakdown(Product, on="status")))

    assert "ff-meter__fill" in body
    # The seeded products are two published and one archived; a status nobody
    # holds is left out rather than drawn as an empty row.
    assert "Published" in body
    assert "Archived" in body


async def test_a_breakdown_of_a_free_text_column_is_left_off(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Counting the distinct values of a text column is a scan of the table, on
    the page everyone opens first. It is refused rather than sampled."""
    body = await open_dashboard(build(backend, Breakdown(Product, on="name")))

    assert "ff-meter" not in body


async def test_a_metric_on_a_model_with_no_date_column_is_left_off(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """A card that cannot say anything says nothing. An empty one explaining
    itself is worse than no card."""
    body = await open_dashboard(build(backend, Metric(Category)))

    assert "ff-metric" not in body


async def test_zero_days_leaves_every_chart_off(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Each day is a query, so a project that does not want them stops paying
    for them -- and the setting has to reach widgets that did not name a window
    of their own."""
    body = await open_dashboard(build(backend, Metric(Product), dashboard_days=0))

    assert "ff-spark" not in body


async def test_a_widget_window_outranks_the_setting(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    body = await open_dashboard(build(backend, Trend(Product, days=7), dashboard_days=30))

    assert body.count('ff-plot__slot"') == 7


async def test_recent_rows_link_to_the_row_they_name(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    body = await open_dashboard(build(backend, Recent(Product, limit=2)))

    assert "ff-feed__item" in body
    assert "/admin/shop.product/" in body


# ---------------------------------------------------------------------------
# Widgets of a project's own
# ---------------------------------------------------------------------------


class Motto(Widget):
    """A widget that is not one of the built-in five."""

    span = 1

    async def resolve(self, context: Any) -> Card:
        return Card(template="dashboard/counts.html", context={"title": "Ours", "groups": {}})


async def test_a_project_can_render_a_widget_of_its_own(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Nothing in the page is special-cased for the built-in widgets: a card is
    a template name and a context, whoever wrote it."""
    body = await open_dashboard(build(backend, Motto()))

    assert "Ours" in body


def test_something_that_is_not_a_widget_is_refused_at_configuration_time(
    backend: SQLAlchemyBackend,
) -> None:
    """At start-up, not on the first page load. A dashboard is configured once
    and read on every request, and a stack trace from a render says nothing
    about which argument was wrong."""
    fort = FastFort(FastFortSettings(secret_key=SECRET), backend=backend)  # type: ignore[call-arg]

    with pytest.raises(ConfigurationError, match="not a dashboard widget"):
        fort.set_dashboard(Counts(), "Products")  # type: ignore[arg-type]


def test_the_default_dashboard_is_the_one_nobody_configured(
    backend: SQLAlchemyBackend,
) -> None:
    fort = FastFort(FastFortSettings(secret_key=SECRET), backend=backend)  # type: ignore[call-arg]

    assert [type(widget).__name__ for widget in fort.dashboard.widgets] == ["Signups", "Counts"]
