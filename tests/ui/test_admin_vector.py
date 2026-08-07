"""Similarity search, against a database rather than against compiled SQL.

Everything up to the SQL is covered on every backend by
`tests/unit/test_vector_search.py`. What is left is the part no amount of
parsing can settle: whether the rows actually come back nearest first, and
whether the metric that was asked for is the one that ranked them -- cosine and
L2 produce the same order for normalised vectors and different orders for
everything else, so a test whose vectors are all unit length proves nothing.

Marked `postgres` so a SQLite or MySQL run skips it, and guarded again at run
time by asking whether pgvector is installed: `docker/postgres.Dockerfile`
builds an image with it, but somebody's own `FASTFORT_TEST_POSTGRES_URL` may
point elsewhere.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from pgvector.sqlalchemy import Vector
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from tests.conftest import sign_in
from tests.orm.models import StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("seeded")]

CELL = re.compile(r"<td[^>]*>\s*(?:<a[^>]*>)?\s*(X|Y|Z|XY|Far)\s*(?:</a>)?\s*</td>")


class VectorBase(DeclarativeBase):
    """Its own base: created and dropped by the fixture, and must not join the
    metadata every SQLite fixture in the suite sweeps."""


class Doc(VectorBase):
    __tablename__ = "vector_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(20))
    embedding: Mapped[object | None] = mapped_column(Vector(3), default=None)

    def __str__(self) -> str:
        return self.title


class DocAdmin(admin.ModelAdmin):
    list_display = ("id", "title")
    ordering = ("id",)


#: Three axes, one between two of them, and one long vector pointing at X.
#:
#: `Far` is what separates the metrics: cosine ignores magnitude, so it ranks
#: `Far` exactly level with `X`, while L2 puts it last. A set of unit vectors
#: would have made the two metrics agree and the test prove nothing.
ROWS = [
    ("X", [1.0, 0.0, 0.0]),
    ("Y", [0.0, 1.0, 0.0]),
    ("Z", [0.0, 0.0, 1.0]),
    ("XY", [0.7, 0.7, 0.0]),
    ("Far", [9.0, 0.0, 0.0]),
]


@pytest.fixture
async def client(
    engine: sa.ext.asyncio.AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    staff_user: StaffUser,
) -> AsyncIterator[httpx.AsyncClient]:
    async with engine.begin() as connection:
        if connection.dialect.name != "postgresql":
            pytest.skip("similarity search needs pgvector")
        try:
            await connection.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
        except Exception:
            pytest.skip("this PostgreSQL has no pgvector extension")
        await connection.run_sync(VectorBase.metadata.create_all)

    async with session_factory() as session:
        session.add_all([Doc(title=title, embedding=vector) for title, vector in ROWS])
        await session.commit()

    backend = SQLAlchemyBackend(session_factory=session_factory)
    await backend.check_connection()
    assert backend.profile.has_pgvector, "the probe should have found pgvector"

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Doc, DocAdmin, key="lab.doc")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened

    async with engine.begin() as connection:
        await connection.run_sync(VectorBase.metadata.drop_all)


async def ranked(client: httpx.AsyncClient, query: str = "") -> list[str]:
    response = await client.get(f"/admin/lab.doc/?{query}")
    assert response.status_code == 200, response.text
    return CELL.findall(response.text)


# ---------------------------------------------------------------------------


async def test_the_unfiltered_list_is_in_id_order(client: httpx.AsyncClient) -> None:
    """The control case. Without it a search that silently did nothing would
    look exactly like one that ranked correctly by coincidence."""
    assert await ranked(client) == ["X", "Y", "Z", "XY", "Far"]


async def test_rows_come_back_nearest_first(client: httpx.AsyncClient) -> None:
    """`XY` is closer to `X` than `Y` or `Z` are, and nothing about the id order
    would put it second."""
    order = await ranked(client, "embedding__near=[1,0,0]")

    assert order[0] in ("X", "Far")  # cosine ranks them level; both are exact
    assert order.index("XY") < order.index("Y")
    assert order.index("XY") < order.index("Z")


async def test_the_metric_asked_for_is_the_one_that_ranks(
    client: httpx.AsyncClient,
) -> None:
    """The test that could not exist without a database. `Far` points exactly at
    X but is nine times as long: cosine ignores magnitude and calls it a perfect
    match, L2 measures the gap and puts it last. If the metric were being
    ignored, one of these two would be wrong."""
    cosine = await ranked(client, "embedding__near=[1,0,0]&embedding__metric=cosine")
    euclidean = await ranked(client, "embedding__near=[1,0,0]&embedding__metric=l2")

    assert cosine.index("Far") <= 1
    assert euclidean[0] == "X"
    assert euclidean.index("Far") > euclidean.index("XY")


async def test_k_limits_the_rows_the_list_reaches(client: httpx.AsyncClient) -> None:
    """`k` is "the nearest two", and the second is the last row there is."""
    assert len(await ranked(client, "embedding__near=[1,0,0]&embedding__k=2")) == 2


async def test_a_page_past_the_neighbour_count_is_empty(
    client: httpx.AsyncClient,
) -> None:
    """Rather than paging on into rows the search already ruled out."""
    assert await ranked(client, "embedding__near=[1,0,0]&embedding__k=2&p=3&ps=1") == []


async def test_a_maximum_distance_narrows_it_further(client: httpx.AsyncClient) -> None:
    """For when "the nearest ten" should return two, because only two are
    actually similar."""
    near = await ranked(client, "embedding__near=[0,1,0]&embedding__within=0.4")

    assert "Y" in near
    assert "Z" not in near


async def test_a_search_outranks_an_explicit_sort(client: httpx.AsyncClient) -> None:
    """ "The nearest, and break ties by title" -- not "by title, and break ties
    by similarity", which would return the alphabet."""
    order = await ranked(client, "embedding__near=[0,0,1]&o=title")

    assert order[0] == "Z"


async def test_a_vector_of_the_wrong_width_leaves_the_page_standing(
    client: httpx.AsyncClient,
) -> None:
    """A stale link is still a request for a list page. The search is dropped
    and every row is rendered, rather than the database refusing the query."""
    assert await ranked(client, "embedding__near=[1,0]") == ["X", "Y", "Z", "XY", "Far"]
