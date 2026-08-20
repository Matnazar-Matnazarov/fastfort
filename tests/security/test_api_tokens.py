"""Personal access tokens: long-lived credentials for things that are not browsers.

The security assertions are the point. A token that is revoked, expired, or
whose owner has been deleted must stop working, and every one of those refusals
must be indistinguishable from a token that never existed -- telling them apart
tells whoever is holding the string which of the four it is.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.orm.models import ApiToken, Product, StaffUser

from fastfort import FastFort, FastFortSettings
from fastfort.auth.api_tokens import PREFIX_LENGTH, hash_token
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


@pytest.fixture
def fort(backend: SQLAlchemyBackend) -> FastFort:
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    built.set_user_model(StaffUser)
    built.enable_api_tokens(ApiToken)
    return built


@pytest.fixture
async def user(staff_user: StaffUser) -> StaffUser:
    """The suite's own signed-in account -- `seeded` creates products, not
    users, so querying for one here found nothing."""
    return staff_user


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_model_without_the_columns_is_refused(backend: SQLAlchemyBackend) -> None:
    """Named at start-up, while somebody is looking at the file that declares
    it -- not on the first authenticated request."""
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    built.set_user_model(StaffUser)

    with pytest.raises(ConfigurationError, match="cannot hold API tokens"):
        built.enable_api_tokens(Product)


def test_the_service_is_absent_until_it_is_enabled(backend: SQLAlchemyBackend) -> None:
    built = FastFort(
        FastFortSettings(secret_key=SECRET, project_name="Test"),  # type: ignore[call-arg]
        backend=backend,
    )
    assert built.api_tokens is None


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------


async def test_a_token_resolves_to_its_owner(fort: FastFort, user: StaffUser) -> None:
    issued = await fort.api_tokens.issue(user=user, name="deploy")

    resolved = await fort.api_tokens.resolve(issued.secret)
    assert resolved is not None
    assert resolved.id == user.id


async def test_the_secret_is_never_stored(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The one thing this feature must get right. A row that held the secret
    would make the database a list of working credentials."""
    issued = await fort.api_tokens.issue(user=user, name="deploy")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()
        stored = " ".join(
            str(getattr(row, column.name, "")) for column in ApiToken.__table__.columns
        )

    assert issued.secret not in stored
    assert row.token_hash == hash_token(issued.secret)


async def test_the_prefix_identifies_without_authenticating(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Enough to match a row against the string in a configuration file, far
    too little to be worth attacking."""
    issued = await fort.api_tokens.issue(user=user, name="deploy")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    assert row.prefix == issued.secret[:PREFIX_LENGTH]
    assert len(row.prefix) < len(issued.secret)
    assert await fort.api_tokens.resolve(row.prefix) is None


async def test_two_tokens_never_collide(fort: FastFort, user: StaffUser) -> None:
    secrets_seen = {(await fort.api_tokens.issue(user=user)).secret for _ in range(5)}
    assert len(secrets_seen) == 5


# ---------------------------------------------------------------------------
# Refusals -- all four look the same
# ---------------------------------------------------------------------------


async def test_an_unknown_secret_resolves_to_nothing(fort: FastFort) -> None:
    assert await fort.api_tokens.resolve("not-a-real-token") is None


async def test_an_empty_secret_resolves_to_nothing(fort: FastFort) -> None:
    assert await fort.api_tokens.resolve("") is None


async def test_a_revoked_token_stops_working(fort: FastFort, user: StaffUser) -> None:
    issued = await fort.api_tokens.issue(user=user, name="deploy")
    assert await fort.api_tokens.resolve(issued.secret) is not None

    await fort.api_tokens.revoke(issued.obj)
    assert await fort.api_tokens.resolve(issued.secret) is None


async def test_an_expired_token_stops_working(fort: FastFort, user: StaffUser) -> None:
    issued = await fort.api_tokens.issue(
        user=user, name="short", expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    )
    assert await fort.api_tokens.resolve(issued.secret) is None


async def test_a_token_with_no_expiry_keeps_working(fort: FastFort, user: StaffUser) -> None:
    """Null means never. An expiry nobody chose is an outage nobody predicted."""
    issued = await fort.api_tokens.issue(user=user, name="forever")
    assert await fort.api_tokens.resolve(issued.secret) is not None


async def test_a_token_whose_owner_is_gone_stops_working(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Unlike a sign-in record, a token must not outlive its account. There is
    no foreign key to enforce it -- a mixin cannot know the user table's name --
    so resolution looks the owner up on every request."""
    issued = await fort.api_tokens.issue(user=user, name="orphan")

    async with session_factory() as session:
        await session.execute(sa.delete(StaffUser).where(StaffUser.id == user.id))
        await session.commit()

    assert await fort.api_tokens.resolve(issued.secret) is None


async def test_revoking_keeps_the_row(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Revoked rather than deleted, so the row still answers what this was and
    when it stopped."""
    issued = await fort.api_tokens.issue(user=user, name="deploy")
    await fort.api_tokens.revoke(issued.obj)

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()

    assert row is not None
    assert row.revoked_at is not None
    assert row.name == "deploy"


# ---------------------------------------------------------------------------
# Last used
# ---------------------------------------------------------------------------


async def test_using_a_token_records_when(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """What makes "this one has not been touched in a year" answerable, which
    is the question that gets old credentials revoked."""
    issued = await fort.api_tokens.issue(user=user, name="deploy")

    async with session_factory() as session:
        assert (await session.execute(sa.select(ApiToken))).scalars().first().last_used_at is None

    await fort.api_tokens.resolve(issued.secret)

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()
    assert row.last_used_at is not None


async def test_resolving_without_touching_leaves_it_alone(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    issued = await fort.api_tokens.issue(user=user, name="deploy")
    await fort.api_tokens.resolve(issued.secret, touch=False)

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()
    assert row.last_used_at is None


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------


async def test_scopes_are_stored_as_written(
    fort: FastFort, user: StaffUser, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """FastFort takes no view on what a scope means: the vocabulary belongs to
    the API being protected."""
    await fort.api_tokens.issue(user=user, name="reader", scopes="orders:read invoices:read")

    async with session_factory() as session:
        row = (await session.execute(sa.select(ApiToken))).scalars().first()
    assert row.scopes == "orders:read invoices:read"
