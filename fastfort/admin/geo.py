"""WKB/EWKB <-> GeoJSON <-> EWKT, pure Python.

No Shapely, no GeoAlchemy2 import. `_check_spatial` in `orm/sqlalchemy/types.py`
already detects a spatial column by where its type class lives rather than by
importing GeoAlchemy2 -- this module keeps the same promise on the admin side:
the write path produces EWKT *text*, which every spatial backend accepts on
insert, so nothing here needs to construct a real geometry object to hand one
value to the ORM.

Replaces the point-only `_parse_point`/`_point_text` that used to live in
`forms.py`, extended from one geometry kind to all seven WKB knows how to
carry: `POINT`, `LINESTRING`, `POLYGON`, `MULTIPOINT`, `MULTILINESTRING`,
`MULTIPOLYGON`, `GEOMETRYCOLLECTION`.
"""

from __future__ import annotations

import binascii
import itertools
import json
import math
import re
import struct
from typing import Any

from fastfort.spec import FieldSpec

__all__ = ["decode", "parse", "render", "summarise", "to_ewkt"]

# ---------------------------------------------------------------------------
# WKB/EWKB -> GeoJSON
# ---------------------------------------------------------------------------

_POINT = 1
_LINESTRING = 2
_POLYGON = 3
_MULTIPOINT = 4
_MULTILINESTRING = 5
_MULTIPOLYGON = 6
_GEOMETRYCOLLECTION = 7

_TYPE_NAMES = {
    _POINT: "Point",
    _LINESTRING: "LineString",
    _POLYGON: "Polygon",
    _MULTIPOINT: "MultiPoint",
    _MULTILINESTRING: "MultiLineString",
    _MULTIPOLYGON: "MultiPolygon",
    _GEOMETRYCOLLECTION: "GeometryCollection",
}

# EWKB's extension flags, packed into the high bits of the type word -- plain
# ISO WKB never sets these, so reading them as 0 is exactly reading plain WKB.
_SRID_FLAG = 0x20000000
_Z_FLAG = 0x80000000
_M_FLAG = 0x40000000

#: Errors that mean "the bytes ran out, or made no sense" while walking a
#: geometry -- every one of them means `decode` returns `None` rather than
#: raising, per this module's one rule: a shape a form cannot parse must not
#: be able to take the page down either.
_DECODE_ERRORS = (struct.error, IndexError, ValueError, KeyError, TypeError)


class _Reader:
    """A cursor over one WKB/EWKB geometry's bytes."""

    __slots__ = ("data", "order", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.order = "<"

    def read_byte_order(self) -> None:
        (flag,) = struct.unpack_from("B", self.data, self.pos)
        self.pos += 1
        self.order = "<" if flag == 1 else ">"

    def uint32(self) -> int:
        (value,) = struct.unpack_from(f"{self.order}I", self.data, self.pos)
        self.pos += 4
        return int(value)

    def doubles(self, count: int) -> tuple[float, ...]:
        values = struct.unpack_from(f"{self.order}{count}d", self.data, self.pos)
        self.pos += 8 * count
        return values


def _read_geometry(reader: _Reader) -> dict[str, Any]:
    """One WKB/EWKB geometry, starting at its own byte-order marker.

    Recurses for `GEOMETRYCOLLECTION` and for the members of a MULTI* type --
    each member of a PostGIS MULTI* is a complete mini-WKB with its own byte
    order and type word, not a bare coordinate list, and only the outermost
    geometry of an EWKB blob ever carries the SRID flag.
    """
    reader.read_byte_order()
    type_word = reader.uint32()
    has_z = bool(type_word & _Z_FLAG)
    has_m = bool(type_word & _M_FLAG)
    has_srid = bool(type_word & _SRID_FLAG)
    code = type_word & 0xFF  # the low byte: 1-7, one of `_TYPE_NAMES`

    if has_srid:
        reader.uint32()  # SRID itself is not part of the GeoJSON shape

    dims = 2 + has_z + has_m
    keep = 2 + has_z  # M, when present, is always the last value -- drop it

    def read_point() -> list[float]:
        return list(reader.doubles(dims)[:keep])

    def read_ring() -> list[list[float]]:
        return [read_point() for _ in range(reader.uint32())]

    if code == _POINT:
        coordinates: Any = read_point()
    elif code == _LINESTRING:
        coordinates = read_ring()
    elif code == _POLYGON:
        coordinates = [read_ring() for _ in range(reader.uint32())]
    elif code in (_MULTIPOINT, _MULTILINESTRING, _MULTIPOLYGON):
        coordinates = [_read_geometry(reader)["coordinates"] for _ in range(reader.uint32())]
    elif code == _GEOMETRYCOLLECTION:
        geometries = [_read_geometry(reader) for _ in range(reader.uint32())]
        return {"type": "GeometryCollection", "geometries": geometries}
    else:
        raise ValueError(f"Unsupported WKB geometry code {code}")

    return {"type": _TYPE_NAMES[code], "coordinates": coordinates}


def decode(raw: Any) -> dict[str, Any] | None:
    """WKB or EWKB hex into a GeoJSON-shaped `{"type": ..., "coordinates": ...}`.

    `raw` is usually a GeoAlchemy2 `WKBElement`, whose `str()` is the hex this
    decodes -- read off `.desc` rather than imported, the same way
    `_check_spatial` in `types.py` detects GeoAlchemy2 without depending on it.
    Never raises: an undecodable value (a type this module does not know, bytes
    that run out early, plain garbage) returns `None` so the caller can fall
    back to displaying the value as-is rather than losing the page.
    """
    text = getattr(raw, "desc", None) or (raw if isinstance(raw, str) else str(raw))
    try:
        data = binascii.unhexlify(text)
    except (binascii.Error, ValueError, TypeError):
        return None
    try:
        return _read_geometry(_Reader(data))
    except _DECODE_ERRORS:
        return None


# ---------------------------------------------------------------------------
# GeoJSON -> WKT / EWKT
# ---------------------------------------------------------------------------


def _num(value: float) -> str:
    text = f"{value:.9f}".rstrip("0").rstrip(".")
    return text or "0"


def _coord_text(point: list[float]) -> str:
    return " ".join(_num(c) for c in point)


def _ring_text(ring: list[list[float]]) -> str:
    return ", ".join(_coord_text(point) for point in ring)


def _wkt_body(geojson: dict[str, Any]) -> str:
    kind = geojson.get("type")
    if kind == "Point":
        return f"POINT({_coord_text(geojson['coordinates'])})"
    if kind == "LineString":
        return f"LINESTRING({_ring_text(geojson['coordinates'])})"
    if kind == "Polygon":
        rings = ", ".join(f"({_ring_text(ring)})" for ring in geojson["coordinates"])
        return f"POLYGON({rings})"
    if kind == "MultiPoint":
        points = ", ".join(f"({_coord_text(p)})" for p in geojson["coordinates"])
        return f"MULTIPOINT({points})"
    if kind == "MultiLineString":
        lines = ", ".join(f"({_ring_text(line)})" for line in geojson["coordinates"])
        return f"MULTILINESTRING({lines})"
    if kind == "MultiPolygon":
        polygons = ", ".join(
            "(" + ", ".join(f"({_ring_text(ring)})" for ring in polygon) + ")"
            for polygon in geojson["coordinates"]
        )
        return f"MULTIPOLYGON({polygons})"
    if kind == "GeometryCollection":
        members = ", ".join(_wkt_body(member) for member in geojson["geometries"])
        return f"GEOMETRYCOLLECTION({members})"
    raise ValueError(f"Unsupported GeoJSON type {kind!r}")


def to_ewkt(geojson: dict[str, Any], srid: int) -> str:
    """A GeoJSON-shaped dict into `SRID=<srid>;<WKT>`.

    Text, not a geometry object -- every spatial backend accepts EWKT on
    write, and building an actual geometry would mean importing GeoAlchemy2 or
    Shapely, which this module exists to avoid.
    """
    return f"SRID={srid};{_wkt_body(geojson)}"


# ---------------------------------------------------------------------------
# Text -> GeoJSON (the write path: lat/lng, WKT, EWKT, or GeoJSON)
# ---------------------------------------------------------------------------


def _split_top_level(text: str) -> list[str]:
    """Split on commas that sit outside every level of parentheses.

    The one primitive the WKT reader below is built from: a ring's points, a
    polygon's rings, a MULTI*'s members and a collection's geometries are all
    "comma-separated things that may themselves contain commas in parens", and
    a plain `str.split(",")` cannot tell those two kinds of comma apart.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [part for part in parts if part]


def _strip_parens(text: str) -> str:
    """Remove one layer of parentheses, but only when it wraps the *whole*
    string as a single group.

    `"(1 2), (3 4)"` starts with `(` and ends with `)` too, but stripping those
    would merge two points into one broken string -- the depth has to return
    to zero only once, at the very last character, for the wrap to be real.
    """
    text = text.strip()
    if not (text.startswith("(") and text.endswith(")")):
        return text
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return text
    return text[1:-1].strip()


def _parse_coord(text: str) -> list[float]:
    parts = _strip_parens(text).split()
    if not parts:
        raise ValueError("empty coordinate")
    return [float(part) for part in parts[:3]]  # X, Y, optional Z -- WKT's M is dropped


def _parse_ring(text: str) -> list[list[float]]:
    body = _strip_parens(text)
    return [_parse_coord(part) for part in _split_top_level(body)]


def _parse_polygon(text: str) -> list[list[list[float]]]:
    return [_parse_ring(part) for part in _split_top_level(text)]


_WKT_RE = re.compile(r"^([A-Za-z]+)\s*(?:Z|M|ZM)?\s*\((.*)\)$", re.IGNORECASE | re.DOTALL)


def _try_wkt(text: str) -> dict[str, Any] | None:
    match = _WKT_RE.match(text.strip())
    if not match:
        return None
    keyword, body = match.group(1).upper(), match.group(2)
    try:
        if keyword == "POINT":
            return {"type": "Point", "coordinates": _parse_coord(body)}
        if keyword == "LINESTRING":
            return {"type": "LineString", "coordinates": _parse_ring(body)}
        if keyword == "POLYGON":
            return {"type": "Polygon", "coordinates": _parse_polygon(body)}
        if keyword == "MULTIPOINT":
            return {"type": "MultiPoint", "coordinates": _parse_ring(body)}
        if keyword == "MULTILINESTRING":
            return {
                "type": "MultiLineString",
                "coordinates": [_parse_ring(part) for part in _split_top_level(body)],
            }
        if keyword == "MULTIPOLYGON":
            return {
                "type": "MultiPolygon",
                "coordinates": [
                    _parse_polygon(_strip_parens(part)) for part in _split_top_level(body)
                ],
            }
        if keyword == "GEOMETRYCOLLECTION":
            members = [_try_wkt(part) for part in _split_top_level(body)]
            if any(member is None for member in members):
                return None
            return {"type": "GeometryCollection", "geometries": members}
    except (ValueError, IndexError):
        return None
    return None


_EWKT_SRID_RE = re.compile(r"^SRID=(-?\d+)\s*;\s*", re.IGNORECASE)


def _split_srid_prefix(text: str, default_srid: int) -> tuple[int, str]:
    match = _EWKT_SRID_RE.match(text)
    if match:
        return int(match.group(1)), text[match.end() :].strip()
    return default_srid, text


def _try_geojson(text: str) -> dict[str, Any] | None:
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or "type" not in payload:
        return None
    if payload.get("type") == "GeometryCollection":
        return payload if "geometries" in payload else None
    return payload if "coordinates" in payload else None


def _try_lat_lng(text: str, srid: int) -> dict[str, Any] | None:
    """`lat, lng` -- latitude first, the order every map, phone and street sign
    uses, even though WKT itself is longitude first. That reversal is exactly
    the trap this function converts away from: get it backwards here and every
    point in the database is silently mirrored across the equator and the
    date line.

    Returns `None` (not a raise) when the text is not shaped like two numbers,
    so `parse` can fall through to WKT/GeoJSON. Once it *is* two numbers, an
    out-of-range value raises immediately rather than falling through, because
    "91, 0" was clearly meant as a coordinate, wrongly.
    """
    parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if len(parts) != 2:
        return None
    try:
        latitude, longitude = (float(part) for part in parts)
    except ValueError:
        return None
    if srid == 4326:
        # Meaningless outside EPSG:4326 -- in a projected system like 3857 the
        # numbers are metres, not degrees, and 91 is an ordinary coordinate.
        if not -90 <= latitude <= 90:
            raise ValueError("Latitude runs from -90 to 90.")
        if not -180 <= longitude <= 180:
            raise ValueError("Longitude runs from -180 to 180.")
    # GeoJSON is [longitude, latitude] -- RFC 7946, the one place the order
    # flips relative to the "latitude first" rule everywhere else here.
    return {"type": "Point", "coordinates": [longitude, latitude]}


def _check_kind(geojson: dict[str, Any], kind: str) -> None:
    """Reject a shape the column was not declared to hold.

    Writing a POLYGON into a `Geometry("POINT")` column is a database error --
    caught here first, with a message that names what the column actually
    wants, instead of a raw driver exception.
    """
    if kind == "GEOMETRY":
        return
    actual = str(geojson.get("type", "")).upper()
    if actual != kind.upper():
        raise ValueError(f"This column takes a {kind.title()}, not a {geojson.get('type')}.")


def parse(text: str, spec: FieldSpec) -> str:
    """A submitted geometry value into the EWKT text a spatial column accepts.

    Tried in this order: `lat, lng`, WKT, EWKT, or GeoJSON. Returned as text
    rather than a geometry object, for the same reason `decode` never imports
    a geometry library: the write path must not depend on a package the
    project may not have installed.
    """
    stripped = text.strip()
    srid = spec.geometry.srid if spec.geometry else 4326
    kind = spec.geometry.kind if spec.geometry else "GEOMETRY"

    point = _try_lat_lng(stripped, srid)
    if point is not None:
        _check_kind(point, kind)
        return to_ewkt(point, srid)

    geojson = _try_geojson(stripped)
    if geojson is not None:
        _check_kind(geojson, kind)
        return to_ewkt(geojson, srid)

    text_srid, body = _split_srid_prefix(stripped, srid)
    parsed = _try_wkt(body)
    if parsed is not None:
        _check_kind(parsed, kind)
        return to_ewkt(parsed, text_srid)

    raise ValueError("Enter a latitude and a longitude (41.2995, 69.2401), WKT, or GeoJSON.")


# ---------------------------------------------------------------------------
# Rendering -- the form control (round-trips through `parse`) and the list
# cell (a human summary that does not have to).
# ---------------------------------------------------------------------------


def render(value: Any, spec: FieldSpec) -> str:
    """The text a geometry's form control shows.

    A point renders as `lat, lng` -- the shape the box also accepts back, and
    the one every map, phone and street sign uses. Anything else renders as
    EWKT instead of a summary: "Polygon · 14 points" reads well in a list cell
    (see `summarise`) but `parse` cannot read it back, and a value nobody
    touched must still save cleanly when the form is submitted unchanged.
    """
    geojson = decode(value)
    if geojson is None:
        return str(value)
    if geojson.get("type") == "Point":
        longitude, latitude = geojson["coordinates"][:2]
        return f"{latitude:g}, {longitude:g}"
    srid = spec.geometry.srid if spec.geometry else 4326
    return to_ewkt(geojson, srid)


_EARTH_RADIUS_KM = 6371.0088


def _haversine_km(a: list[float], b: list[float]) -> float:
    lon1, lat1, lon2, lat2 = (
        math.radians(a[0]),
        math.radians(a[1]),
        math.radians(b[0]),
        math.radians(b[1]),
    )
    d_lat = lat2 - lat1
    d_lon = lon2 - lon1
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, h)))


def _route_km(coordinates: list[list[float]]) -> float:
    return sum(_haversine_km(a, b) for a, b in itertools.pairwise(coordinates))


def summarise(value: Any) -> str:
    """A one-line description of a geometry, for a list cell.

    Falls back to `str(value)` for anything `decode` cannot make sense of --
    the raw WKB hex is ugly, but a list cell must not raise over a value it
    cannot summarise; that is the same rule `decode` itself follows.
    """
    geojson = decode(value)
    if geojson is None:
        return str(value)
    kind = geojson.get("type")
    if kind == "Point":
        longitude, latitude = geojson["coordinates"][:2]
        return f"{latitude:g}, {longitude:g}"
    if kind == "LineString":
        return f"Route · {_route_km(geojson['coordinates']):.1f} km"
    if kind == "Polygon":
        points = sum(len(ring) for ring in geojson["coordinates"])
        return f"Polygon · {points} points"
    if kind == "MultiPoint":
        return f"Multipoint · {len(geojson['coordinates'])} points"
    if kind == "MultiLineString":
        return f"Multiline · {len(geojson['coordinates'])} parts"
    if kind == "MultiPolygon":
        return f"Multipolygon · {len(geojson['coordinates'])} parts"
    if kind == "GeometryCollection":
        return f"Collection · {len(geojson['geometries'])} shapes"
    return str(value)
