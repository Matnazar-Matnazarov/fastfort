"""The page in front of a delete, and what it is allowed to let through.

"This cannot be undone" is not a confirmation. The question anybody pressing a
delete button actually has is what happens to the rows underneath it, and the
answer is different for each relation: some go, some are kept with a column
cleared, and some make the delete impossible. This suite is about the page saying
which -- and about it refusing the last case rather than discovering it inside the
transaction, where the only thing left to show is a constraint violation.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.conftest import sign_in
from tests.orm.models import StaffUser
from tests.orm.relations import Crate, Depot, Ledger, Note

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"


class DepotAdmin(admin.ModelAdmin):
    list_display = ("id", "name")


class CrateAdmin(admin.ModelAdmin):
    list_display = ("id", "label")


class LedgerAdmin(admin.ModelAdmin):
    list_display = ("id", "reference")


def build(backend: SQLAlchemyBackend) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Depot, DepotAdmin, key="shop.depot")
    # Registered so the confirmation page can link to what it is about to touch.
    fort.register(Crate, CrateAdmin, key="shop.crate")
    fort.register(Ledger, LedgerAdmin, key="shop.ledger")

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as ready:
        await sign_in(ready)
        yield ready


@pytest.fixture
async def depot(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        one = Depot(name="North")
        session.add(one)
        await session.flush()
        session.add_all(
            [
                Crate(label="Crate A", depot_id=one.id),
                Note(body="Stocktake due", depot_id=one.id),
            ]
        )
        await session.commit()
        return int(one.id)


async def protect(session_factory: async_sessionmaker[AsyncSession], depot: int) -> None:
    async with session_factory() as session:
        session.add(Ledger(reference="LG-1", depot_id=depot))
        await session.commit()


async def confirmation(client: httpx.AsyncClient, depot: int) -> str:
    response = await client.get(f"/admin/shop.depot/{depot}/delete")
    assert response.status_code == 200, response.text
    return response.text


async def submit_delete(client: httpx.AsyncClient, depot: int) -> httpx.Response:
    """Post the confirmation form, carrying the token it was rendered with."""
    body = await confirmation(client, depot)
    token = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert token, "the confirmation form must carry a CSRF token"
    return await client.post(f"/admin/shop.depot/{depot}/delete", data={"_csrf": token.group(1)})


async def count(session_factory: async_sessionmaker[AsyncSession], model: type) -> int:
    async with session_factory() as session:
        found = await session.execute(sa.select(sa.func.count()).select_from(model))
        return int(found.scalar_one())


# ---------------------------------------------------------------------------
# What the page says
# ---------------------------------------------------------------------------


async def test_the_page_names_the_rows_that_go_with_it(
    client: httpx.AsyncClient, depot: int
) -> None:
    """A count is a statistic. The names are what tell somebody whether this is
    the row they meant.
    """
    body = await confirmation(client, depot)

    assert "will be deleted too" in body
    assert "Crate A" in body


async def test_rows_that_are_kept_are_not_described_as_deleted(
    client: httpx.AsyncClient, depot: int
) -> None:
    """`Note.depot_id` is nullable, so the note survives with the column cleared.
    Rolling it in with the cascades would be a warning that is simply untrue.
    """
    body = await confirmation(client, depot)

    kept = body.index("without the link")
    going = body.index("will be deleted too")
    # The note is under the heading that says it survives, the crate under the
    # one that says it does not.
    assert body.index("Stocktake due") > kept
    assert going < body.index("Crate A") < kept


async def test_a_related_model_with_an_admin_is_linked(
    client: httpx.AsyncClient, depot: int
) -> None:
    body = await confirmation(client, depot)

    assert '/admin/shop.crate/"' in body


async def test_the_confirmation_offers_the_delete_when_nothing_protects_it(
    client: httpx.AsyncClient, depot: int
) -> None:
    body = await confirmation(client, depot)

    assert "Yes, delete this" in body


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


async def test_a_protected_row_is_not_offered_a_delete_button(
    client: httpx.AsyncClient, depot: int, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await protect(session_factory, depot)

    body = await confirmation(client, depot)

    assert "Other records require this one" in body
    assert "Yes, delete this" not in body


async def test_posting_the_delete_anyway_is_refused(
    client: httpx.AsyncClient, depot: int, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The page hides the button; the endpoint is what enforces it. A tab left
    open while somebody else added the row that protects this one arrives here
    with a perfectly valid token.
    """
    await protect(session_factory, depot)

    response = await submit_delete(client, depot)

    assert response.status_code == 303
    assert await count(session_factory, Depot) == 1


async def test_the_refusal_names_what_is_holding_it(
    client: httpx.AsyncClient, depot: int, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An integrity error tells nobody which row to go and fix."""
    await protect(session_factory, depot)

    response = await submit_delete(client, depot)
    landed = await client.get(str(response.headers["location"]))

    # Singular, because there is one of them. "1 ledgers" is what naming the
    # model only in the plural reads as, and one is the commonest count there is.
    assert "1 ledger" in landed.text
    assert "1 ledgers" not in landed.text


# ---------------------------------------------------------------------------
# What it does
# ---------------------------------------------------------------------------


async def test_the_delete_goes_through_and_takes_the_cascade_with_it(
    client: httpx.AsyncClient, depot: int, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await submit_delete(client, depot)

    assert response.status_code == 303
    assert await count(session_factory, Depot) == 0
    assert await count(session_factory, Crate) == 0


async def test_the_rows_it_promised_to_keep_are_kept(
    client: httpx.AsyncClient, depot: int, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await submit_delete(client, depot)

    async with session_factory() as session:
        rows = (await session.execute(sa.select(Note))).scalars().all()

    assert [row.body for row in rows] == ["Stocktake due"]
    assert rows[0].depot_id is None


# ---------------------------------------------------------------------------
# The same rule, in bulk
# ---------------------------------------------------------------------------


async def test_a_bulk_delete_stops_before_it_half_succeeds(
    client: httpx.AsyncClient,
    depot: int,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Half the selection going and the rest failing on a foreign key is the
    worst available outcome, and it is what happens without this check.
    """
    async with session_factory() as session:
        second = Depot(name="South")
        session.add(second)
        await session.commit()
        other = int(second.id)
    await protect(session_factory, depot)

    body = (await client.get("/admin/shop.depot/")).text
    token = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert token

    response = await client.post(
        "/admin/shop.depot/action",
        data={"action": "delete", "_csrf": token.group(1), "keys": [str(depot), str(other)]},
    )

    assert response.status_code == 303
    # Neither of them: the one that was deletable is still there too.
    assert await count(session_factory, Depot) == 2
