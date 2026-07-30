"""Rate limiting for failed sign-ins.

Two counters, not one: an address and an identity. Locking only by address lets a
botnet spread one attempt per host across a whole password list; locking only by
identity lets anyone lock a colleague out by guessing at their email. Both have to
be tracked, and either can trigger.

The default store is in memory, which is correct for a single process and wrong
for several. `LockoutStore` is a protocol so a project running more than one
worker can back it with Redis; `fastfort check --deploy` will say so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from fastfort.core.settings import AuthSettings

__all__ = ["InMemoryLockoutStore", "Lockout", "LockoutState", "LockoutStore"]


@dataclass(frozen=True, slots=True)
class LockoutState:
    """Whether a key is currently blocked, and for how long."""

    locked: bool
    retry_after: int = 0
    failures: int = 0


class LockoutStore(Protocol):
    """Where failure counts live."""

    async def record_failure(self, key: str, *, window: int) -> int:
        """Note a failure and return how many are recorded inside `window`."""
        ...

    async def clear(self, key: str) -> None:
        """Forget a key's failures, after a successful sign-in."""
        ...

    async def failures(self, key: str, *, window: int) -> int: ...


@dataclass
class InMemoryLockoutStore:
    """Per-process failure counts.

    Keeps the timestamps rather than a running total, so a window genuinely
    slides: five failures spread over an hour must not lock an account the way
    five in ten seconds should.
    """

    _events: dict[str, list[float]] = field(default_factory=dict)

    async def record_failure(self, key: str, *, window: int) -> int:
        now = time.monotonic()
        events = [stamp for stamp in self._events.get(key, ()) if now - stamp < window]
        events.append(now)
        self._events[key] = events
        return len(events)

    async def clear(self, key: str) -> None:
        self._events.pop(key, None)

    async def failures(self, key: str, *, window: int) -> int:
        now = time.monotonic()
        events = [stamp for stamp in self._events.get(key, ()) if now - stamp < window]
        if events:
            self._events[key] = events
        else:
            self._events.pop(key, None)
        return len(events)


class Lockout:
    """Applies the policy in `AuthSettings` over a store."""

    def __init__(self, settings: AuthSettings, store: LockoutStore | None = None) -> None:
        self._settings = settings
        self._store = store or InMemoryLockoutStore()

    async def check(self, *, address: str, identity: str) -> LockoutState:
        """Report the worst state across the address and the identity."""
        worst = LockoutState(locked=False)
        for key in self._keys(address, identity):
            state = self._state(
                await self._store.failures(key, window=self._settings.lockout_window_seconds)
            )
            if state.failures > worst.failures:
                worst = state
        return worst

    async def record_failure(self, *, address: str, identity: str) -> LockoutState:
        worst = LockoutState(locked=False)
        for key in self._keys(address, identity):
            state = self._state(
                await self._store.record_failure(key, window=self._settings.lockout_window_seconds)
            )
            if state.failures > worst.failures:
                worst = state
        return worst

    async def record_success(self, *, address: str, identity: str) -> None:
        for key in self._keys(address, identity):
            await self._store.clear(key)

    def _keys(self, address: str, identity: str) -> tuple[str, ...]:
        # Identities are lowercased so "Admin@x" and "admin@x" share a counter.
        return (f"ip:{address}", f"id:{identity.strip().lower()}")

    def _state(self, failures: int) -> LockoutState:
        threshold = self._settings.lockout_threshold
        if failures < threshold:
            return LockoutState(locked=False, failures=failures)

        # Exponential, capped: the delay has to grow faster than an attacker can
        # work through a list, without locking a fat-fingered colleague out for a
        # day.
        excess = failures - threshold
        delay = min(self._settings.lockout_seconds * (2**excess), 3600)
        return LockoutState(locked=True, retry_after=delay, failures=failures)
