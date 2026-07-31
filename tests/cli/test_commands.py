"""The `fastfort` command line interface.

Driven through Typer's runner rather than as a subprocess, so a failure points at
the line that caused it instead of at a captured stdout blob.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.orm.models import Product, StaffUser
from typer.testing import CliRunner

from fastfort import FastFort, FastFortSettings, admin
from fastfort.cli.loader import load_fort
from fastfort.cli.main import app
from fastfort.core.exceptions import ImproperlyConfigured
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"
PASSWORD = "a-good-passphrase-2026"

runner = CliRunner()


@pytest.fixture
def own_loop(db_backend: str) -> None:
    """Restrict a test to SQLite because the command opens its own event loop.

    `createsuperuser` calls `asyncio.run`, so it runs against a loop of its own --
    which is exactly what happens in production, where the CLI is a separate
    process. In-process, that loop is not the one the test's engine was created
    on, and asyncpg and aiomysql bind connections to a loop.

    This is an artefact of sharing an engine across loops in a test, not a
    difference in behaviour: the command reaches the database through the same
    adapter that the ORM suite already exercises on all three.
    """
    if db_backend != "sqlite":
        pytest.skip("the command opens its own event loop; see the fixture docstring")


@pytest.fixture
def fort(backend: SQLAlchemyBackend) -> Iterator[FastFort]:
    """A configured instance, exposed where the CLI can import it."""
    instance = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test Shop"),  # type: ignore[call-arg]
        backend=backend,
    )
    instance.set_user_model(StaffUser)
    instance.register(Product, admin.ModelAdmin, key="shop.product")

    module = sys.modules[__name__]
    module.CLI_FORT = instance  # type: ignore[attr-defined]
    try:
        yield instance
    finally:
        del module.CLI_FORT  # type: ignore[attr-defined]


TARGET = f"{__name__}:CLI_FORT"


def run(*args: str, **env: str) -> Any:
    return runner.invoke(app, list(args), env=env or None)


async def run_async(*args: str, **env: str) -> Any:
    """Invoke the CLI from an async test.

    In a worker thread, because `createsuperuser` calls `asyncio.run` and that
    cannot start a loop inside one that is already running. Running the command
    in its own thread is also what actually happens in production: the CLI is a
    separate process with a loop of its own.
    """
    import asyncio

    return await asyncio.to_thread(runner.invoke, app, list(args), env=env or None)


# ---------------------------------------------------------------------------
# Standalone commands
# ---------------------------------------------------------------------------


def test_version_prints_the_installed_version() -> None:
    from fastfort import __version__

    result = run("version")
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_generate_secret_produces_an_acceptable_key() -> None:
    """The key it prints must satisfy the validator that will read it."""
    key = run("generate-secret").stdout.strip()

    settings = FastFortSettings(secret_key=key)  # type: ignore[arg-type]
    assert settings.secret_key.get_secret_value() == key


def test_generate_secret_never_repeats() -> None:
    assert run("generate-secret").stdout != run("generate-secret").stdout


def test_generate_secret_can_print_a_shell_line() -> None:
    assert run("generate-secret", "--export").stdout.startswith("FASTFORT_SECRET_KEY=")


# ---------------------------------------------------------------------------
# Finding the application
# ---------------------------------------------------------------------------


def test_a_missing_target_says_what_was_tried() -> None:
    with pytest.raises(ImproperlyConfigured) as caught:
        load_fort("nowhere.at.all:fort")

    assert "--app" in str(caught.value)


def test_a_malformed_target_is_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match="module:attribute"):
        load_fort("just-a-module-name")


def test_pointing_at_the_wrong_object_says_so(fort: FastFort) -> None:
    with pytest.raises(ImproperlyConfigured, match="not a FastFort instance"):
        load_fort(f"{__name__}:PASSWORD")


def test_an_error_inside_the_project_is_not_swallowed() -> None:
    """Turning a real traceback into "not found" is far harder to debug."""
    with pytest.raises(ImproperlyConfigured, match="has no attribute"):
        load_fort(f"{__name__}:no_such_attribute")


def test_the_target_can_come_from_the_environment(fort: FastFort) -> None:
    result = run("check", FASTFORT_APP=TARGET)
    assert result.exit_code == 0, result.stdout


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_passes_on_a_sound_configuration(fort: FastFort) -> None:
    result = run("check", "--app", TARGET)

    assert result.exit_code == 0
    assert "no problems found" in result.stdout


def test_check_exits_non_zero_so_it_can_gate_a_deployment(fort: FastFort) -> None:
    """`--deploy` flags the debug and cookie settings a sandbox runs with."""
    fort.settings.debug = True

    result = run("check", "--app", TARGET, "--deploy")

    assert result.exit_code == 1
    assert "debug=True" in result.stdout


def test_check_reports_an_empty_registry(fort: FastFort) -> None:
    fort.registry.clear()

    result = run("check", "--app", TARGET)

    assert result.exit_code == 1
    assert "No models are registered" in result.stdout


# ---------------------------------------------------------------------------
# registered-models
# ---------------------------------------------------------------------------


def test_registered_models_lists_keys_and_urls(fort: FastFort) -> None:
    result = run("registered-models", "--app", TARGET)

    assert result.exit_code == 0
    assert "shop.product" in result.stdout
    assert "/admin/shop.product/" in result.stdout
    assert "tests.orm.models.Product" in result.stdout


def test_registered_models_says_so_when_there_are_none(fort: FastFort) -> None:
    fort.registry.clear()
    assert "No models are registered" in run("registered-models", "--app", TARGET).stdout


# ---------------------------------------------------------------------------
# createsuperuser
# ---------------------------------------------------------------------------


async def test_it_creates_an_account_that_can_sign_in(
    own_loop: None, fort: FastFort, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The point of the command: a fresh install has no way in without it."""
    from fastfort.auth import verify_password

    result = await run_async(
        "createsuperuser",
        "--app",
        TARGET,
        "--identity",
        "cli@example.com",
        "--password-env",
        "FF_PASSWORD",
        "--no-input",
        FF_PASSWORD=PASSWORD,
    )

    assert result.exit_code == 0, result.stdout
    assert "created" in result.stdout

    async with session_factory() as session:
        created = (
            await session.execute(sa.select(StaffUser).where(StaffUser.email == "cli@example.com"))
        ).scalar_one()

    assert created.is_superuser
    assert created.is_staff
    assert created.is_active
    assert verify_password(PASSWORD, created.hashed_password)


async def test_the_password_is_never_stored_as_typed(
    own_loop: None, fort: FastFort, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await run_async(
        "createsuperuser",
        "--app",
        TARGET,
        "--identity",
        "hash@example.com",
        "--password-env",
        "FF_PASSWORD",
        "--no-input",
        FF_PASSWORD=PASSWORD,
    )

    async with session_factory() as session:
        created = (
            await session.execute(sa.select(StaffUser).where(StaffUser.email == "hash@example.com"))
        ).scalar_one()

    assert created.hashed_password.startswith("$argon2id$")
    assert PASSWORD not in created.hashed_password


def test_a_weak_password_is_refused(own_loop: None, fort: FastFort) -> None:
    """The same policy the admin's own form applies."""
    result = run(
        "createsuperuser",
        "--app",
        TARGET,
        "--identity",
        "weak@example.com",
        "--password-env",
        "FF_PASSWORD",
        "--no-input",
        FF_PASSWORD="short",
    )

    assert result.exit_code == 1


def test_a_duplicate_identity_is_refused(own_loop: None, fort: FastFort) -> None:
    for _ in range(2):
        result = run(
            "createsuperuser",
            "--app",
            TARGET,
            "--identity",
            "twice@example.com",
            "--password-env",
            "FF_PASSWORD",
            "--no-input",
            FF_PASSWORD=PASSWORD,
        )

    assert result.exit_code == 1
    assert "already exists" in result.output


def test_an_unset_password_variable_is_reported(own_loop: None, fort: FastFort) -> None:
    result = run(
        "createsuperuser",
        "--app",
        TARGET,
        "--identity",
        "x@example.com",
        "--password-env",
        "NOT_SET_ANYWHERE",
        "--no-input",
    )

    assert result.exit_code == 1
    assert "NOT_SET_ANYWHERE" in result.output


@pytest.mark.parametrize("missing", ["identity", "password"])
def test_no_input_refuses_to_prompt(own_loop: None, fort: FastFort, missing: str) -> None:
    """A pipeline that hangs on a hidden prompt is worse than one that fails."""
    args = ["createsuperuser", "--app", TARGET, "--no-input"]
    env = {}
    if missing != "identity":
        args += ["--identity", "someone@example.com"]
    if missing != "password":
        args += ["--password-env", "FF_PASSWORD"]
        env["FF_PASSWORD"] = PASSWORD

    result = run(*args, **env)
    assert result.exit_code == 1


async def test_it_prompts_when_run_interactively(
    own_loop: None, fort: FastFort, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import asyncio

    result = await asyncio.to_thread(
        runner.invoke,
        app,
        ["createsuperuser", "--app", TARGET],
        input=f"prompted@example.com\n{PASSWORD}\n{PASSWORD}\n",
    )

    assert result.exit_code == 0, result.output
    async with session_factory() as session:
        assert (
            await session.execute(
                sa.select(StaffUser).where(StaffUser.email == "prompted@example.com")
            )
        ).scalar_one_or_none() is not None


def test_a_password_on_the_command_line_is_warned_about(own_loop: None, fort: FastFort) -> None:
    """It is visible in shell history and in `ps`, so the safer route is named."""
    result = runner.invoke(
        app,
        [
            "createsuperuser",
            "--app",
            TARGET,
            "--identity",
            "argv@example.com",
            "--password",
            PASSWORD,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "--password-env" in result.output
