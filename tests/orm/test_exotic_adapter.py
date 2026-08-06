"""Round-trip write path for the PostgreSQL-only column types added in the
column-types phase -- through a real adapter, not just introspected the way
`test_introspection.py`'s `ExoticColumn` is.

`values.py` produces the portable Python value; `SQLAlchemyAdapter._apply`
converts the three that need a driver-specific object
(`fastfort/orm/sqlalchemy/adapter.py`'s `_coerce_for_driver`); the database is
the final judge. This file is the test that actually asks it, against a live
asyncpg connection.

PostgreSQL only: `db_backend` parametrises over every selected engine the same
way the rest of `tests/orm/` does, but `inet`/`bit`/`int4range` do not exist on
SQLite or MySQL, so those are skipped explicitly rather than attempted -- under
the default `--db=sqlite` run (`make test`) this whole file is skipped, and
under `--db=all` only its SQLite/MySQL parametrisations are.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.dialects.postgresql.ranges import Range
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from fastfort.admin import values
from fastfort.orm.sqlalchemy import SQLAlchemyAdapter, SQLAlchemyBackend
from fastfort.spec import FieldSpec, FieldType, RangeSpec


class _Base(DeclarativeBase):
    pass


class Exotic(_Base):
    """One of each type Step 4 established works against a live asyncpg
    connection. Its own `DeclarativeBase`, not `tests.orm.models.Base`: that
    one is swept by every backend's `Base.metadata.create_all`, and a
    PostgreSQL-only type fails to compile on SQLite or MySQL.
    """

    __tablename__ = "adapter_exotic_column"

    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str | None] = mapped_column(pg.INET(), default=None)
    mac: Mapped[str | None] = mapped_column(pg.MACADDR(), default=None)
    attributes: Mapped[dict[str, str] | None] = mapped_column(pg.HSTORE(), default=None)
    price: Mapped[str | None] = mapped_column(pg.MONEY(), default=None)
    # `Mapped[Any]` does not tell SQLAlchemy's declarative mapping "Optional"
    # the way `X | None` does, so without `nullable=True` these came out
    # `NOT NULL` and every write with the column left unset failed outright.
    flags: Mapped[Any] = mapped_column(pg.BIT(8), default=None, nullable=True)
    span: Mapped[Any] = mapped_column(pg.INT4RANGE(), default=None, nullable=True)
    spans: Mapped[Any] = mapped_column(pg.INT4MULTIRANGE(), default=None, nullable=True)
    price_check: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 2), default=None)


@pytest.fixture
async def exotic_backend(db_backend: str, db_url: str) -> AsyncIterator[SQLAlchemyBackend]:
    if db_backend != "postgres":
        pytest.skip("PostgreSQL-only column types (bit, inet, hstore, money, range)")
    engine = create_async_engine(db_url, future=True)
    async with engine.begin() as conn:
        # A project's concern, not the library's -- confirmed in the Phase 2
        # report: without this, `CREATE TABLE` itself fails with "type hstore
        # does not exist" before a single row is ever written.
        await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS hstore"))
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield SQLAlchemyBackend(session_factory=factory, base=_Base)
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(_Base.metadata.drop_all)
        await engine.dispose()


async def _create_and_reread(backend: SQLAlchemyBackend, data: dict[str, Any]) -> Any:
    async with backend.unit_of_work() as uow:
        adapter: SQLAlchemyAdapter = backend.adapter(Exotic, uow, key="t.adapter_exotic")
        obj = await adapter.create(data)
        pk = adapter.primary_key_of(obj)
    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(Exotic, uow, key="t.adapter_exotic")
        return await adapter.require(pk)


def field(name: str, kind: FieldType, **kwargs: Any) -> FieldSpec:
    return FieldSpec(name=name, label=name.title(), type=kind, **kwargs)


# ---------------------------------------------------------------------------
# INET / MACADDR / HSTORE / MONEY -- confirmed to need no driver conversion
# at all: `values.py`'s plain `str`/`dict` already satisfies asyncpg through
# a properly-typed mapped column.
# ---------------------------------------------------------------------------


async def test_inet_round_trips_through_a_real_adapter(exotic_backend: SQLAlchemyBackend) -> None:
    spec = field("address", FieldType.INET)
    written = values.parse_value("192.168.1.5/24", spec)
    row = await _create_and_reread(exotic_backend, {"address": written})
    assert values.render_value(row.address, spec) == "192.168.1.5/24"


async def test_macaddr_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    spec = field("mac", FieldType.MACADDR)
    written = values.parse_value("AA:BB:CC:DD:EE:FF", spec)
    row = await _create_and_reread(exotic_backend, {"mac": written})
    assert values.render_value(row.mac, spec) == "aa:bb:cc:dd:ee:ff"


async def test_hstore_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    spec = field("attributes", FieldType.HSTORE)
    written = values.parse_value("colour: red\nsize: large", spec)
    row = await _create_and_reread(exotic_backend, {"attributes": written})
    assert row.attributes == {"colour": "red", "size": "large"}
    assert values.render_value(row.attributes, spec) == "colour: red\nsize: large"


async def test_money_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    """Confirms the finding in the Phase 2 report: a `Decimal` is refused by
    asyncpg's text-based `money` codec, so `values.py` hands the adapter a
    `str` instead -- and this is the test that proves the `str` actually
    reaches the database rather than merely satisfying a mock."""
    spec = field("price", FieldType.MONEY)
    written = values.parse_value("$1,234.56", spec)
    assert isinstance(written, str)
    row = await _create_and_reread(exotic_backend, {"price": written})
    # PostgreSQL hands the value back formatted with its own locale
    # ("$1,234.56" against en_US.utf8) -- `render_value` strips that back to
    # the plain decimal the control shows.
    assert values.render_value(row.price, spec) == "1234.56"


# ---------------------------------------------------------------------------
# BITS / RANGE -- confirmed to need `orm/sqlalchemy/adapter.py`'s
# `_coerce_for_driver` conversion; this is the test that would fail if that
# conversion were ever removed.
# ---------------------------------------------------------------------------


async def test_bits_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    spec = field("flags", FieldType.BITS)
    written = values.parse_value("01010101", spec)
    assert written == "01010101"  # still a plain str -- the adapter converts it
    row = await _create_and_reread(exotic_backend, {"flags": written})
    assert values.render_value(row.flags, spec) == "01010101"


async def test_range_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    written = values.parse_value("[1, 10)", spec)
    assert written == (1, 10, "[)")  # still a plain tuple -- the adapter converts it
    row = await _create_and_reread(exotic_backend, {"span": written})
    assert isinstance(row.span, Range)
    assert values.render_value(row.span, spec) == "[1, 10)"


async def test_range_with_an_unbounded_end_round_trips(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    """`int4range` is a *discrete* range type, and PostgreSQL canonicalises
    every discrete range to half-open `[)` on write (confirmed here, not
    assumed) -- an inclusive upper bound of 5 comes back as an exclusive upper
    bound of 6, covering the same integers. Only the continuous range types
    (`numrange`, `tsrange`, `tstzrange`) preserve the exact bounds notation
    entered; `daterange` is discrete too and canonicalises the same way.
    """
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    written = values.parse_value("(, 5]", spec)
    row = await _create_and_reread(exotic_backend, {"span": written})
    assert row.span.lower is None
    assert values.render_value(row.span, spec) == "(, 6)"


async def test_multirange_round_trips_through_a_real_adapter(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    spec = field("spans", FieldType.MULTIRANGE, bounds=RangeSpec(FieldType.INTEGER, multi=True))
    written = values.parse_value("[1, 10)\n[20, 30)", spec)
    row = await _create_and_reread(exotic_backend, {"spans": written})
    assert values.render_value(row.spans, spec) == "[1, 10)\n[20, 30)"


async def test_the_database_silently_rounds_excess_decimal_scale(
    exotic_backend: SQLAlchemyBackend,
) -> None:
    """Confirms the finding `check_bounds` (`values.py`) exists to pre-empt:
    written directly, bypassing `check_bounds` on purpose, `NUMERIC(12, 2)`
    rounds 123.456 to 123.46 with no error at all -- which is why the bounds
    check has to catch this client-side rather than trusting the database to
    reject it.
    """
    spec = field("price_check", FieldType.DECIMAL, precision=12, decimal_places=2)
    assert values.check_bounds(Decimal("123.456"), spec) is not None  # caught before the write

    row = await _create_and_reread(exotic_backend, {"price_check": Decimal("123.456")})
    assert row.price_check == Decimal("123.46")
