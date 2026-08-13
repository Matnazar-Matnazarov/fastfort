"""Which client a request came from.

One function, in a module of its own, because two things need the answer and two
answers would mean one of them is wrong. `Lockout` counts failed sign-ins per
address and `RateLimitMiddleware` counts requests per address; if those disagree
about what an address is, an attacker only has to find the looser one.

In `auth/` rather than in `admin/` because it sits below both: the admin imports
from here, and nothing here imports from the admin.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = ["client_address"]


def client_address(scope: Mapping[str, Any], *, forwarded_depth: int = 0) -> str:
    """The address to charge this request to.

    `forwarded_depth` is the number of proxies in front of this process that
    append to `X-Forwarded-For`, and it defaults to none. That default matters: a
    header is written by whoever sent the request, so reading it without being
    told there is a proxy is a documented bypass -- send a different value each
    time and every request is a new client with a fresh budget and a fresh
    lockout counter.

    Counted from the *right*, which is the correction to the conventional
    reading. "The leftmost entry is the original client" is true of a header
    nobody tampered with, and the header arrives with the request: the left is
    what the sender chose, and each proxy appends to the right of it. With one
    proxy in front, the last entry is what that proxy actually observed, and
    everything to the left of it is a claim.
    """
    if forwarded_depth > 0:
        for name, value in scope.get("headers", ()):
            if name != b"x-forwarded-for":
                continue
            hops = [hop.strip() for hop in value.decode("latin-1").split(",") if hop.strip()]
            if len(hops) >= forwarded_depth:
                return str(hops[-forwarded_depth])
            # Fewer hops than configured means the request did not come through
            # the proxies it was supposed to, so its header is not evidence of
            # anything. Fall through to the socket -- which is then the proxy
            # itself, and therefore restrictive rather than forgeable.
            break

    client = scope.get("client")
    return str(client[0]) if client else "unknown"
