"""The CRUD hooks, emitted from the admin's write paths.

`core/hooks.py` has declared `BEFORE_CREATE`, `AFTER_UPDATE` and the rest since
before 0.4.0, with documented kwargs and its own unit tests -- and nothing ever
called them. The mechanism was built and the call sites were missing, so a
project that registered a listener got silence.

What these assert is mostly *when*. `BEFORE_*` runs inside the transaction so a
listener can veto; `AFTER_*` runs once it has committed so a listener that
sends mail never fires for a change that rolled back. Getting that backwards is
the failure that matters, and it is invisible until something goes wrong.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ValidationError
from fastfort.core.hooks import Hook
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "stock", "is_active")
    list_editable = ("stock",)
    bulk_editable = ("stock",)


@pytest.fixture
async def fort(backend: SQLAlchemyBackend, staff_user: StaffUser) -> FastFort:
    built = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    built.set_user_model(StaffUser)
    built.register(Product, ProductAdmin, key="shop.product")
    built.register(Category, admin.ModelAdmin, key="shop.category")
    built.register(StaffUser, admin.ModelAdmin, key="accounts.user")
    return built


@pytest.fixture
async def client(fort: FastFort) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    fort.mount(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


@pytest.fixture
def heard(fort: FastFort) -> list[tuple[str, Any]]:
    """Every CRUD event, in the order it was emitted."""
    seen: list[tuple[str, Any]] = []
    for hook in (
        Hook.BEFORE_CREATE,
        Hook.AFTER_CREATE,
        Hook.BEFORE_UPDATE,
        Hook.AFTER_UPDATE,
        Hook.BEFORE_DELETE,
        Hook.AFTER_DELETE,
    ):

        def listen(name: str = hook.value, **kwargs: Any) -> None:
            seen.append((name, kwargs))

        fort.hooks.add(hook, listen)
    return seen


def names(heard: list[tuple[str, Any]]) -> list[str]:
    return [name for name, _ in heard]


async def submit(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    body = (await client.get(path if path.endswith("/") else "/admin/shop.product/")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match
    data.setdefault("_csrf", match.group(1))
    return await client.post(path, data=data, follow_redirects=True)


async def a_product(session_factory: async_sessionmaker[AsyncSession]) -> Product:
    async with session_factory() as session:
        return (await session.execute(sa.select(Product).order_by(Product.id))).scalars().first()


# ---------------------------------------------------------------------------
# The pairs fire, in order
# ---------------------------------------------------------------------------


async def test_creating_a_row_emits_the_create_pair(
    client: httpx.AsyncClient, heard: list[tuple[str, Any]]
) -> None:
    await submit(client, "/admin/shop.product/add", name="Hooked Product", price="9.99", stock="1")
    assert names(heard) == ["before_create", "after_create"]


async def test_editing_a_row_emits_the_update_pair(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    product = await a_product(session_factory)
    await submit(
        client,
        f"/admin/shop.product/{product.id}/",
        name=product.name,
        price=str(product.price),
        stock="5",
    )
    assert names(heard) == ["before_update", "after_update"]


async def test_deleting_a_row_emits_the_delete_pair(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    product = await a_product(session_factory)
    await submit(client, f"/admin/shop.product/{product.id}/delete")
    assert names(heard) == ["before_delete", "after_delete"]


# ---------------------------------------------------------------------------
# The kwargs match what `Hook` documents
# ---------------------------------------------------------------------------


async def test_the_update_pair_carries_the_changes(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """`changes` is documented for the update pair and for nothing else, so a
    listener written against the contract can take it by name."""
    product = await a_product(session_factory)
    await submit(
        client,
        f"/admin/shop.product/{product.id}/",
        name=product.name,
        price=str(product.price),
        stock="12",
    )

    for _, kwargs in heard:
        assert kwargs["model_key"] == "shop.product"
        assert kwargs["request"] is not None
        assert kwargs["changes"]["stock"] == 12


async def test_the_create_and_delete_pairs_carry_no_changes(
    client: httpx.AsyncClient, heard: list[tuple[str, Any]]
) -> None:
    await submit(client, "/admin/shop.product/add", name="No Changes", price="1.00", stock="1")
    for _, kwargs in heard:
        assert "changes" not in kwargs


async def test_after_create_carries_the_saved_instance(
    client: httpx.AsyncClient, heard: list[tuple[str, Any]]
) -> None:
    """`before_create` has no instance to carry -- there is not one yet, which
    is the whole point of a before-create -- so it carries the cleaned values.
    `after_create` carries the row."""
    await submit(client, "/admin/shop.product/add", name="Saved Row", price="2.00", stock="1")

    before = next(kwargs for name, kwargs in heard if name == "before_create")
    after = next(kwargs for name, kwargs in heard if name == "after_create")

    assert before["obj"]["name"] == "Saved Row"
    assert after["obj"].id is not None
    assert after["obj"].name == "Saved Row"


async def test_after_delete_can_still_read_the_row_it_lost(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The instance is detached from a row that no longer exists, but
    `expire_on_commit` is off, so everything it held is still readable -- which
    is exactly what a listener recording what was deleted needs."""
    product = await a_product(session_factory)
    expected = product.name

    await submit(client, f"/admin/shop.product/{product.id}/delete")

    after = next(kwargs for name, kwargs in heard if name == "after_delete")
    assert after["obj"].name == expected


# ---------------------------------------------------------------------------
# When they fire -- the half that matters
# ---------------------------------------------------------------------------


async def test_a_before_listener_that_raises_aborts_the_write(
    client: httpx.AsyncClient, fort: FastFort, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`BEFORE_*` runs inside the transaction, which is what makes a veto
    possible at all."""

    def refuse(**kwargs: Any) -> None:
        raise ValidationError("Not allowed by policy.")

    fort.hooks.add(Hook.BEFORE_CREATE, refuse)

    await submit(client, "/admin/shop.product/add", name="Vetoed", price="1.00", stock="1")

    async with session_factory() as session:
        found = await session.execute(sa.select(Product).where(Product.name == "Vetoed"))
        assert found.first() is None


async def test_after_create_fires_only_once_the_row_is_durable(
    client: httpx.AsyncClient, fort: FastFort, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The listener reads the database on its own connection. Finding the row
    there proves the transaction had already committed -- an `AFTER_*` emitted
    before the commit would see nothing, and would have announced a change that
    could still have rolled back.
    """
    visible: list[bool] = []

    async def look(*, obj: Any, **kwargs: Any) -> None:
        async with session_factory() as session:
            found = await session.execute(sa.select(Product).where(Product.name == "Durable"))
            visible.append(found.first() is not None)

    fort.hooks.add(Hook.AFTER_CREATE, look)

    await submit(client, "/admin/shop.product/add", name="Durable", price="1.00", stock="1")
    assert visible == [True]


async def test_a_write_that_fails_emits_no_after(
    client: httpx.AsyncClient, fort: FastFort, heard: list[tuple[str, Any]]
) -> None:
    """A form that does not validate never reaches the write, so neither half
    fires -- the hooks describe writes, not attempts."""
    await submit(client, "/admin/shop.product/add", name="", price="nonsense", stock="x")
    assert names(heard) == []


# ---------------------------------------------------------------------------
# The bulk paths
# ---------------------------------------------------------------------------


async def test_a_bulk_delete_emits_one_pair_per_row(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        ids = [
            p.id
            for p in (await session.execute(sa.select(Product).order_by(Product.id)))
            .scalars()
            .all()
        ][:2]

    await submit(client, "/admin/shop.product/action", action="delete", keys=[str(i) for i in ids])

    assert names(heard) == [
        "before_delete",
        "before_delete",
        "after_delete",
        "after_delete",
    ]


async def test_editing_cells_in_place_emits_one_pair_per_row(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        ids = [
            p.id
            for p in (await session.execute(sa.select(Product).order_by(Product.id)))
            .scalars()
            .all()
        ][:2]

    await submit(
        client, "/admin/shop.product/list-edit", **{f"{ids[0]}-stock": "3", f"{ids[1]}-stock": "4"}
    )

    assert names(heard).count("before_update") == 2
    assert names(heard).count("after_update") == 2


async def test_a_bulk_edit_emits_one_pair_per_row(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        ids = [
            p.id
            for p in (await session.execute(sa.select(Product).order_by(Product.id)))
            .scalars()
            .all()
        ][:2]

    await submit(
        client,
        "/admin/shop.product/bulk-edit",
        field="stock",
        value="8",
        keys=[str(i) for i in ids],
    )

    assert names(heard).count("before_update") == 2
    assert names(heard).count("after_update") == 2


async def test_every_before_precedes_every_after_in_a_batch(
    client: httpx.AsyncClient,
    heard: list[tuple[str, Any]],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The batch commits once, so every `AFTER_*` in it has to come after every
    `BEFORE_*` -- one interleaved pair would mean a row was announced durable
    while a later row in the same transaction could still roll it back.
    """
    async with session_factory() as session:
        ids = [
            p.id
            for p in (await session.execute(sa.select(Product).order_by(Product.id)))
            .scalars()
            .all()
        ][:3]

    await submit(
        client,
        "/admin/shop.product/bulk-edit",
        field="stock",
        value="6",
        keys=[str(i) for i in ids],
    )

    ordered = names(heard)
    assert ordered == ["before_update"] * 3 + ["after_update"] * 3
