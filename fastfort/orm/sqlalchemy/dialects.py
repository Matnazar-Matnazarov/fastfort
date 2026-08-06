"""Where SQLite, PostgreSQL and MySQL disagree, and what we do about it.

Everything the query builder does that is not identical on all three databases
goes through this module. Keeping the differences in one file is what makes
"works the same on every supported database" a claim we can actually test rather
than a hope.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import ColumnElement, func

__all__ = ["DialectProfile", "icontains", "order_term", "profile_for", "spatial_condition"]


@dataclass(frozen=True, slots=True)
class DialectProfile:
    """The capabilities of one database, as far as FastFort cares."""

    name: str

    #: PostgreSQL has ILIKE. Elsewhere we lower both sides, which is correct but
    #: cannot use a plain index, so admins on MySQL/SQLite should add a
    #: functional or case-insensitive-collation index for large tables.
    has_ilike: bool

    #: `ORDER BY col DESC NULLS LAST`. MySQL has no such clause.
    has_nulls_ordering: bool

    #: `INSERT ... RETURNING`. MySQL 8 does not support it, so writes flush and
    #: refresh instead of relying on it.
    has_returning: bool

    #: Maximum indexable characters for a utf8mb4 VARCHAR. MySQL caps an index
    #: key at 3072 bytes, which is 768 four-byte characters.
    max_index_chars: int | None

    #: Whether DDL participates in the surrounding transaction. MySQL commits
    #: implicitly on DDL, so migrations there cannot be rolled back as a unit.
    ddl_is_transactional: bool

    #: Whether PostGIS answered when the backend asked. Unlike every other flag
    #: here this cannot be read off the dialect name: PostGIS is an extension,
    #: so two PostgreSQL servers disagree about it. `SQLAlchemyBackend` probes
    #: once and replaces the profile; until it has, this stays false, which is
    #: the safe direction -- a spatial filter that is never offered is a missing
    #: feature, one that is offered and then fails is a broken page.
    has_postgis: bool = False


_PROFILES = {
    "postgresql": DialectProfile(
        name="postgresql",
        has_ilike=True,
        has_nulls_ordering=True,
        has_returning=True,
        max_index_chars=None,
        ddl_is_transactional=True,
    ),
    "mysql": DialectProfile(
        name="mysql",
        has_ilike=False,
        has_nulls_ordering=False,
        has_returning=False,
        max_index_chars=768,
        ddl_is_transactional=False,
    ),
    "sqlite": DialectProfile(
        name="sqlite",
        has_ilike=False,
        has_nulls_ordering=True,
        has_returning=True,
        max_index_chars=None,
        ddl_is_transactional=False,
    ),
}

#: Dialect names that are really one of the supported three under another name.
_ALIASES = {
    "postgres": "postgresql",
    "mariadb": "mysql",
}


def profile_for(dialect_name: str) -> DialectProfile:
    """Return the profile for a SQLAlchemy dialect name.

    An unknown dialect gets the most conservative profile rather than an error:
    FastFort has not been tested against it, but assuming the smallest feature
    set produces correct, if slower, SQL.
    """
    name = _ALIASES.get(dialect_name, dialect_name)
    return _PROFILES.get(
        name,
        DialectProfile(
            name=name,
            has_ilike=False,
            has_nulls_ordering=False,
            has_returning=False,
            max_index_chars=768,
            ddl_is_transactional=False,
        ),
    )


def _escape_like(term: str) -> str:
    """Neutralise LIKE wildcards in user input.

    Without this a search for ``%`` matches every row, which is a cheap way to
    make an admin list page scan a whole table.
    """
    return term.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def icontains(
    column: Any, term: str, profile: DialectProfile, *, anchor: str = "anywhere"
) -> ColumnElement[bool]:
    """Case-insensitive match that behaves identically on all three databases.

    `anchor` selects where the term must appear: ``anywhere``, ``start`` or
    ``end``.
    """
    escaped = _escape_like(term)
    pattern = {
        "start": f"{escaped}%",
        "end": f"%{escaped}",
    }.get(anchor, f"%{escaped}%")
    if profile.has_ilike:
        return cast("ColumnElement[bool]", column.ilike(pattern, escape="\\"))
    return cast("ColumnElement[bool]", func.lower(column).like(func.lower(pattern), escape="\\"))


def order_term(column: Any, *, descending: bool, profile: DialectProfile) -> list[Any]:
    """Build ORDER BY terms that place NULLs last on every database.

    Consistency matters more than matching each database's default here: a list
    that puts empty values first on MySQL and last on PostgreSQL looks broken to
    anyone who uses both.
    """
    direction = column.desc() if descending else column.asc()

    if profile.has_nulls_ordering:
        return [direction.nulls_last()]

    # `col IS NULL` yields 0/1, and sorting by it ascending puts real values
    # first. This is the standard MySQL idiom for the missing clause.
    return [column.is_(None).asc(), direction]


#: The PostGIS function each spatial operator compiles to.
#:
#: All of them take (column, geometry) in that order, which is why one table
#: serves for seven of the eight; `dwithin` takes a third argument and is built
#: separately below.
_SPATIAL_FUNCTIONS = {
    "within": "ST_Within",
    "contains": "ST_Contains",
    "intersects": "ST_Intersects",
    "overlaps": "ST_Overlaps",
    "touches": "ST_Touches",
    "crosses": "ST_Crosses",
    # `&&` is the bounding-box operator and the one a spatial index answers
    # directly, but it is an operator rather than a function; `ST_Intersects`
    # on the box is the portable spelling of the same question and is what a
    # map viewport actually means.
    "bbox": "ST_Intersects",
}


def spatial_condition(
    column: Any,
    operator: str,
    geometry: str,
    distance: float | None,
    profile: DialectProfile,
) -> ColumnElement[bool] | None:
    """One `ST_` predicate, or `None` where it cannot be run.

    Returning `None` rather than raising is the whole contract: a saved link
    carrying a spatial filter is still a perfectly good request for a list page
    on a database without PostGIS, and taking the page down over a condition
    that cannot be evaluated helps nobody. The filter panel does not offer the
    control there in the first place, so reaching this at all means a URL
    outlived the database it was made against.

    `geometry` is EWKT that `spec.geo` produced -- it is bound as a parameter,
    never interpolated, and `ST_GeomFromEWKT` is what turns it back into a
    geometry inside the database.
    """
    if not profile.has_postgis:
        return None

    shape = func.ST_GeomFromEWKT(geometry)

    if operator == "dwithin":
        if distance is None:
            return None
        # Both sides through PostGIS's own `geography()` cast, which is what
        # makes the radius mean metres. Without it, 5000 against a 4326
        # *geometry* column is five thousand degrees -- which matches every row
        # on the planet, and reads as the filter simply not working.
        #
        # `func.geography(...)` rather than `sa.cast(column, Geography)`: the
        # second spelling needs GeoAlchemy2 imported here, and this package
        # promises that a project not using PostGIS never pays for an import of
        # it. The function form is plain SQL that PostGIS defines itself.
        return cast(
            "ColumnElement[bool]",
            func.ST_DWithin(func.geography(column), func.geography(shape), float(distance)),
        )

    name = _SPATIAL_FUNCTIONS.get(operator)
    if name is None:
        return None
    if operator == "bbox":
        # The envelope on both sides, which is the cheap question a viewport is
        # asking -- "is any part of this row's shape in the box I am looking
        # at" -- rather than an exact intersection test per row.
        return cast(
            "ColumnElement[bool]",
            func.ST_Intersects(func.ST_Envelope(column), func.ST_Envelope(shape)),
        )
    return cast("ColumnElement[bool]", getattr(func, name)(column, shape))
