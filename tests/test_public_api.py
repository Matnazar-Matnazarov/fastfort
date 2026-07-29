"""Tests covering the public API surface and versioning guarantees."""

from __future__ import annotations

import re
import subprocess
import sys

import fastfort


def test_version_is_pep440() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(?:\.dev\d+|[ab]\d+|rc\d+)?", fastfort.__version__)


def test_schema_version_is_positive_int() -> None:
    """The schema version increases monotonically, independently of the release version."""
    assert isinstance(fastfort.SCHEMA_VERSION, int)
    assert fastfort.SCHEMA_VERSION >= 1


def test_all_exports_exist() -> None:
    """Every name promised by `__all__` actually resolves."""
    for name in fastfort.__all__:
        assert hasattr(fastfort, name), f"`fastfort.{name}` is exported but does not exist"


def test_importing_fastfort_does_not_pull_in_an_orm() -> None:
    """`import fastfort` must work without any optional dependency installed.

    Checked in a fresh interpreter: within this session other tests have already
    imported the SQLAlchemy backend, so `sys.modules` here proves nothing.
    """
    probe = (
        "import sys, fastfort; "
        "leaked = [m for m in sys.modules if m.split('.')[0] "
        "in {'sqlalchemy', 'tortoise', 'asyncpg', 'asyncmy', 'aiosqlite'}]; "
        "print(','.join(sorted(leaked)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", f"importing fastfort pulled in: {result.stdout.strip()}"
