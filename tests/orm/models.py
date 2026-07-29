"""A deliberately awkward model set, used to exercise the SQLAlchemy backend.

Every column here exists to pin down one behaviour: a type that maps differently
on each database, a constraint that changes what a form may write, or a relation
shape the query builder has to handle. Portability rules are followed on purpose
-- every String has a length, because MySQL rejects one without.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Status(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


product_tag = sa.Table(
    "product_tag",
    Base.metadata,
    sa.Column(
        "product_id", sa.ForeignKey("category_product.id", ondelete="CASCADE"), primary_key=True
    ),
    sa.Column("tag_id", sa.ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), unique=True)
    products: Mapped[list[Product]] = relationship(back_populates="category")

    def __str__(self) -> str:
        return self.name


class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(60), unique=True)


class Product(Base):
    """Named `category_product` in the database to prove the key comes from the class."""

    __tablename__ = "category_product"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    description: Mapped[str | None] = mapped_column(sa.Text(), default=None)

    # Money is Numeric, never Float; rendering it must not lose precision.
    price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2), default=Decimal("0.00"))
    stock: Mapped[int] = mapped_column(sa.BigInteger(), default=0)
    weight: Mapped[float | None] = mapped_column(sa.Float(), default=None)

    is_active: Mapped[bool] = mapped_column(sa.Boolean(), default=True)
    status: Mapped[Status] = mapped_column(
        sa.Enum(Status, native_enum=False, length=20), default=Status.DRAFT
    )

    # Nullable so that NULLS-last ordering can be exercised on every database.
    released_on: Mapped[dt.date | None] = mapped_column(sa.Date(), default=None)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    public_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), default=uuid.uuid4)
    attributes: Mapped[dict | None] = mapped_column(sa.JSON(), default=None)

    # Name-based detection must mark this sensitive without being told.
    api_secret: Mapped[str | None] = mapped_column(sa.String(64), default=None)

    category_id: Mapped[int | None] = mapped_column(sa.ForeignKey("category.id"), default=None)
    category: Mapped[Category | None] = relationship(back_populates="products")
    tags: Mapped[list[Tag]] = relationship(secondary=product_tag)

    def __str__(self) -> str:
        return self.name


class StockLevel(Base):
    """A composite primary key, which must work everywhere a single one does."""

    __tablename__ = "stock_level"

    warehouse: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    sku: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    quantity: Mapped[int] = mapped_column(default=0)
