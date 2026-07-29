"""A small synchronous/asynchronous event dispatcher.

Hooks are how a project reacts to what the admin does -- send an email after a
user is created, push an event onto a queue after an order changes -- without
subclassing anything. Listener errors deliberately propagate: a hook that fails
silently is worse than no hook, because the caller believes the work happened.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterator
from enum import StrEnum
from typing import Any, TypeAlias

from .exceptions import ConfigurationError

__all__ = ["Hook", "HookRegistry", "Listener"]

Listener: TypeAlias = Callable[..., Awaitable[None] | None]


class Hook(StrEnum):
    """Events FastFort emits.

    Listeners receive keyword arguments only, so adding context to an event later
    never breaks an existing listener.
    """

    #: kwargs: request, model_key, obj
    BEFORE_CREATE = "before_create"
    AFTER_CREATE = "after_create"
    #: kwargs: request, model_key, obj, changes
    BEFORE_UPDATE = "before_update"
    AFTER_UPDATE = "after_update"
    #: kwargs: request, model_key, obj
    BEFORE_DELETE = "before_delete"
    AFTER_DELETE = "after_delete"

    #: kwargs: request, user
    USER_LOGGED_IN = "user_logged_in"
    USER_LOGGED_OUT = "user_logged_out"
    #: kwargs: request, identity, reason
    LOGIN_FAILED = "login_failed"
    #: kwargs: request, user
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 -- an event name
    #: kwargs: request, user, session_id
    TOKEN_REUSE_DETECTED = "token_reuse_detected"  # noqa: S105 -- an event name


class HookRegistry:
    """Listeners grouped by event, called in registration order."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = {}

    def on(self, hook: Hook | str) -> Callable[[Listener], Listener]:
        """Decorator form::

        @fort.hooks.on(Hook.AFTER_CREATE)
        async def notify(*, request, model_key, obj, **_): ...
        """
        name = self._name(hook)

        def decorator(listener: Listener) -> Listener:
            self.add(name, listener)
            return listener

        return decorator

    def add(self, hook: Hook | str, listener: Listener) -> None:
        if not callable(listener):
            raise ConfigurationError(f"Hook listener must be callable, got {listener!r}.")
        self._listeners.setdefault(self._name(hook), []).append(listener)

    def remove(self, hook: Hook | str, listener: Listener) -> None:
        listeners = self._listeners.get(self._name(hook), [])
        if listener in listeners:
            listeners.remove(listener)

    def clear(self, hook: Hook | str | None = None) -> None:
        if hook is None:
            self._listeners.clear()
        else:
            self._listeners.pop(self._name(hook), None)

    async def emit(self, hook: Hook | str, **kwargs: Any) -> None:
        """Call every listener for `hook`, awaiting the asynchronous ones.

        Listeners run sequentially so that ordering is predictable; a listener
        that needs to fan out can do so itself.
        """
        for listener in tuple(self._listeners.get(self._name(hook), ())):
            result = listener(**kwargs)
            if inspect.isawaitable(result):
                await result

    def listeners(self, hook: Hook | str) -> tuple[Listener, ...]:
        return tuple(self._listeners.get(self._name(hook), ()))

    def __iter__(self) -> Iterator[str]:
        return iter(self._listeners)

    def __len__(self) -> int:
        return sum(len(items) for items in self._listeners.values())

    def __repr__(self) -> str:
        return f"<HookRegistry: {len(self)} listener(s)>"

    @staticmethod
    def _name(hook: Hook | str) -> str:
        return hook.value if isinstance(hook, Hook) else hook
