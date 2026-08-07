"""Writing a list view out as a file.

Three formats, all produced without a dependency.

CSV and JSON come from the standard library. XLSX is written here rather than
through openpyxl because the whole of what an export needs -- one sheet, a header row,
strings and numbers -- is a few hundred lines of XML in a zip, and openpyxl is a
3 MB install carrying a formula parser, a chart engine and an image pipeline
that an admin export will never call. The narrow thing that is written by hand
is auditable; the wide dependency is not.

CSV and JSON are produced a row at a time and handed to the response as they go.
XLSX cannot be: a zip writes its central directory last, so the archive has to be
finished before any of it can be sent. All three are bounded by
`AdminSettings.export_limit` rather than by how much memory the process has.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape

__all__ = [
    "EXPORT_FORMATS",
    "ExportFormat",
    "stream_csv",
    "stream_json",
    "stream_xlsx",
]


class ExportFormat:
    """One offered format: what it is called, and what it is served as."""

    __slots__ = ("extension", "label", "media_type", "name")

    def __init__(self, name: str, label: str, extension: str, media_type: str) -> None:
        self.name = name
        self.label = label
        self.extension = extension
        self.media_type = media_type


EXPORT_FORMATS: dict[str, ExportFormat] = {
    "csv": ExportFormat("csv", "CSV", "csv", "text/csv; charset=utf-8"),
    "xlsx": ExportFormat(
        "xlsx",
        "Excel",
        "xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "json": ExportFormat("json", "JSON", "json", "application/json; charset=utf-8"),
}


def cell_value(value: Any) -> str | int | float:
    """One value, flattened to something a spreadsheet cell can hold.

    Dates go out ISO-formatted rather than localised: an export is usually read
    by another program, and "31/12/2026" is ambiguous to every one of them.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    return str(value)


def json_value(value: Any) -> Any:
    """One value, as the nearest thing JSON already has.

    Deliberately not `cell_value`. A spreadsheet cell holds text or a number, so
    that one renders `None` as an empty string and `False` as `"false"` -- and
    `"false"` read back out of JSON is a non-empty string, which is *true* in
    every language that has a truthiness rule. JSON has `null`, `false` and
    arrays, so a JSON export should use them and leave nothing to re-parse.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [json_value(item) for item in value]
    return str(value)


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def stream_csv(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[bytes]:
    """The rows as CSV, one chunk per row.

    Prefixed with a UTF-8 byte-order mark, which is the one thing that makes
    Excel on Windows read the file as UTF-8 rather than as the local code page --
    without it every non-ASCII name in the export is mojibake.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")

    yield b"\xef\xbb\xbf"

    writer.writerow(list(headers))
    yield buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)

    for row in rows:
        writer.writerow([cell_value(value) for value in row])
        yield buffer.getvalue().encode("utf-8")
        buffer.seek(0)
        buffer.truncate(0)


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def stream_json(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[bytes]:
    """The rows as a JSON array of objects, one chunk per row.

    Objects keyed by column rather than an array of arrays: an export read by a
    program is read by a program that wants to say `row["email"]`, not
    `row[4]` -- and a column added to the export later renumbers every index.

    Assembled by hand rather than through `json.dumps` on the whole list, so
    that an export of the row limit does not have to exist in memory twice
    before the first byte reaches the browser. Each row is still dumped by
    `json.dumps`, which is what does the escaping.
    """
    yield b"[\n"

    separator = b""
    for row in rows:
        record = {name: json_value(value) for name, value in zip(headers, row, strict=False)}
        yield separator + b"  " + json.dumps(record, ensure_ascii=False).encode("utf-8")
        separator = b",\n"

    yield b"\n]\n"


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""  # noqa: E501

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""  # noqa: E501

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Export" sheetId="1" r:id="rId1"/></sheets>
</workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""  # noqa: E501

#: Two number formats and the cell styles that use them, so a date cell can say
#: it is a date. Ids from 164 up are the file's own; 163 and below are reserved
#: for the ones every spreadsheet has built in.
#:
#: ISO, not a locale format. An export is read by another program as often as by
#: a person, and "31/12/2026" is ambiguous to every one of them -- which is the
#: same reason `cell_value` writes ISO for the formats that stay text.
#:
#: The fonts, fills and borders are not decoration and cannot be dropped: a
#: reader indexes into these lists by number, so a `cellXfs` entry referring to
#: font 0 needs a font 0 to exist. Two fills specifically, the second `gray125`,
#: because that is what the format's own defaults are and openpyxl refuses a
#: file with fewer -- which is the check that caught this being wrong.
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2">
<numFmt numFmtId="164" formatCode="yyyy\\-mm\\-dd"/>
<numFmt numFmtId="165" formatCode="yyyy\\-mm\\-dd\\ hh:mm:ss"/>
</numFmts>
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def _column_name(index: int) -> str:
    """1 -> A, 27 -> AA. Spreadsheet columns are bijective base-26, not base-26."""
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name


#: The style indices `_STYLES` below declares, in the order it declares them.
#: 0 is the default; a date cell points at 1 and a datetime at 2.
_DATE_STYLE = 1
_DATETIME_STYLE = 2

#: Day zero for a spreadsheet serial. Not 1900-01-01: Excel believes 1900 was a
#: leap year, so serial 60 is a day that never existed, and starting two days
#: earlier makes every serial above it land on the right date.
_EPOCH = dt.date(1899, 12, 30)


def _serial(value: dt.date | dt.datetime) -> float:
    """A date as the number of days a spreadsheet stores it as."""
    if isinstance(value, dt.datetime):
        moment = value.replace(tzinfo=None)
        days = (moment.date() - _EPOCH).days
        seconds = moment.hour * 3600 + moment.minute * 60 + moment.second
        return days + seconds / 86_400
    return float((value - _EPOCH).days)


def _cell(reference: str, value: Any) -> str:
    """One cell, typed the way a spreadsheet types it.

    A date goes out as a *number* with a date format on it rather than as text,
    which is what a date in a workbook actually is. Written as text it looked
    right until somebody opened the file: a spreadsheet converts what it
    recognises and leaves the rest alone, so a column came back half real dates
    and half strings, and re-uploading it was a coin toss per row. One format
    for the whole column is the point.

    `dt.date` covers `dt.datetime` too, so the narrower check comes first.
    """
    if isinstance(value, dt.datetime):
        return f'<c r="{reference}" s="{_DATETIME_STYLE}"><v>{_serial(value)}</v></c>'
    if isinstance(value, dt.date):
        return f'<c r="{reference}" s="{_DATE_STYLE}"><v>{_serial(value)}</v></c>'

    flattened = cell_value(value)
    if isinstance(flattened, int | float) and not isinstance(flattened, bool):
        return f'<c r="{reference}"><v>{flattened}</v></c>'
    if flattened == "":
        return f'<c r="{reference}"/>'
    # `t="inlineStr"` rather than the shared-string table: sharing would mean
    # holding every distinct string in memory to build the table, which is the
    # one thing streaming is here to avoid.
    text = escape(str(flattened))
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[str]:
    yield '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    yield (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
    )

    yield (
        '<row r="1">'
        + "".join(
            _cell(f"{_column_name(index)}1", header)
            for index, header in enumerate(headers, start=1)
        )
        + "</row>"
    )

    for number, row in enumerate(rows, start=2):
        yield (
            f'<row r="{number}">'
            + "".join(
                _cell(f"{_column_name(index)}{number}", value)
                for index, value in enumerate(row, start=1)
            )
            + "</row>"
        )

    yield "</sheetData></worksheet>"


def stream_xlsx(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> Iterator[bytes]:
    """The rows as a single-sheet workbook.

    A zip has to be seekable to write its central directory, so unlike the CSV
    writer this one produces the whole file before yielding it. The cap on how
    many rows an export may contain is what keeps that bounded.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", _WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", "".join(_sheet_xml(headers, rows)))

    yield buffer.getvalue()
