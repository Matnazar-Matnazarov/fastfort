"""Reading a file back in, and refusing it clearly when it is wrong.

The mirror of `export.py`, and deliberately its mirror: the same three formats,
and a file this reads is a file that one wrote. Round-tripping an export through
an import is the thing people actually do -- download, edit in a spreadsheet,
upload -- and any column the writer emits that the reader cannot take back is a
bug in the pair rather than in either half.

Two rules hold here.

*The parsers are the form's parsers.* Every value goes through
`values.parse_value` and `values.check_bounds`, the same functions the change
form uses. Import cannot be more permissive than the form -- that would be a way
to write values the admin would reject -- and it must not be stricter, or a row
somebody can type by hand is a row the file cannot carry.

*Nothing is written until everything parses.* A file is checked completely
before a single row reaches the database, and the whole import is one
transaction. A half-applied spreadsheet is worse than a rejected one: the
rejection is a list of line numbers, and the half-application is a data set
nobody can tell apart from the one they meant to upload.

Relations are the exception this module cannot handle alone -- "is there a
category called Phones" is a question only the database can answer. `Plan` marks
those cells and the view resolves them, so that the shape of the answer is still
one row-and-column error alongside all the others.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any
from xml.etree import ElementTree

from fastfort.spec import FieldSpec, FieldType, ModelSpec

from .values import check_bounds, parse_value

__all__ = [
    "IMPORT_FORMATS",
    "ImportFileError",
    "Plan",
    "RowError",
    "column_hint",
    "read_table",
    "sniff_format",
]

#: What a file may be, keyed by the extension people actually upload.
IMPORT_FORMATS: dict[str, str] = {
    "csv": "csv",
    "tsv": "csv",
    "txt": "csv",
    "xlsx": "xlsx",
    "json": "json",
}

#: Rows past which an upload is refused outright. Not a memory bound -- the
#: whole file is already in memory by the time it reaches here, bounded by
#: `MediaSettings.upload_limit` -- but a bound on the transaction, because one
#: statement per row over a hundred thousand rows is a request nobody will wait
#: for and a lock nobody else can work around.
MAX_ROWS = 10_000

#: A row whose first cell starts with this is a note to the reader, not data.
#:
#: The downloadable template's hint row carries it, so that the file which
#: exists to explain the format is not the one file the format refuses. It is
#: also the escape hatch for anybody keeping notes in a spreadsheet they import
#: from, which is a habit worth accommodating rather than arguing with.
COMMENT_PREFIX = "#"

#: Types no import may write. A password is hashed on its way in through the
#: form and a file cannot carry a hash anyone should paste; the rest cannot be
#: typed at all.
_UNIMPORTABLE = frozenset(
    {
        FieldType.PASSWORD,
        FieldType.BINARY,
        FieldType.SEARCH_VECTOR,
        FieldType.FILE,
        FieldType.IMAGE,
        FieldType.REVERSE_FK,
    }
)


class ImportFileError(Exception):
    """The file could not be read at all -- not a row problem, a file problem.

    `tone` is how the page should say it. Most of these are genuine faults and
    render in red, but "you downloaded the template and uploaded it back" is a
    normal thing to do and a normal state to be in: the file is perfectly well
    formed and simply has nothing in it yet. Shouting at somebody for following
    the instructions is how a form teaches people to distrust its warnings.
    """

    def __init__(self, message: str, *, tone: str = "danger") -> None:
        super().__init__(message)
        self.tone = tone


@dataclass(frozen=True, slots=True)
class RowError:
    """One cell the import refuses, named the way somebody can go and fix it.

    `line` is the line in the uploaded file, header included, because that is
    the number the spreadsheet shows down its left edge. Counting data rows from
    one instead was the version people could not act on: it is off by one from
    everything they are looking at.
    """

    line: int
    column: str
    message: str
    #: What was in the cell, trimmed. Echoed back so the report is readable
    #: without opening the file again -- and capped, because a cell can hold a
    #: whole document and an error list is not the place to render one.
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "column": self.column,
            "message": self.message,
            "value": self.value,
        }


@dataclass(slots=True)
class Row:
    """One parsed row: what will be written, and what still needs the database."""

    line: int
    #: Scalar values, already through the form's own parsers.
    values: dict[str, Any] = dataclass_field(default_factory=dict)
    #: Relation cells, as the text the file carried. The view resolves these,
    #: because only the database knows whether a "Phones" category exists.
    relations: dict[str, str] = dataclass_field(default_factory=dict)
    #: The primary key, when the file carried one. Present means update.
    key: tuple[Any, ...] | None = None


@dataclass(slots=True)
class Plan:
    """Everything an upload turned into, before any of it is written."""

    rows: list[Row] = dataclass_field(default_factory=list)
    errors: list[RowError] = dataclass_field(default_factory=list)
    #: Header columns that matched no field. Not an error -- an export carries
    #: columns a project may not want to import back, and refusing the file over
    #: one of them would make the round trip impossible.
    ignored: list[str] = dataclass_field(default_factory=list)
    #: Fields the file did carry, in the order the header had them.
    columns: list[str] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def creates(self) -> int:
        return sum(1 for row in self.rows if row.key is None)

    @property
    def updates(self) -> int:
        return sum(1 for row in self.rows if row.key is not None)


# ---------------------------------------------------------------------------
# Reading a file into rows of text
# ---------------------------------------------------------------------------


def sniff_format(filename: str, content: bytes) -> str:
    """What the upload is, from its name and failing that its first bytes.

    The name is trusted only as far as choosing a parser: a file called `.csv`
    that is really a zip fails in the CSV reader with a message about the file,
    which is the correct outcome either way. The bytes are the fallback for the
    uploads that arrive with no extension at all.
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    found = IMPORT_FORMATS.get(extension)
    if found is not None:
        return found
    if content[:2] == b"PK":  # every zip, and an xlsx is a zip
        return "xlsx"
    if content.lstrip()[:1] in (b"[", b"{"):
        return "json"
    return "csv"


def read_table(content: bytes, kind: str) -> tuple[list[str], list[list[str]]]:
    """A header row and the rows under it, as text, whatever the format was.

    Text rather than typed values, even from JSON and XLSX where the file
    already carries a type. Everything downstream goes through the form's own
    parsers, and handing those a float that a spreadsheet decided `00123` was
    would import a different value from the one on screen.
    """
    if kind == "xlsx":
        return _read_xlsx(content)
    if kind == "json":
        return _read_json(content)
    return _read_csv(content)


def _decode(content: bytes) -> str:
    """UTF-8, with the byte-order mark taken off.

    `stream_csv` writes that mark deliberately -- it is the one thing that makes
    Excel read the file as UTF-8 -- so an export fed straight back in arrives
    with it, and left on it becomes part of the first header's name.
    """
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Nothing decoded cleanly; keep what is readable rather than refusing the
    # file, so the error the person sees is about the column that is wrong
    # rather than about the encoding of a file that is mostly fine.
    return content.decode("utf-8", errors="replace")


def _read_csv(content: bytes) -> tuple[list[str], list[list[str]]]:
    text = _decode(content)
    if not text.strip():
        raise ImportFileError("That file is empty.")

    # Comma, semicolon or tab, because a spreadsheet saved in a European locale
    # uses semicolons and the person who saved it has no idea that it did.
    try:
        dialect: Any = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    rows = [[cell.strip() for cell in row] for row in reader]
    rows = [row for row in rows if any(cell for cell in row)]
    if not rows:
        raise ImportFileError("That file has no rows in it.")
    return rows[0], rows[1:]


def _read_json(content: bytes) -> tuple[list[str], list[list[str]]]:
    try:
        parsed = json.loads(_decode(content))
    except json.JSONDecodeError as exc:
        raise ImportFileError(
            f"That file is not valid JSON ({exc.msg} at position {exc.pos})."
        ) from None

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        raise ImportFileError("Expected a JSON array of objects, one per row.")
    if not all(isinstance(item, dict) for item in parsed):
        raise ImportFileError("Expected a JSON array of objects, one per row.")

    # Every key any row has, in first-seen order -- a JSON export omits nothing,
    # but a hand-written file often leaves out the nulls.
    headers: list[str] = []
    for item in parsed:
        for key in item:
            if key not in headers:
                headers.append(key)

    rows = [[_json_cell(item.get(name)) for name in headers] for item in parsed]
    return headers, rows


def _json_cell(value: Any) -> str:
    """One JSON value as the text the parsers read.

    A nested object or array is re-serialised rather than stringified, because
    a JSON column's own value arrives here as a `dict` and `str(dict)` is Python
    source, which `parse_value` would then refuse.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

#: Most a workbook's parts may expand to, uncompressed. A zip stores how large
#: each entry inflates to, so this is checked before anything is read rather
#: than while memory is filling up. Fifty megabytes of XML is a spreadsheet far
#: larger than `MAX_ROWS` allows; a file that claims more is a compression bomb,
#: because a real one that size would have been refused a step earlier anyway.
MAX_UNCOMPRESSED = 50_000_000


def _parse_xml(data: bytes) -> ElementTree.Element:
    """Parse one part of a workbook, with entity expansion refused.

    `xml.etree` does not resolve *external* entities, so this is not the XXE
    that reads `/etc/passwd`. It does expand internal ones, which is the other
    half of the same family: a document declaring one entity in terms of ten
    copies of the last, ten deep, is a few hundred bytes that expands to
    gigabytes and takes the process with it.

    A DOCTYPE is the only place an entity can be declared, and a spreadsheet has
    no legitimate use for one -- Excel does not write one, and neither does
    `export.py`. So the declaration itself is the refusal, which is a check on
    the bytes rather than a hope about the parser.

    `defusedxml` would also do this. It is a dependency for a check that is one
    `in` on a byte string, on a file format that cannot contain the thing being
    checked for, so it stays out of the wheel.
    """
    head = data[:4096].lstrip()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data[:65536]:
        raise ImportFileError("That workbook declares XML entities, which are not accepted.")
    try:
        # `ElementTree.fromstring` on bytes it has already been shown carries no
        # DOCTYPE. S314's concern is the entity expansion the guard above makes
        # unreachable.
        return ElementTree.fromstring(data)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise ImportFileError(f"That workbook is not readable ({exc}).") from None


def _read_xlsx(content: bytes) -> tuple[list[str], list[list[str]]]:
    """The first worksheet of a workbook.

    Both string encodings are handled. `export.py` writes `t="inlineStr"`, which
    keeps the writer streaming; Excel itself writes `t="s"` with an index into a
    shared-string table. A reader that only understood one of them would refuse
    either its own exports or every file a person actually edited.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise ImportFileError("That file is not a readable .xlsx workbook.") from None

    with archive:
        # Before anything is read. A zip records the inflated size of every
        # entry in its directory, so a bomb can be refused on the strength of
        # what it claims rather than by watching memory disappear while it
        # proves it.
        declared = sum(info.file_size for info in archive.infolist())
        if declared > MAX_UNCOMPRESSED:
            raise ImportFileError(
                f"That workbook expands to {declared:,} bytes, and the limit is "
                f"{MAX_UNCOMPRESSED:,}."
            )

        names = archive.namelist()
        sheets = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))
        if not sheets:
            raise ImportFileError("That workbook has no worksheets in it.")

        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            table = _parse_xml(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in table.findall(f"{_SHEET_NS}si")]

        date_styles = _date_styles(archive, names)
        from_1904 = _uses_1904(archive, names)
        sheet = _parse_xml(archive.read(sheets[0]))

    rows: list[list[str]] = []
    for element in sheet.iter(f"{_SHEET_NS}row"):
        cells: list[str] = []
        for cell in element.findall(f"{_SHEET_NS}c"):
            # Sparse by design: a spreadsheet omits empty cells entirely, so the
            # reference is what says which column this one is. Without honouring
            # it, one blank cell shifts the whole rest of the row left by one.
            index = _column_index(cell.get("r", ""))
            while len(cells) < index:
                cells.append("")
            cells.append(_xlsx_cell(cell, shared, date_styles, from_1904=from_1904))
        if any(cell for cell in cells):
            rows.append(cells)

    if not rows:
        raise ImportFileError("That workbook has no rows in it.")

    # Ragged rows are normal -- a row whose last cells are empty simply ends
    # early -- and every row has to be the width of the header or the column
    # positions stop lining up with it.
    width = max(len(row) for row in rows)
    for row in rows:
        row.extend([""] * (width - len(row)))
    return [cell.strip() for cell in rows[0]], rows[1:]


def _column_index(reference: str) -> int:
    """`"C7"` -> 2. The inverse of `_column_name` in `export.py`."""
    letters = re.match(r"[A-Za-z]+", reference)
    if letters is None:
        return 0
    index = 0
    for character in letters.group(0).upper():
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index - 1


#: Number formats that mean a date or a time, by the id the format has when a
#: spreadsheet uses a built-in one. 14-22 are the dates and times everybody
#: knows; 27-36 and 50-58 are the East Asian locale calendars; 45-47 are
#: durations. Anything from 164 up is the file's own and has to be read.
_BUILTIN_DATE_FORMATS = frozenset({*range(14, 23), *range(27, 37), *range(45, 48), *range(50, 59)})

#: Anything left in a format code once the parts that are not the format itself
#: are removed: quoted literals, escaped characters, colour and condition
#: blocks, and the currency-ish `[$...]`.
_FORMAT_NOISE = re.compile(r'"[^"]*"|\\.|\[[^\]]*\]')


def _is_date_format(code: str) -> bool:
    """Whether a custom number format draws a date or a time.

    Read from the code rather than guessed from the id, because `yyyy-mm-dd` is
    a format a person makes in the format dialog and it lands somewhere above
    164 with no other clue about what it is. The literals are stripped first:
    `0.00" days"` is a number, and the letters in the word would otherwise make
    it look like one of these.
    """
    stripped = _FORMAT_NOISE.sub("", code).lower()
    return any(token in stripped for token in "ymdhs")


def _date_styles(archive: zipfile.ZipFile, names: list[str]) -> frozenset[int]:
    """Which cell-style indices carry a date format.

    A date in a spreadsheet is not a date. It is a *number* -- days since the
    epoch -- with a format applied, and the format lives in `styles.xml` under
    an index the cell refers to by its `s` attribute. Without reading it, a
    column of perfectly good dates arrives as five-figure integers, which is
    exactly what "46218" was.
    """
    if "xl/styles.xml" not in names:
        return frozenset()

    styles = _parse_xml(archive.read("xl/styles.xml"))
    custom = {
        int(node.get("numFmtId", "0")): node.get("formatCode", "")
        for node in styles.iter(f"{_SHEET_NS}numFmt")
    }

    dated: set[int] = set()
    formats = styles.find(f"{_SHEET_NS}cellXfs")
    if formats is None:
        return frozenset()

    for index, entry in enumerate(formats.findall(f"{_SHEET_NS}xf")):
        try:
            number_format = int(entry.get("numFmtId", "0"))
        except ValueError:
            continue
        if number_format in _BUILTIN_DATE_FORMATS or (
            number_format in custom and _is_date_format(custom[number_format])
        ):
            dated.add(index)
    return frozenset(dated)


def _uses_1904(archive: zipfile.ZipFile, names: list[str]) -> bool:
    """Whether the workbook counts days from 1904 rather than from 1900.

    A Mac convention old enough that most people have never met it, and cheap
    enough to honour that getting it wrong by four years and a day is not worth
    the saving.
    """
    if "xl/workbook.xml" not in names:
        return False
    workbook = _parse_xml(archive.read("xl/workbook.xml"))
    properties = workbook.find(f"{_SHEET_NS}workbookPr")
    return properties is not None and properties.get("date1904") in ("1", "true")


def _serial_to_text(serial: float, *, from_1904: bool) -> str:
    """A spreadsheet's day number as the text the form's own parsers read.

    The 1900 epoch is 1899-12-30, not 1900-01-01. Excel believes 1900 was a leap
    year -- it was not -- so serial 60 is a day that never existed, and shifting
    the epoch back two days is the standard way of making every serial from 61
    onwards land on the right date while leaving the ones below it alone.

    A whole number is a date and anything with a fraction is a date and a time,
    which is the distinction the cell itself is making: a person who formatted
    the column as a date gets `YYYY-MM-DD`, and `parse_value` takes both.
    """
    epoch = dt.date(1904, 1, 1) if from_1904 else dt.date(1899, 12, 30)
    days = int(serial)
    seconds = round((serial - days) * 86_400)
    # Rounding a hair under midnight lands on the next day.
    if seconds >= 86_400:
        days += 1
        seconds -= 86_400

    moment = dt.datetime.combine(epoch + dt.timedelta(days=days), dt.time()) + dt.timedelta(
        seconds=seconds
    )
    return moment.date().isoformat() if seconds == 0 else moment.isoformat(sep=" ")


def _xlsx_cell(
    cell: ElementTree.Element,
    shared: list[str],
    date_styles: frozenset[int],
    *,
    from_1904: bool,
) -> str:
    kind = cell.get("t")
    if kind == "s":
        value = cell.findtext(f"{_SHEET_NS}v")
        try:
            return shared[int(value or 0)]
        except (ValueError, IndexError):
            return ""
    if kind == "inlineStr":
        node = cell.find(f"{_SHEET_NS}is")
        return "".join(node.itertext()) if node is not None else ""

    text = (cell.findtext(f"{_SHEET_NS}v") or "").strip()

    # A numeric cell whose style says "date". This is the whole reason styles
    # are read at all: a spreadsheet turns the ISO date an export wrote into a
    # real date cell the moment somebody opens and saves the file, so without
    # this the round trip breaks on exactly the files people edit by hand.
    if not text or kind not in (None, "n"):
        return text
    try:
        style = int(cell.get("s", "-1"))
    except ValueError:
        return text
    if style not in date_styles:
        return text
    try:
        return _serial_to_text(float(text), from_1904=from_1904)
    except (ValueError, OverflowError):
        return text


# ---------------------------------------------------------------------------
# Turning rows of text into a plan
# ---------------------------------------------------------------------------


def _normalise(name: str) -> str:
    """A header down to what two spellings of the same column have in common.

    An export writes labels -- "Released on" -- and a person writing a file by
    hand writes field names -- `released_on`. Both have to land on the same
    field, or the round trip only works in one direction.
    """
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def importable_fields(spec: ModelSpec, admin: Any) -> list[FieldSpec]:
    """Fields a file may set, in declaration order.

    `FieldSpec.editable` first, because it is the mass-assignment boundary and
    an import is a write like any other -- a column the form would discard is a
    column a spreadsheet must not be able to set either. A `ModelAdmin` narrows
    it further with `import_fields`, and `readonly_fields` takes fields out.
    """
    declared = tuple(getattr(admin, "import_fields", ()) or ())
    writable = admin.editable_field_names()
    chosen: list[FieldSpec] = []
    for field in spec:
        if field.name not in writable or field.type in _UNIMPORTABLE:
            continue
        # A sensitive column is masked on its way out and never echoed into a
        # form, so a file cannot carry a meaningful value for one. Leaving it
        # importable would make a spreadsheet the one place in the admin where
        # an API key can be set in the clear, which is exactly the property
        # `FieldSpec.sensitive` exists to deny it.
        if field.sensitive:
            continue
        if declared and field.name not in declared:
            continue
        chosen.append(field)
    return chosen


def column_hint(field: FieldSpec) -> str:
    """What one column wants, in a sentence, for the template's second row.

    A template that is only a header row leaves everybody guessing at the shape
    of a duration, a range or a point -- the three that have no obvious spelling
    -- and guessing wrong is a failed import and a round trip through this
    error report.
    """
    if field.choices:
        return "one of: " + ", ".join(str(choice.value) for choice in field.choices)
    hints = {
        FieldType.BOOLEAN: "true or false",
        FieldType.DATE: "YYYY-MM-DD",
        FieldType.DATETIME: "YYYY-MM-DD HH:MM",
        FieldType.TIME: "HH:MM",
        FieldType.DURATION: "HH:MM:SS, or 2d HH:MM:SS",
        FieldType.UUID: "a UUID",
        FieldType.JSON: "JSON",
        FieldType.ARRAY: "values separated by commas",
        FieldType.HSTORE: "key: value per line",
        FieldType.RANGE: "[low, high)",
        FieldType.MULTIRANGE: "one range per line",
        FieldType.INET: "an IP address, or a network as 10.0.0.0/8",
        FieldType.MACADDR: "aa:bb:cc:dd:ee:ff",
        FieldType.BITS: "0s and 1s",
        FieldType.GEOMETRY: "latitude, longitude -- or WKT or GeoJSON",
        FieldType.EMAIL: "an email address",
    }
    if field.type in hints:
        return hints[field.type]
    if field.is_relation:
        # Whatever the related row is called, which is what the export wrote.
        return "the name of an existing row, or its id"
    if field.type is FieldType.DECIMAL and field.decimal_places:
        return f"a number with up to {field.decimal_places} decimal places"
    if field.max_length:
        return f"text, at most {field.max_length} characters"
    return ""


def build_plan(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    spec: ModelSpec,
    admin: Any,
) -> Plan:
    """Check a whole file, and report every cell that is wrong.

    Every cell, not the first: somebody who has to upload a spreadsheet ten
    times to be told about ten mistakes one at a time will edit the database
    directly instead, which is the thing this admin exists to make unnecessary.
    """
    plan = Plan()
    if len(rows) > MAX_ROWS:
        raise ImportFileError(
            f"That file has {len(rows):,} rows, and the limit is {MAX_ROWS:,}. "
            "Split it and import the parts."
        )

    fields = {field.name: field for field in importable_fields(spec, admin)}
    by_header: dict[str, FieldSpec | None] = {}
    key_columns: dict[int, str] = {}

    for index, header in enumerate(headers):
        matched = _match(header, fields, admin)
        if matched is None:
            # A primary key is not "importable" -- it is generated -- but it is
            # what makes a re-uploaded export update rather than duplicate.
            if _normalise(header) in {_normalise(name) for name in spec.primary_key}:
                key_columns[index] = _primary_key_name(header, spec)
                continue
            plan.ignored.append(header)
        else:
            plan.columns.append(matched.name)
        by_header[str(index)] = matched

    if not plan.columns:
        raise ImportFileError(
            "None of the columns in that file match this model. "
            f"Expected some of: {', '.join(sorted(fields))}."
        )

    for offset, raw in enumerate(rows):
        # A note to the reader rather than a row. The template's own hint row is
        # marked this way, so downloading the template and uploading it back is
        # a no-op instead of one parse error per column -- which is exactly what
        # it was, on the one file that exists to explain the format.
        if raw and raw[0].strip().startswith(COMMENT_PREFIX):
            continue

        # +2: the header is line 1, and a spreadsheet counts from 1 -- so the
        # first data row is line 2, which is the number down the left edge.
        row = Row(line=offset + 2)
        for index, cell in enumerate(raw):
            if index in key_columns:
                text = cell.strip()
                if text:
                    row.key = (text,)
                continue
            field = by_header.get(str(index))
            if field is None:
                continue
            _bind_cell(row, field, cell, plan)
        if row.values or row.relations or row.key:
            plan.rows.append(row)

    if not plan.rows:
        raise ImportFileError(
            "That file has its headings but no rows to import. Add a row under them.",
            tone="info",
        )
    return plan


def _primary_key_name(header: str, spec: ModelSpec) -> str:
    wanted = _normalise(header)
    for name in spec.primary_key:
        if _normalise(name) == wanted:
            return name
    return spec.primary_key[0]


def _match(header: str, fields: dict[str, FieldSpec], admin: Any) -> FieldSpec | None:
    """One header to one field, by name or by the label the export wrote."""
    wanted = _normalise(header)
    if not wanted:
        return None
    for name, field in fields.items():
        label = admin.field_label(name, field.label)
        if wanted in (_normalise(name), _normalise(label), _normalise(field.label)):
            return field
    return None


def _bind_cell(row: Row, field: FieldSpec, cell: str, plan: Plan) -> None:
    """One cell, through the same parser the change form uses."""
    text = cell.strip()

    if field.is_relation:
        # Resolved against the database by the caller. Blank clears the link,
        # which is a real edit and not a missing value.
        row.relations[field.name] = text
        return

    if not text:
        if field.required:
            plan.errors.append(RowError(row.line, field.label, "This column cannot be empty."))
            return
        row.values[field.name] = None
        return

    try:
        parsed = parse_value(text, field)
    except ValueError as exc:
        plan.errors.append(RowError(row.line, field.label, str(exc), _clip(text)))
        return

    problem = check_bounds(parsed, field)
    if problem:
        plan.errors.append(RowError(row.line, field.label, problem, _clip(text)))
        return

    row.values[field.name] = parsed


def _clip(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------


def template_rows(spec: ModelSpec, admin: Any, translate: Any) -> tuple[list[str], list[list[str]]]:
    """A header row and one row of hints, ready for any of the three writers.

    The hint row is why the template is worth downloading rather than typed from
    the field labels: a duration, a range and a point have no spelling anybody
    guesses right first time, and being told in the file beats being told by an
    error afterwards. It is a comment as far as the reader is concerned -- every
    cell in it fails to parse, so a template uploaded unchanged reports itself
    as one bad row rather than importing a row of instructions.
    """
    fields = importable_fields(spec, admin)
    headers = [translate(admin.field_label(f.name, f.label)) for f in fields]
    hints = [column_hint(f) for f in fields]

    # The primary key leads, so a downloaded export and a downloaded template
    # have the same first column and the same meaning for it: fill it in to
    # update an existing row, leave it out to create one.
    key = spec.primary_key[0] if spec.primary_key else ""
    if key:
        headers.insert(0, key)
        hints.insert(0, "leave empty to create, or an existing id to update")

    # Marked as a comment, so downloading the template and uploading it back is
    # a no-op rather than six parse errors. It was not, and the first thing
    # anybody does with a template is exactly that -- which made the file that
    # exists to explain the format the one file the format rejects.
    hints[0] = f"{COMMENT_PREFIX} {hints[0]}"
    return headers, [hints]


def iter_cells(plan: Plan) -> Iterator[tuple[int, str, Any]]:
    """Every parsed scalar, for a caller that wants to log what was written."""
    for row in plan.rows:
        for name, value in row.values.items():
            yield row.line, name, value
