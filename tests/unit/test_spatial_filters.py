"""`ListQuery.from_params` is the only place a spatial filter can be created.

Spatial operators are deliberately not members of `FilterOperator`, whose
docstring states the rule that enum lives by: every operator in it means the
same thing on SQLite, PostgreSQL and MySQL. `ST_DWithin` exists on one of the
three. So they are a separate vocabulary in a separate field, and the boundary
between a query string and a query has to police them the same way it polices
everything else: an allow-list of fields, an enum of operators, and a geometry
that is re-rendered from what this package parsed rather than passed through.

That last part is the one that matters most. The value ends up as the argument
of a SQL function, and the one thing it must never be is text a request chose.
"""

from __future__ import annotations

import pytest

from fastfort.spec import ListQuery, SpatialOperator

#: Tashkent, and a box around it.
POINT = "41.2995, 69.2401"
BOX = "POLYGON((69 41, 70 41, 70 42, 69 42, 69 41))"

#: What the list view passes: every geometry field, mapped to its SRID.
SPATIAL = {"location": 4326, "area": 4326}


def build(params: dict[str, str], **kwargs: object) -> ListQuery:
    return ListQuery.from_params(
        params,
        spatial_fields=SPATIAL,
        filterable_fields=("name", "location"),
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------


def test_a_geometry_is_re_rendered_rather_than_passed_through() -> None:
    """The whole point. What reaches the query builder is EWKT this package
    produced from what it parsed -- never the string the URL carried."""
    query = build({"location__intersects": POINT})

    assert len(query.spatial) == 1
    condition = query.spatial[0]
    assert condition.field == "location"
    assert condition.operator is SpatialOperator.INTERSECTS
    # Latitude first going in, longitude first coming out: the conversion the
    # form's box exists to make, applied here too.
    assert condition.geometry == "SRID=4326;POINT(69.2401 41.2995)"


@pytest.mark.parametrize("operator", [op for op in SpatialOperator if not op.needs_distance])
def test_every_operator_but_dwithin_needs_only_a_geometry(operator: SpatialOperator) -> None:
    query = build({f"area__{operator.value}": BOX})

    assert len(query.spatial) == 1
    assert query.spatial[0].operator is operator
    assert query.spatial[0].distance is None


def test_a_radius_arrives_in_kilometres_and_is_kept_in_metres() -> None:
    """Kilometres in the URL because that is the unit the control offers and the
    one the question is asked in; metres in the filter because that is what
    `ST_DWithin` over a geography takes, and converting once here beats
    converting in the SQL builder where the unit would be invisible."""
    query = build({"location__dwithin": POINT, "location__km": "5"})

    assert query.spatial[0].operator is SpatialOperator.DWITHIN
    assert query.spatial[0].distance == 5000.0


def test_an_ordinary_filter_still_works_alongside_a_spatial_one() -> None:
    """`field__within` and `field__gte` are the same shape, so the spatial keys
    have to be taken out before the ordinary parser sees them -- otherwise it
    reads `location__intersects` as a filter with an unknown operator and files
    a rejection for a parameter that was perfectly valid."""
    query = build({"location__intersects": POINT, "name": "shop"})

    assert len(query.spatial) == 1
    assert len(query.filters) == 1
    assert query.filters[0].field == "name"
    assert query.rejected == ()


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_a_column_that_is_not_a_geometry_never_becomes_a_spatial_filter() -> None:
    """`spatial_fields` is the allow-list. Without it any column name plus a
    known suffix would reach an `ST_` function."""
    query = build({"name__within": BOX})
    assert query.spatial == ()


def test_an_unparseable_geometry_is_dropped_rather_than_raising() -> None:
    """A stale bookmark should still render a list page -- the rule every other
    rejected parameter here already follows."""
    query = build({"location__within": "well over there somewhere"})

    assert query.spatial == ()
    assert "location__within" in query.rejected


def test_an_unknown_spatial_operator_is_not_one() -> None:
    query = build({"location__nearish": POINT})
    assert query.spatial == ()


@pytest.mark.parametrize("radius", ["", "soon", "0", "-3", "99999"])
def test_a_radius_that_is_not_a_usable_distance_drops_the_filter(radius: str) -> None:
    """Zero matches nothing and a negative radius is not a distance. The ceiling
    is a quarter of the way round the planet, past which the query means "every
    row" -- which the unfiltered list already answers more cheaply."""
    query = build({"location__dwithin": POINT, "location__km": radius})

    assert query.spatial == ()
    assert "location__km" in query.rejected


def test_dwithin_without_a_radius_is_not_a_query() -> None:
    query = build({"location__dwithin": POINT})
    assert query.spatial == ()


def test_a_geometry_carrying_sql_is_read_as_a_geometry_and_refused() -> None:
    """Not because the value would be interpolated -- it is bound as a parameter
    and re-rendered besides -- but because nothing that is not a geometry should
    survive this boundary at all."""
    query = build({"location__within": "'); DROP TABLE shop; --"})

    assert query.spatial == ()
    assert "location__within" in query.rejected
