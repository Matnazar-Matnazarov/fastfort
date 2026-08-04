"""Compressing the admin's static assets.

Brotli where the browser takes it, gzip where it does not, and the bytes
themselves where neither is offered. Both are produced once and kept, because
these files ship inside the wheel and cannot change while the process is
running -- so the cost is one compression per asset per encoding for the life of
the deployment, not one per request.

Brotli is worth the branch: on this kind of text it comes out around a sixth
smaller than gzip at the same wall-clock cost to decompress, and every browser
released in the last eight years asks for it. It is an optional dependency
rather than a required one -- `pip install "fastfort[compression]"` -- because a
project behind a proxy that already does this needs nothing here, and a missing
package should mean "gzip, then" rather than a failed import.

Only static assets go through this, deliberately. The admin's HTML is not
compressed, and that is a security decision rather than an oversight: a page
carries a CSRF token *and* text the request chose (a search term, a filter
value), and compressing a response containing both is the BREACH side channel --
the compressed length leaks how much of the attacker's guess matched the secret.
Reverse proxies that compress everything by default have the same problem; the
admin's own routes simply do not offer them the opportunity.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

__all__ = ["available_encodings", "compress_asset", "negotiate_encoding"]

#: The Brotli implementation, if the project installed one. `brotli` is the
#: reference binding and `brotlicffi` the PyPy-friendly one; they share this
#: much of an interface, so either will do.
_brotli: Any = None
for _module in ("brotli", "brotlicffi"):
    try:
        _brotli = __import__(_module)
    except ImportError:
        continue
    break

#: Best first. A browser's `Accept-Encoding` is a set rather than a ranking in
#: practice, so the server's preference is what decides.
_PREFERRED = ("br", "gzip")

#: Brotli quality. 11 is the maximum and is slow enough to be a bad default for
#: a response compressed per request -- but these are compressed once per
#: process, so the smallest possible output costs a few milliseconds at start-up
#: and is served for the life of the deployment.
_BROTLI_QUALITY = 11

#: Below this there is nothing to win: the encoding headers and the framing cost
#: more than the compression saves on a file this size.
_MINIMUM = 512


def available_encodings() -> tuple[str, ...]:
    """The encodings this installation can actually produce, best first."""
    return tuple(name for name in _PREFERRED if name != "br" or _brotli is not None)


def negotiate_encoding(accept_encoding: str) -> str:
    """The best encoding the client asked for and this process can produce.

    Deliberately literal about what it matches. A client that sends `br;q=0`
    is saying it does not want Brotli, and treating the presence of the token as
    consent would serve it something it cannot read.
    """
    offers: dict[str, float] = {}
    for part in accept_encoding.lower().split(","):
        token, _, parameters = part.strip().partition(";")
        quality = 1.0
        for parameter in parameters.split(";"):
            key, _, value = parameter.partition("=")
            if key.strip() == "q":
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        offers[token.strip()] = quality

    for name in available_encodings():
        if offers.get(name, 0.0) > 0 or offers.get("*", 0.0) > 0:
            return name
    return "identity"


@lru_cache(maxsize=32)
def _encode(payload: bytes, encoding: str) -> bytes:
    if encoding == "br" and _brotli is not None:
        return bytes(_brotli.compress(payload, quality=_BROTLI_QUALITY))
    if encoding == "gzip":
        import gzip

        # `mtime=0` so the same input always produces the same bytes: an ETag
        # derived from the response would otherwise change on every restart.
        return gzip.compress(payload, compresslevel=9, mtime=0)
    return payload


def compress_asset(text: str, accept_encoding: str, *, enabled: bool = True) -> tuple[bytes, str]:
    """Return the bytes to send and the `Content-Encoding` to send them under.

    An encoding of `""` means send them as they are, which is also what a caller
    gets when compression is switched off or the payload is too small to be
    worth it.
    """
    payload = text.encode("utf-8")
    if not enabled or len(payload) < _MINIMUM:
        return payload, ""

    encoding = negotiate_encoding(accept_encoding)
    if encoding == "identity":
        return payload, ""

    encoded = _encode(payload, encoding)
    # A compression that made the file bigger is one worth not applying. Rare on
    # text, but free to check and it keeps the promise the header makes honest.
    if len(encoded) >= len(payload):
        return payload, ""
    return encoded, encoding
