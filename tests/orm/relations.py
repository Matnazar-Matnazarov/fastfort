"""Models whose only job is to be pointed at, for deletion planning.

Three shapes, because a delete does three different things depending on how the
foreign key was declared and nothing but a real mapping proves which:

* `Crate` cascades -- `Depot.crates` says ``delete-orphan``, so the rows go too;
* `Ledger` protects -- a `NOT NULL` foreign key with nothing cascading, which is
  the delete the database refuses;
* `Note` is cleared -- nullable, so the rows survive with the column emptied.

They live apart from `tests/orm/models.py` on the same `Base`, so the schema is
created with everything else while the models the rest of the suite introspects
stay exactly as they were.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .models import Base


class Depot(Base):
    __tablename__ = "depot"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(60), unique=True)

    crates: Mapped[list[Crate]] = relationship(back_populates="depot", cascade="all, delete-orphan")
    notes: Mapped[list[Note]] = relationship(back_populates="depot")

    def __str__(self) -> str:
        return self.name


class Crate(Base):
    """Goes with its depot, and takes its own items with it."""

    __tablename__ = "crate"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(60))
    depot_id: Mapped[int] = mapped_column(sa.ForeignKey("depot.id"))
    depot: Mapped[Depot] = relationship(back_populates="crates")
    items: Mapped[list[Item]] = relationship(back_populates="crate", cascade="all, delete-orphan")

    def __str__(self) -> str:
        return self.label


class Item(Base):
    """Two levels down.

    Deleting a depot removes the crates and therefore these, which is what makes
    "2 crates" the wrong number to warn somebody with when what actually goes is
    the four hundred items inside them.
    """

    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(sa.String(60))
    crate_id: Mapped[int] = mapped_column(sa.ForeignKey("crate.id"))
    crate: Mapped[Crate] = relationship(back_populates="items")

    def __str__(self) -> str:
        return self.sku


class Ledger(Base):
    """Refuses to let its depot go.

    `Depot` declares no reverse relationship on purpose: without one there is no
    cascade to find, the column cannot be nulled, and the foreign key constraint
    is what the delete runs into. That is the case the confirmation page has to
    catch before the transaction does.
    """

    __tablename__ = "ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    reference: Mapped[str] = mapped_column(sa.String(60))
    depot_id: Mapped[int] = mapped_column(sa.ForeignKey("depot.id"))
    depot: Mapped[Depot] = relationship()

    def __str__(self) -> str:
        return self.reference


class Note(Base):
    """Survives its depot, with the link cleared."""

    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(sa.String(120))
    depot_id: Mapped[int | None] = mapped_column(sa.ForeignKey("depot.id"), default=None)
    depot: Mapped[Depot | None] = relationship(back_populates="notes")

    def __str__(self) -> str:
        return self.body
