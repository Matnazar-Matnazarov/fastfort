"""Shared pytest configuration and fixtures.

Multi-database matrix
---------------------
By default the suite runs against SQLite only, so a fresh clone needs no services.
Add the other engines explicitly::

    uv run pytest --db=postgres
    uv run pytest --db=all

Connection strings can be overridden through the environment::

    FASTFORT_TEST_POSTGRES_URL   (default: localhost:55432)
    FASTFORT_TEST_MYSQL_URL      (default: localhost:33306)

The default ports are shifted away from 5432/3306 to match
`docker-compose.test.yml`, so the test containers never collide with a database
already installed on the machine. CI sets both variables explicitly.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tests.orm.models import Base, Category, Product, StaffUser, Status, StockLevel, Tag

from fastfort.orm.sqlalchemy import SQLAlchemyBackend

if TYPE_CHECKING:
    from pathlib import Path

ALL_BACKENDS = ("sqlite", "postgres", "mysql")

DEFAULT_URLS = {
    "postgres": "postgresql+asyncpg://fastfort:fastfort@localhost:55432/fastfort_test",
    "mysql": "mysql+aiomysql://fastfort:fastfort@localhost:33306/fastfort_test",
}


# Every test in the suite reaches the admin from the same client address, so a
# per-address budget that is generous for a room full of people is spent in a few
# minutes by a thousand tests -- and `pytest-randomly` would make which test paid
# for it different on every run. Turned off here rather than in each of the
# twenty modules that build their own settings, and turned back on by the tests
# that are actually about it: an explicit `rate_limit=` argument outranks the
# environment in pydantic-settings, which is what `tests/security/test_rate_limit.py`
# relies on.
os.environ.setdefault("FASTFORT_RATE_LIMIT__ENABLED", "false")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--db",
        action="store",
        default=os.environ.get("FASTFORT_TEST_DB", "sqlite"),
        help="Database engine(s) to test against: sqlite | postgres | mysql | all",
    )


def _selected_backends(config: pytest.Config) -> tuple[str, ...]:
    choice = str(config.getoption("--db")).lower()
    if choice == "all":
        return ALL_BACKENDS
    if choice not in ALL_BACKENDS:
        raise pytest.UsageError(
            f"--db={choice!r} is not recognised. Valid values: {', '.join(ALL_BACKENDS)}, all"
        )
    return (choice,)


_BACKENDS_KEY = pytest.StashKey[tuple[str, ...]]()


def pytest_configure(config: pytest.Config) -> None:
    config.stash[_BACKENDS_KEY] = _selected_backends(config)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip tests marked for a backend that was not selected for this run."""
    selected = config.stash[_BACKENDS_KEY]
    for item in items:
        for backend in ALL_BACKENDS:
            if backend in item.keywords and backend not in selected:
                item.add_marker(
                    pytest.mark.skip(reason=f"{backend} not selected (run with --db={backend})")
                )


@pytest.fixture(scope="session")
def db_backends(pytestconfig: pytest.Config) -> tuple[str, ...]:
    """Backends that are active for this test run."""
    return pytestconfig.stash[_BACKENDS_KEY]


@pytest.fixture(params=ALL_BACKENDS)
def db_backend(request: pytest.FixtureRequest) -> str:
    """Parametrise a test across every active backend.

    Backends that were not selected are skipped, so the same test file runs
    unchanged locally (SQLite only) and in CI (all three engines).
    """
    backend: str = request.param
    if backend not in request.config.stash[_BACKENDS_KEY]:
        pytest.skip(f"{backend} not selected (run with --db={backend})")
    return backend


@pytest.fixture
def db_url(db_backend: str, tmp_path: Path) -> Iterator[str]:
    """Connection string for the current backend, cleaned up after the test."""
    if db_backend == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'fastfort_test.db'}"
        return

    env_var = f"FASTFORT_TEST_{db_backend.upper()}_URL"
    yield os.environ.get(env_var, DEFAULT_URLS[db_backend])


@pytest.fixture
async def client_for() -> AsyncIterator[Any]:
    """Factory returning an httpx client wired straight to an ASGI app.

    Requests go through the real routing and middleware stack without opening a
    socket, which is what makes these tests worth writing.
    """
    opened: list[httpx.AsyncClient] = []

    async def factory(app: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )
        opened.append(client)
        return client

    yield factory

    for client in opened:
        await client.aclose()


# ---------------------------------------------------------------------------
# Database fixtures
#
# Each test gets a freshly created schema on whichever databases were selected,
# so a failure on MySQL cannot be masked by state left behind on SQLite.
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[sa.ext.asyncio.AsyncEngine]:
    created = create_async_engine(db_url, future=True)
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield created
    finally:
        async with created.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await created.dispose()


@pytest.fixture
def session_factory(engine: sa.ext.asyncio.AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False keeps attributes readable after the unit of work
    # commits, which is what a view needs when rendering the object it just saved.
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def backend(session_factory: async_sessionmaker[AsyncSession]) -> SQLAlchemyBackend:
    return SQLAlchemyBackend(session_factory=session_factory, base=Base)


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A small, deliberately uneven data set.

    Names differ in case, one product has no category and no release date, and
    prices are decimals with trailing zeros -- each of which has broken one of
    the three databases at some point.
    """
    async with session_factory() as session:
        phones = Category(name="Phones")
        laptops = Category(name="Laptops")
        new = Tag(name="new")
        sale = Tag(name="sale")
        session.add_all([phones, laptops, new, sale])
        await session.flush()

        session.add_all(
            [
                Product(
                    name="Pixel Phone",
                    description="A phone",
                    price=Decimal("799.00"),
                    stock=10,
                    is_active=True,
                    status=Status.PUBLISHED,
                    released_on=dt.date(2026, 1, 15),
                    category=phones,
                    tags=[new],
                ),
                Product(
                    name="pixel case",
                    description="An accessory",
                    price=Decimal("19.50"),
                    stock=200,
                    is_active=True,
                    status=Status.PUBLISHED,
                    released_on=dt.date(2026, 3, 1),
                    category=phones,
                    tags=[sale],
                ),
                Product(
                    name="Retired Laptop",
                    price=Decimal("1200.00"),
                    stock=0,
                    is_active=False,
                    status=Status.ARCHIVED,
                    category=laptops,
                ),
                Product(name="Unfiled Gadget", price=Decimal("5.00"), stock=1),
            ]
        )
        session.add_all(
            [
                StockLevel(warehouse="tashkent", sku="PX-1", quantity=5),
                StockLevel(warehouse="samarkand", sku="PX-1", quantity=2),
            ]
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------

#: Used by every suite that needs an authenticated admin session.
ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct-horse-battery"


@pytest.fixture
async def staff_user(session_factory: async_sessionmaker[AsyncSession]) -> StaffUser:
    """A superuser who can sign in, with a real Argon2 hash."""
    from fastfort.auth import hash_password

    async with session_factory() as session:
        user = StaffUser(
            email=ADMIN_EMAIL,
            full_name="Site Owner",
            hashed_password=hash_password(ADMIN_PASSWORD),
            is_staff=True,
            is_superuser=True,
        )
        session.add(user)
        await session.commit()
        return user


async def sign_in(client: httpx.AsyncClient, *, admin_url: str = "/admin") -> None:
    """Drive the real sign-in form, so tests exercise the gate rather than skip it."""
    import re

    body = (await client.get(f"{admin_url}/login")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, "the login form must carry a CSRF token"

    response = await client.post(
        f"{admin_url}/login",
        data={
            "identity": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "_csrf": match.group(1),
            "next": f"{admin_url}/",
        },
    )
    assert response.status_code == 303, response.text
