"""Shared pytest configuration and fixtures.

Multi-database matrix
---------------------
By default the suite runs against SQLite only, so a fresh clone needs no services.
Add the other engines explicitly::

    uv run pytest --db=postgres
    uv run pytest --db=all

Connection strings can be overridden through the environment::

    FASTFORT_TEST_POSTGRES_URL   (default: localhost:5432)
    FASTFORT_TEST_MYSQL_URL      (default: localhost:3306)
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import httpx
import pytest

if TYPE_CHECKING:
    from pathlib import Path

ALL_BACKENDS = ("sqlite", "postgres", "mysql")

DEFAULT_URLS = {
    "postgres": "postgresql+asyncpg://fastfort:fastfort@localhost:5432/fastfort_test",
    "mysql": "mysql+asyncmy://fastfort:fastfort@localhost:3306/fastfort_test",
}


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
