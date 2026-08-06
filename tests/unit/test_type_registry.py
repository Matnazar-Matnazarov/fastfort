"""The pluggable type registry (`fastfort.orm.sqlalchemy.types`).

`classify` is what lets a project's own SQLAlchemy type be recognised without a
fork, so what matters here is the extension contract itself: a rule can override
a built-in, decline and let the next rule try, or leave a column unrecognised
without the library raising.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastfort.orm.sqlalchemy import types as type_registry
from fastfort.orm.sqlalchemy.types import Classification, classify, register_type
from fastfort.spec import FieldType


class Base(DeclarativeBase):
    pass


class Exotic(sa.types.UserDefinedType[object]):
    """A type nothing built in recognises -- `python_type` raises, same shape as
    the PostgreSQL scalars the registry exists to cover."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "EXOTIC"


class Widget(Base):
    __tablename__ = "widget"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    mystery: Mapped[object | None] = mapped_column(Exotic(), default=None)


def _column(name: str) -> sa.Column[object]:
    column = Widget.__table__.columns[name]
    assert isinstance(column, sa.Column)
    return column


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    """`_RULES` is a module-level list; a test that registers a rule and forgets
    to remove it would leak into whichever test `pytest-randomly` runs next."""
    original = list(type_registry._RULES)
    yield
    type_registry._RULES[:] = original


def test_a_first_true_rule_overrides_a_built_in() -> None:
    def always_string(column: sa.Column[object]) -> Classification | None:
        return Classification(FieldType.STRING)

    assert classify(_column("id")).type is FieldType.INTEGER
    register_type(always_string, first=True)
    assert classify(_column("id")).type is FieldType.STRING


def test_a_rule_returning_none_falls_through_to_the_next_one() -> None:
    def only_for_mystery(column: sa.Column[object]) -> Classification | None:
        if column.name != "mystery":
            return None
        return Classification(FieldType.STRING)

    register_type(only_for_mystery, first=True)
    assert classify(_column("mystery")).type is FieldType.STRING
    # Declined for "id", so the built-in integer rule still gets to run.
    assert classify(_column("id")).type is FieldType.INTEGER


def test_an_unclassifiable_column_is_unknown_not_an_exception() -> None:
    """`Exotic.python_type` raises `NotImplementedError`, and nothing else
    recognises it -- the whole point is that this degrades, it does not raise."""
    assert classify(_column("mystery")).type is FieldType.UNKNOWN


def test_a_default_registered_rule_only_fires_for_what_nothing_else_claimed() -> None:
    """Appended (not `first=True`) rules sit behind every built-in, so they only
    ever run for a column nothing above them -- built in or registered earlier
    -- already recognised."""

    def claim_mystery(column: sa.Column[object]) -> Classification | None:
        if column.name != "mystery":
            return None
        return Classification(FieldType.STRING)

    register_type(claim_mystery)
    assert classify(_column("mystery")).type is FieldType.STRING
    # A type the built-ins already handle is untouched by the appended rule.
    assert classify(_column("id")).type is FieldType.INTEGER


def test_registering_a_rule_does_not_leak_into_the_next_test() -> None:
    """Companion to the autouse fixture above: if restoration ever broke, this
    test would see whatever the previous test (in whatever order pytest-randomly
    picked) left behind."""
    assert classify(_column("mystery")).type is FieldType.UNKNOWN
