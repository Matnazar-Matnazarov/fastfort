"""What deleting a row would do to everything pointing at it.

A delete button that only says "this cannot be undone" is not a confirmation. The
question anybody actually has is *what else goes*, and the honest answer depends
on the schema: a child row whose foreign key is nullable survives with the column
cleared, one whose relationship cascades goes with the parent, and one whose
foreign key is `NOT NULL` with no cascade cannot go anywhere -- the database will
refuse the delete outright.

A `DeletionPlan` is that answer, computed before anything is written. It is
produced by an adapter, which is the only layer that can read a foreign key's
nullability, and consumed by the confirmation page and by the delete views, which
refuse the write when the plan says the database would.

Deliberately a *description*, not a strategy: nothing here changes what happens,
it only reports what the ORM and the schema have already decided. The alternative
-- letting the admin choose per-relation, Django's `on_delete` -- belongs on the
model, where the rest of the application obeys it too, not in a panel that half
the writes go around.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["DeletionEffect", "DeletionPlan", "RelatedRows"]


class DeletionEffect(StrEnum):
    """What becomes of the rows on the far side of one relation."""

    #: Deleted along with the row, by an ORM cascade or by `ON DELETE CASCADE`.
    DELETE = "delete"
    #: Kept, with the foreign key set to NULL.
    CLEAR = "clear"
    #: Neither possible: the foreign key is `NOT NULL` and nothing cascades, so
    #: the database would reject the delete. This is what blocks a plan.
    PROTECT = "protect"


@dataclass(frozen=True, slots=True)
class RelatedRows:
    """One related model's share of a deletion, as counted before the write."""

    #: Registry key of the related model, so the page can link to it. Empty when
    #: the model has no admin of its own, which is normal.
    model_key: str
    #: Plural human name of the related model.
    label: str
    #: The same name in the singular. Carried rather than derived, because
    #: "1 products" is what a sentence that only has the plural comes out as.
    singular: str
    #: The relation as named on the *related* model -- the attribute whose
    #: foreign key is the one at stake, which is what makes the sentence
    #: "Products.category" readable rather than ambiguous.
    field: str
    effect: DeletionEffect
    count: int
    #: True when counting stopped at the scan cap, so `count` is a floor rather
    #: than a total. Confirming a delete must not scan a large table.
    truncated: bool = False
    #: A few of the rows, named the way the list view names them.
    samples: tuple[str, ...] = ()

    @property
    def more(self) -> int:
        """How many rows are not in `samples`."""
        return max(0, self.count - len(self.samples))


@dataclass(frozen=True, slots=True)
class DeletionPlan:
    """Everything one delete would touch."""

    #: Labels of the rows being deleted, in the order they were given.
    targets: tuple[str, ...] = ()
    related: tuple[RelatedRows, ...] = ()

    @property
    def protected(self) -> tuple[RelatedRows, ...]:
        return tuple(row for row in self.related if row.effect is DeletionEffect.PROTECT)

    @property
    def cascaded(self) -> tuple[RelatedRows, ...]:
        return tuple(row for row in self.related if row.effect is DeletionEffect.DELETE)

    @property
    def cleared(self) -> tuple[RelatedRows, ...]:
        return tuple(row for row in self.related if row.effect is DeletionEffect.CLEAR)

    @property
    def blocked(self) -> bool:
        """Whether the database would refuse this delete."""
        return bool(self.protected)

    @property
    def touches_anything(self) -> bool:
        return any(row.count for row in self.related)
