#!/usr/bin/env python
"""Run one full read/write cycle against each database and print what happened.

The test suite already covers this ground, but it reports pass/fail. This prints
the actual values each database returned, which is what you want when adding a
new dialect or chasing a difference you do not yet have a test for.

    uv run python scripts/verify_databases.py                 # SQLite only
    uv run python scripts/verify_databases.py postgres mysql  # needs `make services-up`
    uv run python scripts/verify_databases.py all
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.orm.models import Base, Category, Product

from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.spec import Filter, FilterOperator, ListQuery, SortSpec

URLS = {
    "sqlite": "sqlite+aiosqlite:///{tmp}/fastfort_verify.db",
    "postgres": os.environ.get(
        "FASTFORT_TEST_POSTGRES_URL",
        "postgresql+asyncpg://fastfort:fastfort@localhost:55432/fastfort_test",
    ),
    "mysql": os.environ.get(
        "FASTFORT_TEST_MYSQL_URL",
        "mysql+asyncmy://fastfort:fastfort@localhost:33306/fastfort_test",
    ),
}

GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def check(label: str, actual: object, expected: object) -> bool:
    ok = actual == expected
    mark = f"{GREEN}pass{RESET}" if ok else f"{RED}FAIL{RESET}"
    detail = "" if ok else f"  {DIM}expected {expected!r}, got {actual!r}{RESET}"
    print(f"    {mark}  {label}{detail}")
    return ok


async def verify(name: str, url: str) -> bool:
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    backend = SQLAlchemyBackend(session_factory=factory, base=Base)

    print(f"\n  {name}  {DIM}{backend.dialect}{RESET}")
    print(
        f"    {DIM}ilike={backend.profile.has_ilike} "
        f"nulls_ordering={backend.profile.has_nulls_ordering} "
        f"returning={backend.profile.has_returning}{RESET}"
    )

    results: list[bool] = []
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)

        async with backend.unit_of_work() as uow:
            categories = backend.adapter(Category, uow, key="shop.category")
            phones = await categories.create({"name": "Phones"})

            products = backend.adapter(Product, uow, key="shop.product", search_fields=("name",))
            await products.create(
                {"name": "Pixel Phone", "price": Decimal("799.00"), "category": phones.id}
            )
            await products.create({"name": "pixel case", "price": Decimal("19.50")})
            await products.create(
                {
                    "name": "Laptop",
                    "price": Decimal("1200.00"),
                    "released_on": dt.date(2026, 1, 1),
                }
            )

        async with backend.unit_of_work() as uow:
            products = backend.adapter(
                Product,
                uow,
                key="shop.product",
                search_fields=("name",),
                select_related=("category",),
            )

            page = await products.list(ListQuery(page_size=10))
            results.append(check("three rows written and read back", page.total, 3))

            hits = await products.list(ListQuery(search="PIXEL", page_size=10))
            results.append(
                check(
                    "case-insensitive search",
                    sorted(p.name for p in hits.items),
                    ["Pixel Phone", "pixel case"],
                )
            )

            wild = await products.list(ListQuery(search="%", page_size=10))
            results.append(check("LIKE wildcard escaped", wild.total, 0))

            ordered = await products.list(
                ListQuery(ordering=(SortSpec("released_on"),), page_size=10)
            )
            results.append(
                check(
                    "NULLs sort last",
                    [p.name for p in ordered.items][-3:],
                    ["Laptop", "Pixel Phone", "pixel case"],
                )
            )

            money = await products.list(
                ListQuery(filters=(Filter("price", FilterOperator.EXACT, "19.50"),), page_size=10)
            )
            results.append(
                check("decimal precision preserved", [p.name for p in money.items], ["pixel case"])
            )

            related = await products.list(
                ListQuery(
                    filters=(Filter("category", FilterOperator.EXACT, str(phones.id)),),
                    page_size=10,
                )
            )
            results.append(check("filter through a relation", related.total, 1))

            guarded = await products.create({"name": "Guarded", "id": 999_999})
            results.append(check("generated key not mass-assignable", guarded.id != 999_999, True))

            secret = await products.create({"name": "Secretive", "api_secret": "s3cret"})
            results.append(
                check(
                    "sensitive value kept out of snapshots",
                    "api_secret" in products.snapshot(secret),
                    False,
                )
            )

            removed = await products.bulk_delete(ListQuery(search="pixel", page_size=10))
            results.append(check("bulk delete honours the search", removed, 2))

        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
    except Exception as exc:  # a connection failure is a result, not a crash
        print(f"    {RED}FAIL{RESET}  {type(exc).__name__}: {exc}")
        return False
    finally:
        await engine.dispose()

    return all(results)


async def main() -> int:
    requested = sys.argv[1:] or ["sqlite"]
    names = list(URLS) if "all" in requested else requested

    unknown = [name for name in names if name not in URLS]
    if unknown:
        print(f"Unknown database(s): {', '.join(unknown)}. Choose from: {', '.join(URLS)}, all")
        return 2

    print("FastFort database verification")
    with tempfile.TemporaryDirectory() as tmp:
        outcomes = {name: await verify(name, URLS[name].format(tmp=tmp)) for name in names}

    print()
    for name, ok in outcomes.items():
        print(f"  {GREEN + 'OK  ' + RESET if ok else RED + 'FAIL' + RESET}  {name}")
    return 0 if all(outcomes.values()) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
