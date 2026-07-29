"""Database fixtures for the ORM suite.

Each test gets a freshly created schema on whichever databases were selected, so
a failure on MySQL cannot be masked by state left behind on SQLite.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from fastfort.orm.sqlalchemy import SQLAlchemyBackend

from .models import Base, Category, Product, Status, StockLevel, Tag


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[sa.ext.asyncio.AsyncEngine]:
    created = create_async_engine(db_url, future=True)
    async with created.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield created
    finally:
        async with created.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await created.dispose()


@pytest.fixture
def session_factory(engine: sa.ext.asyncio.AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False keeps attributes readable after the unit of work
    # commits, which is what a view needs when rendering the object it just saved.
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def backend(session_factory: async_sessionmaker[AsyncSession]) -> SQLAlchemyBackend:
    return SQLAlchemyBackend(session_factory=session_factory, base=Base)


@pytest.fixture
async def seeded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """A small, deliberately uneven data set.

    Names differ in case, one product has no category and no release date, and
    prices are decimals with trailing zeros -- each of which has broken one of
    the three databases at some point.
    """
    async with session_factory() as session:
        phones = Category(name="Phones")
        laptops = Category(name="Laptops")
        new = Tag(name="new")
        sale = Tag(name="sale")
        session.add_all([phones, laptops, new, sale])
        await session.flush()

        session.add_all(
            [
                Product(
                    name="Pixel Phone",
                    description="A phone",
                    price=Decimal("799.00"),
                    stock=10,
                    is_active=True,
                    status=Status.PUBLISHED,
                    released_on=dt.date(2026, 1, 15),
                    category=phones,
                    tags=[new],
                ),
                Product(
                    name="pixel case",
                    description="An accessory",
                    price=Decimal("19.50"),
                    stock=200,
                    is_active=True,
                    status=Status.PUBLISHED,
                    released_on=dt.date(2026, 3, 1),
                    category=phones,
                    tags=[sale],
                ),
                Product(
                    name="Retired Laptop",
                    price=Decimal("1200.00"),
                    stock=0,
                    is_active=False,
                    status=Status.ARCHIVED,
                    category=laptops,
                ),
                Product(name="Unfiled Gadget", price=Decimal("5.00"), stock=1),
            ]
        )
        session.add_all(
            [
                StockLevel(warehouse="tashkent", sku="PX-1", quantity=5),
                StockLevel(warehouse="samarkand", sku="PX-1", quantity=2),
            ]
        )
        await session.commit()
