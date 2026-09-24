"""A record of every write the admin makes, and the history of each row.

Turned on with one call, against a table the project owns::

    from fastfort.orm.sqlalchemy import AuditEntryMixin


    class AuditEntry(AuditEntryMixin, Base):
        __tablename__ = "admin_audit"


    fort.enable_audit_log(AuditEntry)

Every create, change and delete the admin performs then leaves a row naming
who, when, from which address, and -- for a change -- which fields went from
what to what. Each record's page gains a *History* link, and the sidebar an
*Activity* page listing everything, newest first.

It is a listener on the CRUD hooks and nothing more, which is why it lives in
`contrib/`: the admin does not know it exists. Five decisions are worth stating.

*The project owns the table, and there is no foreign key.* The target is the
registry key plus the primary key as text -- the pair an admin URL carries --
and the actor is their primary key and identity as text. A log that cascades
away when the row or the account it describes is deleted is not a log; "who
deleted this" is exactly the question it exists to answer.

*Values come from `backend.snapshot`, never off the instance.* A snapshot
issues no queries and omits every sensitive column outright, so a password
hash cannot reach this table even by accident, and reading a relation that was
never loaded cannot turn a save into a `MissingGreenlet`.

*The diff is before-against-after, not the form's values.* `BEFORE_UPDATE`
snapshots the row inside the transaction and `AFTER_UPDATE` snapshots it again
once it is durable. Comparing the form's submitted values instead would log
every field on the form as "changed", including the nineteen nobody touched.

*A row is written only after the commit.* `AFTER_*` fires once the change is
durable, so a save that rolled back leaves no record claiming it happened.

*Writing a record never fails the request.* By the time the listener runs the
change has committed; raising would show an error page for a save that
succeeded, and the natural reaction -- pressing Save again -- makes a second
change. So a failed write is logged, like a failed sign-in record.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from fastfort.auth.addresses import client_address
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.hooks import Hook
from fastfort.core.registry import default_model_key
from fastfort.spec import Change, ChangeSet, Filter, FilterOperator, ListQuery, SortSpec
from fastfort.spec._json import jsonify

if TYPE_CHECKING:
    from fastfort.core.app import FastFort

__all__ = ["AuditAction", "AuditLog", "Event"]

logger = logging.getLogger("fastfort.audit")

#: Columns without which a record is not one. The rest are written only when
#: the model declares them, so a narrower table is a supported choice.
REQUIRED_FIELDS = ("at", "action", "model_key", "object_key")
OPTIONAL_FIELDS = ("object_label", "user_key", "user_label", "address", "changes")

#: A long text column is logged, but not all of it. An article body edited
#: twice a day would otherwise copy itself into this table twice a day, and
#: what a history page needs is enough to recognise the edit, not a backup.
VALUE_LIMIT = 500

#: Where `BEFORE_UPDATE` leaves a row's snapshot for `AFTER_UPDATE` to find.
#: The ASGI scope rather than an attribute on this object: it is per-request
#: by construction, so two concurrent saves can never read each other's.
_SCOPE_KEY = "fastfort.audit.before"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class Event:
    """One stored record, read back."""

    at: dt.datetime | None
    action: AuditAction
    model_key: str
    object_key: str
    object_label: str
    user_label: str
    address: str
    changes: ChangeSet


class AuditLog:
    """Writes an entry per admin write, and reads them back per row or overall."""

    def __init__(self, fort: FastFort, model: type) -> None:
        self.fort = fort
        self.model = model
        self.key = default_model_key(model)
        self._writable: frozenset[str] = frozenset()

    # -- configuration ------------------------------------------------------

    def check(self) -> None:
        """Fail at start-up, naming the missing column, rather than on the first save."""
        spec = self.fort.backend.introspect(self.model, key=self.key)
        available = {field.name for field in spec}
        missing = [name for name in REQUIRED_FIELDS if name not in available]
        if missing:
            raise ConfigurationError(
                f"{self.model.__name__} cannot hold an audit log: it has no "
                f"{', '.join(missing)} column.",
                hint=(
                    "Give the model the columns of an audit entry -- the quickest "
                    "way is to inherit fastfort.orm.sqlalchemy.AuditEntryMixin, "
                    "which declares all of them."
                ),
            )
        self._writable = frozenset(available & {*REQUIRED_FIELDS, *OPTIONAL_FIELDS})

    def attach(self) -> None:
        hooks = self.fort.hooks
        hooks.add(Hook.BEFORE_UPDATE, self._remember)
        hooks.add(Hook.AFTER_CREATE, self._created)
        hooks.add(Hook.AFTER_UPDATE, self._updated)
        hooks.add(Hook.AFTER_DELETE, self._deleted)

    # -- listeners ----------------------------------------------------------

    async def _remember(
        self, *, request: Any = None, model_key: str = "", obj: Any = None, **_: Any
    ) -> None:
        if request is None or obj is None or not self._records(model_key):
            return
        stash = request.scope.setdefault(_SCOPE_KEY, {})
        # Keyed by identity rather than by primary key: a bulk edit announces
        # forty rows before writing any, and the instance is the one thing that
        # is certainly the same object when its `AFTER_UPDATE` arrives.
        stash[(model_key, id(obj))] = self._snapshot(model_key, obj)

    async def _created(
        self, *, request: Any = None, model_key: str = "", obj: Any = None, **_: Any
    ) -> None:
        if obj is None or not self._records(model_key):
            return
        after = {
            name: value
            for name, value in self._snapshot(model_key, obj).items()
            if value is not None
        }
        await self._write(request, AuditAction.CREATE, model_key, obj, ChangeSet.between({}, after))

    async def _updated(
        self,
        *,
        request: Any = None,
        model_key: str = "",
        obj: Any = None,
        changes: Any = None,
        **_: Any,
    ) -> None:
        if obj is None or not self._records(model_key):
            return
        stash = request.scope.get(_SCOPE_KEY, {}) if request is not None else {}
        before = stash.pop((model_key, id(obj)), None)
        after = self._snapshot(model_key, obj)
        if before is None:
            # An `AFTER_UPDATE` with no `BEFORE_UPDATE` -- a project emitting
            # its own. The old values are unknown, and saying so is better than
            # inventing them: the new ones are logged against an empty "before".
            names = set(changes or ()) & set(after)
            diff = ChangeSet.between({}, {name: after[name] for name in sorted(names)})
        else:
            # Over `after`'s keys only. A column the flush expired is missing
            # from it -- see `SQLAlchemyAdapter.snapshot` -- and is not a change.
            diff = ChangeSet.between(before, after)
        if not diff:
            # A save that changed nothing is not an event. Recording it would
            # bury the edits that mattered under every Save pressed twice.
            return
        await self._write(request, AuditAction.UPDATE, model_key, obj, diff)

    async def _deleted(
        self, *, request: Any = None, model_key: str = "", obj: Any = None, **_: Any
    ) -> None:
        if obj is None or not self._records(model_key):
            return
        # The last values the row held. A deleted row has no page left to show
        # them, and "what was this" is the first question anyone asks of a
        # deletion in a log.
        before = self._snapshot(model_key, obj)
        gone = ChangeSet(
            tuple(Change(name, value, None) for name, value in before.items() if value is not None)
        )
        await self._write(request, AuditAction.DELETE, model_key, obj, gone)

    # -- reading ------------------------------------------------------------

    async def history(
        self, *, model_key: str, object_key: str, limit: int = 100
    ) -> tuple[Event, ...]:
        """Everything recorded against one row, newest first. One query."""
        filters = (
            Filter("model_key", FilterOperator.EXACT, model_key),
            Filter("object_key", FilterOperator.EXACT, object_key),
        )
        events, _ = await self._read(filters, page=1, page_size=limit)
        return events

    async def recent(
        self, *, model_key: str = "", page: int = 1, page_size: int = 50
    ) -> tuple[tuple[Event, ...], bool]:
        """One page of everything, newest first, and whether an older page exists."""
        filters = (Filter("model_key", FilterOperator.EXACT, model_key),) if model_key else ()
        return await self._read(filters, page=page, page_size=page_size)

    async def _read(
        self, filters: tuple[Filter, ...], *, page: int, page_size: int
    ) -> tuple[tuple[Event, ...], bool]:
        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            found = await adapter.list(
                ListQuery(
                    filters=filters,
                    ordering=(SortSpec("at", descending=True),),
                    page=page,
                    page_size=page_size,
                )
            )
            # Read inside the unit of work: past it the rows are detached.
            events = tuple(self._event(row) for row in found.items)
        return events, found.has_next

    def _event(self, row: Any) -> Event:
        raw = getattr(row, "changes", "") or ""
        try:
            decoded = json.loads(raw) if raw else {}
        except ValueError:
            decoded = {}
        changes = ChangeSet(
            tuple(
                Change(str(name), pair[0], pair[1])
                for name, pair in decoded.items()
                if isinstance(pair, list) and len(pair) == 2
            )
        )
        try:
            action = AuditAction(str(row.action))
        except ValueError:
            action = AuditAction.UPDATE
        return Event(
            at=row.at,
            action=action,
            model_key=str(row.model_key),
            object_key=str(row.object_key),
            object_label=str(getattr(row, "object_label", "") or ""),
            user_label=str(getattr(row, "user_label", "") or ""),
            address=str(getattr(row, "address", "") or ""),
            changes=changes,
        )

    # -- writing ------------------------------------------------------------

    async def _write(
        self,
        request: Any,
        action: AuditAction,
        model_key: str,
        obj: Any,
        changes: ChangeSet,
    ) -> None:
        try:
            await self._store(self._row(request, action, model_key, obj, changes))
        except Exception:
            # Never re-raised -- see the module docstring. The change this
            # describes has already committed.
            logger.exception("Could not write an audit entry for %s", model_key)

    async def _store(self, data: dict[str, Any]) -> None:
        """One entry, in a transaction of its own -- the request's has closed."""
        async with self.fort.backend.unit_of_work() as uow:
            adapter = self.fort.backend.adapter(self.model, uow, key=self.key)
            await adapter.create(data)

    def _row(
        self,
        request: Any,
        action: AuditAction,
        model_key: str,
        obj: Any,
        changes: ChangeSet,
    ) -> dict[str, Any]:
        user = request.scope.get("fastfort_user") if request is not None else None
        config = self.fort.user_config
        object_key = "~".join(str(part) for part in self._primary_key(model_key, obj))
        data: dict[str, Any] = {
            "at": dt.datetime.now(dt.UTC),
            "action": action.value,
            "model_key": model_key,
            "object_key": object_key,
            "object_label": _label(obj, object_key)[:255],
            "user_key": str(getattr(user, config.id_field, "")) if user is not None else "",
            "user_label": config.identity_of(user)[:255] if user is not None else "",
            "address": (
                client_address(
                    request.scope,
                    forwarded_depth=self.fort.settings.security.effective_forwarded_depth,
                )
                if request is not None
                else ""
            ),
            "changes": json.dumps(
                {change.field: [change.old, change.new] for change in changes},
                ensure_ascii=False,
            ),
        }
        return {name: value for name, value in data.items() if name in self._writable}

    # -- internals ----------------------------------------------------------

    def _records(self, model_key: str) -> bool:
        # Never itself: every entry written would be a create to log, and each
        # of those another.
        return bool(model_key) and model_key != self.key

    def _entry(self, model_key: str) -> Any:
        return self.fort.registry.entry_for_key(model_key)

    def _snapshot(self, model_key: str, obj: Any) -> dict[str, Any]:
        entry = self._entry(model_key)
        state = self.fort.backend.snapshot(entry.model, obj, key=model_key)
        return {name: _plain(value) for name, value in state.items()}

    def _primary_key(self, model_key: str, obj: Any) -> tuple[Any, ...]:
        entry = self._entry(model_key)
        spec = self.fort.backend.introspect(entry.model, key=model_key)
        return tuple(getattr(obj, name, "") for name in spec.primary_key)


def _label(obj: Any, fallback: str) -> str:
    """What to call the row, read now while every attribute is still in memory.

    Guarded because `__str__` is the project's code: one that reaches for a
    relation that was never loaded raises, and a label is not worth losing the
    whole entry over.
    """
    try:
        text = str(obj)
    except Exception:
        return fallback
    # The default `object.__repr__` names a memory address, which identifies
    # nothing once the process has moved on.
    return fallback if " object at 0x" in text else text


def _plain(value: Any) -> Any:
    """`jsonify`, with the two things a log must not copy in whole.

    Compared after conversion, so a value that round-trips to the same text is
    the same value and not a change.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        # `jsonify` decodes bytes as text, which is right for an API and wrong
        # here: an uploaded thumbnail would land in this table as mojibake. That
        # it changed, and how big it is, is what a history page can use.
        return f"<{len(bytes(value))} bytes>"
    plain = jsonify(value)
    if isinstance(plain, str) and len(plain) > VALUE_LIMIT:
        return plain[:VALUE_LIMIT] + "…"
    return plain
