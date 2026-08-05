"""Models that exist only to be introspected -- never created in a database.

`introspect_model` reads mapper metadata, so a PostgreSQL-only type (`INET`,
`HSTORE`, a GeoAlchemy2 `Geometry`) introspects fine without a live connection,
on any backend. These live on their own `DeclarativeBase` rather than
`tests.orm.models.Base`: the shared base's metadata is exactly what
`Base.metadata.create_all` sweeps for every SQLite-backed fixture in this suite,
and a PostgreSQL-only type fails to compile there.
"""

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa
from geoalchemy2 import Geography, Geometry, Raster
from sqlalchemy.dialects import postgresql as pg
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class ExoticBase(DeclarativeBase):
    pass


class ExoticColumn(ExoticBase):
    """One of everything the type registry adds in this phase."""

    __tablename__ = "exotic_column"

    # `Identity()` is a generated-column signal `column.computed` does not
    # cover on its own -- see the comment in `introspect.py::_column_field`.
    id: Mapped[int] = mapped_column(sa.Identity(), primary_key=True)

    blob: Mapped[bytes | None] = mapped_column(sa.LargeBinary(), default=None)
    ip: Mapped[str | None] = mapped_column(pg.INET(), default=None)
    network: Mapped[str | None] = mapped_column(pg.CIDR(), default=None)
    mac: Mapped[str | None] = mapped_column(pg.MACADDR(), default=None)
    attributes: Mapped[dict | None] = mapped_column(pg.HSTORE(), default=None)
    price: Mapped[str | None] = mapped_column(pg.MONEY(), default=None)
    flags: Mapped[str | None] = mapped_column(pg.BIT(8), default=None)
    permissions: Mapped[str | None] = mapped_column(pg.BIT(4, varying=True), default=None)
    search: Mapped[str | None] = mapped_column(pg.TSVECTOR(), default=None)
    legacy_id: Mapped[int | None] = mapped_column(pg.OID(), default=None)

    # CITEXT subclasses TEXT -- the case this phase's ordering exists to fix.
    nickname: Mapped[str | None] = mapped_column(pg.CITEXT(), default=None)

    tags: Mapped[list[str] | None] = mapped_column(sa.ARRAY(sa.String(30)), default=None)
    ratings: Mapped[list[int] | None] = mapped_column(pg.ARRAY(sa.Integer()), default=None)

    booking: Mapped[object | None] = mapped_column(pg.TSRANGE(), default=None)
    bookings: Mapped[object | None] = mapped_column(pg.TSMULTIRANGE(), default=None)
    page_numbers: Mapped[object | None] = mapped_column(pg.INT4RANGE(), default=None)

    # No CHECK constraint -- `max_value` should come from `Numeric(6, 2)` itself.
    price_cap: Mapped[Decimal] = mapped_column(sa.Numeric(6, 2), default=Decimal("0"))

    # A simple two-sided CHECK constraint, one clause per bound.
    rating: Mapped[int] = mapped_column(sa.Integer(), default=1)

    __table_args__ = (
        sa.CheckConstraint("rating >= 1"),
        sa.CheckConstraint("rating <= 5"),
    )


class SpatialColumn(ExoticBase):
    """Kept separate from `ExoticColumn` so its spatial types are not tangled up
    with everything else -- irrelevant to introspection, just readability."""

    __tablename__ = "spatial_column"

    id: Mapped[int] = mapped_column(primary_key=True)
    location: Mapped[object | None] = mapped_column(
        Geometry(geometry_type="POINT", srid=4326), default=None
    )
    area: Mapped[object | None] = mapped_column(
        Geography(geometry_type="POLYGON", srid=4326, spatial_index=False), default=None
    )
    heatmap: Mapped[object | None] = mapped_column(Raster(), default=None)
