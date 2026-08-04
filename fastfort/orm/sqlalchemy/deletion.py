"""Working out what a delete would take with it, before it is attempted.

The rows that point at a row about to be deleted are found from the *child* side:
every mapper in the registry is asked which of its many-to-one relationships lead
here. Reading the parent's own relationships would have been shorter and would
have missed every foreign key whose model never declared a back reference -- which
is most of them in a schema nobody wrote for an admin panel.

What happens to those rows is not a choice this module makes. It reads what the
ORM and the schema have already decided, in this order:

1. an ORM cascade on the parent side (``cascade="all, delete-orphan"``) deletes them;
2. ``ON DELETE CASCADE`` on the foreign key deletes them, in the database;
3. ``ON DELETE SET NULL`` clears the column;
4. a nullable foreign key is cleared -- SQLAlchemy nulls it on flush;
5. anything else is protected: `NOT NULL` with nothing to cascade, so the
   database will refuse the delete and the admin says so instead of showing a
   stack trace afterwards.

Cascades are followed, because "4 categories" is not a useful warning when what
actually disappears is the six hundred products under them. Following them costs
one query per relation per level, so both the depth and the number of rows read
are capped -- a confirmation page must not scan the database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapper, RelationshipProperty
from sqlalchemy.orm.interfaces import MANYTOONE, ONETOMANY

from fastfort.spec import DeletionEffect, DeletionPlan, RelatedRows

from .introspect import humanise, pluralise

__all__ = ["DELETION_COUNT_CAP", "DELETION_DEPTH", "DELETION_SAMPLE", "collect_deletion"]

#: How many related rows are named individually. Enough to recognise what is
#: about to go; a list of ten thousand is not a confirmation either.
DELETION_SAMPLE = 5

#: Counting stops here. A confirmation page that runs an uncapped `COUNT(*)` over
#: an unindexed foreign key is a page that hangs on exactly the tables where the
#: warning matters most, so past this the interface says "1000+".
DELETION_COUNT_CAP = 1000

#: Levels of cascade followed. Deep enough for the schemas people write
#: (order → item → allocation), bounded so a cycle or a wide graph cannot turn
#: one confirmation into hundreds of queries.
DELETION_DEPTH = 3


async def collect_deletion(
    session: AsyncSession,
    model: type[Any],
    objects: Sequence[Any],
    *,
    resolve_key: Any,
    label_of: Any,
    depth: int = DELETION_DEPTH,
) -> DeletionPlan:
    """Describe what deleting `objects` would do.

    `resolve_key` names a model in the registry and `label_of` turns one row into
    the string a person reads; both are injected because neither is something the
    ORM layer is allowed to decide.
    """
    if not objects:
        return DeletionPlan()

    mapper: Mapper[Any] = sa.inspect(model)
    collected: dict[tuple[str, str], RelatedRows] = {}
    await _walk(
        session,
        mapper,
        _identifies(mapper, objects),
        objects=list(objects),
        collected=collected,
        resolve_key=resolve_key,
        label_of=label_of,
        depth=depth,
    )

    return DeletionPlan(
        targets=tuple(label_of(obj) for obj in objects),
        # Protected first: it is the one that changes what the page offers,
        # rather than merely what it says.
        related=tuple(sorted(collected.values(), key=_ordering)),
    )


def _ordering(row: RelatedRows) -> tuple[int, str, str]:
    rank = {DeletionEffect.PROTECT: 0, DeletionEffect.DELETE: 1, DeletionEffect.CLEAR: 2}
    return (rank[row.effect], row.label, row.field)


async def _walk(
    session: AsyncSession,
    mapper: Mapper[Any],
    criteria: sa.ColumnElement[bool] | None,
    *,
    objects: list[Any] | None,
    collected: dict[tuple[str, str], RelatedRows],
    resolve_key: Any,
    label_of: Any,
    depth: int,
) -> None:
    """Add every relation pointing at these rows, recursing into the cascades.

    The rows are named twice over: as `objects` at the top, where they are in
    hand, and as `criteria` -- a predicate over this model's table -- which is
    what a deeper level has. The second is what makes a cascade's own children
    countable: they are found through a subquery rather than through the handful
    of rows this level happened to sample, which would report a fraction of them
    and call it the total.
    """
    if criteria is None:
        return

    for rel in _incoming(mapper):
        predicate = _points_at(rel, criteria, objects)
        if predicate is None:
            continue

        child = rel.parent
        effect = _effect_of(rel, mapper)

        rows, total, truncated = await _sample(session, child.class_, predicate)
        if not total:
            continue

        named = humanise(child.class_.__name__)
        entry = RelatedRows(
            model_key=resolve_key(child.class_),
            label=pluralise(named),
            singular=named,
            field=rel.key,
            effect=effect,
            count=total,
            truncated=truncated,
            samples=tuple(label_of(row) for row in rows),
        )
        # Two foreign keys from the same model can lead here (an order with a
        # billing address and a shipping address); they are separate entries, and
        # the stricter of two paths through the same one is the one that decides.
        seat = (entry.model_key or entry.label, entry.field)
        existing = collected.get(seat)
        if existing is None or _ordering(entry) < _ordering(existing):
            collected[seat] = entry

        # Only a cascade continues: rows that merely have a column cleared keep
        # whatever points at them, so there is nothing further to warn about.
        if effect is DeletionEffect.DELETE and depth > 1:
            await _walk(
                session,
                child,
                predicate,
                objects=None,
                collected=collected,
                resolve_key=resolve_key,
                label_of=label_of,
                depth=depth - 1,
            )


def _incoming(mapper: Mapper[Any]) -> list[RelationshipProperty[Any]]:
    """Every many-to-one relationship in the registry that lands on `mapper`."""
    found: list[RelationshipProperty[Any]] = []
    for other in mapper.registry.mappers:
        for rel in other.relationships:
            # Many-to-many is left out on purpose: deleting a row removes its
            # association rows and nothing else. Reporting "3 tags" would say
            # that three tags are at risk, which is false.
            if rel.direction is not MANYTOONE:
                continue
            if not rel.mapper.isa(mapper):
                continue
            found.append(rel)
    return found


def _effect_of(rel: RelationshipProperty[Any], parent: Mapper[Any]) -> DeletionEffect:
    """What the schema and the mappings say happens to `rel`'s rows."""
    reverse = _reverse_of(rel, parent)
    if reverse is not None and not reverse.viewonly and "delete" in reverse.cascade:
        return DeletionEffect.DELETE

    ondelete = {_ondelete(column) for column in rel.local_columns}
    if "CASCADE" in ondelete:
        return DeletionEffect.DELETE
    if ondelete & {"SET NULL", "SET DEFAULT"}:
        return DeletionEffect.CLEAR

    # SQLAlchemy nulls the column itself on flush, but only through a
    # relationship it is allowed to write. A view-only one leaves the row alone,
    # and the foreign key constraint then refuses the parent's delete.
    nullable = all(bool(column.nullable) for column in rel.local_columns)
    if nullable and reverse is not None and not reverse.viewonly:
        return DeletionEffect.CLEAR
    return DeletionEffect.PROTECT


def _reverse_of(
    rel: RelationshipProperty[Any], parent: Mapper[Any]
) -> RelationshipProperty[Any] | None:
    """The parent-side relationship over the same foreign key, when there is one.

    Matched by columns rather than by `back_populates`: the cascade that matters
    is declared on whichever relationship writes these columns, and a pair joined
    by `backref` -- or by nothing at all -- still writes them.
    """
    columns = set(rel.local_columns)
    for candidate in parent.relationships:
        if candidate.direction is not ONETOMANY:
            continue
        if candidate.mapper is not rel.parent:
            continue
        if columns <= set(candidate.remote_side):
            return candidate
    return None


def _ondelete(column: sa.ColumnElement[Any]) -> str:
    """The `ON DELETE` clause on a column's foreign key, upper-cased.

    A relationship's local side is typed as an expression rather than a column,
    and only a real column carries foreign keys -- a relationship joined by an
    expression has none to read.
    """
    for key in getattr(column, "foreign_keys", ()):
        if key.ondelete:
            return str(key.ondelete).strip().upper()
    return ""


def _identifies(mapper: Mapper[Any], objects: Sequence[Any]) -> sa.ColumnElement[bool] | None:
    """A predicate matching exactly `objects`, by primary key."""
    return _matches(list(mapper.primary_key), _values_of(mapper, mapper.primary_key, objects))


def _points_at(
    rel: RelationshipProperty[Any],
    criteria: sa.ColumnElement[bool],
    objects: Sequence[Any] | None,
) -> sa.ColumnElement[bool] | None:
    """A predicate matching the rows on `rel`'s side that lead to those parents.

    With the parent rows in hand it is written against their values, which is
    one statement and no join. Without them -- a level down a cascade -- it is
    written as a subquery over `criteria`, so the count is of every row rather
    than of the few this walk happened to load.

    A composite foreign key at depth returns None: expressing it needs a row
    constructor in `IN`, which the three supported databases do not agree on
    closely enough to be worth relying on for a warning.
    """
    pairs = list(rel.local_remote_pairs or ())
    if not pairs:
        return None

    locals_ = [local for local, _ in pairs]
    remotes = [remote for _, remote in pairs]

    if objects is not None:
        return _matches(locals_, _values_of(rel.mapper, remotes, objects))
    if len(pairs) != 1:
        return None
    return locals_[0].in_(sa.select(remotes[0]).where(criteria))


def _values_of(
    mapper: Mapper[Any], columns: Sequence[Any], objects: Sequence[Any]
) -> list[tuple[Any, ...]]:
    """Read `columns` off each row, as the tuples a predicate compares against.

    A foreign key may target any unique column, not only the primary key, so the
    columns are read from the relationship rather than assumed.
    """
    names: list[str] = []
    for column in columns:
        try:
            names.append(mapper.get_property_by_column(column).key)
        except Exception:
            # Joined through something that is not a mapped column of the
            # target. Nothing here can read its value, and guessing would be
            # worse than leaving the relation out.
            return []

    values: list[tuple[Any, ...]] = []
    for obj in objects:
        row = tuple(getattr(obj, name, None) for name in names)
        # An unsaved or partially loaded row points at nothing; skip it rather
        # than counting every child whose foreign key happens to be NULL.
        if any(part is None for part in row):
            continue
        values.append(row)
    return values


def _matches(
    columns: Sequence[Any], keys: Sequence[tuple[Any, ...]]
) -> sa.ColumnElement[bool] | None:
    if not columns or not keys:
        return None
    if len(columns) == 1:
        return cast("sa.ColumnElement[bool]", columns[0].in_([key[0] for key in keys]))
    # Composite keys cannot use a single IN, so they become an OR of ANDs.
    return sa.or_(
        *(
            sa.and_(*(column == value for column, value in zip(columns, key, strict=True)))
            for key in keys
        )
    )


async def _sample(
    session: AsyncSession, model: type[Any], predicate: sa.ColumnElement[bool]
) -> tuple[list[Any], int, bool]:
    """A handful of matching rows, how many there are, and whether that is a cap.

    The count is taken over a capped subquery rather than the table: `COUNT(*)`
    on an unindexed foreign key is a sequential scan, and the page that runs it
    is the one somebody opens to decide whether a delete is safe.
    """
    rows = list(
        (await session.execute(sa.select(model).where(predicate).limit(DELETION_SAMPLE)))
        .unique()
        .scalars()
        .all()
    )
    if len(rows) < DELETION_SAMPLE:
        return rows, len(rows), False

    capped = (
        sa.select(sa.literal(1))
        .select_from(model)
        .where(predicate)
        .limit(DELETION_COUNT_CAP)
        .subquery()
    )
    counted = await session.execute(sa.select(sa.func.count()).select_from(capped))
    total = int(counted.scalar_one())
    return rows, total, total >= DELETION_COUNT_CAP
