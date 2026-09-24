"""Starred rows: the handful of records a person actually returns to.

An admin with forty models and a hundred thousand rows has no shortage of ways
to *find* something. What it has no way of expressing is that six of those rows
are the ones somebody opens every morning -- the warehouse they run, the three
customers in escalation, the feature flag they are watching. Search finds a row
if you remember what it was called. A star remembers for you.

Turned on the way every other FastFort feature that needs a table is::

    class Favorite(FavoriteMixin, Base):
        __tablename__ = "admin_favorite"


    fort.enable_favorites(Favorite)

## Why one table and not a foreign key

The target is named by a pair of strings -- the registry key for which model,
the "~"-joined primary key for which row -- rather than by a foreign key. A
column that points at one table cannot point at forty, and the alternative is a
join table per registered model, created and dropped as decorators move. That
is a schema that changes when code moves, which is the property a schema must
not have.

The pair is not a new invention here: it is exactly what an admin URL already
carries, which is why a stored favorite turns back into a link without a
lookup.

## What that costs, and how it is paid

Nothing cascades. Delete a starred row and this table keeps a row pointing at
an address that no longer resolves.

Two things pay it off. `enable_favorites` registers an `AFTER_DELETE`
listener, so anything deleted through the admin clears its own stars on the way
out. And every read resolves its target and drops what it cannot find, so a row
deleted by a migration -- outside the admin, where no hook fires -- still
cannot surface as a broken entry on somebody's list. The first keeps the table
from growing; the second is what makes the page correct regardless.

## Whose stars these are

A favorite belongs to one account. There is no shared or team star here,
because "starred by anyone" and "starred by me" are different features with
different pages, and the second is the one that answers "where was I".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastfort.core.exceptions import ConfigurationError, ValidationError
from fastfort.core.hooks import Hook
from fastfort.core.registry import default_model_key
from fastfort.orm.coerce import coerce_filter_value
from fastfort.spec import Filter, FilterOperator, ListQuery, SortSpec

if TYPE_CHECKING:
    from fastfort.orm.base import ModelAdapter

    from .user_config import UserModelConfig

__all__ = ["Favorite", "Favorites", "object_key_of"]

#: Columns the model must have. Checked when `enable_favorites` is called, so a
#: mistyped schema is a start-up error rather than a 500 the first time
#: somebody clicks a star.
REQUIRED_FIELDS = ("model_key", "object_key", "user_key")

#: How many stars one account may hold. A favourites list is a shortcut, and a
#: shortcut with two thousand entries is a second list view with worse sorting
#: -- but the real reason for a ceiling is that the page resolves every row it
#: shows, so an unbounded list is an unbounded number of lookups.
LIMIT = 200


@dataclass(frozen=True, slots=True)
class Favorite:
    """One starred row, resolved back to the object it points at.

    `obj` is the live instance and `label` is what to call it. Both are read
    inside the unit of work that fetched them, because reading either one after
    it closes is the expired-attribute crash `CLAUDE.md` warns about.
    """

    model_key: str
    object_key: str
    label: str
    obj: Any
    created_at: dt.datetime | None = None


def object_key_of(adapter: ModelAdapter, obj: Any) -> str:
    """The stored spelling of a row's identity.

    "~"-joined for a composite primary key, which is the same spelling the
    admin's URLs use -- so a favorite is a link without a translation step.
    """
    return "~".join(str(part) for part in adapter.primary_key_of(obj))


class Favorites:
    """Star, unstar and list rows against the project's own table."""

    def __init__(self, fort: Any, model: type[Any]) -> None:
        self.fort = fort
        self.model = model
        self.key = default_model_key(model)

    # -- configuration ------------------------------------------------------

    def check(self) -> None:
        """Fail now if the model cannot hold a favorite.

        Called from `enable_favorites`, so the sentence names the missing
        column while somebody is looking at the file that declares it.
        """
        spec = self.fort.backend.introspect(self.model, key=self.key)
        available = {field.name for field in spec}
        missing = [name for name in REQUIRED_FIELDS if name not in available]
        if missing:
            raise ConfigurationError(
                f"{self.model.__name__} cannot hold favorites: it has no "
                f"{', '.join(missing)} column.",
                hint=(
                    "Give the model the columns of a favorite -- the quickest "
                    "way is to inherit fastfort.orm.sqlalchemy.FavoriteMixin, "
                    "which declares all of them."
                ),
            )

    def attach(self) -> None:
        """Clear a row's stars when the admin deletes it.

        `AFTER_DELETE` rather than `BEFORE_DELETE`: before the commit the row
        may still be rolled back, and clearing stars for a deletion that did
        not happen loses somebody's list for no reason. After it, the target
        genuinely is gone.

        Nothing outside the admin fires this, which is why every read resolves
        its target as well -- see the module docstring.
        """
        self.fort.hooks.add(Hook.AFTER_DELETE, self._forget_deleted)

    async def _forget_deleted(self, **kwargs: Any) -> None:
        model_key = kwargs.get("model_key")
        obj = kwargs.get("obj")
        if not model_key or obj is None:
            return
        entry = self.fort.registry.get_by_key(model_key)
        if entry is None:
            return

        async with self.fort.backend.unit_of_work() as uow:
            target = self.fort.backend.adapter(entry.model, uow, key=model_key)
            # The instance is detached by now -- `AFTER_DELETE` fires past the
            # commit -- but its attributes are still readable, which is all a
            # primary key needs.
            try:
                object_key = object_key_of(target, obj)
            except Exception:
                # A key we cannot read is a key we cannot clear, and a listener
                # that raises here would abort a delete that has already
                # committed. The stale row is caught on the read side instead.
                return

            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            await adapter.bulk_delete(
                ListQuery(
                    filters=(
                        Filter("model_key", FilterOperator.EXACT, model_key),
                        Filter("object_key", FilterOperator.EXACT, object_key),
                    ),
                    page_size=LIMIT,
                )
            )
            await uow.commit()

    # -- reading ------------------------------------------------------------

    async def is_starred(self, *, user: Any, model_key: str, obj: Any) -> bool:
        """Whether `user` has starred this row."""
        async with self.fort.backend.unit_of_work() as uow:
            object_key = await self._object_key(uow, model_key, obj)
            if object_key is None:
                return False
            return await self._row(uow, user, model_key, object_key) is not None

    async def starred_keys(self, *, user: Any, model_key: str) -> frozenset[str]:
        """Every `object_key` this account has starred in one model.

        One query for a whole page of rows. The list view draws a star per row
        and asking per row would be twenty-five queries to render a table --
        the N+1 the rest of this codebase is careful to avoid.
        """
        user_key = self._user_key(user)
        if not user_key:
            return frozenset()

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            page = await adapter.list(
                ListQuery(
                    filters=(
                        Filter("user_key", FilterOperator.EXACT, user_key),
                        Filter("model_key", FilterOperator.EXACT, model_key),
                    ),
                    page_size=LIMIT,
                )
            )
            return frozenset(str(row.object_key) for row in page.items)

    async def list_for_user(self, *, user: Any, model_key: str = "") -> tuple[Favorite, ...]:
        """Everything `user` has starred, newest first.

        Rows whose target no longer resolves are skipped rather than shown as a
        broken link: a favorite is a shortcut, and a shortcut to nothing is
        worse than no shortcut at all.
        """
        user_key = self._user_key(user)
        if not user_key:
            return ()

        filters = [Filter("user_key", FilterOperator.EXACT, user_key)]
        if model_key:
            filters.append(Filter("model_key", FilterOperator.EXACT, model_key))

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            page = await adapter.list(
                ListQuery(
                    filters=tuple(filters),
                    ordering=(SortSpec("created_at", descending=True),),
                    page_size=LIMIT,
                )
            )
            # Read every attribute now. Resolving a target opens work of its
            # own, and these rows must not be touched again afterwards.
            stored = [
                (str(row.model_key), str(row.object_key), getattr(row, "created_at", None))
                for row in page.items
            ]

        found: list[Favorite] = []
        for key, object_key, created_at in stored:
            resolved = await self._resolve(key, object_key)
            if resolved is None:
                continue
            obj, label = resolved
            found.append(
                Favorite(
                    model_key=key,
                    object_key=object_key,
                    label=label,
                    obj=obj,
                    created_at=created_at,
                )
            )
        return tuple(found)

    async def count_for_user(self, *, user: Any) -> int:
        """How many stars this account holds, without resolving any of them.

        The sidebar needs the number on every page; resolving each target to
        produce it would put one query per star on every request.
        """
        user_key = self._user_key(user)
        if not user_key:
            return 0
        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            return await self._count(adapter, user_key)

    # -- writing ------------------------------------------------------------

    async def toggle(self, *, user: Any, model_key: str, obj: Any) -> bool:
        """Star an unstarred row or unstar a starred one. Returns the new state.

        One method rather than `add` and `remove` because the button is one
        button: what the person means by pressing it is "the opposite of now",
        and computing that here keeps the two requests from disagreeing about
        what "now" was.
        """
        user_key = self._user_key(user)
        if not user_key:
            return False

        async with self.fort.backend.unit_of_work() as uow:
            object_key = await self._object_key(uow, model_key, obj)
            if object_key is None:
                return False

            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            existing = await self._row(uow, user, model_key, object_key)
            if existing is not None:
                # Every match, not just this one. The unique constraint should
                # make that a set of one, but a table that predates it can hold
                # duplicates, and leaving one behind is a star that will not
                # turn off however many times it is pressed.
                await adapter.bulk_delete(
                    ListQuery(
                        filters=(
                            Filter("user_key", FilterOperator.EXACT, user_key),
                            Filter("model_key", FilterOperator.EXACT, model_key),
                            Filter("object_key", FilterOperator.EXACT, object_key),
                        ),
                        page_size=LIMIT,
                    )
                )
                await uow.commit()
                return False

            # Counted through this unit of work, not `count_for_user`: that
            # opens a second connection while this one is mid-transaction, and
            # on SQLite -- or any pool of one -- the second waits on the first
            # until the request times out.
            if await self._count(adapter, user_key) >= LIMIT:
                raise ValidationError(
                    f"You have already starred {LIMIT} rows.",
                    hint="Remove a star from the favourites page before adding another.",
                )

            await adapter.create(
                {
                    "model_key": model_key,
                    "object_key": object_key,
                    "user_key": user_key,
                    "created_at": dt.datetime.now(dt.UTC),
                }
            )
            await uow.commit()
        return True

    # -- internals ----------------------------------------------------------

    def _user_key(self, user: Any) -> str:
        """The account's primary key as text, or "" when there is no account."""
        if user is None:
            return ""
        config: UserModelConfig = self.fort.user_config
        spec = self.fort.backend.introspect(config.model, key=default_model_key(config.model))
        return "~".join(str(getattr(user, name, "")) for name in spec.primary_key)

    async def _count(self, adapter: ModelAdapter, user_key: str) -> int:
        found: int = await adapter.count(
            ListQuery(filters=(Filter("user_key", FilterOperator.EXACT, user_key),))
        )
        return found

    async def _object_key(self, uow: Any, model_key: str, obj: Any) -> str | None:
        entry = self.fort.registry.get_by_key(model_key)
        if entry is None:
            return None
        adapter = self.fort.backend.adapter(entry.model, uow, key=model_key)
        return object_key_of(adapter, obj)

    async def _row(self, uow: Any, user: Any, model_key: str, object_key: str) -> Any | None:
        user_key = self._user_key(user)
        if not user_key:
            return None
        adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
        page = await adapter.list(
            ListQuery(
                filters=(
                    Filter("user_key", FilterOperator.EXACT, user_key),
                    Filter("model_key", FilterOperator.EXACT, model_key),
                    Filter("object_key", FilterOperator.EXACT, object_key),
                ),
                page_size=1,
            )
        )
        return page.items[0] if page.items else None

    async def _resolve(self, model_key: str, object_key: str) -> tuple[Any, str] | None:
        """The live row a stored key names, with its label, or `None`."""
        entry = self.fort.registry.get_by_key(model_key)
        if entry is None:
            # The model was unregistered since the star was made. Not an error:
            # the row may well still exist, but the admin has no page for it,
            # so there is nothing to link to.
            return None

        spec = self.fort.backend.introspect(entry.model, key=model_key)
        parts = object_key.split("~")
        if len(parts) != len(spec.primary_key):
            return None

        try:
            primary = tuple(
                coerce_filter_value(spec.get(name), part, name)
                for name, part in zip(spec.primary_key, parts, strict=True)
            )
        except (ValidationError, ValueError, TypeError):
            # The column changed type under a stored key, which is a skipped
            # entry rather than a crash.
            return None

        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(entry.model, uow, key=model_key)
            obj = await adapter.get(primary)
            if obj is None:
                return None
            # Read the label here: past this block the instance is detached and
            # `str(obj)` may touch an expired attribute.
            return obj, adapter.label_for(obj)
