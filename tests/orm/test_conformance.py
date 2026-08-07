"""One set of questions, asked of every backend.

This is what the layering is for. `fastfort/admin/`, `fastfort/ui/` and
`fastfort/spec/` read a `ModelSpec` and a `ListQuery` and cannot tell which ORM
produced them -- so the contract those layers depend on is exactly what is
written down here, and a second backend is correct when it answers the same way
the first does.

Every test is parametrised over both backends and uses the same model shapes:
`tests/orm/models.py` for SQLAlchemy and `tests/orm/tortoise_models.py` for
Tortoise, deliberately identical in names, columns and relations. If they
differed, a disagreement between the backends would be indistinguishable from a
disagreement between the fixtures.

Where the two genuinely cannot agree, the test says so and says why rather than
being weakened until both pass. There is one such place so far, noted below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fastfort.spec import FieldType, Filter, FilterOperator, ListQuery, SortSpec

BACKENDS = ("sqlalchemy", "tortoise")


class Harness:
    """Everything a conformance test needs, whichever ORM is underneath."""

    def __init__(self, name: str, backend: Any, models: Any) -> None:
        self.name = name
        self.backend = backend
        self.models = models

    def spec(self, model_name: str) -> Any:
        model = getattr(self.models, model_name)
        return self.backend.introspect(model, key=f"shop.{model_name.lower()}")

    def adapter(self, uow: Any, model_name: str, **kwargs: Any) -> Any:
        model = getattr(self.models, model_name)
        return self.backend.adapter(model, uow, key=f"shop.{model_name.lower()}", **kwargs)


@pytest.fixture(params=BACKENDS)
async def orm(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Harness]:
    """A backend with the shared model set created and seeded.

    SQLite for both: the point here is that the two ORMs agree, and running each
    against a different database would confuse an ORM difference with a dialect
    one. The dialect differences have their own suite.
    """
    if request.param == "sqlalchemy":
        async for harness in _sqlalchemy_harness(tmp_path):
            yield harness
    else:
        async for harness in _tortoise_harness(tmp_path):
            yield harness


async def _sqlalchemy_harness(tmp_path: Path) -> AsyncIterator[Harness]:
    from tests.orm import models

    from fastfort.orm.sqlalchemy import SQLAlchemyBackend

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'conformance.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    backend = SQLAlchemyBackend(session_factory=factory, base=models.Base)

    async with factory() as session:
        phones = models.Category(name="Phones")
        laptops = models.Category(name="Laptops")
        new, sale = models.Tag(name="new"), models.Tag(name="sale")
        session.add_all([phones, laptops, new, sale])
        await session.flush()
        session.add_all(
            [
                models.Product(
                    name="Pixel Phone", price=Decimal("799.00"), stock=12, category=phones
                ),
                models.Product(
                    name="pixel case",
                    price=Decimal("19.50"),
                    stock=240,
                    category=phones,
                    is_active=False,
                ),
                models.Product(
                    name="Retired Laptop", price=Decimal("1200.00"), stock=0, category=laptops
                ),
            ]
        )
        await session.commit()

    yield Harness("sqlalchemy", backend, models)
    await engine.dispose()


async def _tortoise_harness(tmp_path: Path) -> AsyncIterator[Harness]:
    from tests.orm import tortoise_models as models
    from tortoise import Tortoise

    from fastfort.orm.tortoise import TortoiseBackend

    await Tortoise.init(
        db_url=f"sqlite://{tmp_path / 'conformance-tortoise.db'}",
        modules={"models": ["tests.orm.tortoise_models"]},
    )
    await Tortoise.generate_schemas()

    phones = await models.Category.create(name="Phones")
    laptops = await models.Category.create(name="Laptops")
    await models.Tag.create(name="new")
    await models.Tag.create(name="sale")
    await models.Product.create(
        name="Pixel Phone", price=Decimal("799.00"), stock=12, category=phones
    )
    await models.Product.create(
        name="pixel case", price=Decimal("19.50"), stock=240, category=phones, is_active=False
    )
    await models.Product.create(
        name="Retired Laptop", price=Decimal("1200.00"), stock=0, category=laptops
    )

    yield Harness("tortoise", TortoiseBackend(), models)
    await Tortoise.close_connections()
    # Tortoise keeps its models in process-global state, so a second
    # initialisation in the same run inherits the first unless it is torn down.
    await Tortoise._reset_apps()


async def names(adapter: Any, query: ListQuery) -> list[str]:
    page = await adapter.list(query)
    return [str(item) for item in page.items]


# ---------------------------------------------------------------------------
# The spec both backends have to produce
# ---------------------------------------------------------------------------


def test_the_same_model_describes_the_same_fields(orm: Harness) -> None:
    """Not merely similar. Everything above `fastfort/orm/` branches on these
    names and types, so a backend that reported `price` as a float would give
    the same model a different form."""
    spec = orm.spec("Product")
    by_name = {field.name: field for field in spec}

    assert by_name["name"].type is FieldType.STRING
    assert by_name["description"].type is FieldType.TEXT
    assert by_name["price"].type is FieldType.DECIMAL
    assert by_name["stock"].type is FieldType.BIGINT
    assert by_name["weight"].type is FieldType.FLOAT
    assert by_name["is_active"].type is FieldType.BOOLEAN
    assert by_name["status"].type is FieldType.ENUM
    assert by_name["released_on"].type is FieldType.DATE
    assert by_name["created_at"].type is FieldType.DATETIME
    assert by_name["category"].type is FieldType.FOREIGN_KEY
    assert by_name["tags"].type is FieldType.MANY_TO_MANY


def test_the_primary_key_is_always_a_tuple(orm: Harness) -> None:
    """A one-column key is a one-element tuple, on both. The layer above treats
    every key as one and a composite key is not a special case there."""
    assert orm.spec("Product").primary_key == ("id",)


def test_a_generated_key_is_not_editable(orm: Harness) -> None:
    """Offering the box invites somebody to collide with an existing row."""
    assert orm.spec("Product").field("id").editable is False


def test_an_enum_carries_its_choices(orm: Harness) -> None:
    status = orm.spec("Product").field("status")
    assert [choice.value for choice in status.choices] == ["draft", "published", "archived"]


def test_a_relation_points_at_the_key_it_was_resolved_with(orm: Harness) -> None:
    relation = orm.spec("Product").field("category").relation
    assert relation is not None
    assert relation.target.endswith("category")


def test_a_to_many_relation_is_never_sortable(orm: Harness) -> None:
    """Ordering a list by a to-many multiplies rows, which is never what the
    person meant."""
    assert orm.spec("Product").field("tags").sortable is False


def test_a_sensitive_column_is_detected_by_name(orm: Harness) -> None:
    """Name-based and imperfect, and deliberately the same list on both -- a
    model ported from one ORM to the other must not quietly start echoing a
    token back into a form."""
    assert orm.spec("Product").field("api_secret").sensitive is True


def test_a_reverse_relation_appears_on_the_other_model(orm: Harness) -> None:
    """`Category.products` is a foreign key declared on `Product`, and both
    backends have to find it from the far side."""
    products = orm.spec("Category").get("products")
    assert products is not None
    assert products.type is FieldType.REVERSE_FK


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_an_unfiltered_list_returns_every_row(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        page = await orm.adapter(uow, "Product").list(ListQuery())

    assert page.total == 3
    assert len(page.items) == 3


async def test_ordering_is_honoured_and_tie_broken_by_the_key(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(ordering=(SortSpec("price", descending=True),)),
        )

    assert found == ["Retired Laptop", "Pixel Phone", "pixel case"]


async def test_paging_walks_the_whole_set_without_repeating(orm: Harness) -> None:
    """Without a tiebreaker two rows with equal sort keys can swap places
    between pages and one of them is never shown."""
    seen: list[str] = []
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        for page in (1, 2, 3):
            seen.extend(await names(adapter, ListQuery(page=page, page_size=1)))

    assert sorted(seen) == ["Pixel Phone", "Retired Laptop", "pixel case"]


async def test_search_is_case_insensitive_on_every_backend(orm: Harness) -> None:
    """ "pixel case" and "Pixel Phone" differ in case, and a search that only
    matched one of them would be a different feature on each ORM."""
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product", search_fields=("name",)), ListQuery(search="PIXEL")
        )

    assert sorted(found) == ["Pixel Phone", "pixel case"]


@pytest.mark.parametrize(
    ("operator", "value", "expected"),
    [
        (FilterOperator.EXACT, "12", ["Pixel Phone"]),
        (FilterOperator.GT, "100", ["pixel case"]),
        (FilterOperator.GTE, "240", ["pixel case"]),
        (FilterOperator.LT, "12", ["Retired Laptop"]),
        (FilterOperator.NE, "0", ["Pixel Phone", "pixel case"]),
        (FilterOperator.IN, ("0", "12"), ["Pixel Phone", "Retired Laptop"]),
        (FilterOperator.RANGE, ("0", "12"), ["Pixel Phone", "Retired Laptop"]),
    ],
)
async def test_every_filter_operator_means_the_same_thing(
    orm: Harness, operator: FilterOperator, value: Any, expected: list[str]
) -> None:
    """`FilterOperator`'s docstring is the promise being kept here: each one has
    to mean the same thing everywhere. A saved link has to survive the ORM being
    swapped, not merely the database."""
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(filters=(Filter("stock", operator, value),)),
        )

    assert sorted(found) == sorted(expected)


async def test_a_boolean_filter_reads_the_string_a_url_carries(orm: Harness) -> None:
    """`?is_active=0` is what a checkbox list submits. Compared as the *text*
    "0" it matched the rows it was meant to exclude -- silently, because a
    string is a perfectly valid thing to compare against."""
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(filters=(Filter("is_active", FilterOperator.EXACT, "0"),)),
        )

    assert found == ["pixel case"]


async def test_a_relation_is_filtered_by_its_targets_identity(orm: Harness) -> None:
    """Which is what the dropdown submits."""
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(filters=(Filter("category", FilterOperator.EXACT, "1"),)),
        )

    assert sorted(found) == ["Pixel Phone", "pixel case"]


async def test_a_missing_row_is_none_rather_than_an_error(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        assert await orm.adapter(uow, "Product").get((9999,)) is None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


async def test_a_row_can_be_created_read_back_and_deleted(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        created = await adapter.create({"name": "Made Here", "price": Decimal("5.00")})
        key = adapter.primary_key_of(created)

    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        found = await adapter.get(key)
        assert found is not None
        assert str(found) == "Made Here"
        await adapter.delete(found)

    async with orm.backend.unit_of_work() as uow:
        assert await orm.adapter(uow, "Product").get(key) is None


async def test_an_update_writes_only_what_it_was_given(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        row = await adapter.get((1,))
        await adapter.update(row, {"name": "Renamed"})

    async with orm.backend.unit_of_work() as uow:
        found = await orm.adapter(uow, "Product").get((1,))
        assert str(found) == "Renamed"
        # Everything else is as it was.
        assert found.price == Decimal("799.00")


async def test_a_relation_is_set_from_a_bare_identity(orm: Harness) -> None:
    """Forms submit identities and programmatic callers pass objects. Both have
    to work on both backends."""
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        row = await adapter.get((3,))
        await adapter.update(row, {"category": 1})

    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(filters=(Filter("category", FilterOperator.EXACT, "1"),)),
        )
    assert "Retired Laptop" in found


async def test_editable_is_the_only_mass_assignment_boundary(orm: Harness) -> None:
    """The invariant both adapters are built around. A value posted for a field
    the spec marks read-only is dropped, whichever endpoint it arrived through
    and whichever ORM is underneath."""
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        row = await adapter.get((1,))
        # `id` is generated, so `editable` is False and this must not take.
        await adapter.update(row, {"id": 4242, "name": "Still Here"})

    async with orm.backend.unit_of_work() as uow:
        assert await orm.adapter(uow, "Product").get((4242,)) is None
        assert str(await orm.adapter(uow, "Product").get((1,))) == "Still Here"


async def test_nothing_survives_a_rolled_back_unit_of_work(orm: Harness) -> None:
    """Adapters never commit -- the unit of work decides. A request that fails
    halfway has to leave nothing behind on either backend."""
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        await adapter.create({"name": "Should Vanish", "price": Decimal("1.00")})
        await uow.rollback()

    async with orm.backend.unit_of_work() as uow:
        found = await names(orm.adapter(uow, "Product"), ListQuery())
    assert "Should Vanish" not in found


async def test_a_bulk_update_touches_every_matching_row(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        affected = await orm.adapter(uow, "Product").bulk_update(ListQuery(), {"stock": 7})

    assert affected == 3
    async with orm.backend.unit_of_work() as uow:
        found = await names(
            orm.adapter(uow, "Product"),
            ListQuery(filters=(Filter("stock", FilterOperator.EXACT, "7"),)),
        )
    assert len(found) == 3


async def test_a_bulk_delete_removes_every_matching_row(orm: Harness) -> None:
    async with orm.backend.unit_of_work() as uow:
        removed = await orm.adapter(uow, "Product").bulk_delete(
            ListQuery(filters=(Filter("is_active", FilterOperator.EXACT, "0"),))
        )

    assert removed == 1
    async with orm.backend.unit_of_work() as uow:
        assert len(await names(orm.adapter(uow, "Product"), ListQuery())) == 2


# ---------------------------------------------------------------------------
# What the admin asks of an adapter beyond CRUD
# ---------------------------------------------------------------------------


async def test_related_choices_are_searchable_by_label(orm: Harness) -> None:
    """This is what makes a foreign key onto a large table usable at all."""
    async with orm.backend.unit_of_work() as uow:
        found = await orm.adapter(uow, "Product").related_choices("category", "phon", limit=10)

    assert [choice.label for choice in found] == ["Phones"]


async def test_a_snapshot_omits_sensitive_values_entirely(orm: Harness) -> None:
    """Omitted rather than masked, so they cannot reach an audit record even by
    accident."""
    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Product")
        state = adapter.snapshot(await adapter.get((1,)))

    assert "api_secret" not in state
    assert state["name"] == "Pixel Phone"


async def test_a_deletion_plan_names_what_else_would_change(orm: Harness) -> None:
    """Read before anything is written, so the confirmation page can say what
    else goes -- and honest about the effect: `Product.category` is nullable
    with `SET NULL`, so those rows are kept without the link rather than
    deleted."""
    from fastfort.spec import DeletionEffect

    async with orm.backend.unit_of_work() as uow:
        adapter = orm.adapter(uow, "Category")
        plan = await adapter.deletion_plan([await adapter.get((1,))])

    assert plan.related, "deleting a category with products should report them"
    products = next(group for group in plan.related if "roduct" in group.label)
    assert products.count == 2
    assert products.effect is DeletionEffect.CLEAR
