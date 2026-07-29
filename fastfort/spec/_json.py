"""Conversion of Python values into JSON-safe primitives.

Every spec type exposes ``to_dict()``. The result has to survive ``json.dumps``
without a custom encoder, because the same dictionaries will feed a JSON API and
template context alike.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

__all__ = ["jsonify"]


def jsonify(value: Any) -> Any:
    """Return a JSON-serialisable representation of ``value``.

    Numeric precision is preserved by rendering ``Decimal`` as a string rather
    than a float; money columns must not be rounded on their way to the browser.
    Unknown types fall back to ``repr`` so that a surprising column type degrades
    into a readable value instead of raising during serialisation.
    """
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return jsonify(value.value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(key): jsonify(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonify(item) for item in value]
    return repr(value)
