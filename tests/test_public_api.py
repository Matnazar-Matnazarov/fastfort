"""Tests covering the public API surface and versioning guarantees."""

from __future__ import annotations

import re
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

    An ORM is only required once a backend is constructed, so users who install
    the bare package still get a working import.
    """
    assert "fastfort.orm.sqlalchemy" not in sys.modules
    assert "fastfort.orm.tortoise" not in sys.modules
