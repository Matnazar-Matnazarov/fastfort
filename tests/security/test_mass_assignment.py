"""`FieldSpec.editable` is the mass-assignment boundary, and it holds for the
column types the column-types phase added the same way it already did for
every other type.

`SQLAlchemyAdapter._writable` (`fastfort/orm/sqlalchemy/adapter.py`) filters
every write against `editable`, and per `CLAUDE.md`'s non-negotiable
invariants there is deliberately no second flag that could disagree with it.
Phase 2 added a driver-specific conversion step in `_apply` (`_coerce_for_
driver`, for `BITS`/`RANGE`/`MULTIRANGE`) that runs *after* `_writable` has
already dropped anything not on the allow-list -- this is the test that would
fail if that ordering were ever reversed, or if a new type's write path
bypassed `_writable` some other way.

Runs on SQLite: the boundary is enforced by the spec, before any column is
touched, so it needs no PostgreSQL-only column to prove it -- a `ModelSpec`
that merely *describes* a field as `FieldType.INET`/`FieldType.BITS` is
enough. Keeping this in the default `make check` run (no `--db=postgres`
needed) means a regression here is caught on every commit, not only the ones
that remember to bring up a database.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.orm.models import Product

from fastfort.orm.sqlalchemy import SQLAlchemyAdapter, SQLAlchemyBackend, introspect_model
from fastfort.orm.sqlalchemy.dialects import profile_for
from fastfort.spec import FieldType, ModelSpec


def _locked_down_spec(*, field_type: FieldType) -> ModelSpec:
    """`Product.description`'s spec, retyped to one of Phase 2's new
    `FieldType`s and marked `editable=False` -- every other column is
    untouched, so the adapter still maps onto a real, writable table, and
    `name` (required, no database default) can still be supplied legitimately
    alongside the attack.
    """
    spec = introspect_model(Product, key="shop.product")
    locked = spec.field("description").replace(type=field_type, editable=False)
    fields = tuple(locked if f.name == "description" else f for f in spec)
    return replace(spec, fields=fields)


NEW_TYPES = [FieldType.INET, FieldType.MACADDR, FieldType.HSTORE, FieldType.MONEY, FieldType.BITS]


@pytest.fixture
def locked_backend(
    session_factory: async_sessionmaker[AsyncSession],
) -> SQLAlchemyBackend:
    return SQLAlchemyBackend(session_factory=session_factory)


@pytest.mark.parametrize("field_type", NEW_TYPES)
async def test_a_non_editable_field_is_dropped_on_create(
    locked_backend: SQLAlchemyBackend, field_type: FieldType
) -> None:
    spec = _locked_down_spec(field_type=field_type)
    async with locked_backend.unit_of_work() as uow:
        adapter = SQLAlchemyAdapter(Product, spec, uow.session, profile_for("sqlite"))
        obj = await adapter.create({"name": "Legitimate product", "description": "hacked"})
        # Never reached `setattr` at all -- dropped by `_writable` before
        # `_apply` looks at the value, same as any other read-only field.
        assert obj.description is None
        assert obj.name == "Legitimate product"


@pytest.mark.parametrize("field_type", NEW_TYPES)
async def test_a_non_editable_field_is_dropped_on_update(
    locked_backend: SQLAlchemyBackend, field_type: FieldType
) -> None:
    spec = _locked_down_spec(field_type=field_type)
    async with locked_backend.unit_of_work() as uow:
        adapter = SQLAlchemyAdapter(Product, spec, uow.session, profile_for("sqlite"))
        obj = await adapter.create({"name": "Legitimate product"})
        await adapter.update(obj, {"description": "hacked"})
        assert obj.description is None
