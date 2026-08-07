"""Uploading a file back in.

The claim import makes is that it is the mirror of export: a file the writer
produced is a file the reader takes back. So most of what is here writes
something with `export.py` and feeds it straight to the importer, because a
column the pair disagrees about is a bug in the pair rather than in either half.

The rest is about refusal. Everything a person can get wrong in a spreadsheet --
a word where a number goes, a category that does not exist, a coordinate off the
edge of the planet -- has to come back as a line number and a column name, and
none of it may leave half a file in the database.
"""

from __future__ import annotations

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
from fastfort.admin.export import stream_csv, stream_json, stream_xlsx
from fastfort.admin.importing import (
    ImportFileError,
    build_plan,
    read_table,
    sniff_format,
)
from fastfort.orm.sqlalchemy import SQLAlchemyBackend, introspect_model

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price", "category")
    # The list renders the category name, and the session is gone by the time
    # the template asks for it.
    select_related = ("category",)
    importable = True
    verbose_name_plural = "Products"


class ClosedAdmin(admin.ModelAdmin):
    """The default. Import is opt-in, unlike export."""

    list_display = ("id", "name")
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
    fort.register(Category, ClosedAdmin, key="shop.category")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


def spec_and_admin() -> tuple[object, admin.ModelAdmin]:
    spec = introspect_model(Product, key="shop.product")
    return spec, ProductAdmin(spec)


async def upload(
    client: httpx.AsyncClient, content: bytes, name: str = "rows.csv", **fields: str
) -> httpx.Response:
    token = await _csrf(client)
    return await client.post(
        "/admin/shop.product/import",
        files={"file": (name, content, "application/octet-stream")},
        data={"_csrf": token, **fields},
    )


async def _csrf(client: httpx.AsyncClient) -> str:
    import re

    body = (await client.get("/admin/shop.product/import")).text
    match = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert match, "the import form must carry a CSRF token"
    return match.group(1)


def csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    return b"".join(stream_csv(headers, rows))


# ---------------------------------------------------------------------------
# The round trip, in all three formats
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("writer", [stream_csv, stream_xlsx, stream_json])
def test_a_file_the_exporter_wrote_is_a_file_the_importer_reads(writer: object) -> None:
    """The whole claim. Download, edit in a spreadsheet, upload -- and a column
    the writer emits that the reader cannot take back breaks that loop."""
    spec, model_admin = spec_and_admin()
    headers = ["Id", "Name", "Price", "Stock", "Is active", "Released on"]
    blob = b"".join(writer(headers, [[1, "Pixel Phone", 799.00, 12, "true", "2026-01-15"]]))  # type: ignore[operator]

    head, rows = read_table(blob, sniff_format("export.bin", blob))
    plan = build_plan(head, rows, spec, model_admin)  # type: ignore[arg-type]

    assert plan.ok, [error.message for error in plan.errors]
    assert plan.rows[0].values["name"] == "Pixel Phone"
    # A real boolean, not the string "true" -- which is truthy in every language
    # that has a truthiness rule, so the column would read back inverted.
    assert plan.rows[0].values["is_active"] is True
    # The id column means "update this row" rather than "insert with this key".
    assert plan.rows[0].key == ("1",)


def test_a_header_matches_a_field_by_its_label_or_its_name() -> None:
    """An export writes labels and a person writing a file by hand writes field
    names. Both have to land on the same field, or the round trip only works in
    one direction."""
    spec, model_admin = spec_and_admin()

    for header in ("Released on", "released_on", "RELEASED_ON", "  released on  "):
        plan = build_plan([header], [["2026-01-15"]], spec, model_admin)  # type: ignore[arg-type]
        assert plan.columns == ["released_on"], header


def test_a_column_that_matches_nothing_is_ignored_rather_than_refused() -> None:
    """An export carries columns a project may not want back -- a computed
    total, an audit stamp. Refusing the file over one of them would make the
    round trip impossible."""
    spec, model_admin = spec_and_admin()
    plan = build_plan(["Name", "Nonsense"], [["Thing", "x"]], spec, model_admin)  # type: ignore[arg-type]

    assert plan.ok
    assert plan.ignored == ["Nonsense"]
    assert plan.rows[0].values == {"name": "Thing"}


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_every_bad_cell_is_reported_at_once_with_its_line() -> None:
    """Not the first. Somebody who has to upload a spreadsheet ten times to be
    told about ten mistakes will edit the database directly instead, which is
    the thing this admin exists to make unnecessary."""
    spec, model_admin = spec_and_admin()
    plan = build_plan(
        ["Name", "Price", "Is active"],
        [
            ["Fine", "10.00", "true"],
            ["Bad price", "banana", "true"],
            ["Bad boolean", "10.00", "perhaps"],
        ],
        spec,  # type: ignore[arg-type]
        model_admin,
    )

    assert not plan.ok
    assert len(plan.errors) == 2
    # Line 1 is the header, so the first data row is line 2 -- the number the
    # spreadsheet shows down its left edge.
    assert [error.line for error in plan.errors] == [3, 4]
    assert plan.errors[0].column == "Price"
    assert plan.errors[0].value == "banana"


def test_the_parsers_are_the_forms_parsers() -> None:
    """Import must not be more permissive than the change form -- that would be
    a way to write values the admin itself rejects -- and must not be stricter,
    or a row somebody can type by hand is a row the file cannot carry."""
    from fastfort.admin.values import parse_value

    spec, model_admin = spec_and_admin()
    plan = build_plan(["Name", "Released on"], [["x", "31/12/2026"]], spec, model_admin)  # type: ignore[arg-type]

    assert not plan.ok
    with pytest.raises(ValueError, match="YYYY-MM-DD") as raised:
        parse_value("31/12/2026", spec.field("released_on"))  # type: ignore[attr-defined]
    # The same sentence, from the same function -- not merely a similar one.
    assert plan.errors[0].message == str(raised.value)


def test_an_empty_required_column_is_named_rather_than_written_as_null() -> None:
    spec, model_admin = spec_and_admin()
    plan = build_plan(["Name", "Price"], [["", "10.00"]], spec, model_admin)  # type: ignore[arg-type]

    assert not plan.ok
    assert plan.errors[0].column == "Name"


def test_a_file_with_no_matching_columns_says_which_ones_it_wanted() -> None:
    spec, model_admin = spec_and_admin()
    with pytest.raises(ImportFileError, match="Expected some of"):
        build_plan(["Nope", "Also nope"], [["a", "b"]], spec, model_admin)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# What no file may set
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["Api secret", "api_secret"])
def test_a_sensitive_column_cannot_be_set_by_a_file(column: str) -> None:
    """A sensitive column is masked on its way out and never echoed into a form.
    Leaving it importable would make a spreadsheet the one place in the admin
    where an API key can be set in the clear."""
    spec, model_admin = spec_and_admin()
    plan = build_plan(["Name", column], [["x", "hunter2"]], spec, model_admin)  # type: ignore[arg-type]

    assert plan.ignored == [column]
    assert "api_secret" not in plan.rows[0].values


def test_a_read_only_field_is_not_importable_either() -> None:
    """`FieldSpec.editable` is the mass-assignment boundary, and an import is a
    write like any other. A column the form would discard is a column a
    spreadsheet must not be able to set."""

    class Frozen(admin.ModelAdmin):
        importable = True
        readonly_fields = ("name",)

    spec = introspect_model(Product, key="shop.product")
    plan = build_plan(["Name", "Price"], [["x", "10.00"]], spec, Frozen(spec))

    assert plan.ignored == ["Name"]
    assert "name" not in plan.rows[0].values


def test_import_fields_narrows_but_never_widens() -> None:
    class Narrow(admin.ModelAdmin):
        importable = True
        import_fields = ("name",)

    spec = introspect_model(Product, key="shop.product")
    plan = build_plan(["Name", "Price"], [["x", "10.00"]], spec, Narrow(spec))

    assert plan.columns == ["name"]
    assert plan.ignored == ["Price"]


# ---------------------------------------------------------------------------
# Hostile files
# ---------------------------------------------------------------------------


def test_a_workbook_declaring_xml_entities_is_refused() -> None:
    """The billion-laughs half of the XML family. `xml.etree` does not resolve
    external entities, but it does expand internal ones -- a few hundred bytes
    that becomes gigabytes and takes the process with it. A spreadsheet has no
    legitimate use for a DOCTYPE, so the declaration itself is the refusal."""
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "xxxxxxxxxx">'
        b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]><worksheet>&b;</worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", bomb)

    with pytest.raises(ImportFileError, match="entities"):
        read_table(buffer.getvalue(), "xlsx")


def test_a_workbook_that_claims_to_expand_enormously_is_refused() -> None:
    """A zip records the inflated size of every entry, so a compression bomb can
    be refused on the strength of what it claims rather than by watching memory
    disappear while it proves it."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"\0" * 60_000_000)

    with pytest.raises(ImportFileError, match="expands to"):
        read_table(buffer.getvalue(), "xlsx")


def test_a_file_that_is_not_a_workbook_says_so() -> None:
    with pytest.raises(ImportFileError, match="not a readable"):
        read_table(b"this is not a zip", "xlsx")


def test_json_that_is_not_a_list_of_objects_says_so() -> None:
    with pytest.raises(ImportFileError, match="array of objects"):
        read_table(json.dumps([1, 2, 3]).encode(), "json")


# ---------------------------------------------------------------------------
# Through the real stack
# ---------------------------------------------------------------------------


async def test_a_model_that_did_not_opt_in_offers_no_import(
    client: httpx.AsyncClient,
) -> None:
    """Off by default, unlike export. Writing several thousand rows in one
    request is a different thing to hand somebody by accident."""
    assert (await client.get("/admin/shop.category/import")).status_code == 404
    assert "shop.category/import" not in (await client.get("/admin/shop.category/")).text


async def test_the_list_offers_an_import_when_the_model_takes_one(
    client: httpx.AsyncClient,
) -> None:
    assert "/admin/shop.product/import" in (await client.get("/admin/shop.product/")).text


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "json"])
async def test_the_template_downloads_with_a_hint_row_that_is_a_comment(
    client: httpx.AsyncClient, fmt: str
) -> None:
    """It carries the shape of each column, and it carries it as a comment.

    Written as data it was six parse errors on the one file that exists to
    explain the format -- and downloading the template and uploading it back is
    the first thing anybody does with one.
    """
    response = await client.get(f"/admin/shop.product/import/template?format={fmt}")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]

    _, rows = read_table(response.content, fmt)
    assert rows
    assert rows[0][0].startswith("#")
    # And the hints really are in it, or the file explains nothing.
    assert "YYYY-MM-DD" in " ".join(rows[0])


async def test_checking_a_file_writes_nothing(client: httpx.AsyncClient) -> None:
    """Checked by default: somebody uploading for the first time should find out
    what it would do before it does it."""
    before = (await client.get("/admin/shop.product/")).text
    body = csv_bytes(["Name", "Price"], [["Imported Widget", "5.00"]])

    response = await upload(client, body, check="1")

    assert response.status_code == 200
    assert "ready to import" in response.text
    assert "Imported Widget" not in (await client.get("/admin/shop.product/")).text
    assert "Imported Widget" not in before


async def test_a_clean_file_is_written(client: httpx.AsyncClient) -> None:
    body = csv_bytes(["Name", "Price"], [["Imported Widget", "5.00"]])

    response = await upload(client, body)

    assert response.status_code in (302, 303), response.text
    assert "Imported Widget" in (await client.get("/admin/shop.product/")).text


async def test_a_file_with_one_bad_row_writes_none_of_it(
    client: httpx.AsyncClient,
) -> None:
    """All-or-nothing. A half-applied spreadsheet is worse than a rejected one:
    the rejection is a list of line numbers somebody can act on, and the
    half-application is a data set nobody can tell apart from the one they
    meant to upload."""
    body = csv_bytes(
        ["Name", "Price"],
        [["Good One", "5.00"], ["Bad One", "not a price"]],
    )

    response = await upload(client, body)
    listing = (await client.get("/admin/shop.product/")).text

    assert response.status_code == 200
    assert "Nothing was imported" in response.text
    assert "Good One" not in listing
    assert "Bad One" not in listing


async def test_a_row_with_a_key_updates_rather_than_duplicates(
    client: httpx.AsyncClient,
) -> None:
    body = csv_bytes(["Id", "Name", "Price"], [[1, "Renamed Phone", "1.00"]])

    response = await upload(client, body)
    listing = (await client.get("/admin/shop.product/")).text

    assert response.status_code in (302, 303), response.text
    assert "Renamed Phone" in listing
    assert "Pixel Phone" not in listing


# ---------------------------------------------------------------------------
# Relations -- the half only the database can answer
# ---------------------------------------------------------------------------


async def test_a_relation_is_resolved_by_the_name_the_export_wrote(
    client: httpx.AsyncClient,
) -> None:
    """A file edited by a person carries names, not ids."""
    body = csv_bytes(["Name", "Price", "Category"], [["With Category", "5.00", "Phones"]])

    response = await upload(client, body)

    assert response.status_code in (302, 303), response.text
    listing = (await client.get("/admin/shop.product/")).text
    assert "With Category" in listing


async def test_a_relation_is_resolved_by_id_too(client: httpx.AsyncClient) -> None:
    """A file produced by a machine carries ids. Refusing either spelling would
    make one of the two workflows impossible."""
    body = csv_bytes(["Name", "Price", "Category"], [["By Id", "5.00", "1"]])

    assert (await upload(client, body)).status_code in (302, 303)
    assert "By Id" in (await client.get("/admin/shop.product/")).text


async def test_a_relation_that_does_not_exist_names_the_row_and_the_cell(
    client: httpx.AsyncClient,
) -> None:
    """The check the whole feature turns on: is there actually such an object.
    Without it the row is written with a dangling key, or the database refuses
    it with a sentence about a constraint."""
    body = csv_bytes(
        ["Name", "Price", "Category"],
        [["Fine", "5.00", "Phones"], ["Orphan", "5.00", "No Such Category"]],
    )

    response = await upload(client, body)

    assert response.status_code == 200
    assert "No Such Category" in response.text
    assert "Nothing was imported" in response.text
    listing = (await client.get("/admin/shop.product/")).text
    assert "Orphan" not in listing
    # And the good row went back out with the bad one.
    assert "Fine" not in listing


# ---------------------------------------------------------------------------
# Dates, which a spreadsheet does not store as dates
# ---------------------------------------------------------------------------

SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def workbook(
    cell_format: str,
    *,
    custom: str = "",
    serial: str = "46218",
    from_1904: bool = False,
) -> bytes:
    """A one-cell workbook shaped the way a spreadsheet writes one.

    Not the way `export.py` writes one: that uses inline strings and no styles
    at all, which is exactly the case this file already covered and exactly the
    case that never sees a date serial.
    """
    styles = (
        f'<?xml version="1.0"?><styleSheet xmlns="{SHEET_NS}">{custom}'
        f'<cellXfs count="2"><xf numFmtId="0"/>{cell_format}</cellXfs></styleSheet>'
    )
    book = (
        f'<?xml version="1.0"?><workbook xmlns="{SHEET_NS}">'
        f'<workbookPr date1904="{"1" if from_1904 else "0"}"/></workbook>'
    )
    sheet = (
        f'<?xml version="1.0"?><worksheet xmlns="{SHEET_NS}"><sheetData>'
        f'<row r="1"><c r="A1" t="inlineStr"><is><t>Released on</t></is></c></row>'
        f'<row r="2"><c r="A2" s="1"><v>{serial}</v></c></row></sheetData></worksheet>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", book)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def numfmt(identifier: int, code: str) -> str:
    return f'<numFmts><numFmt numFmtId="{identifier}" formatCode="{code}"/></numFmts>'


@pytest.mark.parametrize(
    ("label", "blob", "expected"),
    [
        # The one a person actually hits. `export.py` writes an ISO date as
        # text; a spreadsheet turns it into a real date cell the moment the file
        # is opened and saved, and a real date cell is a *number*.
        (
            "a custom yyyy-mm-dd format",
            workbook('<xf numFmtId="176"/>', custom=numfmt(176, "yyyy\\-mm\\-dd")),
            "2026-07-15",
        ),
        ("the built-in short date", workbook('<xf numFmtId="14"/>'), "2026-07-15"),
        (
            "a built-in date and time",
            workbook('<xf numFmtId="22"/>', serial="46218.5"),
            "2026-07-15 12:00:00",
        ),
        (
            "a workbook counting from 1904",
            workbook('<xf numFmtId="14"/>', serial="44756", from_1904=True),
            "2026-07-15",
        ),
    ],
)
def test_a_date_cell_arrives_as_a_date(label: str, blob: bytes, expected: str) -> None:
    """A date in a spreadsheet is a number of days with a format applied. Read
    without the format it is a five-figure integer, which is what "46218" was --
    and it broke the one workflow this feature exists for: export, edit, upload.
    """
    _, rows = read_table(blob, "xlsx")
    assert rows[0][0] == expected, label


@pytest.mark.parametrize(
    ("label", "blob"),
    [
        ("no format at all", workbook('<xf numFmtId="0"/>', serial="2000")),
        # The literal is stripped before the code is judged, or the letters in
        # the word would make a plain number look like a date.
        (
            "a number whose format contains words",
            workbook(
                '<xf numFmtId="180"/>',
                custom=numfmt(180, "0.00&quot; days&quot;"),
                serial="2000",
            ),
        ),
    ],
)
def test_a_number_that_is_not_a_date_keeps_its_digits(label: str, blob: bytes) -> None:
    _, rows = read_table(blob, "xlsx")
    assert rows[0][0] == "2000", label


# ---------------------------------------------------------------------------
# The round trip, end to end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "json"])
def test_the_template_uploads_unchanged(fmt: str) -> None:
    """The first thing anybody does with a template is upload it, and it used to
    be the one file the format refuses -- one parse error per column, from the
    file that exists to explain the format.

    The hint row is a comment now. What is left is a header and no rows, which
    is a different message and an honest one: fill something in.
    """
    from fastfort.admin.importing import template_rows

    spec, model_admin = spec_and_admin()
    headers, rows = template_rows(spec, model_admin, lambda text: text)  # type: ignore[arg-type]
    writer = {"csv": stream_csv, "xlsx": stream_xlsx, "json": stream_json}[fmt]
    blob = b"".join(writer(headers, rows))

    head, body = read_table(blob, fmt)
    with pytest.raises(ImportFileError, match="no rows to import"):
        build_plan(head, body, spec, model_admin)  # type: ignore[arg-type]


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "json"])
def test_a_template_with_a_row_filled_in_imports(fmt: str) -> None:
    """Which is what a template is for. The comment row is skipped and the row
    under it is read, so the file somebody downloads is the file they fill in."""
    from fastfort.admin.importing import template_rows

    spec, model_admin = spec_and_admin()
    headers, rows = template_rows(spec, model_admin, lambda text: text)  # type: ignore[arg-type]
    filled = [*rows, ["", "Filled In"] + [""] * (len(headers) - 2)]
    writer = {"csv": stream_csv, "xlsx": stream_xlsx, "json": stream_json}[fmt]

    head, body = read_table(b"".join(writer(headers, filled)), fmt)
    plan = build_plan(head, body, spec, model_admin)  # type: ignore[arg-type]

    assert plan.ok, [error.message for error in plan.errors]
    assert len(plan.rows) == 1
    assert plan.rows[0].values["name"] == "Filled In"


@pytest.mark.parametrize("fmt", ["csv", "xlsx", "json"])
def test_an_export_with_dates_reads_straight_back(fmt: str) -> None:
    """The whole loop, with the two column types that used to break it: a date
    and a datetime. An export the reader cannot take back is a bug in the pair,
    and this is the pair."""
    spec, model_admin = spec_and_admin()
    headers = ["Id", "Name", "Price", "Is active", "Released on", "Created at"]
    row = [
        1,
        "Pixel Phone",
        Decimal("799.00"),
        True,
        dt.date(2026, 1, 15),
        dt.datetime(2026, 1, 15, 9, 30, tzinfo=dt.UTC),
    ]
    writer = {"csv": stream_csv, "xlsx": stream_xlsx, "json": stream_json}[fmt]

    head, body = read_table(b"".join(writer(headers, [row])), fmt)
    plan = build_plan(head, body, spec, model_admin)  # type: ignore[arg-type]

    assert plan.ok, [f"{e.column}: {e.message} ({e.value})" for e in plan.errors]
    assert plan.rows[0].values["released_on"] == dt.date(2026, 1, 15)
    assert plan.rows[0].values["is_active"] is True


def test_a_date_leaves_the_xlsx_writer_as_a_date_cell() -> None:
    """Written as text it looked right until somebody opened the file: a
    spreadsheet converts what it recognises and leaves the rest alone, so a
    column came back half real dates and half strings and re-uploading it was a
    coin toss per row. One format for the whole column is the point."""
    blob = b"".join(stream_xlsx(["When"], [[dt.date(2026, 7, 15)]]))

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        sheet = archive.read("xl/worksheets/sheet1.xml").decode()
        # A styles part, and a cell pointing into it rather than an inline string.
        assert "xl/styles.xml" in archive.namelist()
        assert "yyyy" in archive.read("xl/styles.xml").decode()

    assert 's="1"><v>46218.0<' in sheet or 's="1"><v>46218<' in sheet
    assert "inlineStr" not in sheet.split('<row r="2">')[1]


def test_a_row_beginning_with_a_hash_is_a_comment() -> None:
    """Also the escape hatch for anybody keeping notes in the spreadsheet they
    import from, which is a habit worth accommodating rather than arguing with."""
    spec, model_admin = spec_and_admin()
    plan = build_plan(
        ["Name", "Price"],
        [["# everything below is this quarter", ""], ["Real Row", "5.00"]],
        spec,  # type: ignore[arg-type]
        model_admin,
    )

    assert plan.ok, [error.message for error in plan.errors]
    assert len(plan.rows) == 1
    assert plan.rows[0].values["name"] == "Real Row"


def test_a_json_export_writes_an_array_and_reads_one_back() -> None:
    """JSON has arrays, so a JSON export uses one for a many-valued cell -- and
    splitting `["new", "sale"]` on commas produces `["new"` and `"sale"]`, which
    match nothing. The file's own native spelling has to read back, or a JSON
    export is a file its own importer cannot take."""
    from fastfort.admin.values import split_multi

    assert split_multi('["new", "bestseller"]') == ["new", "bestseller"]
    assert split_multi("new, bestseller") == ["new", "bestseller"]
    assert split_multi('["refurbished"]') == ["refurbished"]
    # A JSON column legitimately holds a list of objects, and that is one value
    # rather than a list of them.
    assert split_multi('[{"a": 1}, {"b": 2}]') == ['[{"a": 1}', '{"b": 2}]']


def test_a_geometry_exports_as_something_an_import_can_read() -> None:
    """A list cell summarises one -- "Polygon · 14 points" -- which is right to
    read in a table and impossible to turn back into a polygon. A file is read
    by a program at least as often as by a person."""
    from tests.orm.exotic_models import SpatialColumn

    from fastfort.admin.values import parse_value

    spec = introspect_model(SpatialColumn, key="lab.spatial")

    class Spatial(admin.ModelAdmin):
        importable = True

    model_admin = Spatial(spec)

    class Row:
        # A real polygon, as the WKB a spatial column hands back. Taken from
        # `tests/unit/test_geo_codec.py`, where it came out of PostGIS itself.
        area = (
            "01030000000100000005000000CDCCCCCCCC4C51409A999999999944403333333333535140"
            "9A9999999999444033333333335351406666666666A64440CDCCCCCCCC4C51406666666666"
            "A64440CDCCCCCCCC4C51409A99999999994440"
        )

    exported = model_admin.export_cell(Row(), "area")
    assert "POLYGON" in exported
    # And the parser takes it back, which is the whole claim.
    assert parse_value(exported, spec.field("area")).startswith("SRID=")
