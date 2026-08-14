"""Which device a request came from, in words a person can read.

"Chrome 138 on Windows, from 84.54.72.10" is what someone checking their account
needs to see. A raw user-agent string is not that, and neither is an entry that
says only "Mozilla/5.0".

Three things are worth stating about how this reads it.

*Client hints first.* Chromium browsers send `Sec-CH-UA`, `Sec-CH-UA-Platform`
and `Sec-CH-UA-Mobile` on every request without being asked, and those are
structured fields rather than a sentence that has been accreting since 1994. The
user-agent string is the fallback, not the source of truth.

*The parse is a heuristic and is documented as one.* Every browser lies in its
user-agent -- Chrome claims to be Safari, which claims to be Gecko, which claims
to be Mozilla -- so the order the tokens are tested in is the whole algorithm.
The raw string is always kept alongside the reading, because the reading can be
wrong and the string is evidence.

*None of it is a security control.* A user-agent is written by whoever sent the
request. This is here so a person recognises their own sign-in, not so the
server can decide anything: nothing in FastFort branches on it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeAlias

__all__ = ["DEVICE_KINDS", "Device", "read_device"]

#: Every value `Device.kind` can take. A fixed vocabulary rather than free text,
#: so a column storing it can be introspected as a set of choices -- which is
#: what makes "sign-ins by device" a filter and a breakdown rather than a column
#: nobody can group by. "unknown" is a real answer: a request with no user-agent
#: at all is a script, and filing it as a laptop makes the one entry worth
#: noticing look like all the others.
DEVICE_KINDS = ("desktop", "mobile", "tablet", "bot", "unknown")

#: A case-insensitive header lookup, built once per request.
_Getter: TypeAlias = Callable[[str], str]

#: How much of a user-agent is kept. Long enough for anything real, short enough
#: that a megabyte of junk in a header is not a megabyte in the database.
MAX_USER_AGENT = 400

#: Browser tokens, most specific first. Every one of these appears in user-agents
#: that also contain the ones below it: Edge says "Chrome", Chrome says "Safari",
#: and Safari says "Gecko". Reordering this list silently relabels a browser.
_BROWSERS: tuple[tuple[str, str], ...] = (
    (r"Edg(?:e|A|iOS)?/(\d+)", "Edge"),
    (r"OPR/(\d+)", "Opera"),
    (r"Opera[ /](\d+)", "Opera"),
    (r"YaBrowser/(\d+)", "Yandex"),
    (r"Vivaldi/(\d+)", "Vivaldi"),
    (r"SamsungBrowser/(\d+)", "Samsung Internet"),
    (r"Firefox/(\d+)", "Firefox"),
    (r"FxiOS/(\d+)", "Firefox"),
    (r"CriOS/(\d+)", "Chrome"),
    (r"Chrome/(\d+)", "Chrome"),
    (r"Version/(\d+)[^)]*Safari", "Safari"),
    (r"curl/(\d+)", "curl"),
    (r"[Pp]ython-requests/(\d+)", "python-requests"),
    (r"httpx/(\d+)", "httpx"),
)

#: Platform tokens, again most specific first: an iPad's user-agent contains
#: "Mac OS X", and Android's contains "Linux".
_PLATFORMS: tuple[tuple[str, str], ...] = (
    (r"Windows NT 10", "Windows"),
    (r"Windows NT 6\.[123]", "Windows 7/8"),
    (r"Windows", "Windows"),
    (r"Android (\d+)", "Android"),
    (r"Android", "Android"),
    (r"(?:iPhone|iPad|iPod).*OS (\d+)", "iOS"),
    (r"iPhone|iPad|iPod", "iOS"),
    (r"CrOS", "ChromeOS"),
    (r"Mac OS X 10[._](\d+)", "macOS"),
    (r"Mac OS X", "macOS"),
    (r"Ubuntu", "Ubuntu"),
    (r"Linux", "Linux"),
)

#: Anything that says it is not a person. Checked before everything else, so a
#: crawler is never filed as "Chrome on Linux".
_BOT = re.compile(r"bot\b|crawler|spider|slurp|monitoring|uptime|headless", re.IGNORECASE)

#: The brand list in `Sec-CH-UA`, which is deliberately seasoned with a fictional
#: entry -- "Not)A;Brand" and its many punctuations -- so that parsers cannot
#: assume a fixed shape. Anything matching this is skipped.
_GREASE = re.compile(r"not[)/(\-_. ;:a-z]*brand", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Device:
    """What a request said it came from, read into fields.

    `user_agent` is the raw header, kept because the fields above it are a
    reading of it and a reading can be wrong.
    """

    address: str = ""
    browser: str = ""
    platform: str = ""
    kind: str = "unknown"
    user_agent: str = ""

    @property
    def summary(self) -> str:
        """One line: "Chrome 138 on Windows", or as much of it as is known."""
        parts = [part for part in (self.browser, self.platform) if part]
        if not parts:
            return self.user_agent[:60] or "Unknown device"
        return " on ".join(parts) if len(parts) == 2 else parts[0]


def read_device(headers: Mapping[str, str], *, address: str = "") -> Device:
    """Read one request's headers into a `Device`.

    Takes a mapping rather than a `Request` so it can be called from a test, a
    background job replaying a log, or an API layer -- and so this module never
    imports a web framework.
    """
    get = _lower(headers)
    user_agent = get("user-agent")[:MAX_USER_AGENT]

    return Device(
        address=address,
        browser=_brand(get) or _browser(user_agent),
        platform=_platform_hint(get) or _platform(user_agent),
        kind=_kind(get, user_agent),
        user_agent=user_agent,
    )


def _lower(headers: Mapping[str, str]) -> _Getter:
    lowered = {str(name).lower(): str(value) for name, value in headers.items()}

    def get(name: str) -> str:
        return lowered.get(name, "")

    return get


def _brand(get: _Getter) -> str:
    """The browser from `Sec-CH-UA`, which Chromium sends unprompted.

    The header lists every brand the browser is prepared to answer to --
    Chromium, the real product, and one invented name -- so the last real entry
    is the specific one and the fictional one has to be dropped or a third of
    sign-ins are recorded as being from a browser that does not exist.
    """
    header = get("sec-ch-ua")
    if not header:
        return ""

    found = ""
    for brand, version in re.findall(r'"([^"]+)";\s*v="([^"]+)"', header):
        if _GREASE.search(brand) or brand.strip().lower() == "chromium":
            continue
        found = f"{brand.strip()} {version.split('.')[0]}"
    return found


def _platform_hint(get: _Getter) -> str:
    """`Sec-CH-UA-Platform`, which arrives quoted: `"Windows"`."""
    value = get("sec-ch-ua-platform").strip().strip('"')
    return value if value and value.lower() not in {"unknown", ""} else ""


def _browser(user_agent: str) -> str:
    if not user_agent:
        return ""
    for pattern, name in _BROWSERS:
        match = re.search(pattern, user_agent)
        if match:
            return f"{name} {match.group(1)}" if match.groups() else name
    return ""


def _platform(user_agent: str) -> str:
    if not user_agent:
        return ""
    for pattern, name in _PLATFORMS:
        match = re.search(pattern, user_agent)
        if match:
            return f"{name} {match.group(1)}" if match.groups() else name
    return ""


def _kind(get: _Getter, user_agent: str) -> str:
    """desktop, mobile, tablet, bot -- or unknown, which is a real answer.

    A missing user-agent is unknown rather than desktop: a request with no
    user-agent at all is a script, and filing it as a laptop makes the one entry
    worth noticing look like all the others.
    """
    if _BOT.search(user_agent):
        return "bot"
    mobile = get("sec-ch-ua-mobile")
    if mobile == "?1":
        return "mobile"
    if "iPad" in user_agent or ("Android" in user_agent and "Mobile" not in user_agent):
        return "tablet"
    if "Mobi" in user_agent or "iPhone" in user_agent:
        return "mobile"
    if mobile == "?0" or user_agent:
        return "desktop"
    return "unknown"
