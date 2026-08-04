"""What the adapter says a delete would do, before it does it.

The three answers are not interchangeable and the difference is invisible in the
model unless someone reads the foreign key: a child whose column is nullable
survives, one whose relationship cascades does not, and one whose `NOT NULL`
foreign key has nothing cascading behind it makes the delete impossible. An admin
that guesses which of the three is happening either destroys rows it promised to
keep or offers a delete the database will refuse.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.orm.models import Category, Product
from tests.orm.relations import Crate, Depot, Item, Ledger, Note

from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.deletion import DELETION_SAMPLE
from fastfort.spec import DeletionEffect, DeletionPlan

pytestmark = pytest.mark.usefixtures("seeded")


async def plan_for(backend: SQLAlchemyBackend, model: type, pk: tuple[object, ...]) -> DeletionPlan:
    """The plan for one row, read through the adapter the views use."""
    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(model, uow, key=f"shop.{model.__name__.lower()}")
        obj = await adapter.get(pk)
        assert obj is not None
        return await adapter.deletion_plan([obj])


def group(plan: DeletionPlan, label: str) -> object:
    found = next((row for row in plan.related if row.label == label), None)
    assert found is not None, f"{label} missing from {[row.label for row in plan.related]}"
    return found


@pytest.fixture
async def depot(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """A depot with one of each kind of thing pointing at it."""
    async with session_factory() as session:
        one = Depot(name="North")
        session.add(one)
        await session.flush()
        session.add_all(
            [
                Crate(label="Crate A", depot_id=one.id),
                Crate(label="Crate B", depot_id=one.id),
                Note(body="Stocktake due", depot_id=one.id),
            ]
        )
        await session.commit()
        return int(one.id)


# ---------------------------------------------------------------------------
# The three effects
# ---------------------------------------------------------------------------


async def test_a_cascading_relation_is_reported_as_deleted(
    backend: SQLAlchemyBackend, depot: int
) -> None:
    crates = group(await plan_for(backend, Depot, (depot,)), "Crates")

    assert crates.effect is DeletionEffect.DELETE
    assert crates.count == 2
    assert set(crates.samples) == {"Crate A", "Crate B"}


async def test_a_nullable_foreign_key_is_reported_as_cleared(
    backend: SQLAlchemyBackend, depot: int
) -> None:
    """The rows survive. Warning that they will be deleted is a lie in the
    direction that makes somebody cancel a delete they wanted.
    """
    notes = group(await plan_for(backend, Depot, (depot,)), "Notes")

    assert notes.effect is DeletionEffect.CLEAR
    assert notes.count == 1


async def test_a_non_nullable_foreign_key_blocks_the_plan(
    backend: SQLAlchemyBackend,
    depot: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(Ledger(reference="LG-1", depot_id=depot))
        await session.commit()

    plan = await plan_for(backend, Depot, (depot,))

    assert plan.blocked
    assert [row.label for row in plan.protected] == ["Ledgers"]


async def test_nothing_pointing_at_a_row_is_an_empty_plan(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        empty = Depot(name="Empty")
        session.add(empty)
        await session.commit()
        key = int(empty.id)

    plan = await plan_for(backend, Depot, (key,))

    assert not plan.related
    assert not plan.blocked
    assert plan.targets == ("Empty",)


# ---------------------------------------------------------------------------
# Finding the relations at all
# ---------------------------------------------------------------------------


async def test_a_relation_declared_only_on_the_child_is_still_found(
    backend: SQLAlchemyBackend,
    depot: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`Depot` never mentions `Ledger`. Reading only the parent's own
    relationships would miss every foreign key whose model did not declare a back
    reference, which is most of them in a schema nobody wrote for an admin.
    """
    async with session_factory() as session:
        session.add(Ledger(reference="LG-2", depot_id=depot))
        await session.commit()

    assert group(await plan_for(backend, Depot, (depot,)), "Ledgers")


async def test_a_cascade_is_followed_to_what_it_takes_with_it(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ "2 crates" is the wrong number to warn somebody with when what actually
    goes is everything inside them.
    """
    async with session_factory() as session:
        yard = Depot(name="Yard")
        session.add(yard)
        await session.flush()
        for n in range(3):
            crate = Crate(label=f"Crate {n}", depot_id=yard.id)
            session.add(crate)
            await session.flush()
            session.add_all([Item(sku=f"{n}-{i}", crate_id=crate.id) for i in range(2)])
        await session.commit()
        key = int(yard.id)

    items = group(await plan_for(backend, Depot, (key,)), "Items")

    assert items.effect is DeletionEffect.DELETE
    assert items.count == 6


async def test_a_deeper_level_is_counted_in_full_not_from_the_sample(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The level above is only sampled -- five rows of however many there are --
    so counting its children from those rows would report a fraction and print it
    as a total. The subquery is what makes the second number mean anything.
    """
    crates = DELETION_SAMPLE + 3
    async with session_factory() as session:
        depot = Depot(name="Warehouse")
        session.add(depot)
        await session.flush()
        for n in range(crates):
            crate = Crate(label=f"Crate {n}", depot_id=depot.id)
            session.add(crate)
            await session.flush()
            session.add(Item(sku=f"SKU-{n}", crate_id=crate.id))
        await session.commit()
        key = int(depot.id)

    items = group(await plan_for(backend, Depot, (key,)), "Items")

    assert items.count == crates


async def test_many_to_many_is_left_out(backend: SQLAlchemyBackend) -> None:
    """Deleting a product removes its rows from the association table and nothing
    else. Reporting "1 tag" would say a tag is at risk, which is false.
    """
    plan = await plan_for(backend, Product, (1,))

    assert [row.label for row in plan.related] == []


async def test_the_shop_schema_clears_rather_than_deletes(backend: SQLAlchemyBackend) -> None:
    """`Category.products` declares no cascade, so SQLAlchemy nulls the column.
    That is the default nobody writes down, and the admin has to report what will
    happen rather than the cascade people assume is there.
    """
    products = group(await plan_for(backend, Category, (1,)), "Products")

    assert products.effect is DeletionEffect.CLEAR
    assert products.count == 2
    assert set(products.samples) == {"Pixel Phone", "pixel case"}


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------


async def test_only_a_handful_of_rows_are_named(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A confirmation lists enough to recognise what is going, not all of it."""
    async with session_factory() as session:
        big = Depot(name="Big")
        session.add(big)
        await session.flush()
        session.add_all(
            [Crate(label=f"Crate {n}", depot_id=big.id) for n in range(DELETION_SAMPLE + 4)]
        )
        await session.commit()
        key = int(big.id)

    crates = group(await plan_for(backend, Depot, (key,)), "Crates")

    assert len(crates.samples) == DELETION_SAMPLE
    assert crates.count == DELETION_SAMPLE + 4
    assert crates.more == 4
    assert not crates.truncated


async def test_a_count_that_hits_the_cap_says_so(
    backend: SQLAlchemyBackend,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past the cap the number is a floor. `truncated` is what stops the page
    printing it as a total, which would understate the very thing it warns about.
    """
    from fastfort.orm.sqlalchemy import deletion

    monkeypatch.setattr(deletion, "DELETION_COUNT_CAP", 3)
    monkeypatch.setattr(deletion, "DELETION_SAMPLE", 2)

    async with session_factory() as session:
        crowded = Depot(name="Crowded")
        session.add(crowded)
        await session.flush()
        session.add_all([Crate(label=f"C{n}", depot_id=crowded.id) for n in range(10)])
        await session.commit()
        key = int(crowded.id)

    crates = group(await plan_for(backend, Depot, (key,)), "Crates")

    assert crates.truncated
    assert crates.count == 3
