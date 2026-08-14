"""Columns FastFort knows how to fill, as mixins a project can inherit.

FastFort ships no migrations and creates no tables: a project's schema is the
project's. What it can do is hand over the column definitions, so that turning a
feature on is one class rather than a page of `mapped_column` calls that have to
match a list of names exactly.

    class SignInRecord(SignInRecordMixin, Base):
        __tablename__ = "admin_sign_in"

    fort.record_sign_ins(SignInRecord)

Everything here is a plain column. There is deliberately no foreign key to the
user table: an audit row that cascades away with the account is not an audit
row, and "who deleted this account, from where" is exactly what these are for.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from fastfort.auth.devices import DEVICE_KINDS

__all__ = ["SignInRecordMixin"]


class SignInRecordMixin:
    """Every column `fastfort.auth.SignIn` can fill.

    Widths are deliberate rather than uniform: a user-agent is a sentence and
    everything else is a token, and a `TEXT` column per field costs an index
    that cannot be built on several databases.

    A project that wants fewer columns can declare fewer -- FastFort writes the
    ones that exist and requires only `at`, `successful` and `address`.
    """

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )

    #: When, in UTC. Indexed because every question asked of this table is
    #: "recently", and a sign-in log is the fastest-growing table in an admin.
    at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True
    )

    successful: Mapped[bool] = mapped_column(sa.Boolean(), default=False, index=True)

    #: What was typed into the identity box, stored as typed. Not proof of an
    #: account: a password typed into the wrong field lands here, which is a
    #: reason to review this column rather than to trust it.
    identity: Mapped[str] = mapped_column(sa.String(255), default="")

    #: The account's primary key as text, or "" for a refusal. Text rather than a
    #: foreign key, so the row outlives the account it describes.
    user_key: Mapped[str] = mapped_column(sa.String(64), default="")

    #: Indexed: "everything from this address" is the query somebody runs while
    #: something is going wrong.
    address: Mapped[str] = mapped_column(sa.String(64), default="", index=True)

    browser: Mapped[str] = mapped_column(sa.String(64), default="")
    platform: Mapped[str] = mapped_column(sa.String(64), default="")

    #: desktop, mobile, tablet, bot or unknown -- as a fixed set rather than as
    #: free text, so the admin can filter and group by it. `native_enum=False`
    #: keeps it a VARCHAR with a CHECK constraint, which is one schema on all
    #: three databases and needs no type to be created or migrated.
    kind: Mapped[str] = mapped_column(
        sa.Enum(*DEVICE_KINDS, name="ff_device_kind", native_enum=False, length=16),
        default="unknown",
    )

    #: The raw header, kept because the fields above are a reading of it.
    user_agent: Mapped[str] = mapped_column(sa.String(400), default="")

    #: Filled only when the project passed `locate=` -- FastFort bundles no GeoIP
    #: database and calls no service.
    location: Mapped[str] = mapped_column(sa.String(120), default="")
