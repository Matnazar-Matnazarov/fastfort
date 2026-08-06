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


def hosts_in(directive: str) -> list[str]:
    """The origins a directive admits, dropping the keywords and schemes.

    `'self'`, `data:` and `blob:` are the page's own -- none of them is a server
    anything is fetched from. What these tests are about is which *hosts* the
    policy lets in, so they are read past.
    """
    return [
        source
        for source in directive.split()
        if not source.startswith("'") and not source.endswith(":")
    ]


async def test_tiles_are_blocked_until_a_project_names_a_host(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """The default policy is `default-src 'none'`, and a map is somebody else's
    images. Off is the honest default: that host learns which rows are being
    looked at and roughly where they are.
    """
    async with opened(backend) as client:
        policy = csp_of(await client.get("/admin/"))

    assert hosts_in(policy["img-src"]) == []


async def test_naming_a_tile_url_admits_exactly_that_host(client: httpx.AsyncClient) -> None:
    policy = csp_of(await client.get("/admin/"))

    assert hosts_in(policy["img-src"]) == ["https://tile.openstreetmap.org"]


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

    assert hosts_in(policy["img-src"]) == []


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
    party bound by that -- so FastFort renders whatever the project wrote.

    Handed to the map rather than printed under the field: on the map is where
    every other map puts it, and where it reads as a notice about the pictures
    instead of as help text for the input.
    """
    async with opened(
        backend, map_tile_url=TILES, map_attribution="© OpenStreetMap contributors"
    ) as client:
        body = await form_body(client)

    assert 'data-ff-map-credit="© OpenStreetMap contributors"' in body


# ---------------------------------------------------------------------------
# Handling
#
# A map you cannot tell is draggable is a map nobody drags. These check that the
# parts are served; the gestures themselves need a browser.
# ---------------------------------------------------------------------------


async def test_the_map_says_it_can_be_dragged(client: httpx.AsyncClient) -> None:
    """A crosshair says "click to place a point" and says nothing about the map
    moving, so the one gesture that makes a map a map was invisible."""
    sheet = (await client.get("/admin/static/fastfort.css")).text
    canvas = sheet[sheet.index(".ff-map__canvas {") :].split("}", 1)[0]

    assert "cursor: grab" in canvas
    assert 'data-ff-dragging="true"' in sheet
    assert "cursor: grabbing" in sheet


async def test_the_tiles_cannot_steal_the_drag(client: httpx.AsyncClient) -> None:
    """This is what made the map immovable, and it hid behind two other bugs.

    A tile is an `<img>`, and an image is draggable by default: pressing on one
    and moving started the browser's own drag-and-drop, which fires
    `pointercancel` and hands back a ghost of the tile. Every pan died two
    pixels in, on every tile, in every browser -- so the arithmetic fixes that
    came before this one changed nothing anybody could see.
    """
    sheet = (await client.get("/admin/static/fastfort.css")).text
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text
    tiles = sheet[sheet.index(".ff-map__tiles {") :].split("}", 1)[0]

    # The layer is inert, so every pointer event lands on the canvas that pans.
    assert "pointer-events: none" in tiles
    # And the attribute too, for the browsers that honour only that.
    assert 'draggable: "false"' in script
    assert 'addEventListener("dragstart"' in script


async def test_the_marker_is_a_pin_anchored_at_its_point(client: httpx.AsyncClient) -> None:
    """The place is where the tip touches the map. A dot has to be guessed at."""
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text
    sheet = (await client.get("/admin/static/fastfort.css")).text
    marker = sheet[sheet.index(".ff-map__marker {") :].split("}", 1)[0]

    assert 'icon("map-pin"' in script
    # The whole height and half the width, so the pin hangs above its coordinate.
    assert "margin: -30px 0 0 -15px" in marker


async def test_the_map_offers_to_find_where_you_are(client: httpx.AsyncClient) -> None:
    """The commonest thing anyone puts in a location field is where they are
    standing, and without this that means dragging there from the whole world."""
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text
    sprite = await form_body(client)

    assert "getCurrentPosition" in script
    assert 'button("crosshair"' in script
    # Offered only where the browser has it: geolocation needs a secure context,
    # and a button that can only ever fail is worse than no button.
    assert "if (navigator.geolocation)" in script
    # And the icon it names is one the sprite actually carries.
    assert 'id="ff-i-crosshair"' in sprite


async def test_the_locate_control_has_a_translated_label(client: httpx.AsyncClient) -> None:
    body = await form_body(client)

    assert "data-ff-t-my-location=" in body


# ---------------------------------------------------------------------------
# Zooming
#
# A zoom used to blank the map: every tile of the new level was requested, the
# old ones stayed frozen at the previous level's positions, and the view only
# changed once the last request had come back. What that looks like from a chair
# is a flash of empty canvas, a repaint, and then the zoom.
#
# The fix is the one every slippy map uses -- a layer per zoom level, so the level
# already on screen can be scaled to line up with the new one and stay underneath
# while its tiles load. These check the machinery is served; the geometry itself
# is arithmetic with no server side to assert against.
# ---------------------------------------------------------------------------


async def test_the_script_draws_a_layer_per_zoom_level(client: httpx.AsyncClient) -> None:
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text

    assert "ff-map__layer" in script
    assert "layerFor" in script
    # The backdrop: what the new level is drawn over rather than instead of.
    assert "backdrop" in script


async def test_a_layer_scales_from_its_own_corner(client: httpx.AsyncClient) -> None:
    """The coordinates inside a layer are absolute pixels of a world whose origin
    is its top-left corner. Scaling about the centre -- the CSS default -- would
    put every tile somewhere else.
    """
    sheet = (await client.get("/admin/static/fastfort.css")).text
    rule = sheet[sheet.index(".ff-map__layer") :].split("}", 1)[0]

    assert "transform-origin: 0 0" in rule


async def test_tiles_are_not_deferred(client: httpx.AsyncClient) -> None:
    """A lazily loaded tile never fires the event that retires the backdrop, so
    the level underneath would stay there for as long as the map was off screen.
    """
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text
    built = script[script.index('class: "ff-map__tile"') :].split("});", 1)[0]

    assert 'loading: "lazy"' not in built
    assert 'decoding: "async"' in built


# ---------------------------------------------------------------------------
# Zooming past the imagery, and not asking a tile server for everything at once
# ---------------------------------------------------------------------------


async def test_the_view_zooms_further_than_the_tiles_go(client: httpx.AsyncClient) -> None:
    """Pressing + at the old ceiling did nothing at all -- no movement, no
    disabled button -- which reads as the map having broken rather than as the
    map having run out of pictures.

    Two ceilings now. Requests stop at the deepest level the server has, because
    asking past it returns a 404 and a failed tile is a blank square; the view
    keeps going and scales the last level up, which is what every map does past
    its own imagery.
    """
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text

    assert "const MAX_TILE_ZOOM = 19" in script
    assert "const MAX_ZOOM = 21" in script
    # Tiles are requested at the tile level, never at the view's zoom.
    assert 'replace("{z}", String(tileZoom))' in script
    # And the button says so when there is nowhere further to go.
    assert "this.zoomIn.disabled = this.zoom >= MAX_ZOOM" in script
    assert "this.zoomOut.disabled = this.zoom <= MIN_ZOOM" in script


async def test_the_shape_is_placed_in_the_views_pixels_not_the_tiles(
    client: httpx.AsyncClient,
) -> None:
    """The two zooms are equal up to MAX_TILE_ZOOM and diverge past it. Tile
    indices belong to the tile level, because that is the grid the server
    serves; the marker, the shape and every handle belong to the view. Mixing
    them put every vertex a whole world to one side, but only past zoom 19,
    which is exactly the kind of bug that ships."""
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text
    assert "this.drawShape(originX, width, 2 ** this.zoom)" in script


async def test_a_map_is_not_built_until_it_is_scrolled_near(
    client: httpx.AsyncClient,
) -> None:
    """One geometry column was fine. Nine -- which is what a page of shapes is
    -- fired every map's first draw at once, twenty-odd tiles each: about two
    hundred requests in one burst to one tile server. OpenStreetMap's policy
    says not to and it answers by throttling, a throttled tile fails, and a
    failed tile is `opacity: 0`. The map came out with a rectangular hole in
    it."""
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text

    assert "IntersectionObserver" in script
    # A screen early, so scrolling to a field finds it drawn rather than filling.
    assert 'rootMargin: "100% 0px"' in script
    # And still built outright where the browser has no observer to offer.
    assert "else build(control)" in script


async def test_a_failed_tile_is_asked_for_once_more(client: httpx.AsyncClient) -> None:
    """Throttling is temporary, and the alternative is a permanent hole. Once
    only: a server that has refused twice is saying something, and a map that
    keeps retrying is the behaviour those usage policies exist to stop."""
    script = (await client.get("/admin/static/js/fastfort-geo.js")).text

    assert "let retried = false" in script
    # A different URL, so the browser's negative cache is not what answers.
    assert "retry=1" in script
