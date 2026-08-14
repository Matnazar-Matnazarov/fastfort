"""A record of who signed in, from where, and on what.

The two things worth guarding are at opposite ends. A record has to be written
for the attempts that matter -- including the failures, because a log of
successes says nothing about the night somebody tried four hundred passwords.
And writing one must never be able to fail a sign-in: an audit table that is
missing or full must not lock out the people who could fix it.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import ADMIN_EMAIL, sign_in
from tests.orm.models import Product, SignInRecord, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


def build(backend: SQLAlchemyBackend, **kwargs: Any) -> tuple[FastAPI, FastFort]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.record_sign_ins(kwargs.pop("model", SignInRecord), **kwargs)

    app = FastAPI()
    fort.mount(app)
    return app, fort


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    app, _ = build(backend)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"user-agent": CHROME},
    ) as opened:
        yield opened


async def records(factory: async_sessionmaker[AsyncSession]) -> list[SignInRecord]:
    async with factory() as session:
        found = await session.execute(sa.select(SignInRecord).order_by(SignInRecord.id))
        return list(found.scalars())


async def attempt(client: httpx.AsyncClient, *, password: str) -> httpx.Response:
    import re

    body = (await client.get("/admin/login")).text
    token = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert token
    return await client.post(
        "/admin/login",
        data={
            "identity": ADMIN_EMAIL,
            "password": password,
            "_csrf": token.group(1),
            "next": "/admin/",
        },
    )


# ---------------------------------------------------------------------------
# What is written
# ---------------------------------------------------------------------------


async def test_a_sign_in_is_recorded_with_the_device_it_came_from(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await sign_in(client)

    written = await records(session_factory)
    assert len(written) == 1
    assert written[0].successful is True
    assert written[0].identity == ADMIN_EMAIL
    assert written[0].browser == "Chrome 138"
    assert written[0].platform == "Windows"
    assert written[0].kind == "desktop"
    assert written[0].user_agent == CHROME


async def test_the_account_is_stored_as_text_rather_than_a_foreign_key(
    client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    staff_user: StaffUser,
) -> None:
    """An audit row that cascades away with the account is not an audit row, and
    "who deleted this account" is what these exist to answer."""
    await sign_in(client)

    written = await records(session_factory)
    assert written[0].user_key == str(staff_user.id)
    assert not SignInRecord.__table__.foreign_keys


async def test_a_refusal_is_recorded_too(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A log of successes says nothing about the night somebody tried four
    hundred passwords."""
    await attempt(client, password="not-the-password")

    written = await records(session_factory)
    assert len(written) == 1
    assert written[0].successful is False
    # Stored as typed. It is not proof of an account -- which is why this column
    # is worth reviewing rather than trusting.
    assert written[0].identity == ADMIN_EMAIL
    assert written[0].user_key == ""


async def test_failures_can_be_left_out(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app, _ = build(backend, failures=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await attempt(client, password="not-the-password")
        await sign_in(client)

    written = await records(session_factory)
    assert [record.successful for record in written] == [True]


async def test_a_project_can_supply_where_an_address_is(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FastFort ships no GeoIP database and calls no service: one would go stale
    inside a wheel, and the other would hand every administrator's address to a
    third party from inside the login handler."""
    app, _ = build(backend, locate=lambda _: "Tashkent, UZ")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await sign_in(client)

    written = await records(session_factory)
    assert written[0].location == "Tashkent, UZ"


# ---------------------------------------------------------------------------
# What must never happen
# ---------------------------------------------------------------------------


async def test_a_failing_record_cannot_fail_the_sign_in(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A missing or full audit table must not be able to keep the people who
    could fix it out of the admin."""

    app, fort = build(backend)
    # Standing in for the table that was never migrated: the recorder is pointed
    # at a class the backend cannot write.
    fort._sign_in_recorder.model = _NotATable

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with caplog.at_level(logging.ERROR, logger="fastfort.auth"):
            await sign_in(client)

    assert "Could not record a sign-in" in caplog.text


async def test_a_locate_callback_that_raises_costs_the_place_and_nothing_else(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    def explode(address: str) -> str:
        raise RuntimeError("the geoip database is not loaded")

    app, _ = build(backend, locate=explode)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        with caplog.at_level(logging.ERROR, logger="fastfort.auth"):
            await sign_in(client)

    written = await records(session_factory)
    assert len(written) == 1
    assert written[0].location == ""


def test_a_model_that_cannot_hold_a_record_is_refused_at_configuration_time(
    backend: SQLAlchemyBackend,
) -> None:
    """At start-up, while somebody is looking at the file that declares it --
    not on the first sign-in in the middle of the night."""
    fort = FastFort(FastFortSettings(secret_key=SECRET), backend=backend)  # type: ignore[call-arg]
    fort.set_user_model(StaffUser)

    with pytest.raises(ConfigurationError, match="cannot record sign-ins"):
        fort.record_sign_ins(Product)


class _NotATable:
    """A class the backend cannot write, standing in for a missing table."""
