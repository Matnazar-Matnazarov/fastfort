"""Where Tortoise keeps its connections, and what happens when a view cannot see them.

Tortoise 1.1 moved its connection registry into a `contextvars.ContextVar`. An
ASGI server runs the lifespan in a different task from the requests, and a
contextvar set inside a task dies with it -- so `await Tortoise.init(...)` in a
lifespan is invisible to every view. Start-up looks perfectly healthy and the
first page that touches the database is a 500, which is how this project's own
Tortoise sandbox behaved until `_enable_global_fallback` was passed.

The reproduction here is that shape in miniature: init inside a child task, then
ask from the parent, which never sees what the child set.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from tortoise import Tortoise

from fastfort.core.exceptions import ImproperlyConfigured
from fastfort.orm.tortoise import TortoiseBackend

#: Absent before Tortoise 1.1, where the state was process-global and none of
#: this could happen. `pyproject.toml` still allows those versions.
HAS_CONTEXTS = "_enable_global_fallback" in inspect.signature(Tortoise.init).parameters

pytestmark = pytest.mark.skipif(not HAS_CONTEXTS, reason="Tortoise keeps no per-task context")

MODULES = {"models": ["tests.orm.tortoise_models"]}

#: `await elsewhere(**init_kwargs)` -- start Tortoise out of this task's reach.
Starter = Callable[..., Awaitable[None]]


@pytest.fixture
async def elsewhere(tmp_path: Path) -> AsyncIterator[Starter]:
    """Initialise Tortoise in a task this test cannot see into.

    `asyncio.create_task` copies the current context, so what the child sets is
    written to its own copy and discarded with it -- exactly what uvicorn's
    lifespan does to every request handler.
    """
    opened: list[Any] = []

    async def start(**kwargs: Any) -> None:
        async def run() -> Any:
            context = await Tortoise.init(
                db_url=f"sqlite://{tmp_path / 'context.db'}", modules=MODULES, **kwargs
            )
            await Tortoise.generate_schemas()
            return context

        opened.append(await asyncio.create_task(run()))

    yield start

    # Re-entered by hand, because the task that held the context is gone. Without
    # this the SQLite client's worker thread outlives the test.
    for context in opened:
        with context:
            await Tortoise.close_connections()
            await Tortoise._reset_apps()


async def test_a_view_is_told_how_to_reach_a_connection_opened_in_another_task(
    elsewhere: Starter,
) -> None:
    """The bare `RuntimeError` Tortoise raises here names no fix.

    "No TortoiseContext is currently active", as a 500, on a page listing rows
    that the lifespan read a moment earlier, is not something a project can act
    on -- so the backend replaces it with the two ways out.
    """
    await elsewhere()

    with pytest.raises(ImproperlyConfigured) as caught:
        async with TortoiseBackend().unit_of_work():
            pass  # pragma: no cover -- opening the unit of work is what raises

    message = str(caught.value)
    assert "not reachable" in message
    assert "_enable_global_fallback" in message
    assert "RegisterTortoise" in message


async def test_the_global_fallback_is_what_makes_a_lifespan_init_reachable(
    elsewhere: Starter,
) -> None:
    """The other half: with the flag, the same cross-task shape works.

    This is the line `test_api_tortoise/db.py` carries, and the reason it is not
    a plain `Tortoise.init()` there.
    """
    await elsewhere(_enable_global_fallback=True)

    backend = TortoiseBackend()
    await backend.check_connection()
    async with backend.unit_of_work() as uow:
        assert uow.connection is not None
