"""Column types the browser has no native control for.

A duration, an array and a geometry all end up in a plain text box, so each one
needs a shape it accepts and a shape it renders. All three used to degrade to a
read-only row -- or worse, in the geometry's case, print raw WKB hex at whoever
opened the page.
"""

from __future__ import annotations

import datetime as dt

import pytest

from fastfort.admin.forms import _parse_point, _point_text, widget_for
from fastfort.spec import FieldSpec, FieldType

# A point in Tashkent, as EWKB with an SRID prefix and as plain WKB without one.
TASHKENT_EWKB = "0101000020E610000041F163CC5D4F51407593180456A64440"
TASHKENT_WKB = "010100000041f163cc5d4f51407593180456a64440"


def field(name: str, kind: FieldType) -> FieldSpec:
    return FieldSpec(name=name, label=name.title(), type=kind)


# ---------------------------------------------------------------------------
# Which control each type gets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (FieldType.DURATION, "duration"),
        (FieldType.ARRAY, "list"),
        (FieldType.GEOMETRY, "point"),
    ],
)
def test_each_exotic_type_has_a_control(kind: FieldType, expected: str) -> None:
    """Not `readonly`. These are editable columns, and rendering them read-only
    made them uneditable through the admin for no reason anyone could see."""
    assert widget_for(field("x", kind)) == expected


# ---------------------------------------------------------------------------
# Durations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("01:30:00", dt.timedelta(hours=1, minutes=30)),
        ("1:30", dt.timedelta(minutes=1, seconds=30)),
        ("90", dt.timedelta(seconds=90)),
        ("2d 03:00:00", dt.timedelta(days=2, hours=3)),
        ("00:00:01.5", dt.timedelta(seconds=1.5)),
    ],
)
def test_a_duration_reads_the_shapes_people_write(text: str, expected: dt.timedelta) -> None:
    """Right-aligned, so "90" is ninety seconds rather than ninety hours."""
    from fastfort.admin.forms import _parse_duration

    assert _parse_duration(text) == expected


def test_a_duration_that_is_not_one_says_so() -> None:
    from fastfort.admin.forms import _parse_duration

    with pytest.raises(ValueError, match="HH:MM:SS"):
        _parse_duration("about an hour")


def test_a_duration_round_trips_through_its_control() -> None:
    """What the box renders has to be what the box accepts back."""
    from fastfort.admin.forms import _parse_duration, _render

    original = dt.timedelta(days=2, hours=4, minutes=15)
    rendered = _render(original, field("runs_for", FieldType.DURATION))
    assert _parse_duration(rendered) == original


# ---------------------------------------------------------------------------
# Arrays
# ---------------------------------------------------------------------------


def test_an_array_is_comma_separated_both_ways() -> None:
    from fastfort.admin.forms import _parse, _render

    spec = field("keywords", FieldType.ARRAY)
    assert _render(["alpha", "beta"], spec) == "alpha, beta"
    assert _parse("alpha, beta", spec) == ["alpha", "beta"]


def test_an_array_drops_blank_entries() -> None:
    """ "a, b," is a typo, not a three-element list with an empty string in it."""
    from fastfort.admin.forms import _parse

    assert _parse("a, b, ,", field("keywords", FieldType.ARRAY)) == ["a", "b"]


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------


def test_a_point_is_written_latitude_first() -> None:
    """Every map, phone and street sign puts latitude first. WKT does not, which
    is exactly the trap this converts away from."""
    assert _parse_point("41.2995, 69.2401") == "SRID=4326;POINT(69.2401 41.2995)"


@pytest.mark.parametrize("raw", [TASHKENT_EWKB, TASHKENT_WKB])
def test_a_point_decodes_without_a_geometry_library(raw: str) -> None:
    """Both with and without the EWKB SRID prefix, and with no Shapely present.

    The alternative was printing the hex, which reads as corruption to anyone
    who opens the page.
    """
    assert _point_text(raw) == "41.2995, 69.2401"


def test_a_point_round_trips() -> None:
    assert _parse_point(_point_text(TASHKENT_EWKB)) == "SRID=4326;POINT(69.2401 41.2995)"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nowhere", "latitude and a longitude"),
        ("41.2995", "latitude and a longitude"),
        ("91, 0", "-90 to 90"),
        ("0, 200", "-180 to 180"),
    ],
)
def test_a_point_that_is_not_one_says_why(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _parse_point(text)


def test_an_undecodable_geometry_falls_back_rather_than_raising() -> None:
    """A polygon is not editable as two numbers, but it must not take the page
    down either."""
    assert _point_text("not hex at all") == "not hex at all"
