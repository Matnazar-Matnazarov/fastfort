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

    # No browser has a control for a duration, so this is the column that
    # proves the one FastFort draws is reachable from a real model.
    warranty: Mapped[dt.timedelta | None] = mapped_column(sa.Interval(), default=None)

    public_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), default=uuid.uuid4)
    attributes: Mapped[dict | None] = mapped_column(sa.JSON(), default=None)

    # A plain string column, exactly like `brand_colour` elsewhere: the database
    # holds a path, never bytes. `formfield_overrides` is what says this one
    # means "file" rather than "text".
    attachment: Mapped[str | None] = mapped_column(sa.String(500), default=None)

    # Name-based detection must mark this sensitive without being told.
    api_secret: Mapped[str | None] = mapped_column(sa.String(64), default=None)

    category_id: Mapped[int | None] = mapped_column(sa.ForeignKey("category.id"), default=None)
    category: Mapped[Category | None] = relationship(back_populates="products")
    tags: Mapped[list[Tag]] = relationship(secondary=product_tag)
    listing: Mapped[Listing | None] = relationship(back_populates="product", uselist=False)

    def __str__(self) -> str:
        return self.name


class Listing(Base):
    """A one-to-one whose key is on this side.

    The half that matters is the other one: `Product.listing` has no column on
    the product's own table, so the spec must offer neither sorting nor
    filtering by it. Nothing else in the suite had that shape, and the sandboxes
    that did found it as a 500.
    """

    __tablename__ = "listing"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(40), unique=True)
    product_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("category_product.id"), unique=True, default=None
    )
    product: Mapped[Product | None] = relationship(back_populates="listing")

    def __str__(self) -> str:
        return self.code


class StockLevel(Base):
    """A composite primary key, which must work everywhere a single one does."""

    __tablename__ = "stock_level"

    warehouse: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    sku: Mapped[str] = mapped_column(sa.String(40), primary_key=True)
    quantity: Mapped[int] = mapped_column(default=0)


class StaffUser(Base):
    """The user model the admin tests sign in with.

    A mapped model rather than a stand-in object, because authentication looks a
    user up through the adapter and a plain class has no row to find.
    """

    __tablename__ = "staff_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True)
    full_name: Mapped[str | None] = mapped_column(sa.String(120), default=None)
    hashed_password: Mapped[str] = mapped_column(sa.String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_staff: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)

    # What the dashboard's signups chart counts. Every real user model records
    # this; without it here the chart's only coverage would be its own unit
    # tests, and never the path the dashboard actually takes.
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    def __str__(self) -> str:
        return self.email
