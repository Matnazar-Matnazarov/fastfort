"""The map beside a geometry field.

A pair of coordinates is a number nobody can check: "51.5074, -0.1278" is either
the right place or a transposed pair a thousand miles away, and the only way to
tell is to look at it.

The map is off unless a project names a tile URL, and that is the part with
teeth. Turning it on means the admin fetches images from somebody else's server,
which the admin's `default-src 'none'` policy otherwise forbids -- so the setting
is also what widens the policy, and it must widen it by exactly one host.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"
TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"

    # A geometry column needs PostGIS, and this suite runs on SQLite by default.
    # The override drives the same template branch a real one would, which is
    # what these tests are about -- `widget_for(GEOMETRY) == "point"` is already
    # covered in tests/unit/test_exotic_columns.py.
    formfield_overrides: ClassVar[dict[str, str]] = {"description": "point"}


def build(backend: SQLAlchemyBackend, **ui: object) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            ui=ui,  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app


@asynccontextmanager
async def opened(backend: SQLAlchemyBackend, **ui: object) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, **ui)), base_url="http://testserver"
    ) as client:
        await sign_in(client)
        yield client


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with opened(backend, map_tile_url=TILES) as ready:
        yield ready


def csp_of(response: httpx.Response) -> dict[str, str]:
    return {
        directive.split(" ", 1)[0]: directive.split(" ", 1)[1] if " " in directive else ""
        for directive in (
            part.strip() for part in response.headers["content-security-policy"].split(";")
        )
        if directive
    }


# ---------------------------------------------------------------------------
# The policy
# ---------------------------------------------------------------------------


async def test_tiles_are_blocked_until_a_project_names_a_host(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """The default policy is `default-src 'none'`, and a map is somebody else's
    images. Off is the honest default: that host learns which rows are being
    looked at and roughly where they are.
    """
    async with opened(backend) as client:
        policy = csp_of(await client.get("/admin/"))

    assert policy["img-src"] == "'self' data:"


async def test_naming_a_tile_url_admits_exactly_that_host(client: httpx.AsyncClient) -> None:
    policy = csp_of(await client.get("/admin/"))

    assert policy["img-src"] == "'self' data: https://tile.openstreetmap.org"


async def test_the_rest_of_the_policy_is_untouched(client: httpx.AsyncClient) -> None:
    """A map is images. It has no business loosening the script policy, which is
    the directive that decides whether the admin can be made to run someone
    else's code.
    """
    policy = csp_of(await client.get("/admin/"))

    assert policy["script-src"] == "'self'"
    assert policy["default-src"] == "'none'"
    assert policy["connect-src"] == "'self'"


async def test_only_the_origin_is_admitted_not_the_path(client: httpx.AsyncClient) -> None:
    """A CSP source is an origin. Leaving the `{z}/{x}/{y}` template in would be
    a source no browser matches, which fails closed but silently."""
    policy = csp_of(await client.get("/admin/"))

    assert "{z}" not in policy["img-src"]
    assert ".png" not in policy["img-src"]


async def test_a_relative_tile_url_does_not_widen_the_policy(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Tiles served by the project itself are already covered by `'self'`."""
    async with opened(backend, map_tile_url="/tiles/{z}/{x}/{y}.png") as client:
        policy = csp_of(await client.get("/admin/"))

    assert policy["img-src"] == "'self' data:"


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------


async def form_body(client: httpx.AsyncClient) -> str:
    response = await client.get("/admin/shop.product/1/")
    assert response.status_code == 200, response.text
    return response.text


async def test_a_point_field_carries_the_tile_template(client: httpx.AsyncClient) -> None:
    body = await form_body(client)
    box = re.search(r'<input[^>]*\bname="description"[^>]*>', body, re.S)

    assert box, "the point field should render an input"
    assert f'data-ff-map="{TILES}"' in box.group(0)


async def test_a_point_field_has_no_map_without_a_tile_url(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """It stays the pair of coordinates it already was, which is a working
    control rather than an empty grey rectangle."""
    async with opened(backend) as client:
        body = await form_body(client)

    assert "data-ff-map" not in body
    assert 'name="description"' in body


async def test_the_coordinates_stay_the_control_that_submits(
    client: httpx.AsyncClient,
) -> None:
    """The map writes into the text box rather than replacing it. Without that
    the field stops working the moment script does, and stops working entirely
    for anyone who types or pastes coordinates.
    """
    body = await form_body(client)
    box = re.search(r'<input[^>]*\bname="description"[^>]*>', body, re.S)

    assert box
    assert 'type="text"' in box.group(0)
    assert 'name="description"' in box.group(0)


async def test_the_map_field_spans_the_form(client: httpx.AsyncClient) -> None:
    """A map in half a column is a map you cannot navigate."""
    body = await form_body(client)
    field = body[: body.index('name="description"')]

    assert "ff-field--wide" in field.rsplit("<div", 1)[-1]


async def test_the_attribution_is_rendered_when_configured(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """Tile services generally require a credit line, and the project is the
    party bound by that -- so FastFort renders whatever the project wrote."""
    async with opened(
        backend, map_tile_url=TILES, map_attribution="© OpenStreetMap contributors"
    ) as client:
        body = await form_body(client)

    assert "© OpenStreetMap contributors" in body
