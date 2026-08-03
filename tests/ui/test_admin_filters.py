"""Filter controls and live list updates.

The live path and the plain path render the same fragment from the same context,
so most of these assert that both give the same answer -- a partial that quietly
diverges from the full page is the failure mode worth guarding.
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
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

PARTIAL = {"X-FastFort-Partial": "results"}


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "category", "price", "is_active", "released_on")
    # One of each kind: a boolean, an enumeration, a relation and a date.
    list_filter = ("is_active", "status", "category", "released_on")
    search_fields = ("name", "description")
    ordering = ("-id",)
    select_related = ("category",)
    verbose_name_plural = "Products"


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
    fort.register(Category, admin.ModelAdmin, key="shop.category")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def page(client: httpx.AsyncClient, path: str, **kwargs: Any) -> str:
    response = await client.get(path, **kwargs)
    assert response.status_code == 200, response.text
    return html.unescape(response.text)


# ---------------------------------------------------------------------------
# Filter controls
# ---------------------------------------------------------------------------


async def test_a_boolean_filter_offers_both_answers(client: httpx.AsyncClient) -> None:
    """`__in`, not a bare name: every value filter takes more than one value.

    "Show me cancelled and refunded" is the normal question, and answering it
    one value at a time means running the report twice.
    """
    body = await page(client, "/admin/shop.product/")
    assert 'name="is_active__in"' in body


async def test_an_enum_filter_offers_its_members(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/shop.product/")
    assert 'name="status__in"' in body
    assert "Archived" in body


async def test_a_relation_filter_lists_its_targets(client: httpx.AsyncClient) -> None:
    """A foreign key column is only useful as a filter if it names the rows."""
    body = await page(client, "/admin/shop.product/")

    assert 'name="category__in"' in body
    assert "Phones" in body
    assert "Laptops" in body


async def test_several_values_of_one_filter_apply_together(
    client: httpx.AsyncClient,
) -> None:
    """Two ticked boxes are one query, not two runs of the same report."""
    both = await page(client, "/admin/shop.product/", params={"category__in": "1,2"})
    assert "Pixel Phone" in both
    assert "Retired Laptop" in both

    # And the repeated form a plain checkbox group submits means the same thing.
    repeated = await page(client, "/admin/shop.product/?category__in=1&category__in=2")
    assert "Pixel Phone" in repeated
    assert "Retired Laptop" in repeated


async def test_a_date_filter_is_a_pair_of_bounds(client: httpx.AsyncClient) -> None:
    """ "Created this month" is the question people actually ask of a date."""
    body = await page(client, "/admin/shop.product/")

    assert 'name="released_on__gte"' in body
    assert 'name="released_on__lte"' in body
    assert 'type="date"' in body


async def test_a_relation_with_too_many_rows_is_searched_not_listed(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Past the cap the control searches the server instead of listing rows.

    It used to be dropped altogether, which is worse than it sounds: a filter
    that silently disappears once the target table grows leaves the person who
    set it up certain it worked, because when they tried it, it did.
    """
    from fastfort.admin.site import _filter_controls

    class Tiny(admin.ModelAdmin):
        list_filter = ("category",)

    spec = backend.introspect(Product, key="shop.product")
    model_admin = Tiny(spec)

    class Crowded:
        async def related_choices(
            self, field: str, term: str, *, limit: int, search_fields: Any = ()
        ) -> list[Any]:
            from fastfort.orm.base import RelatedChoice

            return [RelatedChoice(value=n, label=str(n)) for n in range(limit)]

    controls = await _filter_controls(
        spec,
        model_admin,
        {},
        Crowded(),
        relation_limit=5,
        autocomplete_url="/admin/shop.product/autocomplete",
    )

    assert len(controls) == 1
    assert controls[0]["kind"] == "search"
    assert controls[0]["url"] == "/admin/shop.product/autocomplete?field=category"
    # Nothing is inlined: the whole point is that the rows do not travel.
    assert controls[0]["choices"] == []


async def test_a_searched_relation_filter_keeps_the_active_option(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """The chosen row still has to render, or the control shows an empty box."""
    from fastfort.admin.site import _filter_controls

    class Tiny(admin.ModelAdmin):
        list_filter = ("category",)

    spec = backend.introspect(Product, key="shop.product")
    model_admin = Tiny(spec)

    class Crowded:
        async def related_choices(
            self, field: str, term: str, *, limit: int, search_fields: Any = ()
        ) -> list[Any]:
            from fastfort.orm.base import RelatedChoice

            return [RelatedChoice(value=n, label=f"Category {n}") for n in range(limit)]

    controls = await _filter_controls(
        spec, model_admin, {"category": "3"}, Crowded(), relation_limit=5
    )

    assert controls[0]["choices"] == [
        {"value": "3", "label": "Category 3", "selected": True},
    ]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


async def test_a_relation_filter_narrows_the_table(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/shop.product/", params={"category": "2"})

    assert "Retired Laptop" in body
    assert "Pixel Phone" not in body


async def test_a_date_range_narrows_from_both_ends(client: httpx.AsyncClient) -> None:
    later = await page(client, "/admin/shop.product/", params={"released_on__gte": "2026-02-01"})
    assert "pixel case" in later
    assert "Pixel Phone" not in later

    earlier = await page(client, "/admin/shop.product/", params={"released_on__lte": "2026-02-01"})
    assert "Pixel Phone" in earlier
    assert "pixel case" not in earlier


async def test_filters_combine(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/shop.product/", params={"category": "1", "is_active": "1"})

    assert "Pixel Phone" in body
    assert "Retired Laptop" not in body


async def test_a_filter_keeps_its_selection_visible(client: httpx.AsyncClient) -> None:
    """A filter that resets its own control after applying is unusable."""
    body = await page(client, "/admin/shop.product/", params={"is_active__in": "0"})
    assert re.search(r'value="0"[^>]*checked', body)

    # The single-value spelling older links carry still shows as applied.
    legacy = await page(client, "/admin/shop.product/", params={"is_active": "0"})
    assert re.search(r'value="0"[^>]*checked', legacy)


async def test_a_malformed_date_bound_does_not_break_the_page(
    client: httpx.AsyncClient,
) -> None:
    body = await page(client, "/admin/shop.product/", params={"released_on__gte": "not-a-date"})
    assert "ff-table" in body


# ---------------------------------------------------------------------------
# Live updates
# ---------------------------------------------------------------------------


async def test_the_partial_response_is_the_results_only(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/shop.product/", headers=PARTIAL)

    assert "ff-table" in body
    assert "Pixel Phone" in body
    # No shell: no sidebar, no topbar, no document.
    assert "ff-sidebar" not in body
    assert "<!doctype html>" not in body.lower()


async def test_the_partial_includes_pagination_and_sorting(
    client: httpx.AsyncClient,
) -> None:
    """They live inside the swapped fragment, so leaving them out would strand
    someone on page one with no way to sort."""
    body = await page(client, "/admin/shop.product/", params={"ps": "2"}, headers=PARTIAL)

    assert "ff-pagination" in body
    assert "ff-table__sort" in body


@pytest.mark.parametrize(
    "params",
    [
        {"q": "pixel"},
        {"category": "1"},
        {"is_active": "0"},
        {"o": "name"},
        {"released_on__gte": "2026-02-01"},
        {"p": "2", "ps": "2"},
    ],
)
async def test_the_partial_agrees_with_the_full_page(
    client: httpx.AsyncClient, params: dict[str, str]
) -> None:
    """The two paths render the same fragment from the same context. A partial
    that quietly diverges is the failure this guards."""
    full = await page(client, "/admin/shop.product/", params=params)
    partial = await page(client, "/admin/shop.product/", params=params, headers=PARTIAL)

    body = full.split('id="ff-results"', 1)[1]
    for name in ("Pixel Phone", "pixel case", "Retired Laptop", "Unfiled Gadget"):
        assert (name in body) == (name in partial), name


async def test_a_partial_request_still_needs_a_session(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """A fragment endpoint is still an endpoint; it must not become a side door."""
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
        response = await anonymous.get("/admin/shop.product/", headers=PARTIAL)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/login")


async def test_the_list_form_is_marked_for_live_updates(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/shop.product/")

    assert "data-ff-live" in body
    assert 'id="ff-results"' in body
    # And it is still a real GET form, so it works with JavaScript off.
    assert 'method="get"' in body


async def test_the_script_carries_the_live_behaviour(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/static/js/fastfort.js")).text
    assert "X-FastFort-Partial" in body
    assert "AbortController" in body
