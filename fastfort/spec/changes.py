"""Field-level difference between two states of an object.

A `ChangeSet` is what the audit log stores and what the interface shows as
"old value → new value". Building it here, rather than inside the audit module,
keeps the masking rule in one place: a value from a sensitive field must never
reach a log, a template or an API response.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ._json import jsonify

__all__ = ["Change", "ChangeSet"]

#: What a masked value is replaced with. A fixed placeholder, so that the length
#: of the original never leaks.
MASK = "********"


@dataclass(frozen=True, slots=True)
class Change:
    """A single field that differs between two states."""

    field: str
    old: Any
    new: Any
    masked: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.masked:
            return {"field": self.field, "old": MASK, "new": MASK, "masked": True}
        return {
            "field": self.field,
            "old": jsonify(self.old),
            "new": jsonify(self.new),
            "masked": False,
        }


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """The set of fields that changed, in a stable order."""

    changes: tuple[Change, ...] = ()

    @classmethod
    def between(
        cls,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        sensitive: frozenset[str] = frozenset(),
        fields: tuple[str, ...] | None = None,
    ) -> ChangeSet:
        """Diff two snapshots of an object.

        Only keys present in `after` are considered, so a partial update records
        exactly the fields it touched. When `fields` is given it fixes the order
        of the result; otherwise the order of `after` is preserved, which keeps
        the diff readable for a human.

        A sensitive field that changed is reported as having changed, but both of
        its values are replaced with a placeholder.
        """
        names = fields if fields is not None else tuple(after.keys())
        changes: list[Change] = []

        for name in names:
            if name not in after:
                continue
            old = before.get(name)
            new = after[name]
            if old == new:
                continue
            changes.append(Change(field=name, old=old, new=new, masked=name in sensitive))

        return cls(tuple(changes))

    def __bool__(self) -> bool:
        return bool(self.changes)

    def __len__(self) -> int:
        return len(self.changes)

    def __iter__(self) -> Iterator[Change]:
        return iter(self.changes)

    def __contains__(self, name: object) -> bool:
        return any(change.field == name for change in self.changes)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(change.field for change in self.changes)

    def to_dict(self) -> dict[str, Any]:
        return {"changes": [change.to_dict() for change in self.changes]}
