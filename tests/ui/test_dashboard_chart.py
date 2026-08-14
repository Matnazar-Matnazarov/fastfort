"""The signups chart on the dashboard.

The counts already on the dashboard answer "how much is there". This answers "is
it growing", which is the question anyone opening an admin every morning is
actually asking.

The parts worth pinning are the ones that fail quietly: the column is found by
convention, so a model that does not have one has to leave the chart off rather
than raise; and the bars are heights in percent, so a scaling mistake draws a
chart that is wrong rather than one that is missing.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.insights import Bar, Series, build_series, signup_field
from fastfort.orm.sqlalchemy import SQLAlchemyBackend, introspect_model
from fastfort.spec import FieldSpec, FieldType, ListQuery, ModelSpec

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"


def build(backend: SQLAlchemyBackend, **admin_settings: object) -> FastAPI:
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

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def dashboard(client: httpx.AsyncClient) -> str:
    response = await client.get("/admin/")
    assert response.status_code == 200, response.text
    return response.text


def spec_with(*fields: FieldSpec) -> ModelSpec:
    return ModelSpec(
        key="a.b",
        name="Thing",
        verbose_name="Thing",
        verbose_name_plural="Things",
        fields=(field("id", FieldType.INTEGER), *fields),
        primary_key=("id",),
    )


def field(name: str, kind: FieldType = FieldType.DATETIME) -> FieldSpec:
    return FieldSpec(name=name, label=name.title(), type=kind)


# ---------------------------------------------------------------------------
# Finding the column
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["date_joined", "joined_at", "registered_at", "signed_up_at", "created_at", "created"]
)
def test_a_conventional_name_is_found(name: str) -> None:
    assert signup_field(spec_with(field(name))) == name


def test_the_preferred_name_wins_when_a_model_has_several() -> None:
    """A Django model carries `date_joined` *and* often a generic `created_at`.
    On a user model the first one means exactly this and nothing else."""
    found = signup_field(spec_with(field("created_at"), field("date_joined")))

    assert found == "date_joined"


def test_a_model_with_no_such_column_has_no_series() -> None:
    """The chart is left off. Guessing at some other date column would draw a
    chart with a title that does not describe it."""
    assert signup_field(spec_with(field("name", FieldType.STRING))) == ""


def test_a_date_column_counts_even_without_a_time() -> None:
    assert signup_field(spec_with(field("created_at", FieldType.DATE))) == "created_at"


def test_a_configured_name_overrides_the_conventional_one() -> None:
    spec = spec_with(field("created_at"), field("onboarded_at"))

    assert signup_field(spec, "onboarded_at") == "onboarded_at"


@pytest.mark.parametrize("configured", ["nope", "name"])
def test_a_configured_name_that_is_not_a_date_column_leaves_the_chart_off(
    configured: str,
) -> None:
    """Rather than raising. A mistyped setting should cost a chart, not the
    dashboard -- and it must not silently fall back to a different column,
    because then the setting appears to work while charting something else.
    """
    spec = spec_with(field("name", FieldType.STRING), field("created_at"))

    assert signup_field(spec, configured) == ""


# ---------------------------------------------------------------------------
# Scaling the bars
# ---------------------------------------------------------------------------


def series(*counts: int) -> Series:
    start = dt.date(2026, 1, 1)
    return Series(
        field="created_at",
        bars=tuple(
            Bar(day=start + dt.timedelta(days=index), count=count)
            for index, count in enumerate(counts)
        ),
    )


def test_the_tallest_bar_fills_the_chart() -> None:
    chart = series(1, 5, 2)

    assert chart.height(chart.bars[1]) == 100


def test_a_day_with_nothing_has_no_bar() -> None:
    chart = series(0, 5)

    assert chart.height(chart.bars[0]) == 0


def test_a_very_small_day_still_draws() -> None:
    """One signup beside four hundred is still a signup. Rounded to zero it
    would read as "nothing happened" rather than as "very little did"."""
    chart = series(1, 400)

    assert chart.height(chart.bars[0]) == 2


def test_an_empty_week_does_not_divide_by_zero() -> None:
    """A new install has one, and it should draw as a flat baseline rather than
    raise on the first dashboard anyone opens."""
    chart = series(0, 0, 0)

    assert chart.peak == 1
    assert chart.total == 0
    assert all(chart.height(bar) == 0 for bar in chart.bars)


def test_the_total_is_the_sum_of_the_bars() -> None:
    assert series(1, 2, 3).total == 6


# ---------------------------------------------------------------------------
# Counting
# ---------------------------------------------------------------------------


async def test_the_series_counts_one_bar_per_day(backend: SQLAlchemyBackend) -> None:
    async with backend.unit_of_work() as uow:
        chart = await build_series(
            backend.adapter(StaffUser, uow, key="accounts.user"),
            introspect_model(StaffUser, key="accounts.user"),
            days=7,
        )

    assert chart.field == "created_at"
    assert len(chart.bars) == 7
    assert chart.bars[-1].day == dt.date.today()  # noqa: DTZ011


async def test_a_day_belongs_to_exactly_one_bar(backend: SQLAlchemyBackend) -> None:
    """The window is half-open, so a row created at midnight is counted once.
    Closed on both ends, every such row would appear in two bars and the total
    would exceed the number of rows that exist.
    """
    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(StaffUser, uow, key="accounts.user")
        everything = await adapter.count(ListQuery())
        chart = await build_series(
            adapter, introspect_model(StaffUser, key="accounts.user"), days=365
        )

    assert chart.total <= everything


async def test_a_model_without_the_column_yields_an_empty_series(
    backend: SQLAlchemyBackend,
) -> None:
    async with backend.unit_of_work() as uow:
        chart = await build_series(
            backend.adapter(Category, uow, key="shop.category"),
            introspect_model(Category, key="shop.category"),
            days=7,
        )

    assert chart.field == ""
    assert chart.bars == ()


# ---------------------------------------------------------------------------
# On the page
# ---------------------------------------------------------------------------


async def test_the_dashboard_draws_the_chart(client: httpx.AsyncClient) -> None:
    """The default dashboard plots the user model as an area chart.

    One `<polyline>` of thirty points rather than thirty boxes: the shape of a
    month reads better as a line, and it is one attribute instead of thirty
    elements. The bar form is still there behind `Trend(kind="bars")`.
    """
    body = await dashboard(client)

    assert "ff-plot__line" in body
    assert body.count('ff-plot__slot"') == 30


async def test_the_chart_is_readable_without_seeing_it(client: httpx.AsyncClient) -> None:
    """Bars mean nothing read one at a time, so the same numbers are also a
    table -- the version a screen reader is offered."""
    body = await dashboard(client)

    assert 'role="img"' in body
    assert "ff-sr-only" in body
    assert "<caption>" in body


async def test_the_hidden_table_does_not_grow_the_page(client: httpx.AsyncClient) -> None:
    """The clip has to be on a wrapper, because a table ignores it.

    A `<table class="ff-sr-only">` keeps its full height -- `height` is a minimum
    for a table -- so an invisible 30-row box stayed in the page's scrollable
    overflow and made the document taller than the shell around it. The sidebar
    is sticky inside that shell, so the extra scroll had nothing to hold it and
    it went off the top of the screen, which is how this was noticed at all.
    """
    body = await dashboard(client)

    assert '<table class="ff-sr-only"' not in body
    assert '<div class="ff-sr-only">' in body


async def test_the_chart_carries_no_script(client: httpx.AsyncClient) -> None:
    """It is drawn by the server. A chart that needs script to appear is a chart
    that is missing from the first paint of every dashboard."""
    body = await dashboard(client)
    chart = body[body.index("ff-plot") : body.index("</table>")]

    assert "<script" not in chart
    assert "<canvas" not in chart


async def test_zero_days_switches_the_chart_off(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Each day is a query, so a project that does not want them should be able
    to stop paying for them."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, dashboard_days=0)),
        base_url="http://testserver",
    ) as client:
        await sign_in(client)
        body = await dashboard(client)

    assert "ff-plot" not in body


async def test_the_window_follows_the_setting(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, dashboard_days=7)),
        base_url="http://testserver",
    ) as client:
        await sign_in(client)
        body = await dashboard(client)

    assert body.count('ff-plot__slot"') == 7
