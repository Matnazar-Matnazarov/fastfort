"""The controls the column types added in this round are drawn with.

Every assertion here is about markup the server produced with no script having
run, because that is the whole claim: a `daterange` is two date boxes and a
bounds selector, an `hstore` is a textarea of `key: value` lines, and an `inet`
is a text box with a pattern -- before `fastfort.js` has upgraded a single one
of them. The browser-side editors come later and are built on top of these; if
one of these stops being rendered, the editor built on it has nothing to attach
to and nothing else in the suite notices.

The models come from `tests/orm/exotic_models.py` and their tables are never
created. They do not need to be: the add form is built from the `ModelSpec`,
which `introspect_model` reads off mapper metadata, and a form with no relations
to populate never issues a query. That is what lets a PostgreSQL-only column
type be rendered and asserted on the SQLite run everyone actually executes.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.exotic_models import ExoticColumn, SpatialColumn
from tests.orm.models import StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

#: A tile host, so the geometry control emits its map hooks. Any URL will do --
#: nothing here fetches a tile, and the CSP only cares that there is exactly one.
TILES = "https://tile.example.org/{z}/{x}/{y}.png"


def exotic_backend(session_factory: async_sessionmaker[AsyncSession]) -> SQLAlchemyBackend:
    """A backend with no declarative base pinned.

    The suite's own `backend` fixture pins `tests.orm.models.Base`, and
    `supports()` is an `issubclass` against it -- so the models here, which sit
    on their own base precisely so their PostgreSQL-only columns never reach
    `create_all`, are refused at mount time. Left unpinned, the backend accepts
    any mapped class, which is what a single admin covering two bases needs.
    """
    return SQLAlchemyBackend(session_factory=session_factory)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            ui={"map_tile_url": TILES},  # type: ignore[arg-type]
        ),
        backend=exotic_backend(session_factory),
    )
    fort.set_user_model(StaffUser)
    fort.register(ExoticColumn, admin.ModelAdmin, key="lab.exotic")
    fort.register(SpatialColumn, admin.ModelAdmin, key="lab.spatial")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def add_form(client: httpx.AsyncClient, key: str) -> str:
    response = await client.get(f"/admin/{key}/add")
    assert response.status_code == 200
    return response.text


def control_for(body: str, name: str) -> str:
    """The one tag that submits `name`, so an assertion cannot pass by matching
    an attribute that belongs to a different field further down the page."""
    match = re.search(rf"<(?:input|textarea|select)\b[^>]*\bname=\"{re.escape(name)}\"[^>]*>", body)
    assert match is not None, f"no control submits {name!r}"
    return match.group(0)


# ---------------------------------------------------------------------------
# Every new type is editable at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["ip", "network", "mac", "attributes", "price", "flags", "permissions", "nickname", "tags"],
)
async def test_every_new_type_renders_an_editable_control(
    client: httpx.AsyncClient, name: str
) -> None:
    """Not a read-only row. Each of these fell through to `FieldType.UNKNOWN`
    before the type registry, and `UNKNOWN` renders as text nobody can edit --
    an admin that cannot change its own data."""
    control = control_for(await add_form(client, "lab.exotic"), name)
    assert "disabled" not in control
    assert "readonly" not in control


@pytest.mark.parametrize("name", ["search", "legacy_id", "blob"])
async def test_a_column_nothing_can_write_is_not_offered_as_a_box(
    client: httpx.AsyncClient, name: str
) -> None:
    """A `tsvector` is an index Postgres maintains, an `oid` is a system value
    and a `bytea` is not typeable. Offering a box for any of them invites
    someone to fill it in and be told off by the database -- so the classifier
    marks them read-only, which folds into `editable` and keeps them out of the
    form entirely."""
    body = await add_form(client, "lab.exotic")
    assert re.search(rf"<(?:input|textarea)\b[^>]*\bname=\"{name}\"", body) is None


# ---------------------------------------------------------------------------
# The controls themselves
# ---------------------------------------------------------------------------


async def test_an_address_box_carries_a_pattern_and_the_hook_that_upgrades_it(
    client: httpx.AsyncClient,
) -> None:
    control = control_for(await add_form(client, "lab.exotic"), "ip")
    assert "data-ff-inet" in control
    assert "pattern=" in control
    # `spellcheck` off: a browser underlining an IP address in red as a spelling
    # mistake reads as the field rejecting it.
    assert 'spellcheck="false"' in control


async def test_a_mac_box_accepts_every_spelling_the_parser_does(
    client: httpx.AsyncClient,
) -> None:
    """The pattern is a hint, not the validation -- but a hint stricter than the
    parser rejects values the server would have taken, which is worse than none."""
    control = control_for(await add_form(client, "lab.exotic"), "mac")
    pattern = re.search(r'pattern="([^"]+)"', control)
    assert pattern is not None
    expression = re.compile(pattern.group(1).replace("&#34;", '"'))
    for spelling in ("aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "aabbccddeeff"):
        assert expression.fullmatch(spelling), spelling


async def test_an_hstore_is_a_textarea_of_lines_not_a_single_box(
    client: httpx.AsyncClient,
) -> None:
    control = control_for(await add_form(client, "lab.exotic"), "attributes")
    assert control.startswith("<textarea")
    assert "data-ff-keyvalue" in control


async def test_an_array_carries_the_type_of_its_own_entries(
    client: httpx.AsyncClient,
) -> None:
    """`ratings` is `ARRAY(Integer)`. Without the item type on the control there
    is nothing for the browser to validate an entry against, and "banana" only
    fails after a round trip."""
    body = await add_form(client, "lab.exotic")
    assert 'data-ff-item-type="integer"' in control_for(body, "ratings")
    assert 'data-ff-item-type="string"' in control_for(body, "tags")


async def test_a_bit_string_only_accepts_bits(client: httpx.AsyncClient) -> None:
    control = control_for(await add_form(client, "lab.exotic"), "flags")
    assert 'pattern="[01]*"' in control


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


async def test_a_range_is_two_boxes_and_a_bounds_selector(
    client: httpx.AsyncClient,
) -> None:
    """Not one text box carrying `[1, 10)`. Typing brackets and a comma is a
    hostile control for a date range, and splitting it is what lets each bound
    pick up the same calendar a plain date column gets."""
    body = await add_form(client, "lab.exotic")
    for suffix in ("__lower", "__upper", "__bounds"):
        control_for(body, f"booking{suffix}")
    assert re.search(r'<input\b[^>]*\bname="booking"', body) is None


async def test_each_bound_gets_the_control_its_own_type_would_get(
    client: httpx.AsyncClient,
) -> None:
    """A `tsrange`'s ends are datetimes, so they carry `data-ff-date` and become
    the calendar. An `int4range`'s ends are numbers and must not."""
    body = await add_form(client, "lab.exotic")
    assert "data-ff-date" in control_for(body, "booking__lower")
    assert "data-ff-date" not in control_for(body, "page_numbers__lower")
    assert 'type="number"' in control_for(body, "page_numbers__upper")


async def test_a_multirange_stays_one_textarea(client: httpx.AsyncClient) -> None:
    """Repeatable pairs of boxes need script to add and remove rows, and a
    control that only half exists without it is worse than a textarea holding
    exactly what the parser reads."""
    control = control_for(await add_form(client, "lab.exotic"), "bookings")
    assert control.startswith("<textarea")


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


async def test_a_point_stays_one_line_and_a_polygon_does_not(
    client: httpx.AsyncClient,
) -> None:
    """A point reads as one line. A polygon's rings do not fit on one, and
    scrolling a text input sideways to check coordinates is what sends people
    around the admin to edit the database directly."""
    body = await add_form(client, "lab.spatial")
    assert control_for(body, "location").startswith("<input")
    assert control_for(body, "area").startswith("<textarea")


async def test_every_geometry_kind_gets_the_map_hooks(client: httpx.AsyncClient) -> None:
    """Not only points. The editor is built per kind, so the kind, the SRID and
    whether it is a geography rather than a geometry all have to reach it -- the
    last one decides whether a distance is metres or degrees."""
    body = await add_form(client, "lab.spatial")
    for name, kind, geography in (("location", "POINT", "false"), ("area", "POLYGON", "true")):
        control = control_for(body, name)
        assert f'data-ff-geometry-kind="{kind}"' in control
        assert 'data-ff-srid="4326"' in control
        assert f'data-ff-geography="{geography}"' in control


async def test_a_raster_is_not_offered_as_a_box(client: httpx.AsyncClient) -> None:
    """PostGIS rasters are not editable as text by any stretch. The registry
    classifies one read-only rather than drawing a control that cannot work."""
    body = await add_form(client, "lab.spatial")
    assert re.search(r'<(?:input|textarea)\b[^>]*\bname="heatmap"', body) is None


async def test_no_map_host_means_no_map_hooks(
    session_factory: async_sessionmaker[AsyncSession], staff_user: StaffUser
) -> None:
    """The admin's CSP names exactly one tile host and starts at `img-src
    'none'`. Without a configured one there is nowhere for a tile to come from,
    so the field is a plain box rather than a map frame that can never load."""
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=exotic_backend(session_factory),
    )
    fort.set_user_model(StaffUser)
    fort.register(SpatialColumn, admin.ModelAdmin, key="lab.spatial")
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        body = await add_form(opened, "lab.spatial")

    assert "data-ff-map" not in body
    # Still editable, though -- the text box is the control, the map was only
    # ever the thing beside it.
    control_for(body, "location")
