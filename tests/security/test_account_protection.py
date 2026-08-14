"""What a deployment will let the admin do to an account.

The case these exist for is a public demo: everything visible, nothing that can
be taken apart. The administrator visitors are signed in as has to survive the
visit, and so do the accounts they came to look at.

Every default is what the admin has always done, so the first test here is that
a project which sets none of them notices nothing. After that, each setting is
checked at both ends -- the control the page renders *and* the write a hand-made
request could still attempt -- because a read-only box that accepts a posted
value is not a protection, it is a label.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.auth import hash_password, verify_password
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

USERS = "/admin/accounts.user"


class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email")
    verbose_name_plural = "Users"
    password_fields = ("hashed_password",)


def build(backend: SQLAlchemyBackend, **auth: Any) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            auth=auth,  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(StaffUser, UserAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def other(session_factory: async_sessionmaker[AsyncSession]) -> StaffUser:
    """A second superuser: the account the signed-in one might try to remove."""
    async with session_factory() as session:
        user = StaffUser(
            email="second@example.com",
            hashed_password=hash_password("another-correct-horse"),
            is_staff=True,
            is_superuser=True,
        )
        session.add(user)
        await session.commit()
        return user


@pytest.fixture
async def ordinary(session_factory: async_sessionmaker[AsyncSession]) -> StaffUser:
    async with session_factory() as session:
        user = StaffUser(
            email="staff@example.com",
            hashed_password=hash_password("a-third-correct-horse"),
            is_staff=True,
        )
        session.add(user)
        await session.commit()
        return user


@asynccontextmanager
async def signed_in(backend: SQLAlchemyBackend, **auth: Any) -> AsyncIterator[httpx.AsyncClient]:
    """An admin client signed in through the real form, on an app configured
    with `auth` settings."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, **auth)), base_url="http://testserver"
    ) as client:
        await sign_in(client)
        yield client


async def delete(client: httpx.AsyncClient, user: StaffUser) -> httpx.Response:
    body = (await client.get(f"{USERS}/{user.id}/")).text
    token = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert token, body[:400]
    return await client.post(f"{USERS}/{user.id}/delete", data={"_csrf": token.group(1)})


async def set_password(client: httpx.AsyncClient, user: StaffUser, password: str) -> httpx.Response:
    body = (await client.get(f"{USERS}/{user.id}/")).text
    token = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert token, body[:400]
    return await client.post(
        f"{USERS}/{user.id}/",
        data={
            "email": user.email,
            "hashed_password": password,
            "hashed_password__confirm": password,
            "_csrf": token.group(1),
        },
    )


async def reread(factory: async_sessionmaker[AsyncSession], user_id: int) -> StaffUser | None:
    async with factory() as session:
        return await session.get(StaffUser, user_id)


# ---------------------------------------------------------------------------
# The defaults
# ---------------------------------------------------------------------------


async def test_by_default_an_account_can_still_be_deleted(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every one of these settings defaults to what the admin has always done."""
    async with signed_in(backend) as client:
        await delete(client, ordinary)

    assert await reread(session_factory, ordinary.id) is None


async def test_by_default_a_password_can_still_be_changed(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with signed_in(backend) as client:
        await set_password(client, ordinary, "a-brand-new-correct-horse")

    changed = await reread(session_factory, ordinary.id)
    assert changed is not None
    assert verify_password("a-brand-new-correct-horse", changed.hashed_password)


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


async def test_deletion_can_be_switched_off_for_every_account(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with signed_in(backend, allow_user_delete=False) as client:
        response = await delete(client, ordinary)

    assert response.status_code == 303
    assert await reread(session_factory, ordinary.id) is not None


async def test_superusers_alone_can_be_protected(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    other: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The setting a demo usually wants: visitors may tidy up the accounts they
    made, and the administrator the demo signs them in as stays where it is."""
    async with signed_in(backend, allow_superuser_delete=False) as client:
        await delete(client, other)
        await delete(client, ordinary)

    assert await reread(session_factory, other.id) is not None
    assert await reread(session_factory, ordinary.id) is None


async def test_the_confirmation_page_refuses_before_it_offers_a_button(
    backend: SQLAlchemyBackend, staff_user: StaffUser, other: StaffUser
) -> None:
    """Offering the button and refusing the press is worse than saying so on the
    way in."""
    async with signed_in(backend, allow_superuser_delete=False) as client:
        response = await client.get(f"{USERS}/{other.id}/delete", follow_redirects=False)

    assert response.status_code == 303


async def test_a_bulk_delete_keeps_the_protected_rows_and_removes_the_rest(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    other: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Someone who ticked forty rows and one protected account meant to delete
    the forty."""
    async with signed_in(backend, allow_superuser_delete=False) as client:
        body = (await client.get(f"{USERS}/")).text
        token = re.search(r'name="_csrf" value="([^"]+)"', body)
        assert token
        await client.post(
            f"{USERS}/action",
            data={
                "action": "delete",
                "keys": [str(other.id), str(ordinary.id)],
                "_csrf": token.group(1),
            },
        )

    assert await reread(session_factory, other.id) is not None
    assert await reread(session_factory, ordinary.id) is None


async def test_a_bulk_delete_of_nothing_but_protected_rows_says_so(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    other: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with signed_in(backend, allow_superuser_delete=False) as client:
        body = (await client.get(f"{USERS}/")).text
        token = re.search(r'name="_csrf" value="([^"]+)"', body)
        assert token
        response = await client.post(
            f"{USERS}/action",
            data={"action": "delete", "keys": [str(other.id)], "_csrf": token.group(1)},
        )

    assert response.status_code == 303
    assert await reread(session_factory, other.id) is not None


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


async def test_a_protected_password_is_not_written_even_when_it_is_posted(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The half that matters: a read-only control that still accepts a posted
    value is a label, not a protection."""
    async with signed_in(backend, allow_password_change=False) as client:
        await set_password(client, ordinary, "a-brand-new-correct-horse")

    unchanged = await reread(session_factory, ordinary.id)
    assert unchanged is not None
    assert verify_password("a-third-correct-horse", unchanged.hashed_password)


async def test_a_protected_password_field_is_rendered_read_only(
    backend: SQLAlchemyBackend, staff_user: StaffUser, ordinary: StaffUser
) -> None:
    async with signed_in(backend, allow_password_change=False) as client:
        body = (await client.get(f"{USERS}/{ordinary.id}/")).text

    assert 'type="password"' not in body


async def test_another_superusers_password_can_be_protected_on_its_own(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    other: StaffUser,
    ordinary: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with signed_in(backend, allow_superuser_password_change=False) as client:
        await set_password(client, other, "a-brand-new-correct-horse")
        await set_password(client, ordinary, "a-brand-new-correct-horse")

    protected = await reread(session_factory, other.id)
    assert protected is not None
    assert verify_password("another-correct-horse", protected.hashed_password)

    changed = await reread(session_factory, ordinary.id)
    assert changed is not None
    assert verify_password("a-brand-new-correct-horse", changed.hashed_password)


async def test_a_superuser_can_still_change_their_own_password(
    backend: SQLAlchemyBackend,
    staff_user: StaffUser,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Locking people out of their own password is a different feature, and not
    one anybody asked for."""
    async with signed_in(backend, allow_superuser_password_change=False) as client:
        await set_password(client, staff_user, "my-own-brand-new-horse")

    changed = await reread(session_factory, staff_user.id)
    assert changed is not None
    assert verify_password("my-own-brand-new-horse", changed.hashed_password)


async def test_the_protection_applies_to_the_user_model_and_nothing_else(
    backend: SQLAlchemyBackend, staff_user: StaffUser, seeded: None
) -> None:
    """Every other model behaves exactly as it did. The setting is about
    accounts, and a product is not one."""
    from tests.orm.models import Product

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            auth={"allow_user_delete": False},  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)

    class ProductAdmin(admin.ModelAdmin):
        list_display = ("id", "name")

    fort.register(Product, ProductAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await sign_in(client)
        response = await client.get("/admin/shop.product/1/delete")

    assert response.status_code == 200
    assert "Delete" in response.text
