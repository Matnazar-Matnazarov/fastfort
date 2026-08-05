"""Spec (IR) layer -- the single data contract passed between layers.

An ORM adapter turns a model into a `ModelSpec`; the admin, the form system and
the templates read nothing else. That indirection is what keeps every layer above
this one portable across ORMs, and it is why these types are immutable and
JSON-serialisable: the same specs that render a Jinja2 page can also serve a JSON
API without a second code path.

ORM and web-framework imports are forbidden here, enforced by
`tests/test_architecture.py`.
"""

from __future__ import annotations

from .changes import Change, ChangeSet
from .deletion import DeletionEffect, DeletionPlan, RelatedRows
from .fields import Choice, FieldSpec, FieldType, GeometrySpec, RangeSpec, RelationSpec
from .model import ModelSpec
from .query import Filter, FilterOperator, ListQuery, Page, SortSpec

__all__ = [
    "Change",
    "ChangeSet",
    "Choice",
    "DeletionEffect",
    "DeletionPlan",
    "FieldSpec",
    "FieldType",
    "Filter",
    "FilterOperator",
    "GeometrySpec",
    "ListQuery",
    "ModelSpec",
    "Page",
    "RangeSpec",
    "RelatedRows",
    "RelationSpec",
    "SortSpec",
]
