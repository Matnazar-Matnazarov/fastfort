"""Canonical description of a model, as produced by an ORM adapter.

A `ModelSpec` is what introspection yields: the fields, the primary key and the
names used to label things in the interface. Admin-level configuration
(`list_display`, fieldsets, permissions) is layered on top of this later and never
edits it in place -- the spec always reflects the model itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .fields import FieldSpec

__all__ = ["ModelSpec"]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Everything FastFort knows about one registered model."""

    #: Stable registry key, e.g. ``"shop.product"``. Appears in URLs and
    #: permission names, so it must not change between runs.
    key: str
    #: Python class name of the model.
    name: str
    verbose_name: str
    verbose_name_plural: str
    fields: tuple[FieldSpec, ...]
    #: Names of the fields forming the primary key. More than one entry means a
    #: composite key, which the whole stack has to keep supporting.
    primary_key: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("ModelSpec.key must not be empty")
        if not self.fields:
            raise ValueError(f"{self.key!r} has no fields")

        names = [f.name for f in self.fields]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"{self.key!r} declares duplicate fields: {sorted(duplicates)}")

        known = set(names)
        missing = [name for name in self.primary_key if name not in known]
        if missing:
            raise ValueError(f"{self.key!r} primary key refers to unknown fields: {missing}")
        if not self.primary_key:
            raise ValueError(f"{self.key!r} has no primary key; FastFort cannot address its rows")

    # -- lookups ------------------------------------------------------------

    def __iter__(self) -> Iterator[FieldSpec]:
        return iter(self.fields)

    def __contains__(self, name: object) -> bool:
        return any(f.name == name for f in self.fields)

    def get(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    def field(self, name: str) -> FieldSpec:
        """Return a field by name, raising `KeyError` when it does not exist."""
        found = self.get(name)
        if found is None:
            available = ", ".join(sorted(f.name for f in self.fields))
            raise KeyError(f"{self.key!r} has no field {name!r}. Available fields: {available}")
        return found

    def select(self, names: Iterable[str]) -> tuple[FieldSpec, ...]:
        """Return the named fields, in the order given."""
        return tuple(self.field(name) for name in names)

    # -- derived views ------------------------------------------------------

    @property
    def is_composite_key(self) -> bool:
        return len(self.primary_key) > 1

    @property
    def editable_fields(self) -> tuple[FieldSpec, ...]:
        """Fields a form may write.

        This is the allow-list behind mass-assignment protection; a value posted
        for anything outside it is discarded.
        """
        return tuple(f for f in self.fields if f.editable)

    @property
    def sortable_fields(self) -> tuple[str, ...]:
        """Field names that may appear in an ``ORDER BY``.

        Multi-valued relations are excluded: ordering a list by a to-many field
        multiplies rows, which is never what the user meant.
        """
        return tuple(f.name for f in self.fields if f.sortable and not f.type.is_multi_valued)

    @property
    def searchable_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.searchable)

    @property
    def filterable_fields(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.filterable)

    @property
    def spatial_fields(self) -> dict[str, int]:
        """Geometry fields, mapped to the SRID each one is stored in.

        The allow-list `ListQuery.from_params` checks a spatial filter against,
        and the reason it is a mapping rather than a set: the geometry a filter
        carries has to be parsed against the column's own SRID, or a viewport in
        Web Mercator would be compared to a column in WGS 84 and match nothing.
        """
        return {f.name: f.geometry.srid for f in self.fields if f.geometry is not None}

    @property
    def sensitive_fields(self) -> frozenset[str]:
        """Fields whose values are masked in audit records and never re-rendered."""
        return frozenset(f.name for f in self.fields if f.sensitive)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "verbose_name": self.verbose_name,
            "verbose_name_plural": self.verbose_name_plural,
            "primary_key": list(self.primary_key),
            "fields": [f.to_dict() for f in self.fields],
        }
