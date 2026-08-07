"""Turning a filter's string value into the type its column holds.

Shared by both backends, because it is not about either of them. A query string
carries text; a column holds a date, a decimal or a boolean; and the conversion
between the two is decided by `FieldSpec.type`, which is the same fact whichever
ORM produced it.

Keeping one copy is what makes "a saved link means the same thing after the ORM
is swapped" true rather than hopeful. Two copies would drift, and the first
symptom would be a filter that quietly matches different rows on one backend
than on the other -- which nothing would fail on.

`fastfort/orm/` may import the spec layer but not the admin layer, so this is
deliberately *not* `admin/values.py`. The two parse overlapping shapes for
different reasons: that one turns what a person typed into a value to write,
this one turns what a URL carried into a value to compare against.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from fastfort.core.exceptions import ValidationError
from fastfort.spec import FieldSpec, FieldType

__all__ = ["coerce_filter_value", "coerce_identity"]

#: Strings accepted as boolean true and false in a query string.
#:
#: Wide on purpose: `?active=0` comes from a checkbox list, `?active=false` from
#: a hand-written link, and `?active=` from a form that submitted an empty
#: select. All three are answers, and refusing any of them would make a filter
#: fail on a spelling nobody would think to check.
_TRUTHY = frozenset({"1", "true", "t", "yes", "y", "on"})
_FALSY = frozenset({"0", "false", "f", "no", "n", "off", ""})


def as_bool(value: str) -> bool:
    text = value.strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ValidationError(f"{text!r} is not a boolean value.")


def coerce_identity(raw: str) -> Any:
    """Best-effort conversion for a relation target's key.

    An integer, a UUID, or the string itself -- which covers every primary key
    an admin dropdown submits. Tried in that order because a UUID never parses
    as an integer, so there is no case where the first answer shadows a better
    one.
    """
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return uuid.UUID(raw)
    except ValueError:
        return raw


def _datetime(raw: str) -> dt.datetime:
    """ISO 8601, tolerating a trailing `Z`, which `fromisoformat` refuses.

    A value with no offset is read as UTC, which is the same rule the form's own
    parser follows -- a date filter carries "2026-08-06" and nothing else, so
    without this a filter compared a naive datetime against a column the form
    only ever writes aware ones to. PostgreSQL raises on that comparison and
    Tortoise warns about it; both are saying the same thing.
    """
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _duration(raw: str) -> dt.timedelta:
    """`HH:MM:SS`, `MM:SS` or `Nd HH:MM:SS` into a timedelta.

    A near-twin of `parse_duration` in `fastfort/admin/values.py` and
    deliberately not shared with it: `fastfort/orm/` may import the spec layer
    but not the admin layer, and inverting that to save fifteen lines would put
    the ORM adapters downstream of the web layer they exist to be independent
    of.

    It exists at all because `DURATION` is offered as a filter, and without a
    coercer the bound reaches the database as the string it arrived as -- which
    PostgreSQL refuses against an `interval` column, so a filter that looked
    available was a 500 waiting to be clicked.
    """
    text = raw.strip()
    days = 0
    if "d" in text:
        head, _, text = text.partition("d")
        days = int(head.strip())
    parts = text.strip().split(":") if text.strip() else ["0"]
    if len(parts) > 3:
        raise ValueError(f"{raw!r} is not a duration")
    numbers = [float(part) for part in parts]
    # Right-aligned, so "90" is ninety seconds rather than ninety hours -- the
    # same rule the form's own box follows.
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    return dt.timedelta(days=days, hours=numbers[0], minutes=numbers[1], seconds=numbers[2])


#: One converter per type that needs one. A type absent from here compares as
#: the string it arrived as, which is right for text and harmless for the rest:
#: every database accepts a string literal where it expects text.
COERCERS: dict[FieldType, Callable[[str], Any]] = {
    FieldType.INTEGER: int,
    FieldType.BIGINT: int,
    FieldType.FLOAT: float,
    FieldType.DECIMAL: Decimal,
    FieldType.BOOLEAN: as_bool,
    FieldType.DATE: dt.date.fromisoformat,
    FieldType.DATETIME: _datetime,
    FieldType.TIME: dt.time.fromisoformat,
    FieldType.DURATION: _duration,
    FieldType.UUID: uuid.UUID,
}


def coerce_filter_value(field: FieldSpec | None, raw: str, path: str) -> Any:
    """One filter value, as the type the column compares against.

    A failure is a `ValidationError` naming the field, not a stack trace: the
    value came from a query string, so it is somebody's typo or a stale
    bookmark, and both deserve a sentence rather than a 500.
    """
    field_type = field.type if field is not None else FieldType.STRING

    # A relation is filtered by the identity of its target, which is what the
    # dropdown submits and what the target's primary key column holds.
    if field_type.is_relation:
        return coerce_identity(raw)

    coercer = COERCERS.get(field_type)
    if coercer is None:
        return raw
    try:
        return coercer(raw)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(
            f"{raw!r} is not a valid value for {path!r}.",
            field_errors={path: [f"Expected {field_type.value}."]},
        ) from exc
