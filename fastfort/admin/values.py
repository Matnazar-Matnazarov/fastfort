"""Parsing a submitted string into a column's Python type, and rendering it back.

Split out of `forms.py`; see that module's docstring for the two rules this half
of the split carries: the allow-list is the spec, and nothing is coerced
silently. Every error message here names the shape it wants ("Enter a date as
YYYY-MM-DD", never "invalid value"), and what a control renders has to be what
it accepts back -- `test_a_duration_round_trips_through_its_control` exists
because that broke once already: a multi-day duration rendered as "2 days,
4:15:00" and the parser rejected it, so opening a row and pressing Save failed
on a field nobody had touched. Every type added since carries its own
round-trip test for the same reason.

No SQLAlchemy import, enforced by `tests/test_architecture.py`: a range parses
to a plain `(lower, upper, bounds)` tuple rather than a
`sqlalchemy.dialects.postgresql.Range`, and `orm/sqlalchemy/adapter.py` is the
only layer that turns it into one.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from fastfort.spec import FieldSpec, FieldType

from . import geo

__all__ = [
    "check_bounds",
    "duration_text",
    "parse_duration",
    "parse_value",
    "range_parts",
    "render_value",
]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_value(raw: str, spec: FieldSpec) -> Any:
    """Turn a submitted string into the column's Python type.

    Messages name what was expected, because "invalid value" tells the person
    filling the form nothing they can act on.
    """
    text = raw.strip()

    if spec.is_relation:
        # The identity of the related row, as the dropdown submitted it. The
        # adapter resolves it to an object and reports a missing target itself.
        return text

    if spec.type in {FieldType.INTEGER, FieldType.BIGINT}:
        try:
            return int(text)
        except ValueError:
            raise ValueError("Enter a whole number.") from None

    if spec.type is FieldType.FLOAT:
        try:
            return float(text)
        except ValueError:
            raise ValueError("Enter a number.") from None

    if spec.type is FieldType.DECIMAL:
        try:
            return Decimal(text)
        except InvalidOperation:
            raise ValueError("Enter a number, for example 1234.56.") from None

    if spec.type is FieldType.MONEY:
        return _parse_money(text)

    if spec.type is FieldType.DATE:
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a date as YYYY-MM-DD.") from None

    if spec.type is FieldType.DATETIME:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Enter a date and time as YYYY-MM-DD HH:MM.") from None
        # A naive value from a datetime-local input is read as UTC, so the column
        # never receives a mix of aware and naive values.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    if spec.type is FieldType.TIME:
        try:
            return dt.time.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a time as HH:MM.") from None

    if spec.type is FieldType.DURATION:
        return parse_duration(text)

    if spec.type is FieldType.GEOMETRY:
        return geo.parse(text, spec)

    if spec.type is FieldType.ARRAY:
        return _parse_array(text, spec)

    if spec.type is FieldType.RANGE:
        return _parse_range(text, spec)

    if spec.type is FieldType.MULTIRANGE:
        return _parse_multirange(text, spec)

    if spec.type is FieldType.INET:
        return _parse_inet(text)

    if spec.type is FieldType.MACADDR:
        return _parse_macaddr(text)

    if spec.type is FieldType.HSTORE:
        return _parse_hstore(text)

    if spec.type is FieldType.BITS:
        return _parse_bits(text)

    if spec.type is FieldType.UUID:
        try:
            return uuid.UUID(text)
        except ValueError:
            raise ValueError("Enter a valid UUID.") from None

    if spec.type is FieldType.JSON:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Enter valid JSON ({exc.msg} at position {exc.pos}).") from None

    if spec.type is FieldType.EMAIL and "@" not in text:
        raise ValueError("Enter an email address.")

    if spec.choices and not any(str(choice.value) == text for choice in spec.choices):
        allowed = ", ".join(str(choice.value) for choice in spec.choices)
        raise ValueError(f"Choose one of: {allowed}.")

    return raw if spec.type is FieldType.TEXT else text


def parse_duration(text: str) -> dt.timedelta:
    """A timedelta from `HH:MM:SS`, `MM:SS`, or `Nd HH:MM:SS`.

    There is no HTML control for a duration, so this is a text box and the shape
    it accepts has to be the one people already write. Seconds may carry a
    fraction, because that is what `str(timedelta)` prints and round-tripping
    the rendered value is the least surprising behaviour.
    """
    days = 0
    remainder = text
    if "d" in remainder:
        head, _, remainder = remainder.partition("d")
        try:
            days = int(head.strip())
        except ValueError:
            raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.") from None

    parts = remainder.strip().split(":") if remainder.strip() else ["0"]
    if len(parts) > 3:
        raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.")

    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.") from None

    # Right-aligned, so "90" is ninety seconds and "1:30" is a minute and a half.
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    hours, minutes, seconds = numbers
    return dt.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


# -- arrays -------------------------------------------------------------


def _parse_array(text: str, spec: FieldSpec) -> list[Any]:
    """Comma-separated, which is what people type and what `render_value`
    shows back. Blank entries are dropped rather than stored as empty
    strings: "a, b," is a typo, not a three-element list.

    Each entry is parsed through `spec.item` when the adapter supplied one, so
    `ARRAY(Integer)` rejects "banana" the same way a bare `INTEGER` column
    would -- naming which entry failed, since "entry 3" is something a person
    can find and "invalid value" is not.
    """
    entries = [part.strip() for part in text.split(",") if part.strip()]
    if spec.item is None:
        return list(entries)
    parsed: list[Any] = []
    for index, entry in enumerate(entries, start=1):
        try:
            parsed.append(parse_value(entry, spec.item))
        except ValueError as exc:
            raise ValueError(f'Entry {index} ("{entry}"): {exc}') from None
    return parsed


# -- inet / macaddr -------------------------------------------------------


def _parse_inet(text: str) -> str:
    """A host or a network -- `ipaddress.ip_interface` accepts both an address
    on its own and an address/prefix pair without masking the host bits the
    way `ip_network` would, so `192.168.1.5/24` survives exactly as typed
    rather than being silently rounded down to `192.168.1.0/24`.

    Handed back as the original text, not a re-serialised form: `inet`/`cidr`
    already accept a plain `str` from this project's driver (confirmed against
    a live asyncpg connection -- see the Phase 2 report), so there is nothing
    to convert, only to validate before the database does.
    """
    try:
        ipaddress.ip_interface(text)
    except ValueError:
        raise ValueError("Enter an IP address or network, as 192.168.1.5 or 10.0.0.0/8.") from None
    return text


_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}([0-9a-fA-F]{4})?$")


def _parse_macaddr(text: str) -> str:
    """`aa:bb:cc:dd:ee:ff`, `AA-BB-CC-DD-EE-FF`, or `aabb.ccdd.eeff` -- every
    separator PostgreSQL itself accepts, normalised to the lowercase colon
    form it hands back on read (confirmed empirically: a live column already
    returns `macaddr` values that way), so a value nobody touched renders
    identically to what it would parse back into.

    Also accepts a 64-bit EUI-64 address (16 hex digits, for `macaddr8`),
    since `types.py` classifies both column types as the same `FieldType`.
    """
    hex_only = re.sub(r"[:\-.]", "", text)
    if not _MAC_HEX_RE.match(hex_only):
        raise ValueError("Enter a MAC address, as aa:bb:cc:dd:ee:ff.")
    hex_only = hex_only.lower()
    return ":".join(hex_only[i : i + 2] for i in range(0, len(hex_only), 2))


# -- hstore ---------------------------------------------------------------


def _parse_hstore(text: str) -> dict[str, str]:
    """`key: value`, one pair per line -- matches what `render_value` shows
    back, and what a multi-line control (this phase's fallback: the plain
    `textarea` widget in `widgets.py`) can actually hold.
    """
    result: dict[str, str] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            raise ValueError(f'Line {lineno} needs a colon, as key: value (got "{stripped}").')
        result[key.strip()] = value.strip()
    return result


# -- money ------------------------------------------------------------------

#: Strips whatever currency formatting PostgreSQL's `money` type applies on
#: the way out ("$1,234.56", "-$1,234.56" against `en_US.utf8` -- confirmed
#: against a live database, not assumed) and whatever a person typing an
#: amount adds on the way in ("$1234.56", "1,234.56"). Keeping only digits,
#: the sign and the point covers both directions with one pattern.
_MONEY_STRIP_RE = re.compile(r"[^0-9.\-]")


def _parse_money(text: str) -> str:
    """A plain decimal string -- `Decimal` validates the shape, but the value
    handed to the adapter stays a `str`: asyncpg's `money` codec is
    text-based and refuses a `Decimal` outright (confirmed against a live
    connection -- see the Phase 2 report), so returning one here would only
    move the failure from this message to an unhandled 500 at `flush()`.
    """
    cleaned = _MONEY_STRIP_RE.sub("", text)
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Enter an amount, for example 1234.56.") from None
    return str(amount)


# -- bits ---------------------------------------------------------------


def _parse_bits(text: str) -> str:
    """`0`/`1` characters only. Handed to the adapter as `str` -- converting to
    the `asyncpg.BitString` the driver actually wants happens in
    `orm/sqlalchemy/adapter.py`, the one layer allowed to know a driver type
    exists.
    """
    cleaned = text.strip()
    if not cleaned or any(char not in "01" for char in cleaned):
        raise ValueError("Enter 0s and 1s only.")
    return cleaned


# -- ranges -----------------------------------------------------------------

#: `[1, 10)`, `(2026-01-01, 2026-02-01]`, `(, 5]` for an unbounded low end.
#: The two bracket characters are captured verbatim as the bounds notation --
#: PostgreSQL's own `[)`/`(]`/`[]`/`()` -- rather than normalised, so a range
#: written as inclusive-upper stays that way rather than being silently
#: rewritten to the half-open convention `int4range` defaults to.
_RANGE_RE = re.compile(r"^([\[(])\s*([^,]*?)\s*,\s*([^\])]*?)\s*([)\]])$")


def _parse_bound(text: str, bound_type: FieldType) -> Any:
    # A throwaway spec: range endpoints parse through the *bound* type, so an
    # `int4range` gets "Enter a whole number." and a `daterange` gets "Enter a
    # date as YYYY-MM-DD." -- the same messages a bare column of that type
    # would give, reused rather than duplicated.
    return parse_value(text, FieldSpec(name="bound", label="Bound", type=bound_type))


def _parse_range_text(text: str, bound_type: FieldType) -> tuple[Any, Any, str]:
    stripped = text.strip()
    if stripped.lower() == "empty":
        return (None, None, "empty")
    match = _RANGE_RE.match(stripped)
    if not match:
        raise ValueError("Enter a range, as [1, 10) — or (, 5] for an unbounded low end.")
    open_char, lower_text, upper_text, close_char = match.groups()
    lower = _parse_bound(lower_text, bound_type) if lower_text.strip() else None
    upper = _parse_bound(upper_text, bound_type) if upper_text.strip() else None
    return (lower, upper, f"{open_char}{close_char}")


def _bound_type(spec: FieldSpec) -> FieldType:
    return spec.bounds.bound if spec.bounds is not None else FieldType.STRING


def _parse_range(text: str, spec: FieldSpec) -> tuple[Any, Any, str]:
    """A plain `(lower, upper, bounds)` tuple, never a
    `sqlalchemy.dialects.postgresql.Range` -- this module may not import
    SQLAlchemy, so `orm/sqlalchemy/adapter.py` is the one that builds the real
    object from this tuple.
    """
    return _parse_range_text(text, _bound_type(spec))


def _parse_multirange(text: str, spec: FieldSpec) -> list[tuple[Any, Any, str]]:
    """One range per line -- the same tuple shape as `_parse_range`, one per
    disjoint range in the value.
    """
    bound_type = _bound_type(spec)
    ranges: list[tuple[Any, Any, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            ranges.append(_parse_range_text(line, bound_type))
        except ValueError as exc:
            raise ValueError(f"Line {lineno}: {exc}") from None
    return ranges


# ---------------------------------------------------------------------------
# Bounds checking
# ---------------------------------------------------------------------------


def check_bounds(value: Any, spec: FieldSpec) -> str | None:
    """Range and length checks the database would otherwise reject on write,
    or -- for `DECIMAL`'s excess scale -- silently round without telling
    anyone, which is worse than a rejection because `bind()`'s contract is
    that nothing here is coerced without being shown.
    """
    if spec.type is FieldType.ARRAY and isinstance(value, list):
        return _check_array_bounds(value, spec)

    if spec.max_length is not None and isinstance(value, str) and len(value) > spec.max_length:
        return f"Use at most {spec.max_length} characters (this is {len(value)})."

    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        as_decimal = Decimal(str(value))
        if spec.min_value is not None and as_decimal < spec.min_value:
            return f"Must be at least {spec.min_value}."
        if spec.max_value is not None and as_decimal > spec.max_value:
            return f"Must be at most {spec.max_value}."

    if spec.type is FieldType.DECIMAL and isinstance(value, Decimal):
        return _check_decimal_precision(value, spec)

    return None


def _check_array_bounds(value: list[Any], spec: FieldSpec) -> str | None:
    """`max_length` applies per entry for an `ARRAY` -- the old unconditional
    check below never fired for one at all, since `value` here is a `list`,
    never the `str` that check tests for. Each entry additionally goes
    through its own item spec's bounds (a `DECIMAL` item's precision, for
    instance), the same way `_parse_array` validates each entry's shape.
    """
    for index, entry in enumerate(value, start=1):
        if spec.max_length is not None and isinstance(entry, str) and len(entry) > spec.max_length:
            return f'Entry {index} ("{entry}") uses more than {spec.max_length} characters.'
        if spec.item is not None:
            error = check_bounds(entry, spec.item)
            if error:
                return f"Entry {index}: {error}"
    return None


def _check_decimal_precision(value: Decimal, spec: FieldSpec) -> str | None:
    """A `Numeric(precision, decimal_places)` given more digits than the
    column allows.

    Confirmed against a live database (see the Phase 2 report): PostgreSQL
    *rounds* excess fractional digits with no error at all -- 123.456 into a
    `NUMERIC(12, 2)` column silently becomes 123.46, exactly the kind of
    unannounced change this module's docstring says nothing here does.
    Excess digits before the point *do* raise, but as a raw
    `NumericValueOutOfRangeError` with no field to attach it to -- caught here
    instead, before either happens.
    """
    if spec.decimal_places is None:
        return None
    _sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        return None  # NaN / Infinity -- not a value `parse_value` could produce
    fraction_digits = max(-exponent, 0)
    whole_digits = max(len(digits) + exponent, 0)
    if fraction_digits > spec.decimal_places:
        return f"Use at most {spec.decimal_places} decimal place(s)."
    if spec.precision is not None:
        max_whole = spec.precision - spec.decimal_places
        if whole_digits > max_whole:
            return f"Use at most {max_whole} digit(s) before the decimal point."
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _boolean_raw(value: Any) -> str:
    """The submitted form of a three-state boolean."""
    if value is None:
        return ""
    return "1" if value else "0"


def render_value(value: Any, spec: FieldSpec) -> str:
    """The string a control shows for a value.

    A sensitive field renders empty: echoing a stored secret back into a form is
    how it ends up in a browser cache or a screenshot.
    """
    if spec.type is FieldType.BOOLEAN and spec.nullable:
        return _boolean_raw(value)
    if spec.sensitive or value is None:
        return ""
    if spec.is_relation:
        return str(_identity(value))
    if spec.type is FieldType.DATETIME and isinstance(value, dt.datetime):
        # datetime-local wants exactly this shape and rejects an offset.
        return value.strftime("%Y-%m-%dT%H:%M")
    if spec.type is FieldType.DATE and isinstance(value, dt.date):
        return value.isoformat()
    if spec.type is FieldType.TIME and isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if spec.type is FieldType.JSON:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if spec.type is FieldType.DURATION and isinstance(value, dt.timedelta):
        return duration_text(value)
    if spec.type is FieldType.ARRAY and isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if spec.type is FieldType.GEOMETRY:
        return geo.render(value, spec)
    if spec.type is FieldType.MONEY:
        return _MONEY_STRIP_RE.sub("", str(value))
    if spec.type is FieldType.BITS:
        return _bits_text(value)
    if spec.type is FieldType.HSTORE and isinstance(value, dict):
        return _hstore_text(value)
    if spec.type is FieldType.RANGE:
        return _range_text(value, _bound_type(spec))
    if spec.type is FieldType.MULTIRANGE:
        return _multirange_text(value, _bound_type(spec))
    return str(value)


def duration_text(value: dt.timedelta) -> str:
    """A duration in exactly the shape `parse_duration` accepts.

    `str(timedelta)` renders "2 days, 4:15:00", which that parser rejects -- so
    opening a row with a multi-day duration and pressing Save produced a
    validation error on a field nobody had touched.
    """
    seconds = value.total_seconds()
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)

    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    # `%g` so a whole number of seconds does not render as "05.000000".
    clock = f"{int(hours):02d}:{int(minutes):02d}:{seconds:09.6f}".rstrip("0").rstrip(".")
    if len(clock) == 6:  # HH:MM: with the seconds stripped to nothing
        clock += "00"
    return f"{sign}{int(days)}d {clock}" if days else f"{sign}{clock}"


def _hstore_text(value: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {'' if val is None else val}" for key, val in sorted(value.items()))


def _bits_text(value: Any) -> str:
    as_string = getattr(value, "as_string", None)
    if callable(as_string):
        # `asyncpg.BitString`, what the driver hands back for `bit`/`varbit`
        # (duck-typed rather than `isinstance`, so this module still does not
        # need to import asyncpg). `.as_string()` groups digits in fours
        # ("0101 0101"); the space is display-only and `_parse_bits` rejects
        # it, so it has to come out before the box shows the value.
        return str(as_string()).replace(" ", "")
    return str(value)


def range_parts(value: Any, spec: FieldSpec) -> tuple[str, str, str]:
    """The lower text, the upper text and the bracket notation a two-input
    range control renders -- the exact three pieces `Form.bind` reassembles
    into `[1, 10)` before handing the result to `parse_value`, and the
    counterpart to `_range_text` for a control that is two boxes instead of
    one.

    Duck-typed against `.lower`/`.upper`/`.bounds`, exactly like `_range_text`:
    the live value is a `sqlalchemy.dialects.postgresql.Range` on the way in
    from the database, and the plain `(lower, upper, bounds)` tuple this
    module's own `_parse_range` produces on the way back from a submission
    that failed validation elsewhere on the form.

    An explicit empty range (`'empty'::int4range`) has no lower or upper to
    put in two boxes, so it renders as though nothing were stored -- the one
    case this control cannot round-trip byte for byte, and rare enough in
    practice (nobody types "empty" into a date range) that it is not worth a
    fifth, unlabelled option in the bounds selector.
    """
    default = ("", "", "[)")
    if value is None or getattr(value, "isempty", False):
        return default

    bound_type = _bound_type(spec)
    bound_spec = FieldSpec(name="bound", label="Bound", type=bound_type)

    if isinstance(value, tuple) and len(value) == 3:
        lower, upper, notation = value
    else:
        bounds = getattr(value, "bounds", None)
        if bounds is None:
            return default
        lower = getattr(value, "lower", None)
        upper = getattr(value, "upper", None)
        notation = str(bounds)

    lower_text = "" if lower is None else render_value(lower, bound_spec)
    upper_text = "" if upper is None else render_value(upper, bound_spec)
    return (lower_text, upper_text, notation)


def _range_text(value: Any, bound_type: FieldType) -> str:
    """`[1, 10)`, from whatever the ORM handed back.

    Duck-typed against `.lower`/`.upper`/`.bounds` rather than imported: the
    live value is a `sqlalchemy.dialects.postgresql.Range`, and this module is
    not allowed to know that type exists.
    """
    if getattr(value, "isempty", False):
        return "empty"
    bounds = getattr(value, "bounds", None)
    if bounds is None:
        return str(value)
    lower = getattr(value, "lower", None)
    upper = getattr(value, "upper", None)
    bound_spec = FieldSpec(name="bound", label="Bound", type=bound_type)
    lower_text = "" if lower is None else render_value(lower, bound_spec)
    upper_text = "" if upper is None else render_value(upper, bound_spec)
    return f"{bounds[0]}{lower_text}, {upper_text}{bounds[-1]}"


def _multirange_text(value: Any, bound_type: FieldType) -> str:
    try:
        items = list(value)
    except TypeError:
        return str(value)
    return "\n".join(_range_text(item, bound_type) for item in items)


def _identity(obj: Any) -> Any:
    """A related object's primary key, or the value itself if it is already one."""
    for attribute in ("id", "pk"):
        found = getattr(obj, attribute, None)
        if found is not None:
            return found
    return obj
