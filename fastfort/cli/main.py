"""The `fastfort` command.

Four things a project needs before it can run an admin at all: a secret key, a
first account to sign in with, a way to see whether the configuration is sound,
and a way to see what is registered. Without the first two, a fresh install is
unusable without writing a script -- which is the gap this closes.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from typing import TYPE_CHECKING, Annotated, Any

import typer

from fastfort._version import __version__
from fastfort.core.exceptions import FastFortError

if TYPE_CHECKING:
    from fastfort.core.app import FastFort

__all__ = ["app", "main"]

app = typer.Typer(
    name="fastfort",
    help="Administration and authentication for FastAPI.",
    no_args_is_help=True,
    add_completion=False,
)

#: Shared by every command that has to reach the project's own instance.
AppOption = Annotated[
    str | None,
    typer.Option(
        "--app",
        "-a",
        help="Where the FastFort instance lives, as module:attribute (e.g. main:fort).",
    ),
]

_GREEN, _RED, _YELLOW, _DIM, _RESET = (
    "\033[32m",
    "\033[31m",
    "\033[33m",
    "\033[2m",
    "\033[0m",
)


def _fail(error: Exception) -> None:
    """Print a FastFort error the way it was written, then exit non-zero.

    The messages already say what happened and what to do; a traceback on top of
    that is noise for someone running a command, not a developer debugging one.
    """
    typer.echo(f"{_RED}error{_RESET} {error}", err=True)
    raise typer.Exit(1)


def _load(target: str | None) -> FastFort:
    from .loader import load_fort

    try:
        return load_fort(target)
    except FastFortError as error:
        _fail(error)
        raise  # unreachable; keeps the type checker honest


# ---------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(__version__)


@app.command("generate-secret")
def generate_secret(
    length: Annotated[int, typer.Option(help="Bytes of entropy.")] = 48,
    export: Annotated[bool, typer.Option("--export", help="Print as a shell export line.")] = False,
) -> None:
    """Generate a signing key.

    Printed and nothing else -- not written to a file. A secret that a tool put on
    disk is a secret that ends up committed.
    """
    key = secrets.token_urlsafe(max(32, length))
    typer.echo(f"FASTFORT_SECRET_KEY={key}" if export else key)


@app.command()
def check(
    app_target: AppOption = None,
    deploy: Annotated[
        bool,
        typer.Option("--deploy", help="Also apply the checks that only matter in production."),
    ] = False,
) -> None:
    """Report configuration problems.

    Exits non-zero when anything is wrong, so it can gate a deployment.
    """
    fort = _load(app_target)
    issues = fort.check(deploy=deploy)

    typer.echo(f"{_DIM}{fort.settings.project_name} · {len(fort.registry)} model(s){_RESET}")

    if not issues:
        typer.echo(f"{_GREEN}ok{_RESET}    no problems found")
        return

    for issue in issues:
        typer.echo(f"{_YELLOW}warn{_RESET}  {issue}")
    typer.echo(f"\n{len(issues)} problem(s) found.", err=True)
    raise typer.Exit(1)


@app.command("registered-models")
def registered_models(app_target: AppOption = None) -> None:
    """List what the admin manages, and where each model lives."""
    fort = _load(app_target)

    if not len(fort.registry):
        typer.echo("No models are registered.")
        return

    for group, entries in fort.registry.grouped().items():
        typer.echo(f"\n{group}")
        for entry in entries:
            module = f"{entry.model.__module__}.{entry.model.__name__}"
            typer.echo(f"  {entry.key:<28} {_DIM}{module}{_RESET}")
            typer.echo(f"  {'':<28} {_DIM}{fort.settings.admin.url}/{entry.key}/{_RESET}")


@app.command("createsuperuser")
def create_superuser(
    app_target: AppOption = None,
    identity: Annotated[str | None, typer.Option(help="Email or username.")] = None,
    password: Annotated[
        str | None,
        typer.Option(
            help="Read from an environment variable instead, with --password-env.",
        ),
    ] = None,
    password_env: Annotated[
        str | None,
        typer.Option("--password-env", help="Name of an environment variable holding it."),
    ] = None,
    no_input: Annotated[
        bool, typer.Option("--no-input", help="Never prompt; fail if something is missing.")
    ] = False,
) -> None:
    """Create an account that can sign in to the admin.

    Interactive by default. `--no-input` with `--password-env` is the shape a CI
    pipeline needs: a password passed as `--password` is visible in the process
    list and in shell history, so the environment variable is the documented way.
    """
    fort = _load(app_target)

    if password and not no_input:
        typer.echo(
            f"{_YELLOW}warn{_RESET}  --password is visible in your shell history and in "
            "`ps`. Prefer --password-env.",
            err=True,
        )

    if password_env:
        from_env = os.environ.get(password_env)
        if not from_env:
            _fail(
                FastFortError(
                    f"Environment variable {password_env!r} is empty or unset.",
                    hint=f"Set it before running, for example: {password_env}=... fastfort ...",
                )
            )
        password = from_env

    try:
        config = fort.user_config
    except FastFortError as error:
        _fail(error)
        return

    label = config.identity_field.replace("_", " ").capitalize()

    if not identity:
        if no_input:
            _fail(FastFortError(f"{label} is required.", hint="Pass --identity."))
        identity = typer.prompt(label)

    if not password:
        if no_input:
            _fail(
                FastFortError(
                    "A password is required.",
                    hint="Pass --password-env with the name of a variable holding it.",
                )
            )
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)

    try:
        created = asyncio.run(_create_superuser(fort, identity, password or ""))
    except FastFortError as error:
        _fail(error)
        return

    typer.echo(f"{_GREEN}ok{_RESET}    superuser {created!r} created.")


async def _create_superuser(fort: FastFort, identity: str, password: str) -> str:
    """Write the account, refusing a weak password and a duplicate identity."""
    from fastfort.auth.passwords import hash_password, validate_password
    from fastfort.spec import Filter, FilterOperator, ListQuery

    config = fort.user_config
    validate_password(password, fort.settings.auth, identity=identity)

    async with fort.backend.unit_of_work() as uow:
        adapter = fort.backend.adapter(config.model, uow, key="fastfort.user")

        existing = await adapter.list(
            ListQuery(
                filters=(Filter(config.identity_field, FilterOperator.IEXACT, identity),),
                page_size=1,
            )
        )
        if existing.total:
            raise FastFortError(
                f"An account with {config.identity_field} {identity!r} already exists.",
                hint="Use a different one, or change that account's password in the admin.",
            )

        values: dict[str, Any] = {
            config.identity_field: identity,
            config.password_field: hash_password(password),
            config.superuser_field: True,
            config.staff_field: True,
            config.active_field: True,
        }
        # An adapter only writes fields the spec marks editable, so a model
        # without one of these columns is not a failure -- the extra key is
        # dropped and the account is created without it.
        await adapter.create(values)

    return identity


def main() -> None:
    """Entry point for the `fastfort` script."""
    try:
        app()
    except FastFortError as error:  # anything that escaped a command
        typer.echo(f"{_RED}error{_RESET} {error}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
