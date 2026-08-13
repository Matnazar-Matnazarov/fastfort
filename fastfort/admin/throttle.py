"""How much of the admin one client is allowed to ask for.

`Lockout` next door already answers "how many passwords may this address try".
This answers the different question underneath it: how many *requests* may an
address make at all. They are not the same defence and neither substitutes for
the other -- lockout only ever sees a request that reached the sign-in handler,
and by then the expensive part has usually already happened.

## Why an admin needs this at all

Three things here cost far more to serve than to ask for, and each is a way to
take the site down with a laptop:

**Sign-in.** Argon2 is deliberately slow -- that is the entire point of it. A
password hash tuned to take 100 ms means eleven unauthenticated requests a second
saturate a core, and the attacker spends nothing. `login_per_minute` is the
tightest budget here for exactly this reason, and it is charged *before* the
handler runs, so a refused request never reaches the hash.

**Writes.** A `POST` runs validation, a transaction and a flush.

**Lists.** A page of a large table with a filter and a sort is real database
work, and `page_size` is a query parameter.

## Shape

A token bucket per key rather than a counter per window. A fixed window lets
twice the budget through across a window boundary, and a sliding log costs a
timestamp per request, which is itself a way to spend someone's memory. A bucket
is two floats, refills continuously, and lets a burst through as long as the
average holds -- which is what a person clicking around an admin actually looks
like.

Three buckets, checked in order of how expensive the thing being asked for is:
one for sign-in, one for writes, one for everything else. A client that is over
any of them is refused, and the answer says which and for how long.

## What this is not

This is not a defence against a distributed flood. A million hosts sending one
request each are a million different keys, and no per-key budget helps -- that
is a job for the layer holding the socket, and `deploy/Caddyfile` in the docs
repository is where it is done for this project. What an in-process limiter is
good at is the single loud client: the scraper, the runaway script, the brute
force, and the accidental infinite retry loop in someone's integration.

The store is a protocol for the usual reason: the default is per-process, which
is correct for one worker and wrong for four, and `fastfort check --deploy` says
so rather than leaving it to be discovered.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from starlette.types import ASGIApp, Receive, Scope, Send

from fastfort.auth.addresses import client_address

if TYPE_CHECKING:
    from fastfort.core.settings import FastFortSettings, RateLimitSettings

__all__ = [
    "Bucket",
    "InMemoryRateLimitStore",
    "RateLimitMiddleware",
    "RateLimitStore",
    "RateLimiter",
    "Verdict",
    "client_address",
]


@dataclass(frozen=True, slots=True)
class Bucket:
    """What one budget allows: `capacity` requests, refilled at `rate` a second.

    Capacity is the burst. Someone opening an admin page pulls the HTML and then
    whatever the page needs, in parallel, in well under a second -- a budget
    expressed only as an average would refuse the second half of a page load.
    """

    capacity: float
    rate: float

    @classmethod
    def per_minute(cls, allowance: int, *, burst: float = 1.5) -> Bucket:
        """`allowance` requests a minute, with room for `burst` times that in one go."""
        return cls(capacity=max(1.0, allowance * burst), rate=allowance / 60.0)


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether a request may proceed, and what to tell the client if not."""

    allowed: bool
    retry_after: int = 0
    remaining: int = 0
    limit: int = 0
    scope: str = ""


class RateLimitStore(Protocol):
    """Where the buckets live.

    One method, and it both reads and writes: "may this key spend a token" has to
    be one indivisible step, or two requests arriving together each read the same
    remaining count and both spend it. A Redis-backed implementation does this in
    a script for the same reason.
    """

    async def consume(self, key: str, bucket: Bucket, *, cost: float = 1.0) -> tuple[bool, float]:
        """`(allowed, tokens_left)`. On refusal, `tokens_left` is what is there now."""
        ...


@dataclass
class InMemoryRateLimitStore:
    """Per-process buckets, with a bound on how many.

    The bound is the part worth explaining. A limiter keyed by address that keeps
    an entry per address it has ever seen is itself the vulnerability: a client
    walking through a range of source addresses spends the server's memory rather
    than its CPU, and the defence becomes the attack. So entries expire -- a
    bucket that has had time to refill completely carries no information and is
    dropped -- and a sweep runs whenever the table crosses `max_keys`.

    If a sweep cannot get back under the ceiling, the table is cleared outright.
    Forgetting every counter is a bad answer; it is a much better one than an
    unbounded dictionary, and it can only happen when there are more genuinely
    active clients than the ceiling allows for, which is a number to raise.
    """

    max_keys: int = 100_000
    #: key -> (tokens, when that was true, how long a full refill takes)
    _buckets: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    _last_sweep: float = 0.0

    async def consume(self, key: str, bucket: Bucket, *, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        tokens, last, _ = self._buckets.get(key, (bucket.capacity, now, 0.0))

        # Refill for the time that has passed, capped at the bucket's size: an
        # address that has been quiet for a day gets a full bucket, not a day's
        # worth of credit to spend all at once.
        tokens = min(bucket.capacity, tokens + (now - last) * bucket.rate)
        # Carried on the entry rather than recomputed at sweep time, because the
        # sweep walks keys belonging to every budget at once and the sign-in
        # bucket refills far more slowly than the read one. Deriving it from
        # whichever bucket happened to trigger the sweep would evict live
        # counters for the strict budget and keep dead ones for the loose.
        refill = bucket.capacity / bucket.rate if bucket.rate > 0 else 0.0

        if tokens < cost:
            self._buckets[key] = (tokens, now, refill)
            return False, tokens

        tokens -= cost
        self._buckets[key] = (tokens, now, refill)

        if len(self._buckets) > self.max_keys:
            self._sweep(now)
        return True, tokens

    def _sweep(self, now: float) -> None:
        # At most once a second. A sweep is O(keys), and running one on every
        # request once the table is full would turn a full table into the outage
        # it was meant to prevent.
        if now - self._last_sweep < 1.0:
            return
        self._last_sweep = now

        # A bucket that has had time to refill to capacity is indistinguishable
        # from one that never existed, so it is not worth a dictionary entry.
        self._buckets = {
            key: entry for key, entry in self._buckets.items() if now - entry[1] < entry[2]
        }
        if len(self._buckets) > self.max_keys:
            self._buckets.clear()


class RateLimiter:
    """Applies the policy in `RateLimitSettings` over a store."""

    def __init__(self, settings: RateLimitSettings, store: RateLimitStore | None = None) -> None:
        self._settings = settings
        self._store = store or InMemoryRateLimitStore()

    async def check(self, address: str, *, scope: str) -> Verdict:
        """Spend one token from the `scope` budget for `address`."""
        allowance = self._allowance(scope)
        if allowance <= 0:
            return Verdict(allowed=True)

        bucket = Bucket.per_minute(allowance, burst=self._settings.burst)
        allowed, tokens = await self._store.consume(f"{scope}:{address}", bucket)
        if allowed:
            return Verdict(allowed=True, remaining=int(tokens), limit=allowance, scope=scope)

        # How long until one token exists, rounded up -- a `Retry-After: 0` is an
        # invitation to come straight back and be refused again.
        wait = (1.0 - tokens) / bucket.rate if bucket.rate > 0 else 60.0
        return Verdict(
            allowed=False,
            retry_after=max(1, int(wait) + 1),
            remaining=0,
            limit=allowance,
            scope=scope,
        )

    def _allowance(self, scope: str) -> int:
        if scope == "login":
            return self._settings.login_per_minute
        if scope == "write":
            return self._settings.write_per_minute
        return self._settings.read_per_minute


class RateLimitMiddleware:
    """Charges every admin request against its client's budget.

    Scoped to the admin's own prefix, like `SecurityHeadersMiddleware` and for the
    same reason: a framework that starts refusing requests to the application it
    was mounted into has exceeded its brief.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        settings: FastFortSettings,
        store: RateLimitStore | None = None,
    ) -> None:
        self.app = app
        self.settings = settings
        self._prefix = settings.admin.url
        # The token API when it is mounted, because it is the same Argon2 hash
        # behind a different door. Metering the admin's sign-in form and leaving
        # `POST /auth/token` open would be a lock on one of two doors.
        self._auth_prefix = settings.auth_url if settings.auth.api_enabled else None
        self._login_paths = frozenset(
            {f"{settings.admin.url}/login"}
            | ({f"{settings.auth_url}/token"} if settings.auth.api_enabled else set())
        )
        self._depth = settings.security.effective_forwarded_depth
        self._limiter = RateLimiter(
            settings.rate_limit,
            store or InMemoryRateLimitStore(max_keys=settings.rate_limit.max_tracked_clients),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._applies(scope):
            await self.app(scope, receive, send)
            return

        address = client_address(scope, forwarded_depth=self._depth)
        verdict = await self._limiter.check(address, scope=self._bucket_for(scope))

        if verdict.allowed:
            await self.app(scope, receive, send)
            return

        await self._refuse(verdict, send)

    def _applies(self, scope: Scope) -> bool:
        if scope["type"] != "http" or not self.settings.rate_limit.enabled:
            return False
        path = scope.get("path", "")
        if self._auth_prefix is not None and path.startswith(self._auth_prefix):
            return True
        if not path.startswith(self._prefix):
            return False
        # Static assets are cached, compressed and immutable, and a page pulls
        # several of them at once. Counting them turns one page view into a dozen
        # requests against a budget meant to describe page views.
        return not path.startswith(f"{self._prefix}/static/")

    def _bucket_for(self, scope: Scope) -> str:
        method = scope.get("method", "GET")
        if scope.get("path", "") in self._login_paths and method == "POST":
            return "login"
        return "write" if method not in ("GET", "HEAD", "OPTIONS") else "read"

    async def _refuse(self, verdict: Verdict, send: Send) -> None:
        # Plain text rather than a rendered page: this runs outside the router, so
        # it has no translator, no template context and no session -- and a
        # limiter that needs to render a template in order to say no is a limiter
        # that can be made expensive by being triggered.
        body = (
            f"Too many requests. Try again in {verdict.retry_after} seconds.\n"
            "\n"
            "This limit is per client address. If it is in the way of ordinary "
            "use, raise rate_limit in the FastFort settings.\n"
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    (b"retry-after", str(verdict.retry_after).encode("latin-1")),
                    (b"x-ratelimit-limit", str(verdict.limit).encode("latin-1")),
                    (b"x-ratelimit-remaining", b"0"),
                    # So a page cached by a shared proxy is never a cached 429.
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
