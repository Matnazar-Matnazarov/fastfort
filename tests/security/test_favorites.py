"""Starred rows, at the service level.

Two properties carry this feature. A star belongs to exactly one account, so
one person's list can never leak into another's; and a star that points at a
row which has since gone must disappear rather than render as a broken link.
Everything else here is the toggle behaving like a toggle.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.orm.models import Category, Favorite, Product, StaffUser

from fastfort import FastFort, FastFortSettings
from fastfort.admin import ModelAdmin
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.hooks import Hook
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

PRODUCT_KEY = "category_product.product"
CATEGORY_KEY = "category.category"


@pytest.fixture
def fort(backend: SQLAlchemyBackend) -> FastFort:
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    built.set_user_model(StaffUser)
    built.register(Product, ModelAdmin, key=PRODUCT_KEY)
    built.register(Category, ModelAdmin, key=CATEGORY_KEY)
    built.enable_favorites(Favorite)
    return built


@pytest.fixture
async def user(staff_user: StaffUser) -> StaffUser:
    return staff_user


@pytest.fixture
async def product(session_factory: async_sessionmaker[AsyncSession]) -> Product:
    async with session_factory() as session:
        found = await session.scalar(sa.select(Product).limit(1))
    assert found is not None
    return found


async def _second_user(session_factory: async_sessionmaker[AsyncSession], email: str) -> StaffUser:
    async with session_factory() as session:
        user = StaffUser(email=email, hashed_password="x", is_staff=True)
        session.add(user)
        await session.commit()
    return user


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_model_without_the_columns_is_refused(backend: SQLAlchemyBackend) -> None:
    """Named at start-up, not on the first click of a star."""
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    built.set_user_model(StaffUser)

    with pytest.raises(ConfigurationError, match="cannot hold favorites"):
        built.enable_favorites(Product)


def test_the_service_is_absent_until_it_is_enabled(backend: SQLAlchemyBackend) -> None:
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    assert built.favorites is None


def test_enabling_registers_the_delete_listener(fort: FastFort) -> None:
    """Without this the table grows a row per deleted object, forever."""
    assert len(fort.hooks.listeners(Hook.AFTER_DELETE)) == 1


# ---------------------------------------------------------------------------
# Toggling
# ---------------------------------------------------------------------------


async def test_a_star_turns_on_and_off(fort: FastFort, user: StaffUser, product: Product) -> None:
    on = await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    assert on is True
    assert await fort.favorites.is_starred(user=user, model_key=PRODUCT_KEY, obj=product)

    off = await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    assert off is False
    assert not await fort.favorites.is_starred(user=user, model_key=PRODUCT_KEY, obj=product)


async def test_an_unstarred_row_reads_as_unstarred(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    assert not await fort.favorites.is_starred(user=user, model_key=PRODUCT_KEY, obj=product)


async def test_toggling_repeatedly_never_accumulates_rows(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    """Three presses land on "starred", and on exactly one row -- not on three
    that a later unstar would have to find all of."""
    for _ in range(3):
        await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)

    assert await fort.favorites.is_starred(user=user, model_key=PRODUCT_KEY, obj=product)
    assert await fort.favorites.count_for_user(user=user) == 1


async def test_no_account_stars_nothing(fort: FastFort, product: Product) -> None:
    """A signed-out request must not write a row keyed on an empty account."""
    assert await fort.favorites.toggle(user=None, model_key=PRODUCT_KEY, obj=product) is False
    assert await fort.favorites.count_for_user(user=None) == 0


async def test_an_unregistered_model_cannot_be_starred(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    """The key comes from a URL, so a made-up one must be a refusal rather than
    a row pointing at a model the admin has no page for."""
    starred = await fort.favorites.toggle(user=user, model_key="nope.nothing", obj=product)
    assert starred is False
    assert await fort.favorites.count_for_user(user=user) == 0


# ---------------------------------------------------------------------------
# Whose stars these are
# ---------------------------------------------------------------------------


async def test_one_account_cannot_see_anothers_stars(
    fort: FastFort,
    user: StaffUser,
    product: Product,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    other = await _second_user(session_factory, "other@example.com")

    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)

    assert await fort.favorites.count_for_user(user=other) == 0
    assert not await fort.favorites.is_starred(user=other, model_key=PRODUCT_KEY, obj=product)
    assert await fort.favorites.list_for_user(user=other) == ()


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_the_list_resolves_targets_and_labels(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)

    found = await fort.favorites.list_for_user(user=user)

    assert len(found) == 1
    assert found[0].model_key == PRODUCT_KEY
    assert found[0].label
    assert found[0].obj is not None


async def test_the_list_narrows_to_one_model(
    fort: FastFort,
    user: StaffUser,
    product: Product,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        category = await session.scalar(sa.select(Category).limit(1))
    assert category is not None

    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    await fort.favorites.toggle(user=user, model_key=CATEGORY_KEY, obj=category)

    assert len(await fort.favorites.list_for_user(user=user)) == 2
    narrowed = await fort.favorites.list_for_user(user=user, model_key=PRODUCT_KEY)
    assert len(narrowed) == 1
    assert narrowed[0].model_key == PRODUCT_KEY


async def test_starred_keys_answers_a_whole_page_at_once(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    """The list view draws a star per row; asking per row would be one query
    per row to render a table."""
    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)

    keys = await fort.favorites.starred_keys(user=user, model_key=PRODUCT_KEY)

    assert keys == frozenset({str(product.id)})


async def test_a_star_whose_target_is_gone_is_skipped(
    fort: FastFort,
    user: StaffUser,
    product: Product,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Deleted outside the admin, so no hook fired and the row is still there.
    The page must not show it."""
    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    async with session_factory() as session:
        await session.delete(await session.get(Product, product.id))
        await session.commit()

    assert await fort.favorites.list_for_user(user=user) == ()
    # The row itself survives -- this is the read-side guard, not the cleanup.
    assert await fort.favorites.count_for_user(user=user) == 1


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def test_deleting_through_the_admin_clears_the_stars(
    fort: FastFort, user: StaffUser, product: Product
) -> None:
    """`AFTER_DELETE` is what keeps this table from growing a row per deletion
    forever."""
    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    assert await fort.favorites.count_for_user(user=user) == 1

    await fort.hooks.emit(Hook.AFTER_DELETE, request=None, model_key=PRODUCT_KEY, obj=product)

    assert await fort.favorites.count_for_user(user=user) == 0


async def test_the_cleanup_clears_every_accounts_star(
    fort: FastFort,
    user: StaffUser,
    product: Product,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A deleted row is gone for everyone, not just for whoever deleted it."""
    other = await _second_user(session_factory, "second@example.com")

    await fort.favorites.toggle(user=user, model_key=PRODUCT_KEY, obj=product)
    await fort.favorites.toggle(user=other, model_key=PRODUCT_KEY, obj=product)

    await fort.hooks.emit(Hook.AFTER_DELETE, request=None, model_key=PRODUCT_KEY, obj=product)

    assert await fort.favorites.count_for_user(user=user) == 0
    assert await fort.favorites.count_for_user(user=other) == 0
