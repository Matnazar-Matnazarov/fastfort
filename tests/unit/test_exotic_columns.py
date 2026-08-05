"""Column types the browser has no native control for.

A duration, an array, a range and a handful of PostgreSQL scalars each need a
shape they accept and a shape they render -- what this file tests is that
shape, at the `parse_value`/`render_value` level, independent of which control
draws it (that half lives in `tests/ui/test_admin_widgets.py`, which drives a
real form). Geometry has its own file, `test_geo_codec.py`, since the codec
underneath it is large enough to want fixtures of its own.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from fastfort.admin.values import (
    check_bounds,
    duration_text,
    parse_duration,
    parse_value,
    render_value,
)
from fastfort.admin.widgets import widget_for
from fastfort.spec import FieldSpec, FieldType, RangeSpec


def field(name: str, kind: FieldType, **kwargs: object) -> FieldSpec:
    return FieldSpec(name=name, label=name.title(), type=kind, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Which control each type gets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (FieldType.DURATION, "duration"),
        (FieldType.ARRAY, "tags"),
        (FieldType.GEOMETRY, "geometry"),
        (FieldType.INET, "inet"),
        (FieldType.MACADDR, "mac"),
        (FieldType.MONEY, "money"),
        (FieldType.BITS, "bits"),
        # RANGE and MULTIRANGE share one widget -- `range_control` in
        # `_widgets.html` is what actually tells them apart, by `FieldType`
        # rather than by widget name; see `widgets.py`'s `_WIDGETS` docstring.
        (FieldType.RANGE, "range"),
        (FieldType.MULTIRANGE, "range"),
        (FieldType.HSTORE, "keyvalue"),
        (FieldType.BINARY, "readonly"),
        (FieldType.SEARCH_VECTOR, "readonly"),
    ],
)
def test_each_exotic_type_has_a_control(kind: FieldType, expected: str) -> None:
    """Not `readonly` for the editable ones. These are writable columns, and
    rendering them read-only made them uneditable through the admin for no
    reason anyone could see."""
    assert widget_for(field("x", kind)) == expected


def test_the_old_widget_names_still_validate() -> None:
    """`formfield_overrides = {"keywords": "list"}`, written before `ARRAY` had
    a widget of its own, must not turn into a start-up error on upgrade."""
    from fastfort.admin.widgets import WIDGET_NAMES, canonical_widget

    assert "list" in WIDGET_NAMES
    assert "point" in WIDGET_NAMES
    assert canonical_widget("list") == "tags"
    assert canonical_widget("point") == "geometry"
    # Unknown names pass through unchanged -- `canonical_widget` only ever
    # redirects the two names this phase renamed.
    assert canonical_widget("richtext") == "richtext"


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
    assert parse_duration(text) == expected


def test_a_duration_that_is_not_one_says_so() -> None:
    with pytest.raises(ValueError, match="HH:MM:SS"):
        parse_duration("about an hour")


def test_a_duration_round_trips_through_its_control() -> None:
    """What the box renders has to be what the box accepts back."""
    original = dt.timedelta(days=2, hours=4, minutes=15)
    rendered = render_value(original, field("runs_for", FieldType.DURATION))
    assert parse_duration(rendered) == original
    assert render_value(original, field("runs_for", FieldType.DURATION)) == duration_text(original)


# ---------------------------------------------------------------------------
# Arrays
# ---------------------------------------------------------------------------


def test_an_array_is_comma_separated_both_ways() -> None:
    spec = field("keywords", FieldType.ARRAY)
    assert render_value(["alpha", "beta"], spec) == "alpha, beta"
    assert parse_value("alpha, beta", spec) == ["alpha", "beta"]


def test_an_array_drops_blank_entries() -> None:
    """ "a, b," is a typo, not a three-element list with an empty string in it."""
    assert parse_value("a, b, ,", field("keywords", FieldType.ARRAY)) == ["a", "b"]


def test_an_array_with_an_item_spec_parses_each_entry() -> None:
    """`ARRAY(Integer)` rejects "banana" the same way a bare INTEGER column
    would, not by silently keeping it as a string."""
    item = field("item", FieldType.INTEGER)
    spec = field("scores", FieldType.ARRAY, item=item)
    assert parse_value("1, 2, 3", spec) == [1, 2, 3]


def test_an_array_names_the_offending_entry_and_its_position() -> None:
    item = field("item", FieldType.INTEGER)
    spec = field("scores", FieldType.ARRAY, item=item)
    with pytest.raises(ValueError, match=r'Entry 2 \("banana"\)'):
        parse_value("1, banana, 3", spec)


def test_an_arrays_max_length_applies_per_entry() -> None:
    """The old bounds check only ever bounded a `str`, so it never fired for an
    ARRAY at all -- `value` there is a `list`."""
    spec = field("tags", FieldType.ARRAY, max_length=3)
    error = check_bounds(["ok", "toolong"], spec)
    assert error is not None
    assert "toolong" in error


# ---------------------------------------------------------------------------
# Decimal precision
# ---------------------------------------------------------------------------


def test_decimal_excess_scale_is_rejected_not_silently_rounded() -> None:
    """PostgreSQL rounds a `NUMERIC(12, 2)` given three decimal places with no
    error at all (confirmed against a live database) -- caught here instead,
    since nothing in this module is supposed to change a value without saying
    so.
    """
    spec = field("price", FieldType.DECIMAL, precision=12, decimal_places=2)
    error = check_bounds(Decimal("123.456"), spec)
    assert error is not None
    assert "2 decimal place" in error


def test_decimal_within_scale_is_accepted() -> None:
    spec = field("price", FieldType.DECIMAL, precision=12, decimal_places=2)
    assert check_bounds(Decimal("123.45"), spec) is None


def test_decimal_excess_whole_digits_is_rejected() -> None:
    """The database itself raises `NumericValueOutOfRangeError` for this one
    (confirmed live) -- as a raw driver exception, not a field-level message,
    which is exactly what this check exists to pre-empt."""
    spec = field("price", FieldType.DECIMAL, precision=12, decimal_places=2)
    error = check_bounds(Decimal("12345678901.23"), spec)
    assert error is not None
    assert "digit" in error


# ---------------------------------------------------------------------------
# INET / CIDR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["192.168.1.5", "10.0.0.0/8", "192.168.1.5/24"])
def test_inet_accepts_a_host_or_a_network(text: str) -> None:
    spec = field("address", FieldType.INET)
    assert parse_value(text, spec) == text


def test_inet_that_is_not_one_says_so() -> None:
    with pytest.raises(ValueError, match="IP address"):
        parse_value("not an address", field("address", FieldType.INET))


# ---------------------------------------------------------------------------
# MACADDR
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF"],
)
def test_macaddr_accepts_every_separator(text: str) -> None:
    spec = field("mac", FieldType.MACADDR)
    assert parse_value(text, spec) == "aa:bb:cc:dd:ee:ff"


def test_macaddr_round_trips() -> None:
    spec = field("mac", FieldType.MACADDR)
    parsed = parse_value("AA:BB:CC:DD:EE:FF", spec)
    rendered = render_value(parsed, spec)
    assert parse_value(rendered, spec) == parsed


def test_macaddr_that_is_not_one_says_so() -> None:
    with pytest.raises(ValueError, match="MAC address"):
        parse_value("not a mac", field("mac", FieldType.MACADDR))


# ---------------------------------------------------------------------------
# HSTORE
# ---------------------------------------------------------------------------


def test_hstore_reads_one_pair_per_line() -> None:
    spec = field("attrs", FieldType.HSTORE)
    assert parse_value("colour: red\nsize: large", spec) == {"colour": "red", "size": "large"}


def test_hstore_renders_sorted_by_key() -> None:
    spec = field("attrs", FieldType.HSTORE)
    rendered = render_value({"size": "large", "colour": "red"}, spec)
    assert rendered == "colour: red\nsize: large"


def test_hstore_round_trips_through_its_control() -> None:
    spec = field("attrs", FieldType.HSTORE)
    original = {"colour": "red", "size": "large"}
    rendered = render_value(original, spec)
    assert parse_value(rendered, spec) == original


def test_hstore_without_a_colon_says_so() -> None:
    with pytest.raises(ValueError, match="colon"):
        parse_value("colour red", field("attrs", FieldType.HSTORE))


# ---------------------------------------------------------------------------
# MONEY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1234.56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("$1234.56", "1234.56"),
        ("$1,234.56", "1234.56"),
        ("-$1,234.56", "-1234.56"),
    ],
)
def test_money_accepts_currency_formatting(text: str, expected: str) -> None:
    assert parse_value(text, field("price", FieldType.MONEY)) == expected


def test_money_renders_as_plain_decimal() -> None:
    """PostgreSQL's own `money` codec hands back "$1,234.56" against an
    `en_US.utf8` server (confirmed live) -- the box has to show the plain
    number, or a value nobody touched round-trips into something that no
    longer matches what `parse_value` normally sees typed."""
    rendered = render_value("$1,234.56", field("price", FieldType.MONEY))
    assert rendered == "1234.56"


def test_money_round_trips_through_its_control() -> None:
    spec = field("price", FieldType.MONEY)
    db_value = "-$1,234.56"  # what PostgreSQL's money codec actually returns
    rendered = render_value(db_value, spec)
    assert parse_value(rendered, spec) == "-1234.56"


def test_money_that_is_not_one_says_so() -> None:
    with pytest.raises(ValueError, match="amount"):
        parse_value("free", field("price", FieldType.MONEY))


# ---------------------------------------------------------------------------
# BITS
# ---------------------------------------------------------------------------


def test_bits_accepts_zeros_and_ones() -> None:
    assert parse_value("01010101", field("flags", FieldType.BITS)) == "01010101"


def test_bits_rejects_anything_else() -> None:
    with pytest.raises(ValueError, match="0s and 1s"):
        parse_value("0102", field("flags", FieldType.BITS))


def test_bits_renders_a_bitstring_like_object_without_the_grouping_space() -> None:
    """`asyncpg.BitString.as_string()` groups digits in fours ("0101 0101");
    `parse_value` rejects the space, so it must not reach the box."""

    class FakeBitString:
        def as_string(self) -> str:
            return "0101 0101"

    rendered = render_value(FakeBitString(), field("flags", FieldType.BITS))
    assert rendered == "01010101"
    assert parse_value(rendered, field("flags", FieldType.BITS)) == "01010101"


# ---------------------------------------------------------------------------
# RANGE / MULTIRANGE
# ---------------------------------------------------------------------------


def test_range_parses_into_a_plain_tuple_not_a_sqlalchemy_object() -> None:
    """`values.py` may not import SQLAlchemy -- `orm/sqlalchemy/adapter.py` is
    the layer that turns this tuple into a real `Range`."""
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    assert parse_value("[1, 10)", spec) == (1, 10, "[)")


def test_range_endpoints_parse_through_the_bound_type() -> None:
    """A `daterange` gets the date parser's own message, reused rather than
    duplicated."""
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.DATE, multi=False))
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_value("[not-a-date, 2026-02-01]", spec)


def test_range_allows_an_unbounded_end() -> None:
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    assert parse_value("(, 5]", spec) == (None, 5, "(]")


def test_range_that_is_not_one_says_so() -> None:
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    with pytest.raises(ValueError, match="range"):
        parse_value("nonsense", spec)


class _FakeRange:
    """Duck-types `sqlalchemy.dialects.postgresql.Range` -- `values.py` cannot
    import the real thing, so `render_value` has to work off attributes alone.
    """

    def __init__(self, lower: object, upper: object, bounds: str) -> None:
        self.lower = lower
        self.upper = upper
        self.bounds = bounds
        self.isempty = False


def test_range_round_trips_through_its_control() -> None:
    spec = field("span", FieldType.RANGE, bounds=RangeSpec(FieldType.INTEGER, multi=False))
    rendered = render_value(_FakeRange(1, 10, "[)"), spec)
    assert rendered == "[1, 10)"
    assert parse_value(rendered, spec) == (1, 10, "[)")


def test_multirange_reads_one_range_per_line() -> None:
    spec = field("spans", FieldType.MULTIRANGE, bounds=RangeSpec(FieldType.INTEGER, multi=True))
    assert parse_value("[1, 10)\n[20, 30)", spec) == [(1, 10, "[)"), (20, 30, "[)")]


def test_multirange_names_the_offending_line() -> None:
    spec = field("spans", FieldType.MULTIRANGE, bounds=RangeSpec(FieldType.INTEGER, multi=True))
    with pytest.raises(ValueError, match="Line 2"):
        parse_value("[1, 10)\nnonsense", spec)


def test_multirange_round_trips_through_its_control() -> None:
    spec = field("spans", FieldType.MULTIRANGE, bounds=RangeSpec(FieldType.INTEGER, multi=True))
    value = [_FakeRange(1, 10, "[)"), _FakeRange(20, 30, "[)")]
    rendered = render_value(value, spec)
    assert rendered == "[1, 10)\n[20, 30)"
    assert parse_value(rendered, spec) == [(1, 10, "[)"), (20, 30, "[)")]
