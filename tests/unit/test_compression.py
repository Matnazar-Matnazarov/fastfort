"""Choosing a content encoding, and producing it.

Pure functions over a string and an `Accept-Encoding` header, so they live here
rather than in a suite that stands a database up three times to test arithmetic.
The half that needs a running admin -- headers, `Vary`, and the pages that are
deliberately left uncompressed -- is in `tests/ui/test_assets.py`.
"""

from __future__ import annotations

from fastfort.ui.compression import available_encodings, compress_asset, negotiate_encoding

# ---------------------------------------------------------------------------
# Choosing an encoding
# ---------------------------------------------------------------------------


def test_brotli_is_preferred_where_the_browser_takes_it() -> None:
    """Every browser released in the last eight years asks for it, and on this
    kind of text it is around a sixth smaller than gzip."""
    assert negotiate_encoding("gzip, deflate, br, zstd") == "br"


def test_gzip_is_the_fallback_for_anything_older() -> None:
    assert negotiate_encoding("gzip, deflate") == "gzip"


def test_a_client_that_asks_for_neither_gets_neither() -> None:
    assert negotiate_encoding("") == "identity"
    assert negotiate_encoding("identity") == "identity"


def test_a_refused_encoding_is_not_used() -> None:
    """`br;q=0` says "not Brotli". Reading the token as consent would send a
    body the client has just said it cannot decode."""
    assert negotiate_encoding("br;q=0, gzip") == "gzip"


def test_a_wildcard_is_accepted() -> None:
    assert negotiate_encoding("*") in available_encodings()


def test_compression_actually_makes_it_smaller() -> None:
    text = "body { color: red; }\n" * 400
    raw = len(text.encode("utf-8"))

    zipped, encoding = compress_asset(text, "gzip")
    assert encoding == "gzip"
    assert len(zipped) < raw / 4

    if "br" in available_encodings():
        brotli, encoding = compress_asset(text, "br, gzip")
        assert encoding == "br"
        assert len(brotli) <= len(zipped)


def test_something_too_small_is_left_alone() -> None:
    """Below a few hundred bytes the framing costs more than it saves."""
    body, encoding = compress_asset("h1 { color: red }", "br, gzip")

    assert encoding == ""
    assert body == b"h1 { color: red }"
