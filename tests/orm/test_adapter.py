"""The SQLAlchemy adapter, exercised against every selected database.

This is the suite that backs the claim "SQLite, PostgreSQL and MySQL behave the
same". Run it with `--db=all`; a behaviour that only holds on SQLite fails here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fastfort.core.exceptions import AdapterError, ObjectNotFound, ValidationError
from fastfort.orm.sqlalchemy import SQLAlchemyAdapter, SQLAlchemyBackend
from fastfort.spec import Filter, FilterOperator, ListQuery, SortSpec

from .models import Category, Product, StockLevel, Tag

pytestmark = pytest.mark.usefixtures("seeded")


def query(**kwargs: Any) -> ListQuery:
    kwargs.setdefault("page_size", 25)
    return ListQuery(**kwargs)


@pytest.fixture
async def products(backend: SQLAlchemyBackend) -> AsyncIterator[SQLAlchemyAdapter]:
    async with backend.unit_of_work() as uow:
        yield backend.adapter(
            Product,
            uow,
            key="shop.product",
            search_fields=("name", "description"),
            select_related=("category",),
            prefetch_related=("tags",),
        )


@pytest.fixture
async def stock(backend: SQLAlchemyBackend) -> AsyncIterator[SQLAlchemyAdapter]:
    async with backend.unit_of_work() as uow:
        yield backend.adapter(StockLevel, uow, key="shop.stock_level")


async def names(adapter: SQLAlchemyAdapter, list_query: ListQuery) -> list[str]:
    page = await adapter.list(list_query)
    return [item.name for item in page.items]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_list_returns_every_row_with_a_total(products: SQLAlchemyAdapter) -> None:
    page = await products.list(query())
    assert page.total == 4
    assert len(page.items) == 4


async def test_pagination_splits_without_losing_or_repeating_rows(
    products: SQLAlchemyAdapter,
) -> None:
    first = await names(products, query(page=1, page_size=3))
    second = await names(products, query(page=2, page_size=3))

    assert len(first) == 3
    assert len(second) == 1
    assert set(first) & set(second) == set()


async def test_rows_have_a_stable_order_without_an_explicit_sort(
    products: SQLAlchemyAdapter,
) -> None:
    """Without a tiebreaker a row can swap pages and never be shown."""
    assert await names(products, query()) == await names(products, query())


async def test_ordering_ascending_and_descending(products: SQLAlchemyAdapter) -> None:
    ascending = await names(products, query(ordering=(SortSpec("price"),)))
    descending = await names(products, query(ordering=(SortSpec("price", descending=True),)))
    assert ascending == list(reversed(descending))
    assert ascending[0] == "Unfiled Gadget"


async def test_nulls_sort_last_on_every_database(products: SQLAlchemyAdapter) -> None:
    """MySQL has no NULLS LAST clause; a list that differs per database looks broken."""
    for descending in (False, True):
        ordered = await names(products, query(ordering=(SortSpec("released_on", descending),)))
        assert ordered[-2:] == sorted(ordered[-2:])
        assert set(ordered[-2:]) == {"Retired Laptop", "Unfiled Gadget"}


async def test_search_is_case_insensitive(products: SQLAlchemyAdapter) -> None:
    """PostgreSQL has ILIKE, the others do not; the result must not differ."""
    assert sorted(await names(products, query(search="PIXEL"))) == ["Pixel Phone", "pixel case"]
    assert sorted(await names(products, query(search="pixel"))) == ["Pixel Phone", "pixel case"]


async def test_search_covers_every_configured_field(products: SQLAlchemyAdapter) -> None:
    assert await names(products, query(search="accessory")) == ["pixel case"]


async def test_like_wildcards_in_a_search_are_escaped(products: SQLAlchemyAdapter) -> None:
    """An unescaped `%` turns a search into a full table scan that matches all."""
    assert await names(products, query(search="%")) == []
    assert await names(products, query(search="_")) == []


async def test_count_ignores_pagination(products: SQLAlchemyAdapter) -> None:
    page = await products.list(query(page_size=1))
    assert len(page.items) == 1
    assert page.total == 4
    assert page.pages == 4


async def test_get_by_primary_key(products: SQLAlchemyAdapter) -> None:
    page = await products.list(query())
    target = page.items[0]

    found = await products.get(products.primary_key_of(target))

    assert found is not None
    assert found.name == target.name


async def test_get_returns_none_for_a_missing_row(products: SQLAlchemyAdapter) -> None:
    assert await products.get((999_999,)) is None
    with pytest.raises(ObjectNotFound):
        await products.require((999_999,))


async def test_a_wrong_shaped_key_is_rejected(products: SQLAlchemyAdapter) -> None:
    with pytest.raises(ValidationError, match="primary key"):
        await products.get((1, 2))


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (Filter("is_active", FilterOperator.EXACT, "true"), 3),
        (Filter("is_active", FilterOperator.EXACT, "0"), 1),
        (Filter("stock", FilterOperator.GT, "5"), 2),
        (Filter("stock", FilterOperator.LTE, "1"), 2),
        (Filter("stock", FilterOperator.NE, "0"), 3),
        (Filter("status", FilterOperator.EXACT, "archived"), 1),
        (Filter("released_on", FilterOperator.ISNULL, "1"), 2),
        (Filter("released_on", FilterOperator.ISNULL, "0"), 2),
        (Filter("stock", FilterOperator.IN, ("0", "1")), 2),
        (Filter("stock", FilterOperator.RANGE, ("1", "10")), 2),
        (Filter("name", FilterOperator.ICONTAINS, "PIXEL"), 2),
        (Filter("name", FilterOperator.ISTARTSWITH, "pixel"), 2),
        (Filter("name", FilterOperator.IENDSWITH, "case"), 1),
        (Filter("description", FilterOperator.IEXACT, "A PHONE"), 1),
    ],
)
async def test_filter_operators(
    products: SQLAlchemyAdapter, condition: Filter, expected: int
) -> None:
    page = await products.list(query(filters=(condition,)))
    assert page.total == expected


async def test_decimal_filters_keep_their_precision(products: SQLAlchemyAdapter) -> None:
    page = await products.list(query(filters=(Filter("price", FilterOperator.EXACT, "19.50"),)))
    assert [item.name for item in page.items] == ["pixel case"]


async def test_a_relation_can_be_filtered_by_its_target(
    products: SQLAlchemyAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        phones = (
            await session.execute(sa.select(Category).where(Category.name == "Phones"))
        ).scalar_one()

    page = await products.list(
        query(filters=(Filter("category", FilterOperator.EXACT, str(phones.id)),))
    )
    assert page.total == 2


async def test_a_malformed_filter_value_is_reported_not_ignored(
    products: SQLAlchemyAdapter,
) -> None:
    """Silently dropping the filter would show every row and look like a bug."""
    with pytest.raises(ValidationError, match="not a valid value"):
        await products.list(query(filters=(Filter("stock", FilterOperator.GT, "many"),)))


async def test_a_range_needs_exactly_two_bounds(products: SQLAlchemyAdapter) -> None:
    with pytest.raises(ValidationError, match="exactly two values"):
        await products.list(query(filters=(Filter("stock", FilterOperator.RANGE, ("1",)),)))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_create_populates_generated_values(products: SQLAlchemyAdapter) -> None:
    created = await products.create({"name": "New Thing", "price": Decimal("42.00")})

    assert created.id is not None  # supplied by the database on flush
    assert created.public_id is not None  # supplied by the column default
    assert created.status.value == "draft"


async def test_update_changes_only_what_was_given(products: SQLAlchemyAdapter) -> None:
    created = await products.create({"name": "Before", "stock": 7})
    updated = await products.update(created, {"name": "After"})

    assert updated.name == "After"
    assert updated.stock == 7


async def test_delete_removes_the_row(products: SQLAlchemyAdapter) -> None:
    created = await products.create({"name": "Temporary"})
    key = products.primary_key_of(created)

    await products.delete(created)

    assert await products.get(key) is None


async def test_a_non_editable_field_cannot_be_written(products: SQLAlchemyAdapter) -> None:
    """Mass-assignment protection: the spec's allow-list is the only gate."""
    created = await products.create({"name": "Fixed", "id": 999_999})
    assert created.id != 999_999


async def test_an_unknown_field_is_discarded_rather_than_raising(
    products: SQLAlchemyAdapter,
) -> None:
    created = await products.create({"name": "Tolerant", "is_superuser": True})
    assert not hasattr(created, "is_superuser")


async def test_a_relation_can_be_set_from_an_identity(
    products: SQLAlchemyAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Forms submit identities; programmatic callers pass objects. Both must work."""
    async with session_factory() as session:
        laptops = (
            await session.execute(sa.select(Category).where(Category.name == "Laptops"))
        ).scalar_one()

    created = await products.create({"name": "By Identity", "category": laptops.id})
    assert created.category_id == laptops.id

    changed = await products.update(created, {"category": None})
    assert changed.category_id is None


async def test_a_many_to_many_can_be_set_from_identities(
    products: SQLAlchemyAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        tags = (await session.execute(sa.select(Tag))).scalars().all()

    created = await products.create({"name": "Tagged", "tags": [tag.id for tag in tags]})
    assert len(created.tags) == len(tags)


async def test_setting_a_relation_to_a_missing_row_is_reported(
    products: SQLAlchemyAdapter,
) -> None:
    with pytest.raises(ValidationError, match="No Category with key"):
        await products.create({"name": "Dangling", "category": 999_999})


# ---------------------------------------------------------------------------
# Bulk operations
# ---------------------------------------------------------------------------


async def test_bulk_update_affects_only_matching_rows(products: SQLAlchemyAdapter) -> None:
    affected = await products.bulk_update(
        query(filters=(Filter("is_active", FilterOperator.EXACT, "1"),)), {"stock": 0}
    )
    assert affected == 3

    remaining = await products.list(query(filters=(Filter("stock", FilterOperator.GT, "0"),)))
    assert remaining.total == 0


async def test_bulk_update_respects_the_editable_allow_list(
    products: SQLAlchemyAdapter,
) -> None:
    assert await products.bulk_update(query(), {"id": 1}) == 0


async def test_bulk_delete_honours_the_filter(products: SQLAlchemyAdapter) -> None:
    deleted = await products.bulk_delete(
        query(filters=(Filter("is_active", FilterOperator.EXACT, "0"),))
    )
    assert deleted == 1
    assert (await products.list(query())).total == 3


async def test_bulk_operations_apply_to_a_search(products: SQLAlchemyAdapter) -> None:
    assert await products.bulk_delete(query(search="pixel")) == 2


# ---------------------------------------------------------------------------
# Composite primary keys
# ---------------------------------------------------------------------------


async def test_composite_keys_round_trip(stock: SQLAlchemyAdapter) -> None:
    created = await stock.create({"warehouse": "bukhara", "sku": "PX-2", "quantity": 9})
    key = stock.primary_key_of(created)

    assert key == ("bukhara", "PX-2")
    found = await stock.get(key)
    assert found is not None
    assert found.quantity == 9


async def test_bulk_delete_with_a_composite_key(stock: SQLAlchemyAdapter) -> None:
    """A composite key cannot use a single IN, so it becomes an OR of ANDs."""
    assert await stock.bulk_delete(query()) == 2
    assert (await stock.list(query())).total == 0


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


async def test_a_failed_unit_of_work_leaves_nothing_behind(
    backend: SQLAlchemyBackend,
) -> None:
    """The adapter only flushes; the unit of work decides what survives."""

    async def create_then_fail() -> None:
        async with backend.unit_of_work() as uow:
            adapter = backend.adapter(Product, uow, key="shop.product")
            await adapter.create({"name": "Rolled Back"})
            raise RuntimeError("request failed")

    with pytest.raises(RuntimeError):
        await create_then_fail()

    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(Product, uow, key="shop.product")
        assert (await adapter.list(query(search="Rolled Back"))).total == 0


async def test_a_successful_unit_of_work_commits(backend: SQLAlchemyBackend) -> None:
    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(Product, uow, key="shop.product")
        await adapter.create({"name": "Committed"})

    async with backend.unit_of_work() as uow:
        adapter = backend.adapter(Product, uow, key="shop.product")
        assert (await adapter.list(query(search="Committed"))).total == 1


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


async def test_snapshot_omits_sensitive_values(products: SQLAlchemyAdapter) -> None:
    """Omitted rather than masked, so they cannot reach an audit record by accident."""
    created = await products.create({"name": "Secretive", "api_secret": "s3cret"})
    state = products.snapshot(created)

    assert "api_secret" not in state
    assert "s3cret" not in repr(state)
    assert state["name"] == "Secretive"


async def test_snapshot_reduces_relations_to_identities(
    products: SQLAlchemyAdapter, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        phones = (
            await session.execute(sa.select(Category).where(Category.name == "Phones"))
        ).scalar_one()

    created = await products.create({"name": "Snapshot", "category": phones.id})
    assert products.snapshot(created)["category"] == phones.id


async def test_label_uses_the_models_own_str(products: SQLAlchemyAdapter) -> None:
    created = await products.create({"name": "Readable"})
    assert products.label_for(created) == "Readable"


async def test_label_falls_back_when_there_is_no_str(stock: SQLAlchemyAdapter) -> None:
    created = await stock.create({"warehouse": "khiva", "sku": "PX-9"})
    assert "khiva" in stock.label_for(created)


async def test_related_choices_search_the_target(products: SQLAlchemyAdapter) -> None:
    choices = await products.related_choices("category", "phon", limit=10)
    assert [choice.label for choice in choices] == ["Phones"]


async def test_related_choices_are_limited(products: SQLAlchemyAdapter) -> None:
    assert len(await products.related_choices("category", "", limit=1)) == 1


async def test_related_choices_reject_a_non_relation(products: SQLAlchemyAdapter) -> None:
    with pytest.raises(ValidationError, match="not a relation"):
        await products.related_choices("name", "x", limit=5)


# ---------------------------------------------------------------------------
# Query cost
# ---------------------------------------------------------------------------


async def test_listing_with_relations_does_not_issue_a_query_per_row(
    backend: SQLAlchemyBackend, engine: Any
) -> None:
    """Without eager loading, showing a related name costs N extra queries."""
    statements: list[str] = []

    @sa.event.listens_for(engine.sync_engine, "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    try:
        async with backend.unit_of_work() as uow:
            adapter = backend.adapter(
                Product,
                uow,
                key="shop.product",
                select_related=("category",),
                prefetch_related=("tags",),
            )
            page = await adapter.list(query())
            # Touching the relations is what would trigger lazy loads.
            for item in page.items:
                _ = item.category.name if item.category else None
                _ = [tag.name for tag in item.tags]
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    # One for the page, one for the count, one for the prefetched tags.
    assert len(selects) <= 3, selects


async def test_dialect_profile_matches_the_connection(backend: SQLAlchemyBackend) -> None:
    assert backend.profile.name == backend.dialect
    await backend.check_connection()


async def test_an_unreachable_database_says_which_one() -> None:
    """The driver reports the address it dialled and nothing else, which leaves
    nobody able to tell which of a project's databases refused them or what was
    supposed to be listening there.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(
        "postgresql+asyncpg://someone:hunter2@127.0.0.1:1/nowhere",
        connect_args={"timeout": 2},
    )
    unreachable = SQLAlchemyBackend(session_factory=async_sessionmaker(engine))

    with pytest.raises(AdapterError) as raised:
        await unreachable.check_connection()
    await engine.dispose()

    assert "127.0.0.1:1/nowhere" in raised.value.message
    # Masked, or every log that catches this error now holds the password.
    assert "hunter2" not in str(raised.value)
    assert raised.value.hint


async def test_created_at_survives_the_round_trip(products: SQLAlchemyAdapter) -> None:
    """MySQL stores no timezone; the value must still come back as an instant."""
    created = await products.create({"name": "Timed"})
    found = await products.get(products.primary_key_of(created))

    assert found is not None
    assert isinstance(found.created_at, dt.datetime)
