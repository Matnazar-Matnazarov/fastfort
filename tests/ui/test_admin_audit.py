"""The audit log, driven through the real admin.

Signed in through the real form, so the gate and CSRF are exercised. What
matters: an entry per write, a diff of what actually changed rather than of
everything on the form, no sensitive value ever stored, and a failed write to
the log that cannot turn a successful save into an error.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import AuditEntry, Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


def _fort(backend: SQLAlchemyBackend) -> FastFort:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, admin.ModelAdmin, key="shop.product")
    fort.register(Category, admin.ModelAdmin, key="shop.category")
    return fort


@asynccontextmanager
async def _client(fort: FastFort) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    fort.mount(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = _fort(backend)
    fort.enable_audit_log(AuditEntry)
    async with _client(fort) as opened:
        yield opened


@pytest.fixture
async def off_client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with _client(_fort(backend)) as opened:
        yield opened


async def submit(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    body = (await client.get(path)).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, f"{path} must render a CSRF token"
    data.setdefault("_csrf", match.group(1))
    return await client.post(path, data=data, follow_redirects=True)


async def entries(session_factory: async_sessionmaker[AsyncSession]) -> list[AuditEntry]:
    async with session_factory() as session:
        found = await session.scalars(sa.select(AuditEntry).order_by(AuditEntry.id))
        return list(found)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_table_without_the_columns_is_refused_at_start_up(backend: SQLAlchemyBackend) -> None:
    with pytest.raises(ConfigurationError, match="audit log"):
        _fort(backend).enable_audit_log(Category)


async def test_nothing_is_drawn_until_it_is_enabled(off_client: httpx.AsyncClient) -> None:
    body = (await off_client.get("/admin/shop.product/1/")).text

    assert "/history" not in body
    assert (await off_client.get("/admin/activity")).status_code == 404
    assert (await off_client.get("/admin/shop.product/1/history")).status_code == 404


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


async def test_a_change_records_only_the_fields_that_changed(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The form posts every field; the log must not claim all of them changed."""
    async with session_factory() as session:
        product = await session.get(Product, 1)
        assert product is not None
        price, stock = str(product.price), str(product.stock)

    response = await submit(
        client,
        "/admin/shop.product/1/",
        name="Renamed",
        price=price,
        stock=stock,
        is_active="on",
    )
    assert "was saved" in response.text

    [entry] = await entries(session_factory)
    assert entry.action == "update"
    assert entry.model_key == "shop.product"
    assert entry.object_key == "1"
    assert entry.user_label
    changes = json.loads(entry.changes)
    assert changes["name"] == ["Pixel Phone", "Renamed"]
    assert "price" not in changes
    assert "stock" not in changes


async def test_a_save_that_changes_nothing_writes_nothing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        product = await session.get(Product, 1)
        assert product is not None
        values = {
            "name": product.name,
            "price": str(product.price),
            "stock": str(product.stock),
            "status": product.status.value,
        }
        if product.is_active:
            values["is_active"] = "on"
        if product.category_id is not None:
            values["category"] = str(product.category_id)

    await submit(client, "/admin/shop.product/1/", **values)

    assert await entries(session_factory) == []


async def test_create_and_delete_are_recorded_with_their_values(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await submit(client, "/admin/shop.product/add", name="Logged", price="3.00", stock="1")
    async with session_factory() as session:
        created = await session.scalar(sa.select(Product).where(Product.name == "Logged"))
        assert created is not None

    await submit(client, f"/admin/shop.product/{created.id}/delete")

    first, last = await entries(session_factory)
    assert first.action == "create"
    assert json.loads(first.changes)["name"] == [None, "Logged"]
    assert last.action == "delete"
    assert last.object_label == first.object_label
    assert json.loads(last.changes)["name"] == ["Logged", None]


async def test_a_sensitive_column_never_reaches_the_log(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await submit(
        client, "/admin/shop.product/add", name="Keyed", price="1.00", api_secret="s3cret-value"
    )

    [entry] = await entries(session_factory)
    assert "api_secret" not in json.loads(entry.changes)
    assert "s3cret-value" not in entry.changes


async def test_a_failed_log_write_does_not_fail_the_save(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The change has committed by then. An error page would invite pressing
    Save again, which is a second change."""
    fort = _fort(backend)
    audit = fort.enable_audit_log(AuditEntry)

    async def broken(data: dict[str, Any]) -> None:
        raise RuntimeError("the audit table is gone")

    monkeypatch.setattr(audit, "_store", broken)
    async with _client(fort) as opened:
        response = await submit(opened, "/admin/shop.product/1/", name="Still saved", price="1.00")

    assert response.status_code == 200
    assert "was saved" in response.text
    async with session_factory() as session:
        assert (await session.get(Product, 1)).name == "Still saved"  # type: ignore[union-attr]
    assert "Could not write an audit entry" in caplog.text


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


async def test_the_change_form_links_to_its_history(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/")).text
    assert 'href="/admin/shop.product/1/history"' in body


async def test_a_rows_history_shows_before_and_after(client: httpx.AsyncClient) -> None:
    await submit(client, "/admin/shop.product/1/", name="Renamed Phone", price="1.00")

    body = (await client.get("/admin/shop.product/1/history")).text

    assert "Pixel Phone" in body
    assert "Renamed Phone" in body
    assert "ff-badge--info" in body


async def test_the_history_of_a_row_that_does_not_exist_is_404(client: httpx.AsyncClient) -> None:
    """Not an empty page: that would confirm to a guesser which keys once existed."""
    assert (await client.get("/admin/shop.product/99999/history")).status_code == 404


async def test_the_activity_page_lists_every_model_and_links_back(
    client: httpx.AsyncClient,
) -> None:
    await submit(client, "/admin/shop.product/1/", name="Seen", price="1.00")

    body = (await client.get("/admin/activity")).text

    assert 'href="/admin/activity"' in body, "the sidebar should link to it"
    assert 'href="/admin/shop.product/1/"' in body
    assert "Seen" in body


async def test_the_activity_page_ignores_a_model_it_does_not_know(
    client: httpx.AsyncClient,
) -> None:
    await submit(client, "/admin/shop.product/1/", name="Kept", price="1.00")

    body = (await client.get("/admin/activity?model=nope.nothing&p=-3")).text

    assert "Kept" in body


async def test_the_activity_page_says_so_when_empty(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/activity")).text
    assert "Nothing recorded yet" in body


async def test_the_feed_pages_newest_first(
    backend: SQLAlchemyBackend, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import datetime as dt

    audit = _fort(backend).enable_audit_log(AuditEntry)
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    async with session_factory() as session:
        session.add_all(
            AuditEntry(
                at=start + dt.timedelta(minutes=n),
                action="update",
                model_key="shop.product",
                object_key=str(n),
                object_label=f"row {n}",
            )
            for n in range(5)
        )
        await session.commit()

    first, more = await audit.recent(page=1, page_size=2)
    last, after_last = await audit.recent(page=3, page_size=2)

    assert [event.object_label for event in first] == ["row 4", "row 3"]
    assert more
    assert [event.object_label for event in last] == ["row 0"]
    assert not after_last
