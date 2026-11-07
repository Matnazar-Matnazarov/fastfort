"""An uploaded workbook cannot declare XML entities, wherever it hides them.

The XLSX importer parses each part with the standard library, which expands
internal entities: a few hundred bytes of nested declarations ("billion
laughs") expand to gigabytes and take the worker down. Up to 1.1.1 the guard
searched the first 4 KB of bytes for `<!DOCTYPE`, and two cheap shapes walked
past it. These are those two shapes, and the ordinary part that must still read.
"""

from __future__ import annotations

import pytest

from fastfort.admin.importing import ImportFileError, _parse_xml

DTD = '<!DOCTYPE r [<!ENTITY a "aa"><!ENTITY b "&a;&a;&a;&a;">]><r>&b;</r>'


def test_a_declaration_after_a_long_comment_is_refused() -> None:
    padded = b'<?xml version="1.0"?><!--' + b"x" * 70_000 + b"-->" + DTD.encode()

    with pytest.raises(ImportFileError, match="declares XML entities"):
        _parse_xml(padded)


def test_a_declaration_in_utf16_is_refused() -> None:
    utf16 = ('<?xml version="1.0" encoding="UTF-16"?>' + DTD).encode("utf-16")

    with pytest.raises(ImportFileError, match="declares XML entities"):
        _parse_xml(utf16)


def test_a_plain_declaration_is_refused() -> None:
    with pytest.raises(ImportFileError, match="declares XML entities"):
        _parse_xml(('<?xml version="1.0"?>' + DTD).encode())


def test_an_ordinary_part_still_reads() -> None:
    root = _parse_xml(b'<?xml version="1.0"?><sheetData><row r="1"/></sheetData>')

    assert root.tag == "sheetData"
