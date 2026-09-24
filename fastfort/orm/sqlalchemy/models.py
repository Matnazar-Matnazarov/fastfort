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
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from fastfort.auth.devices import DEVICE_KINDS

__all__ = ["ApiTokenMixin", "AuditEntryMixin", "FavoriteMixin", "SignInRecordMixin"]


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


class ApiTokenMixin:
    """Every column `fastfort.auth.ApiTokens` needs.

        class ApiToken(ApiTokenMixin, Base):
            __tablename__ = "admin_api_token"

        fort.enable_api_tokens(ApiToken)

    The secret itself is never stored. What is stored is its SHA-256 digest,
    and the only moment the secret exists in readable form is the response
    that created it -- which is why the admin shows it once and cannot show it
    again.

    Unlike `SignInRecordMixin` there is no argument for outliving the account:
    a token belonging to a deleted user must stop working, not keep working.
    `user_key` is still text rather than a foreign key, because a mixin cannot
    know what the user table is called -- resolution looks the account up on
    every request and refuses a token whose owner has gone. A project that
    wants the rows cleaned up too can declare its own foreign key column
    beside these.
    """

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )

    #: What this token is for, in somebody's own words. The only field on the
    #: row a person chooses, and the only one that makes a list of eight
    #: tokens readable.
    name: Mapped[str] = mapped_column(sa.String(100), default="")

    #: SHA-256 of the secret, hex. Unique, so a collision is a constraint
    #: violation rather than two accounts sharing a credential, and indexed
    #: because this is the lookup on every authenticated request.
    #:
    #: SHA-256 rather than Argon2 deliberately -- see `auth/api_tokens.py`,
    #: which explains why a full-entropy secret needs no work factor and why
    #: putting one here would cost every request about a tenth of a second.
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)

    #: The first few characters of the secret, kept in the clear. It is what
    #: lets a person match a row in this table against the token in their
    #: configuration file without being able to reconstruct the rest.
    prefix: Mapped[str] = mapped_column(sa.String(16), default="", index=True)

    #: The owning account's primary key, as text.
    user_key: Mapped[str] = mapped_column(sa.String(64), default="", index=True)

    #: Space-separated names the project defines and checks. FastFort stores
    #: and returns them and takes no view on what they mean: a scope
    #: vocabulary belongs to the API being protected, not to the admin.
    scopes: Mapped[str] = mapped_column(sa.String(255), default="")

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    #: Null means it does not expire. A default of "never" is the honest one
    #: for a machine credential -- an expiry nobody chose is an outage nobody
    #: predicted.
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), default=None, nullable=True
    )

    #: Written on use, which is what makes "this one has not been touched in a
    #: year" answerable -- the question that gets old credentials revoked.
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), default=None, nullable=True
    )

    #: Revoked rather than deleted, so the row still answers "what was this,
    #: and when did it stop". A revoked token is refused before its expiry is
    #: even looked at.
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        sa.DateTime(timezone=True), default=None, nullable=True
    )


class FavoriteMixin:
    """Every column `fastfort.auth.Favorites` needs.

        class Favorite(FavoriteMixin, Base):
            __tablename__ = "admin_favorite"

        fort.enable_favorites(Favorite)

    One table holds the starred rows of every registered model, which is why
    the target is named by two strings rather than by a foreign key. A column
    that points at one table cannot point at forty, and the alternative --
    forty join tables, one per model, created as models are registered -- is a
    schema that changes when a decorator moves.

    So the target is spelled the way the admin already spells it everywhere
    else: the registry key for *which model*, and the primary key joined on
    "~" for *which row*. That is the same pair a URL carries, which means a
    favorite can be turned back into a link without a lookup.

    The cost of no foreign key is that nothing cascades, and a starred row that
    is deleted would leave this one behind pointing at nothing. `Favorites`
    pays that off by listening for `AFTER_DELETE` and clearing the rows itself,
    and by skipping targets it cannot resolve when it reads -- a row deleted by
    a migration, outside the admin entirely, still cannot show up as a broken
    entry on somebody's list.
    """

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )

    #: The registry key of the starred model, `shop.product`. Indexed together
    #: with `object_key` below rather than on its own.
    model_key: Mapped[str] = mapped_column(sa.String(120), default="")

    #: The starred row's primary key as text, "~"-joined for a composite one --
    #: the same spelling `object_key` has in an admin URL.
    object_key: Mapped[str] = mapped_column(sa.String(255), default="")

    #: Whose star this is. Text rather than a foreign key for the same reason
    #: as `ApiTokenMixin.user_key`: a mixin cannot know the user table's name.
    user_key: Mapped[str] = mapped_column(sa.String(64), default="")

    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    #: Declared by the subclass. Named here so the constraint names below can
    #: be built from it -- two models inheriting this mixin need two differently
    #: named constraints, and a fixed name would collide on the second.
    __tablename__: str

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:  # noqa: N805
        # Unique because starring twice is the same statement made twice, and
        # without this a double-submitted form grows the table forever. The
        # service still deletes every match when unstarring, so a row that
        # predates this constraint cannot strand a star that will not turn off.
        #
        # The composite index leads with `user_key`: every read is "what has
        # *this person* starred", either across all models or narrowed to one,
        # and an index that leads with `model_key` cannot serve the first.
        return (
            sa.UniqueConstraint(
                "user_key", "model_key", "object_key", name=f"uq_{cls.__tablename__}_target"
            ),
            sa.Index(f"ix_{cls.__tablename__}_owner", "user_key", "model_key"),
        )


class AuditEntryMixin:
    """Every column `fastfort.contrib.audit.AuditLog` writes.

        class AuditEntry(AuditEntryMixin, Base):
            __tablename__ = "admin_audit"

        fort.enable_audit_log(AuditEntry)

    No foreign keys, for the reason `SignInRecordMixin` has none: a log that
    cascades away with the row or the account it describes cannot answer who
    deleted either of them. The target is the registry key and the "~"-joined
    primary key -- the pair an admin URL carries -- and the actor is text.
    """

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )
    #: `create`, `update` or `delete`.
    action: Mapped[str] = mapped_column(sa.String(16), default="")
    model_key: Mapped[str] = mapped_column(sa.String(120), default="")
    object_key: Mapped[str] = mapped_column(sa.String(255), default="")
    #: What the row was called when this happened. Stored rather than looked up,
    #: because a deleted row has nothing left to look up.
    object_label: Mapped[str] = mapped_column(sa.String(255), default="")
    user_key: Mapped[str] = mapped_column(sa.String(64), default="")
    user_label: Mapped[str] = mapped_column(sa.String(255), default="")
    #: IPv6 at its longest is 45 characters.
    address: Mapped[str] = mapped_column(sa.String(45), default="")
    #: JSON, `{"field": [before, after]}`. Text rather than a JSON column so the
    #: one mixin works on SQLite, PostgreSQL and MySQL without a dialect branch.
    changes: Mapped[str] = mapped_column(sa.Text, default="")

    __tablename__: str

    @declared_attr.directive
    def __table_args__(cls) -> tuple[Any, ...]:  # noqa: N805
        # The two questions this table is asked: "what happened to this row",
        # which the history page asks, and "what happened lately", which the
        # activity page asks. Each index leads with what its question filters on.
        return (
            sa.Index(f"ix_{cls.__tablename__}_target", "model_key", "object_key", "at"),
            sa.Index(f"ix_{cls.__tablename__}_at", "at"),
        )
