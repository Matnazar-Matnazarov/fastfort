"""Validated description of a list request.

`ListQuery.from_params` is the only place where raw query-string input becomes a
structured query, and it is a security boundary: sort keys, filter fields and
operators are checked against allow-lists derived from the model spec, and every
numeric bound is clamped. Nothing downstream re-validates, so nothing downstream
has to.

Values themselves stay as strings here. Coercing ``"42"`` into an integer needs to
know the column type, which is the adapter's job, not this layer's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

__all__ = [
    "Filter",
    "FilterOperator",
    "ListQuery",
    "Page",
    "SortSpec",
    "SpatialFilter",
    "SpatialOperator",
]

T = TypeVar("T")

#: Query-string keys with a reserved meaning; they are never treated as filters.
SEARCH_PARAM = "q"
ORDERING_PARAM = "o"
PAGE_PARAM = "p"
PAGE_SIZE_PARAM = "ps"
RESERVED_PARAMS = frozenset({SEARCH_PARAM, ORDERING_PARAM, PAGE_PARAM, PAGE_SIZE_PARAM})

#: Hard ceilings. They exist to bound the work a single request can ask for, and
#: apply on top of whatever the admin configuration allows.
MAX_SEARCH_LENGTH = 256
MAX_SORT_TERMS = 3
MAX_FILTERS = 20
MAX_IN_VALUES = 100


class FilterOperator(StrEnum):
    """Comparisons a filter may request.

    Kept deliberately small: each one has to be implementable on SQLite,
    PostgreSQL and MySQL with identical semantics.
    """

    EXACT = "exact"
    IEXACT = "iexact"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    ISNULL = "isnull"
    RANGE = "range"

    # String matching is case-insensitive on every database, and named so that
    # the call site says as much. A case-sensitive variant would need three
    # different mechanisms (GLOB, LIKE BINARY, plain LIKE) to mean the same
    # thing on SQLite, MySQL and PostgreSQL, and an admin filter never wants it.
    ICONTAINS = "icontains"
    ISTARTSWITH = "istartswith"
    IENDSWITH = "iendswith"

    @property
    def takes_many_values(self) -> bool:
        return self in {FilterOperator.IN, FilterOperator.RANGE}


@dataclass(frozen=True, slots=True)
class Filter:
    """A single validated ``field op value`` condition."""

    field: str
    operator: FilterOperator
    value: str | tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "operator": self.operator.value,
            "value": list(self.value) if isinstance(self.value, tuple) else self.value,
        }


class SpatialOperator(StrEnum):
    """Comparisons a spatial filter may request.

    Deliberately *not* members of `FilterOperator`, whose docstring states the
    rule that enum lives by: every operator in it has to mean the same thing on
    SQLite, PostgreSQL and MySQL. `ST_DWithin` does not exist on two of the
    three. Rather than quietly weakening that promise, spatial filtering is its
    own vocabulary carried in its own field, offered only where the backend
    reports PostGIS -- so a filter that cannot run is never rendered, instead of
    being rendered and then failing when it is used.
    """

    #: The row's geometry lies inside the one the filter names, and vice versa.
    WITHIN = "within"
    CONTAINS = "contains"
    #: They share any point at all. The cheapest useful question, and the one an
    #: index answers directly.
    INTERSECTS = "intersects"
    OVERLAPS = "overlaps"
    TOUCHES = "touches"
    CROSSES = "crosses"
    #: Within `distance` of the named geometry. Metres on a `geography` column,
    #: SRID units on a `geometry` one -- the distinction `GeometrySpec.geography`
    #: exists to keep.
    DWITHIN = "dwithin"
    #: Bounding-box overlap, which is what a map viewport actually asks.
    BBOX = "bbox"

    @property
    def needs_distance(self) -> bool:
        return self is SpatialOperator.DWITHIN


@dataclass(frozen=True, slots=True)
class SpatialFilter:
    """A single validated spatial condition.

    `geometry` is EWKT, already parsed and re-rendered by `spec.geo` before it
    got here -- so what reaches the query builder is a string this package
    produced, never the one the query string carried. That matters more than
    usual: the value ends up as an argument to a SQL function, and the one thing
    it must not be is text a request chose.
    """

    field: str
    operator: SpatialOperator
    geometry: str
    #: Metres, for `dwithin`. `None` for every other operator.
    distance: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "operator": self.operator.value,
            "geometry": self.geometry,
            "distance": self.distance,
        }


@dataclass(frozen=True, slots=True)
class SortSpec:
    """One ordering term."""

    field: str
    descending: bool = False

    @classmethod
    def parse(cls, token: str) -> SortSpec | None:
        """Parse a ``name`` or ``-name`` token, returning None if it is empty."""
        token = token.strip()
        if not token:
            return None
        if token.startswith("-"):
            name = token[1:].strip()
            return cls(name, descending=True) if name else None
        return cls(token)

    def as_token(self) -> str:
        return f"-{self.field}" if self.descending else self.field

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "descending": self.descending}


@dataclass(frozen=True, slots=True)
class ListQuery:
    """A validated list request, safe to hand straight to an adapter."""

    search: str = ""
    filters: tuple[Filter, ...] = ()
    #: Spatial conditions, kept apart from `filters` because they compile to SQL
    #: only PostGIS has. A backend without it ignores them; `from_params` never
    #: produces one for a field the caller did not declare spatial.
    spatial: tuple[SpatialFilter, ...] = ()
    ordering: tuple[SortSpec, ...] = ()
    page: int = 1
    page_size: int = 25

    #: Parameters that were dropped during validation. Never affects the query;
    #: exposed so that a typo in an admin configuration can be logged instead of
    #: silently changing what the user sees.
    rejected: tuple[str, ...] = field(default=(), compare=False)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "search": self.search,
            "filters": [f.to_dict() for f in self.filters],
            "spatial": [f.to_dict() for f in self.spatial],
            "ordering": [s.to_dict() for s in self.ordering],
            "page": self.page,
            "page_size": self.page_size,
        }

    @classmethod
    def from_params(
        cls,
        params: Mapping[str, str],
        *,
        sortable_fields: Iterable[str] = (),
        filterable_fields: Iterable[str] = (),
        spatial_fields: Mapping[str, int] | None = None,
        searchable: bool = True,
        default_ordering: Sequence[SortSpec] = (),
        page_size: int = 25,
        max_page_size: int = 200,
    ) -> ListQuery:
        """Build a query from untrusted query-string parameters.

        Unrecognised sort keys, filter fields and operators are dropped rather
        than raising: a stale bookmark should still render a list page. Every
        drop is recorded in `rejected`.
        """
        sortable = frozenset(sortable_fields)
        filterable = frozenset(filterable_fields)
        rejected: list[str] = []

        search = ""
        if searchable:
            search = params.get(SEARCH_PARAM, "").strip()[:MAX_SEARCH_LENGTH]

        ordering = _parse_ordering(params.get(ORDERING_PARAM, ""), sortable, rejected)
        if not ordering:
            ordering = tuple(default_ordering)

        spatial = _parse_spatial(params, spatial_fields or {}, rejected)
        # Spatial keys are `field__within`, and `field` is a real field name, so
        # `_split_filter_key` would happily read them as an ordinary filter with
        # an unknown operator and reject them by name. Taken out first.
        remaining = {
            key: value for key, value in params.items() if key not in _spatial_keys(spatial)
        }
        filters = _parse_filters(remaining, filterable, rejected)

        size = _clamp_int(params.get(PAGE_SIZE_PARAM), default=page_size, low=1, high=max_page_size)
        page = _clamp_int(params.get(PAGE_PARAM), default=1, low=1, high=None)

        return cls(
            search=search,
            filters=filters,
            spatial=spatial,
            ordering=ordering,
            page=page,
            page_size=size,
            rejected=tuple(rejected),
        )


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    """One page of results together with the counters a paginator needs."""

    items: tuple[T, ...]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return -(-self.total // self.page_size)  # ceiling division

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def start_index(self) -> int:
        """1-based index of the first item, or 0 when the page is empty."""
        return 0 if not self.items else (self.page - 1) * self.page_size + 1

    @property
    def end_index(self) -> int:
        return 0 if not self.items else self.start_index + len(self.items) - 1


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _clamp_int(raw: str | None, *, default: int, low: int, high: int | None) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    value = max(value, low)
    if high is not None:
        value = min(value, high)
    return value


def _parse_ordering(
    raw: str, sortable: frozenset[str], rejected: list[str]
) -> tuple[SortSpec, ...]:
    terms: list[SortSpec] = []
    seen: set[str] = set()

    for token in raw.split(","):
        spec = SortSpec.parse(token)
        if spec is None:
            continue
        if spec.field not in sortable:
            rejected.append(f"{ORDERING_PARAM}={token.strip()}")
            continue
        if spec.field in seen:
            continue
        seen.add(spec.field)
        terms.append(spec)
        if len(terms) == MAX_SORT_TERMS:
            break

    return tuple(terms)


def _split_filter_key(key: str, filterable: frozenset[str]) -> tuple[str, FilterOperator] | None:
    """Split ``field__operator`` into its parts.

    Field names may themselves contain ``__`` when they traverse a relation
    (``category__name``), so the suffix only counts as an operator when the
    remaining prefix is a known filterable field.
    """
    if key in filterable:
        return key, FilterOperator.EXACT

    name, separator, suffix = key.rpartition("__")
    if not separator or name not in filterable:
        return None
    try:
        return name, FilterOperator(suffix)
    except ValueError:
        return None


#: Suffix carrying the radius of a `dwithin`, in metres.
DISTANCE_SUFFIX = "__km"

#: Largest radius a single request may ask for, in metres. Ten thousand
#: kilometres is a quarter of the way round the planet -- past that the query is
#: "every row", which the unfiltered list already answers more cheaply.
MAX_DISTANCE_M = 10_000_000.0


def _spatial_keys(filters: tuple[SpatialFilter, ...]) -> frozenset[str]:
    keys: set[str] = set()
    for condition in filters:
        keys.add(f"{condition.field}__{condition.operator.value}")
        keys.add(f"{condition.field}{DISTANCE_SUFFIX}")
    return frozenset(keys)


def _parse_spatial(
    params: Mapping[str, str], spatial_fields: Mapping[str, int], rejected: list[str]
) -> tuple[SpatialFilter, ...]:
    """Read `field__within=<geometry>` and friends.

    `spatial_fields` maps a field name to its SRID, and it is the allow-list: a
    name that is not in it never becomes a spatial condition, so a column that
    is not a geometry can never reach an `ST_` function.

    The geometry itself is parsed by `spec.geo` and re-rendered from what that
    produced, never passed through. It ends up as an argument to a SQL function,
    and the one thing it must not be is the string a request supplied.
    """
    from .fields import FieldSpec, FieldType, GeometrySpec
    from .geo import parse as parse_geometry

    found: list[SpatialFilter] = []

    for key, raw_value in params.items():
        name, separator, suffix = key.rpartition("__")
        if not separator or name not in spatial_fields:
            continue
        try:
            operator = SpatialOperator(suffix)
        except ValueError:
            continue

        srid = spatial_fields[name]
        try:
            geometry = parse_geometry(
                raw_value,
                FieldSpec(
                    name=name,
                    type=FieldType.GEOMETRY,
                    label=name,
                    # No `kind`, so any shape is accepted: a viewport is a
                    # polygon and a "near here" is a point, against the same
                    # column.
                    geometry=GeometrySpec(srid=srid),
                ),
            )
        except ValueError:
            # A stale bookmark should still render a list page -- the same rule
            # every other rejected parameter here follows.
            rejected.append(key)
            continue

        distance: float | None = None
        if operator.needs_distance:
            try:
                kilometres = float(params.get(f"{name}{DISTANCE_SUFFIX}", ""))
            except ValueError:
                rejected.append(f"{name}{DISTANCE_SUFFIX}")
                continue
            if not 0 < kilometres * 1000 <= MAX_DISTANCE_M:
                rejected.append(f"{name}{DISTANCE_SUFFIX}")
                continue
            distance = kilometres * 1000

        found.append(
            SpatialFilter(field=name, operator=operator, geometry=geometry, distance=distance)
        )
        if len(found) >= MAX_FILTERS:
            break

    return tuple(found)


def _parse_filters(
    params: Mapping[str, str], filterable: frozenset[str], rejected: list[str]
) -> tuple[Filter, ...]:
    filters: list[Filter] = []

    for key, raw_value in params.items():
        if key in RESERVED_PARAMS:
            continue
        if len(filters) >= MAX_FILTERS:
            rejected.append(key)
            continue

        parsed = _split_filter_key(key, filterable)
        if parsed is None:
            rejected.append(key)
            continue

        name, operator = parsed
        value: str | tuple[str, ...]
        if operator.takes_many_values:
            parts = tuple(p for p in raw_value.split(",") if p != "")[:MAX_IN_VALUES]
            if not parts:
                rejected.append(key)
                continue
            value = parts
        else:
            value = raw_value

        filters.append(Filter(field=name, operator=operator, value=value))

    return tuple(filters)
