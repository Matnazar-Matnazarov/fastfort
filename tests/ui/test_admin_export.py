"""Downloading a list view as CSV, as a workbook, or as JSON.

The contract worth pinning is that the file matches the table it came from: the
same search, the same filters, the same order. An export that quietly returned
the whole model would be worse than none, because the person who filtered a list
before pressing the button would not notice until they had acted on it.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.export import json_value
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price", "is_active")
    list_filter = ("is_active",)
    search_fields = ("name",)
    ordering = ("id",)
    verbose_name_plural = "Products"


class PrivateAdmin(admin.ModelAdmin):
    """A model whose rows should not leave the admin in a file."""

    exportable = False
    verbose_name_plural = "Categories"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, PrivateAdmin, key="shop.category")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def rows_of(client: httpx.AsyncClient, **params: str) -> list[list[str]]:
    response = await client.get("/admin/shop.product/export", params={"format": "csv", **params})
    assert response.status_code == 200, response.text
    text = response.content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


async def test_the_export_carries_a_header_and_every_row(client: httpx.AsyncClient) -> None:
    rows = await rows_of(client)

    assert rows[0] == ["Id", "Name", "Price", "Is active"]
    assert len(rows) > 1


async def test_the_export_is_served_as_a_download(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/shop.product/export", params={"format": "csv"})

    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment;" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]


async def test_the_export_starts_with_a_byte_order_mark(client: httpx.AsyncClient) -> None:
    """Without it, Excel on Windows reads the file in the local code page and
    every non-ASCII name in the export is mojibake."""
    response = await client.get("/admin/shop.product/export", params={"format": "csv"})

    assert response.content.startswith(b"\xef\xbb\xbf")


async def test_the_export_honours_the_search(client: httpx.AsyncClient) -> None:
    everything = await rows_of(client)
    searched = await rows_of(client, q="pixel")

    assert len(searched) < len(everything)
    assert all("pixel" in row[1].lower() for row in searched[1:])


async def test_the_export_honours_the_filters(client: httpx.AsyncClient) -> None:
    """The file is the table on screen, not the whole model."""
    active = await rows_of(client, is_active__in="1")

    assert len(active) > 1
    assert all(row[3] == "true" for row in active[1:])


async def test_the_export_ignores_the_page(client: httpx.AsyncClient) -> None:
    """Someone on page two wants the whole result, not rows 26 to 50."""
    everything = await rows_of(client)
    from_page_two = await rows_of(client, p="2", ps="1")

    assert len(from_page_two) == len(everything)


# ---------------------------------------------------------------------------
# Workbooks
# ---------------------------------------------------------------------------


async def test_the_workbook_is_one_a_spreadsheet_can_open(client: httpx.AsyncClient) -> None:
    """Written by hand rather than through openpyxl, so this reads it back with
    openpyxl to prove the result is a real workbook and not merely a zip."""
    openpyxl = pytest.importorskip("openpyxl")

    response = await client.get("/admin/shop.product/export", params={"format": "xlsx"})
    assert response.status_code == 200

    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    sheet = workbook.active
    rows = list(sheet.iter_rows(values_only=True))

    assert rows[0] == ("Id", "Name", "Price", "Is active")
    # Numbers arrive as numbers, not as text a spreadsheet has to be told to
    # convert -- which is most of the reason to offer a workbook at all.
    assert isinstance(rows[1][0], int)
    assert isinstance(rows[1][2], float)


async def test_the_workbook_is_a_valid_archive(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/shop.product/export", params={"format": "xlsx"})
    archive = zipfile.ZipFile(io.BytesIO(response.content))

    assert archive.testzip() is None
    assert "xl/worksheets/sheet1.xml" in archive.namelist()


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


async def test_json_arrives_as_objects_keyed_by_column(client: httpx.AsyncClient) -> None:
    """Not arrays of arrays. The program reading this wants `row["name"]`, and
    an array renumbers every index the moment a column is added."""
    response = await client.get("/admin/shop.product/export", params={"format": "json"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert ".json" in response.headers["content-disposition"]

    rows = json.loads(response.text)
    assert isinstance(rows, list)
    assert set(rows[0]) == {"Id", "Name", "Price", "Is active"}


async def test_json_is_parseable_when_the_export_is_empty(client: httpx.AsyncClient) -> None:
    """Streaming an array by hand means writing the brackets by hand, and the
    row that never arrives is where a hand-written one produces `[,]`."""
    response = await client.get(
        "/admin/shop.product/export", params={"format": "json", "q": "no-such-product-anywhere"}
    )

    assert json.loads(response.text) == []


async def test_json_keeps_the_search_and_filters(client: httpx.AsyncClient) -> None:
    everything = json.loads(
        (await client.get("/admin/shop.product/export", params={"format": "json"})).text
    )
    searched = json.loads(
        (
            await client.get("/admin/shop.product/export", params={"format": "json", "q": "pixel"})
        ).text
    )

    assert 0 < len(searched) < len(everything)
    assert all("pixel" in row["Name"].lower() for row in searched)


async def test_json_uses_the_types_json_already_has(client: httpx.AsyncClient) -> None:
    """A spreadsheet cell holds text or a number, so the CSV writer renders
    `False` as the string `"false"` and `None` as an empty string. Carried into
    JSON that is a trap: `"false"` is a non-empty string, which is *true* in
    every language with a truthiness rule, and the reader has no way to tell it
    apart from a column that genuinely holds that word.
    """
    rows = json.loads(
        (await client.get("/admin/shop.product/export", params={"format": "json"})).text
    )
    by_name = {row["Name"]: row for row in rows}

    assert by_name["Retired Laptop"]["Is active"] is False
    assert by_name["Pixel Phone"]["Is active"] is True
    assert isinstance(by_name["Pixel Phone"]["Price"], float)
    assert isinstance(by_name["Pixel Phone"]["Id"], int)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # The two the spreadsheet writer has to flatten and JSON does not.
        (None, None),
        (False, False),
        (True, True),
        (Decimal("19.50"), 19.5),
        (dt.date(2026, 1, 15), "2026-01-15"),
        (dt.datetime(2026, 1, 15, 9, 30, tzinfo=dt.UTC), "2026-01-15 09:30:00+00:00"),
        (["new", "sale"], ["new", "sale"]),
    ],
)
def test_a_json_value_is_the_nearest_thing_json_has(value: object, expected: object) -> None:
    assert json_value(value) == expected
    assert json.loads(json.dumps(json_value(value))) == expected


async def test_json_keeps_non_ascii_readable(client: httpx.AsyncClient) -> None:
    """`ensure_ascii` would turn every Cyrillic name into `\\u0421...`, which is
    correct JSON and unreadable to the person who opened the file to look."""
    response = await client.get("/admin/shop.product/export", params={"format": "json"})

    assert "\\u" not in response.text


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


async def test_an_unknown_format_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/shop.product/export", params={"format": "pdf"})
    assert response.status_code == 404


async def test_a_model_that_opted_out_cannot_be_exported(client: httpx.AsyncClient) -> None:
    """`exportable = False` has to hold at the endpoint, not only by hiding the
    button: a URL anyone can type is not an access control."""
    response = await client.get("/admin/shop.category/export", params={"format": "csv"})
    assert response.status_code == 404


async def test_the_button_is_absent_for_a_model_that_opted_out(
    client: httpx.AsyncClient,
) -> None:
    listing = await client.get("/admin/shop.category/")
    assert "/export?" not in listing.text


async def test_exporting_needs_a_session(backend: SQLAlchemyBackend) -> None:
    """It hands out every row of the model, so it sits behind the same gate as
    the list it came from."""
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        response = await anonymous.get("/admin/shop.product/export", params={"format": "csv"})

    assert response.status_code in {302, 303, 401, 403}
