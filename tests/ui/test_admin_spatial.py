"""Spatial filters, against a database rather than against a compiled string.

These only exist on PostGIS, so they only run there. Everything up to the SQL is
covered on every backend by `tests/unit/test_spatial_filters.py` -- the
allow-list, the operator vocabulary, the geometry being re-parsed rather than
passed through. What is left is the part no amount of unit testing can settle:
whether `ST_DWithin` over a geography actually returns the rows within five
kilometres, or whether the radius was quietly interpreted as five thousand
degrees and matched the planet.

Marked `postgres` so a SQLite or MySQL run skips them, and guarded again at
run time by asking the server for `postgis_version()`: `docker-compose.test.yml`
runs the PostGIS image, but somebody's own `FASTFORT_TEST_POSTGRES_URL` may not.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from geoalchemy2 import Geography, Geometry, WKBElement
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from tests.conftest import sign_in
from tests.orm.models import StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("seeded")]

#: Tashkent and Samarkand, about 270 km apart -- far enough that a radius can
#: tell them apart, close enough that both fit on one map.
TASHKENT = "41.2995, 69.2401"
SAMARKAND = "39.6270, 66.9597"
AROUND_TASHKENT = "POLYGON((69.1 41.2, 69.4 41.2, 69.4 41.4, 69.1 41.4, 69.1 41.2))"

#: Matches the label cell of a rendered row, so an assertion cannot pass by
#: finding the name in a filter control or a page title.
CELL = re.compile(r"<td[^>]*>\s*(?:<a[^>]*>)?\s*(Tashkent|Samarkand)\s*(?:</a>)?\s*</td>")


class SpatialBase(DeclarativeBase):
    """Its own base: these tables are created and dropped by the fixture below,
    and must not join the metadata every other SQLite fixture sweeps."""


class Place(SpatialBase):
    __tablename__ = "spatial_place"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(60))
    #: A geography, so a distance is metres.
    where: Mapped[WKBElement | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326), default=None
    )
    #: A geometry, so a distance would be degrees unless the query casts -- which
    #: is exactly what `dialects.spatial_condition` does, and what this proves.
    marker: Mapped[WKBElement | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), default=None
    )

    def __str__(self) -> str:
        return self.label


class PlaceAdmin(admin.ModelAdmin):
    list_display = ("id", "label")
    ordering = ("id",)


@pytest.fixture
async def client(
    engine: sa.ext.asyncio.AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    staff_user: StaffUser,
) -> AsyncIterator[httpx.AsyncClient]:
    async with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            pytest.skip("spatial filters need PostGIS")
        try:
            await connection.execute(sa.text("SELECT postgis_version()"))
        except Exception:
            pytest.skip("this PostgreSQL has no PostGIS extension")
        await connection.run_sync(SpatialBase.metadata.create_all)

    tashkent = "SRID=4326;POINT(69.2401 41.2995)"
    samarkand = "SRID=4326;POINT(66.9597 39.6270)"
    async with session_factory() as session:
        session.add_all(
            [
                Place(label="Tashkent", where=tashkent, marker=tashkent),
                Place(label="Samarkand", where=samarkand, marker=samarkand),
            ]
        )
        await session.commit()

    backend = SQLAlchemyBackend(session_factory=session_factory)
    # The probe that sets `has_postgis`. Without it the profile says no and every
    # spatial condition is dropped -- which is the correct default, and exactly
    # what would make these tests silently pass by returning every row.
    await backend.check_connection()
    assert backend.profile.has_postgis, "the probe should have found PostGIS"

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Place, PlaceAdmin, key="lab.place")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened

    async with engine.begin() as connection:
        await connection.run_sync(SpatialBase.metadata.drop_all)


async def places(client: httpx.AsyncClient, query: str = "") -> set[str]:
    response = await client.get(f"/admin/lab.place/?{query}")
    assert response.status_code == 200, response.text
    return set(CELL.findall(response.text))


# ---------------------------------------------------------------------------


async def test_the_unfiltered_list_holds_both(client: httpx.AsyncClient) -> None:
    """The control case. Without it a broken filter that returns nothing looks
    exactly like a working filter that correctly excluded everything."""
    assert await places(client) == {"Tashkent", "Samarkand"}


@pytest.mark.parametrize(
    ("kilometres", "expected"),
    [
        ("50", {"Tashkent"}),
        ("400", {"Tashkent", "Samarkand"}),
    ],
)
async def test_a_radius_over_a_geography_is_metres(
    client: httpx.AsyncClient, kilometres: str, expected: set[str]
) -> None:
    """The two cities are about 270 km apart, so 50 separates them and 400 does
    not. If the radius were being read as degrees instead, both queries would
    return both rows and neither would look wrong on its own."""
    found = await places(client, f"where__dwithin={TASHKENT}&where__km={kilometres}")
    assert found == expected


async def test_a_radius_over_a_plain_geometry_is_metres_too(
    client: httpx.AsyncClient,
) -> None:
    """`marker` is a `geometry`, where PostGIS measures in SRID units -- degrees
    at 4326. Without the cast `spatial_condition` applies, a 50 km radius would
    be fifty thousand degrees and match every row on the planet."""
    assert await places(client, f"marker__dwithin={TASHKENT}&marker__km=50") == {"Tashkent"}


async def test_a_point_inside_a_polygon(client: httpx.AsyncClient) -> None:
    assert await places(client, f"marker__within={AROUND_TASHKENT}") == {"Tashkent"}


async def test_a_viewport_asks_for_a_bounding_box(client: httpx.AsyncClient) -> None:
    """What a map actually asks as it is panned."""
    assert await places(client, f"marker__bbox={AROUND_TASHKENT}") == {"Tashkent"}


async def test_intersecting_the_other_city_finds_the_other_city(
    client: httpx.AsyncClient,
) -> None:
    assert await places(client, f"where__intersects={SAMARKAND}") == {"Samarkand"}


async def test_a_geometry_that_will_not_parse_leaves_the_page_standing(
    client: httpx.AsyncClient,
) -> None:
    """A stale bookmark is still a request for a list page. The condition is
    dropped and the unfiltered list is rendered, rather than a 500."""
    assert await places(client, "where__dwithin=somewhere&where__km=5") == {
        "Tashkent",
        "Samarkand",
    }
