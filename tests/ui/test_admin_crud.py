"""Creating, changing and deleting through the rendered admin.

The write path is where an admin panel gets breached, so the security-shaped
tests here are as important as the ones that check a row was saved.
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
from fastfort.admin.decorators import take_pending
from fastfort.core.exceptions import RegistrationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "category", "price", "stock", "is_active")
    search_fields = ("name",)
    ordering = ("-id",)
    select_related = ("category",)
    readonly_fields = ("created_at",)
    verbose_name_plural = "Products"


class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "is_staff")
    search_fields = ("email",)
    # `hashed_password` is deliberately not declared anywhere: it is detected as a
    # password column from the spec, which is what a project should be able to
    # rely on without saying anything.
    verbose_name_plural = "Users"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, admin.ModelAdmin, key="shop.category")
    fort.register(StaffUser, UserAdmin, key="accounts.user")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def token(client: httpx.AsyncClient, path: str) -> str:
    body = (await client.get(path)).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, f"{path} must render a CSRF token"
    return match.group(1)


async def submit(client: httpx.AsyncClient, path: str, **data: Any) -> httpx.Response:
    data.setdefault("_csrf", await token(client, path))
    return await client.post(path, data=data, follow_redirects=True)


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------


def test_register_buffers_until_an_application_takes_it() -> None:
    """A decorator runs at import time, before any application need exist."""
    take_pending()  # start from a clean buffer

    @admin.register(Category, key="demo.category")
    class DemoAdmin(admin.ModelAdmin):
        list_display = ("id", "name")

    pending = take_pending()
    assert len(pending) == 1
    assert pending[0].model is Category
    assert pending[0].admin is DemoAdmin
    assert pending[0].key == "demo.category"
    # The class is returned unchanged, so it stays usable and testable on its own.
    assert DemoAdmin.list_display == ("id", "name")


def test_register_rejects_a_model_instance() -> None:
    with pytest.raises(RegistrationError, match="expects a model class"):
        admin.register(Category())  # type: ignore[arg-type]


def test_register_rejects_a_class_that_is_not_a_model_admin() -> None:
    take_pending()
    with pytest.raises(RegistrationError, match="ModelAdmin subclass"):

        @admin.register(Category)
        class NotAnAdmin:  # type: ignore[type-var]
            pass

    take_pending()


# ---------------------------------------------------------------------------
# The list view's new affordances
# ---------------------------------------------------------------------------


async def test_the_list_offers_a_way_to_add(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text
    assert 'href="/admin/shop.product/add"' in body


async def test_each_row_links_to_itself(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text
    assert re.search(r'href="/admin/shop\.product/\d+/"', body)
    assert re.search(r'href="/admin/shop\.product/\d+/delete"', body)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


async def test_the_add_form_renders_a_control_per_field(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/add")).text

    assert 'id="ff-name"' in body
    assert 'type="checkbox"' in body  # is_active
    assert 'name="category"' in body  # relation dropdown
    assert "Phones" in body  # populated from the database
    assert "ff-readonly" in body  # created_at


async def test_creating_a_row_saves_it_and_says_so(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await submit(
        client, "/admin/shop.product/add", name="Test Widget", price="49.90", stock="7"
    )

    assert response.status_code == 200
    assert "was created" in response.text
    async with session_factory() as session:
        found = (
            await session.execute(sa.select(Product).where(Product.name == "Test Widget"))
        ).scalar_one()
        assert str(found.price) == "49.90"


async def test_a_missing_required_field_is_reported_on_the_field(
    client: httpx.AsyncClient,
) -> None:
    response = await submit(client, "/admin/shop.product/add", name="", price="1.00")

    assert "This field is required." in response.text
    assert 'data-invalid="true"' in response.text


async def test_an_unparseable_value_says_what_was_expected(client: httpx.AsyncClient) -> None:
    """ "Invalid value" tells the person filling the form nothing they can act on."""
    response = await submit(client, "/admin/shop.product/add", name="X", price="abc")

    assert "Enter a number, for example 1234.56." in response.text


async def test_every_problem_is_shown_at_once(client: httpx.AsyncClient) -> None:
    response = await submit(client, "/admin/shop.product/add", name="", price="abc")

    assert "This field is required." in response.text
    assert "Enter a number" in response.text


async def test_a_rejected_form_keeps_what_was_typed(client: httpx.AsyncClient) -> None:
    """Retyping a long form because one field was wrong is the classic annoyance."""
    response = await submit(client, "/admin/shop.product/add", name="Keep This Name", price="abc")
    assert 'value="Keep This Name"' in response.text


async def test_a_failed_create_writes_nothing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await submit(client, "/admin/shop.product/add", name="Half Saved", price="abc")

    async with session_factory() as session:
        rows = (
            (await session.execute(sa.select(Product).where(Product.name == "Half Saved")))
            .scalars()
            .all()
        )
        assert rows == []


async def test_a_relation_can_be_chosen_from_the_dropdown(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async with session_factory() as session:
        phones = (
            await session.execute(sa.select(Category).where(Category.name == "Phones"))
        ).scalar_one()

    await submit(
        client,
        "/admin/shop.product/add",
        name="Related Widget",
        price="1.00",
        category=str(phones.id),
    )

    async with session_factory() as session:
        found = (
            await session.execute(sa.select(Product).where(Product.name == "Related Widget"))
        ).scalar_one()
        assert found.category_id == phones.id


# ---------------------------------------------------------------------------
# Change
# ---------------------------------------------------------------------------


async def test_the_change_form_is_populated(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/")).text
    assert 'value="Pixel Phone"' in body


async def test_saving_a_change_persists_it(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await submit(
        client, "/admin/shop.product/1/", name="Renamed", price="51.00", stock="7"
    )

    assert "was saved" in response.text
    async with session_factory() as session:
        assert (await session.get(Product, 1)).name == "Renamed"  # type: ignore[union-attr]


async def test_an_unchecked_checkbox_means_false(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A browser submits nothing for an unchecked box, which is easy to read as
    "unchanged" and thereby make a boolean impossible to switch off."""
    await submit(client, "/admin/shop.product/1/", name="Pixel Phone", price="799.00")

    async with session_factory() as session:
        assert (await session.get(Product, 1)).is_active is False  # type: ignore[union-attr]


async def test_editing_a_row_with_a_relation_does_not_fail(
    client: httpx.AsyncClient,
) -> None:
    """Reading a lazily loaded relation raises MissingGreenlet under asyncio, so
    the form's relations have to be eagerly loaded whether or not the admin
    declared select_related."""
    assert (await client.get("/admin/shop.category/1/")).status_code == 200
    assert (await client.get("/admin/shop.product/1/")).status_code == 200


async def test_an_invalid_edit_re_renders_instead_of_crashing(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A rejected submission on an *existing* row used to be a 500.

    `change_submit` rolled the transaction back and only afterward built the
    form's own action URL from the instance's primary key. A rollback expires
    every attribute on every object still in the session, `instance` included,
    so that read was a synchronous refresh attempt against an async session
    outside the greenlet bridge that makes those work -- `MissingGreenlet`,
    on every edit that failed validation, not on a fraction of them.
    """
    response = await submit(client, "/admin/shop.product/1/", name="")

    assert response.status_code == 200
    assert "This field is required." in response.text

    # And the stored row is untouched -- a rejected submission must not clear
    # it, whatever the re-rendered form shows back for the field being fixed.
    async with session_factory() as session:
        assert (await session.get(Product, 1)).name == "Pixel Phone"  # type: ignore[union-attr]


async def test_a_missing_row_is_a_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/shop.product/999999/")).status_code == 404
    assert (await client.get("/admin/shop.product/not-a-number/")).status_code == 404


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_deleting_asks_first(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/delete")).text

    assert "cannot be undone" in body
    assert "Pixel Phone" in body
    assert 'href="/admin/shop.product/1/"' in body  # a way back


async def test_deleting_removes_the_row(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await submit(client, "/admin/shop.product/1/delete")

    assert "was deleted" in response.text
    async with session_factory() as session:
        assert await session.get(Product, 1) is None


async def test_a_get_does_not_delete(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Otherwise a crawler, or any image tag, can empty a table."""
    await client.get("/admin/shop.product/1/delete")

    async with session_factory() as session:
        assert await session.get(Product, 1) is not None


# ---------------------------------------------------------------------------
# Security of the write path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/admin/shop.product/add", "/admin/shop.product/1/", "/admin/shop.product/1/delete"]
)
async def test_a_write_without_a_valid_token_is_refused(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession], path: str
) -> None:
    response = await client.post(
        path, data={"_csrf": "forged", "name": "Forged", "price": "1.00"}, follow_redirects=True
    )

    assert "security token" in response.text
    async with session_factory() as session:
        assert (
            await session.execute(sa.select(Product).where(Product.name == "Forged"))
        ).scalars().all() == []
        # And nothing was deleted either.
        assert await session.get(Product, 1) is not None


async def test_a_read_only_field_cannot_be_written(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`created_at` has no control, so a value for it was added by hand."""
    async with session_factory() as session:
        before = (await session.get(Product, 1)).created_at  # type: ignore[union-attr]

    await submit(
        client,
        "/admin/shop.product/1/",
        name="Pixel Phone",
        price="799.00",
        created_at="1999-01-01T00:00",
    )

    async with session_factory() as session:
        assert (await session.get(Product, 1)).created_at == before  # type: ignore[union-attr]


async def test_a_field_the_form_never_rendered_cannot_be_written(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The spec's allow-list is the only gate, whatever the request contained."""
    await submit(client, "/admin/shop.product/1/", name="Pixel Phone", price="799.00", id="4242")

    async with session_factory() as session:
        assert await session.get(Product, 4242) is None
        assert await session.get(Product, 1) is not None


async def test_a_sensitive_value_is_never_rendered(client: httpx.AsyncClient) -> None:
    """A password hash echoed into a page ends up in a cache or a screenshot."""
    body = (await client.get("/admin/accounts.user/1/")).text

    assert "$argon2id$" not in body
    # The control is rendered, and it is a password box rather than a text one.
    assert 'name="hashed_password"' in body
    assert 'type="password"' in body


async def test_a_hostile_value_is_escaped_into_the_form(client: httpx.AsyncClient) -> None:
    payload = '"><script>alert(1)</script>'
    response = await submit(client, "/admin/shop.product/add", name=payload, price="abc")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


async def test_a_banner_is_shown_exactly_once(client: httpx.AsyncClient) -> None:
    """One that survives a refresh makes people wonder whether they saved twice."""
    first = await submit(client, "/admin/shop.product/add", name="Once", price="1.00")
    assert "was created" in first.text

    again = await client.get("/admin/shop.product/")
    assert "was created" not in again.text


async def test_a_forged_message_cookie_is_ignored(client: httpx.AsyncClient) -> None:
    """Rendering attacker-chosen text inside our own chrome is a phishing surface."""
    client.cookies.set("fastfort_session_messages", "not-a-signed-value")

    body = (await client.get("/admin/shop.product/")).text
    assert "ff-messages" not in body


# ---------------------------------------------------------------------------
# Password fields
# ---------------------------------------------------------------------------


async def test_a_password_column_renders_a_password_control(client: httpx.AsyncClient) -> None:
    """A text box expecting a pasted Argon2 hash is not a usable control.

    Nothing declares `hashed_password` as a password field; it is detected from
    the spec, because the adapter marked it sensitive and the name says so.
    """
    body = (await client.get("/admin/accounts.user/add")).text

    assert 'name="hashed_password"' in body
    assert 'name="hashed_password__confirm"' in body
    assert 'type="password"' in body


async def test_a_new_account_needs_a_password(client: httpx.AsyncClient) -> None:
    response = await submit(client, "/admin/accounts.user/add", email="a@example.com")
    assert "Set a password for the new account." in response.text


async def test_a_weak_password_is_refused(client: httpx.AsyncClient) -> None:
    response = await submit(
        client,
        "/admin/accounts.user/add",
        email="a@example.com",
        hashed_password="short",
        hashed_password__confirm="short",
    )
    assert "at least 10 characters" in response.text


async def test_a_mismatched_confirmation_is_refused(client: httpx.AsyncClient) -> None:
    response = await submit(
        client,
        "/admin/accounts.user/add",
        email="a@example.com",
        hashed_password="a-good-passphrase-2026",
        hashed_password__confirm="something-else-entirely",
    )
    assert "The two passwords do not match." in response.text


async def test_a_password_is_stored_hashed_and_works(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Hashing happens inside the form, so no view can store plaintext by
    forgetting to call something."""
    from fastfort.auth import verify_password

    response = await submit(
        client,
        "/admin/accounts.user/add",
        email="fresh@example.com",
        hashed_password="a-good-passphrase-2026",
        hashed_password__confirm="a-good-passphrase-2026",
        is_active="1",
        is_staff="1",
    )
    assert "was created" in response.text
    assert "a-good-passphrase-2026" not in response.text

    async with session_factory() as session:
        created = (
            await session.execute(
                sa.select(StaffUser).where(StaffUser.email == "fresh@example.com")
            )
        ).scalar_one()

    assert created.hashed_password.startswith("$argon2id$")
    assert verify_password("a-good-passphrase-2026", created.hashed_password)


async def test_leaving_a_password_blank_keeps_the_current_one(
    client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Otherwise editing an unrelated field would clear the password, or force
    the person to retype it."""
    async with session_factory() as session:
        before = (
            (await session.execute(sa.select(StaffUser).where(StaffUser.id == 1)))
            .scalar_one()
            .hashed_password
        )

    await submit(client, "/admin/accounts.user/1/", email="admin@example.com", is_staff="1")

    async with session_factory() as session:
        after = (
            (await session.execute(sa.select(StaffUser).where(StaffUser.id == 1)))
            .scalar_one()
            .hashed_password
        )

    assert after == before


async def test_a_password_column_is_never_prefilled(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/accounts.user/1/")).text

    assert "$argon2id$" not in body
    # The control is present but empty, with an explanation of what blank means.
    assert "Leave blank to keep the current password." in body
