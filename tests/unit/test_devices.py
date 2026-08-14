"""Reading a request's headers into "Chrome 138 on Windows".

Every browser lies in its user-agent -- Chrome claims to be Safari, which claims
to be Gecko, which claims to be Mozilla -- so the order the tokens are tested in
is the whole algorithm, and these are the cases that catch a reordering.
"""

from __future__ import annotations

import pytest

from fastfort.auth.devices import DEVICE_KINDS, MAX_USER_AGENT, read_device

CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
EDGE = CHROME + " Edg/138.0.0.0"
SAFARI_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1"
)
FIREFOX = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
ANDROID_TABLET = (
    "Mozilla/5.0 (Linux; Android 14; SM-X200) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def test_the_client_hint_beats_the_user_agent() -> None:
    """`Sec-CH-UA` is a structured field; the user-agent is a sentence that has
    been accreting since 1994. Chromium sends both without being asked."""
    device = read_device(
        {
            "user-agent": CHROME,
            "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Google Chrome";v="138"',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-mobile": "?0",
        }
    )

    assert device.browser == "Google Chrome 138"
    assert device.platform == "Windows"
    assert device.kind == "desktop"


def test_the_invented_brand_is_dropped() -> None:
    """The header is deliberately seasoned with a brand that does not exist, so
    that parsers cannot assume a fixed shape. Kept, it would file a third of all
    sign-ins under a browser nobody has installed."""
    device = read_device({"sec-ch-ua": '"Not/A)Brand";v="99", "Opera";v="120"'})

    assert device.browser == "Opera 120"


def test_headers_are_read_whatever_their_case() -> None:
    device = read_device({"User-Agent": FIREFOX, "Sec-CH-UA-Platform": '"Linux"'})

    assert device.browser == "Firefox 130"
    assert device.platform == "Linux"


@pytest.mark.parametrize(
    ("user_agent", "browser"),
    [
        (EDGE, "Edge 138"),
        (CHROME, "Chrome 138"),
        (SAFARI_IPHONE, "Safari 18"),
        (FIREFOX, "Firefox 130"),
        ("curl/8.7.1", "curl 8"),
    ],
)
def test_the_specific_browser_wins_over_the_ones_it_impersonates(
    user_agent: str, browser: str
) -> None:
    assert read_device({"user-agent": user_agent}).browser == browser


@pytest.mark.parametrize(
    ("user_agent", "kind"),
    [
        (CHROME, "desktop"),
        (SAFARI_IPHONE, "mobile"),
        (ANDROID_TABLET, "tablet"),
        ("Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)", "bot"),
        ("", "unknown"),
    ],
)
def test_what_kind_of_device_it_is(user_agent: str, kind: str) -> None:
    """A missing user-agent is unknown rather than desktop: a request with no
    user-agent is a script, and filing it as a laptop makes the one entry worth
    noticing look like all the others."""
    assert read_device({"user-agent": user_agent}).kind == kind


def test_a_crawler_is_never_filed_as_a_browser() -> None:
    """It claims to be Chrome, and it is checked for first."""
    headless = read_device({"user-agent": CHROME + " HeadlessChrome/138"})

    assert headless.kind == "bot"


@pytest.mark.parametrize(
    "user_agent",
    [CHROME, SAFARI_IPHONE, FIREFOX, ANDROID_TABLET, "curl/8.7.1", "Googlebot/2.1", ""],
)
def test_the_kind_is_always_one_of_the_declared_values(user_agent: str) -> None:
    """The vocabulary is fixed because a column stores it as a set of choices --
    which is what makes "sign-ins by device" a filter and a breakdown rather
    than a column nobody can group by. A sixth value would be a row the schema
    rejects."""
    assert read_device({"user-agent": user_agent}).kind in DEVICE_KINDS


def test_the_address_is_carried_through_untouched() -> None:
    """Read by the caller, from the same helper the lockout and the rate limiter
    use -- two answers to "who is this" would mean one of them is wrong."""
    assert read_device({}, address="84.54.72.10").address == "84.54.72.10"


def test_a_huge_user_agent_is_truncated() -> None:
    """A header is written by whoever sent the request. A megabyte of it must
    not become a megabyte in the database."""
    device = read_device({"user-agent": "x" * 5000})

    assert len(device.user_agent) == MAX_USER_AGENT


def test_the_summary_says_as_much_as_is_known() -> None:
    assert read_device({"user-agent": CHROME}).summary == "Chrome 138 on Windows"
    assert read_device({"user-agent": "curl/8.7.1"}).summary == "curl 8"
    assert read_device({}).summary == "Unknown device"


def test_nothing_here_decides_anything() -> None:
    """The raw string is always kept, because the fields beside it are a reading
    of it and a reading can be wrong. Nothing in FastFort branches on either."""
    device = read_device({"user-agent": "Mozilla/5.0 (Nintendo Switch)"})

    assert device.browser == ""
    assert device.user_agent == "Mozilla/5.0 (Nintendo Switch)"
