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

__all__ = ["DialectProfile", "icontains", "order_term", "profile_for"]


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
