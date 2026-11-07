"""CSV export cannot carry a spreadsheet formula (CWE-1236).

Anyone who can write a row -- through the admin, an import, or the project's own
API -- chooses the text of a cell. Exported to CSV and opened in a spreadsheet,
a cell starting with `=` is a formula: `=HYPERLINK("https://evil", "Invoice")`
is a phishing link in a trusted file, and DDE payloads have run commands. The
XLSX export is immune by construction (every string is an `inlineStr`, never an
`<f>`); CSV has no types, so the text itself has to be defused.
"""

from __future__ import annotations

import csv
import io

import pytest

from fastfort.admin.export import defuse, stream_csv
from fastfort.admin.importing import _read_csv


@pytest.mark.parametrize(
    "payload",
    [
        '=HYPERLINK("https://evil.example","Invoice")',
        "+1+cmd|' /C calc'!A0",
        "-2+3",
        "@SUM(A1:A9)",
        "\t=1+1",
        "\r=1+1",
    ],
)
def test_a_formula_goes_out_as_text(payload: str) -> None:
    body = b"".join(stream_csv(["name"], [[payload]])).decode("utf-8-sig")
    cell = next(iter(csv.reader(io.StringIO(body[body.index("\n") + 1 :]))))[0]

    assert cell == "'" + payload


def test_numbers_and_ordinary_text_are_untouched() -> None:
    assert defuse(-5) == -5
    assert defuse(-1.5) == -1.5
    assert defuse("Pixel Phone") == "Pixel Phone"
    assert defuse("") == ""


def test_an_export_uploaded_back_is_the_data_that_was_exported() -> None:
    """The apostrophe is taken off on import, or a round trip would write it into
    every such row -- and only before a trigger, so "'90s hits" survives."""
    rows = [["=1+1"], ["-2"], ["'90s hits"]]
    body = b"".join(stream_csv(["name"], rows))

    _, read = _read_csv(body)

    assert read == rows
