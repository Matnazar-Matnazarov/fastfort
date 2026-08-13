"""How much of the admin one client is allowed to ask for.

The limiter is off for the rest of the suite -- a thousand tests from one address
would spend any realistic budget -- so every test here turns it back on with an
explicit `rate_limit=`, which outranks the environment.

What is worth guarding: that a refused request is refused *before* the expensive
part rather than after it, that the three budgets are genuinely separate, that
the header naming the client's address cannot be used to mint a fresh budget on
demand, and that the table of buckets cannot be made to grow without bound --
a limiter whose bookkeeping is the resource exhaustion is worse than none.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.throttle import (
    Bucket,
    InMemoryRateLimitStore,
    RateLimiter,
    client_address,
)
from fastfort.core.settings import RateLimitSettings, SecuritySettings
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"


def build(backend: SQLAlchemyBackend, **limits: Any) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            rate_limit={"enabled": True, **limits},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app


async def opened(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
async def client(backend: SQLAlchemyBackend, staff_user: StaffUser) -> AsyncIterator[Any]:
    """A factory, because each test wants its own limits and its own budget."""

    made: list[httpx.AsyncClient] = []

    async def make(*, sign_in_first: bool = True, **limits: Any) -> httpx.AsyncClient:
        connection = await opened(build(backend, **limits))
        made.append(connection)
        if sign_in_first:
            await sign_in(connection)
        return connection

    yield make
    for connection in made:
        await connection.aclose()


# ---------------------------------------------------------------------------
# The budgets
# ---------------------------------------------------------------------------


async def test_reading_past_the_budget_is_refused(client: Any) -> None:
    connection = await client(read_per_minute=60, burst=1.0)

    statuses = [(await connection.get("/admin/shop.product/")).status_code for _ in range(70)]

    assert 200 in statuses, "the budget has to let the first requests through"
    assert 429 in statuses, "and refuse once it is spent"
    # Once refused, refused: a bucket refilling at one a second cannot let the
    # next request in immediately.
    assert statuses[-1] == 429


async def test_a_refusal_says_when_to_come_back(client: Any) -> None:
    """A 429 with no `Retry-After` leaves a well-behaved client guessing, and the
    badly behaved one retrying immediately -- which is the traffic that caused
    it."""
    connection = await client(read_per_minute=60, burst=1.0)
    for _ in range(70):
        response = await connection.get("/admin/shop.product/")
        if response.status_code == 429:
            break

    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-limit"] == "60"
    assert response.headers["cache-control"] == "no-store"


async def test_writing_has_its_own_budget(client: Any) -> None:
    """A save is a transaction; a list is a query. Sharing one counter would mean
    a morning of reading left no allowance to save anything."""
    connection = await client(read_per_minute=600, write_per_minute=0)

    # `write_per_minute=0` means "no budget at all", which is the clearest way to
    # ask whether reads and writes are counted apart.
    assert (await connection.get("/admin/shop.product/")).status_code == 200

    posted = await connection.post("/admin/shop.product/add/", data={})
    assert posted.status_code != 429, "0 means unlimited, matching every other budget here"


async def test_signing_in_is_the_tightest_budget(backend: SQLAlchemyBackend) -> None:
    """Argon2 is slow on purpose, which makes an unauthenticated POST to the
    sign-in form the cheapest thing on the site to attack: the sender spends a
    packet and the server spends a core.

    So the charge happens in the middleware, before the handler, and the budget
    is small. Lockout is the separate defence and it works on a different axis --
    it counts failures per identity; this counts attempts per address, and an
    attacker spraying one password across a thousand accounts never trips the
    first one.
    """
    connection = await opened(build(backend, login_per_minute=5, burst=1.0))
    try:
        statuses = [
            (
                await connection.post(
                    "/admin/login",
                    data={"identity": ADMIN_EMAIL, "password": "wrong-" + str(attempt)},
                )
            ).status_code
            for attempt in range(12)
        ]
    finally:
        await connection.aclose()

    assert statuses.count(429) >= 5, statuses
    # And reading the sign-in page is not what was budgeted: that costs nothing.
    assert statuses[0] != 429


async def test_reading_the_sign_in_page_is_not_charged_to_the_sign_in_budget(
    backend: SQLAlchemyBackend,
) -> None:
    connection = await opened(build(backend, login_per_minute=1, burst=1.0))
    try:
        for _ in range(5):
            assert (await connection.get("/admin/login")).status_code == 200
    finally:
        await connection.aclose()


async def test_static_assets_are_not_counted(client: Any) -> None:
    """One page pulls the stylesheet, two scripts and the favicon. Counting them
    turns a single page view into six requests against a budget whose numbers
    were chosen to describe page views."""
    connection = await client(read_per_minute=30, burst=1.0)

    for asset in ("/admin/static/fastfort.css", "/admin/static/js/boot.js"):
        for _ in range(40):
            assert (await connection.get(asset)).status_code == 200, asset


async def test_the_application_around_the_admin_is_left_alone(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """A framework that starts refusing requests to the application it was
    mounted into has exceeded its brief."""
    app = build(backend, read_per_minute=1, burst=1.0)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    connection = await opened(app)
    try:
        for _ in range(20):
            assert (await connection.get("/health")).status_code == 200
    finally:
        await connection.aclose()


async def test_zero_means_unlimited(client: Any) -> None:
    """Because the alternative -- zero meaning "refuse everything" -- is a
    setting whose obvious reading takes the admin down."""
    connection = await client(read_per_minute=0)
    for _ in range(50):
        assert (await connection.get("/admin/shop.product/")).status_code == 200


async def test_the_limiter_can_be_turned_off(backend: SQLAlchemyBackend) -> None:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            rate_limit={"enabled": False, "read_per_minute": 1},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)

    connection = await opened(app)
    try:
        for _ in range(10):
            assert (await connection.get("/admin/login")).status_code == 200
    finally:
        await connection.aclose()


# ---------------------------------------------------------------------------
# Which client a request belongs to
# ---------------------------------------------------------------------------


def scope_with(
    headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None
) -> dict[str, Any]:
    return {"type": "http", "headers": headers, "client": client}


def test_a_forwarded_header_is_ignored_unless_a_proxy_was_declared() -> None:
    """This is the bypass, and it is one line of curl: if the limiter reads
    `X-Forwarded-For` without being told there is a proxy, every request can
    claim to be a different client and the budget never applies to anybody."""
    scope = scope_with([(b"x-forwarded-for", b"9.9.9.9")], ("10.0.0.1", 5000))
    assert client_address(scope) == "10.0.0.1"


def test_a_declared_proxy_is_counted_from_the_right() -> None:
    """The client writes the left of the header and each proxy appends to the
    right, so the right is the only end an attacker cannot lengthen."""
    scope = scope_with(
        [(b"x-forwarded-for", b"203.0.113.9, 198.51.100.7, 192.0.2.4")], ("10.0.0.1", 5000)
    )
    assert client_address(scope, forwarded_depth=1) == "192.0.2.4"
    assert client_address(scope, forwarded_depth=2) == "198.51.100.7"


def test_a_spoofed_header_cannot_shift_the_reading() -> None:
    """With one proxy declared, prefixing the header with invented hops moves the
    entries the attacker wrote *away* from the position that is read."""
    honest = scope_with([(b"x-forwarded-for", b"192.0.2.4")], ("10.0.0.1", 5000))
    padded = scope_with(
        [(b"x-forwarded-for", b"1.1.1.1, 2.2.2.2, 3.3.3.3, 192.0.2.4")], ("10.0.0.1", 5000)
    )
    assert client_address(honest, forwarded_depth=1) == client_address(padded, forwarded_depth=1)


def test_too_few_hops_falls_back_to_the_socket() -> None:
    """A request arriving with fewer hops than configured did not come through
    the proxies it was meant to, so its header is not evidence of anything."""
    scope = scope_with([(b"x-forwarded-for", b"9.9.9.9")], ("10.0.0.1", 5000))
    assert client_address(scope, forwarded_depth=3) == "10.0.0.1"


def test_an_older_deployment_keeps_the_meaning_it_had() -> None:
    """`trust_forwarded_for` predates `forwarded_depth` and says "there is a
    proxy" without saying how many. One is what it meant."""
    assert SecuritySettings().effective_forwarded_depth == 0
    assert SecuritySettings(trust_forwarded_for=True).effective_forwarded_depth == 1
    assert SecuritySettings(forwarded_depth=2).effective_forwarded_depth == 2


# ---------------------------------------------------------------------------
# The bucket
# ---------------------------------------------------------------------------


async def test_a_bucket_refills_over_time() -> None:
    """The reason for a token bucket rather than a counter per fixed window: a
    window resets all at once, so twice the budget gets through across a
    boundary."""
    store = InMemoryRateLimitStore()
    bucket = Bucket(capacity=2, rate=100.0)  # refills fully in 20ms

    assert (await store.consume("k", bucket))[0] is True
    assert (await store.consume("k", bucket))[0] is True
    assert (await store.consume("k", bucket))[0] is False

    await asyncio.sleep(0.05)
    assert (await store.consume("k", bucket))[0] is True


async def test_a_quiet_client_does_not_accrue_unlimited_credit() -> None:
    """Refill is capped at the bucket's size. Without the cap, an address silent
    for a day arrives with a day's allowance to spend in one second, which is
    exactly the traffic shape the limiter exists to stop."""
    store = InMemoryRateLimitStore()
    bucket = Bucket(capacity=3, rate=1000.0)

    for _ in range(3):
        assert (await store.consume("k", bucket))[0] is True
    await asyncio.sleep(0.05)  # 50 tokens' worth of time

    allowed = [(await store.consume("k", bucket))[0] for _ in range(10)]
    assert allowed.count(True) == 3


async def test_the_table_of_buckets_is_bounded() -> None:
    """A limiter that keeps an entry per address it has ever seen spends the
    server's memory instead of its CPU. The defence must not be the attack."""
    store = InMemoryRateLimitStore(max_keys=100)
    bucket = Bucket(capacity=1, rate=1000.0)  # refills, so entries go stale fast

    for index in range(5_000):
        await store.consume(f"ip:{index}", bucket)

    assert len(store._buckets) <= 5_000
    assert len(store._buckets) < 5_000, "nothing was ever evicted"


async def test_separate_keys_do_not_share_a_budget() -> None:
    store = InMemoryRateLimitStore()
    bucket = Bucket(capacity=1, rate=0.001)

    assert (await store.consume("a", bucket))[0] is True
    assert (await store.consume("a", bucket))[0] is False
    assert (await store.consume("b", bucket))[0] is True


async def test_the_three_budgets_are_counted_apart() -> None:
    limiter = RateLimiter(RateLimitSettings(read_per_minute=1, write_per_minute=60, burst=1.0))

    assert (await limiter.check("1.2.3.4", scope="read")).allowed is True
    assert (await limiter.check("1.2.3.4", scope="read")).allowed is False
    assert (await limiter.check("1.2.3.4", scope="write")).allowed is True


async def test_a_refusal_reports_a_usable_delay() -> None:
    limiter = RateLimiter(RateLimitSettings(read_per_minute=60, burst=1.0))
    for _ in range(70):
        verdict = await limiter.check("1.2.3.4", scope="read")
        if not verdict.allowed:
            break

    assert verdict.allowed is False
    # Never zero: "come back in no time at all" is an invitation to come straight
    # back and be refused again.
    assert verdict.retry_after >= 1
    assert verdict.limit == 60


# ---------------------------------------------------------------------------
# Replacing the store
# ---------------------------------------------------------------------------


async def test_the_store_can_be_replaced_before_mounting(backend: SQLAlchemyBackend) -> None:
    """The default is per-process, which is right for one worker and wrong for
    four: four processes each holding a quarter of the counters is four times the
    allowance. A project running more than one needs a shared one."""
    seen: list[str] = []

    class Recording(InMemoryRateLimitStore):
        async def consume(
            self, key: str, bucket: Bucket, *, cost: float = 1.0
        ) -> tuple[bool, float]:
            seen.append(key)
            return await super().consume(key, bucket, cost=cost)

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            rate_limit={"enabled": True},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.set_rate_limit_store(Recording())

    app = FastAPI()
    fort.mount(app)

    connection = await opened(app)
    try:
        await connection.get("/admin/login")
    finally:
        await connection.aclose()

    assert seen, "the replacement store should have been asked"
    assert seen[0].startswith("read:")


async def test_the_store_cannot_be_swapped_after_mounting(backend: SQLAlchemyBackend) -> None:
    """Because the middleware already holds the one it was given, and a call that
    silently did nothing would be worse than one that says so."""
    from fastfort.core.exceptions import ConfigurationError

    fort = FastFort(
        FastFortSettings(secret_key=SECRET),  # type: ignore[call-arg]
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.mount(FastAPI())

    with pytest.raises(ConfigurationError, match="before mount"):
        fort.set_rate_limit_store(InMemoryRateLimitStore())


# ---------------------------------------------------------------------------
# It has to stay usable
# ---------------------------------------------------------------------------


async def test_the_defaults_do_not_get_in_the_way_of_ordinary_use(client: Any) -> None:
    """The budget is per address, and an address is not a person -- a whole office
    behind one NAT is one address. A limit tuned to what one human does would
    throttle the fifth colleague to open a list, so the defaults are loose."""
    connection = await client()  # whatever ships

    for _ in range(120):
        assert (await connection.get("/admin/shop.product/")).status_code == 200


async def test_signing_in_normally_is_not_throttled(backend: SQLAlchemyBackend) -> None:
    """Getting a password wrong twice and then right must not be a 429: that is
    a Tuesday, not an attack."""
    connection = await opened(build(backend))
    try:
        for _ in range(3):
            wrong = await connection.post(
                "/admin/login", data={"identity": ADMIN_EMAIL, "password": "nope"}
            )
            assert wrong.status_code != 429

        right = await connection.post(
            "/admin/login",
            data={"identity": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            follow_redirects=False,
        )
        assert right.status_code != 429
    finally:
        await connection.aclose()
