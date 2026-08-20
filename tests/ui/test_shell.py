"""The admin shell: icons, the account card, the theme control and the header.

These guard the parts of the chrome that fail quietly. An icon whose name is
misspelled renders an empty box, a control that only works with JavaScript looks
identical to one that is broken, and a theme control that cannot express "follow
the system" silently pins the admin the first time it is touched.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import ADMIN_EMAIL, sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.orm.sqlalchemy import SQLAlchemyBackend, introspect_model
from fastfort.spec import ModelSpec
from fastfort.ui.icons import ICONS, icon_names, is_icon
from fastfort.ui.renderer import icon, sprite

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price")
    verbose_name_plural = "Products"
    icon = "box"


def build(backend: SQLAlchemyBackend, **ui: Any) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            ui=ui,  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


@pytest.fixture(scope="module")
def spec() -> ModelSpec:
    """Enough of a spec to instantiate a ModelAdmin. No database needed."""
    return introspect_model(Product, key="shop.product")


async def page(client: httpx.AsyncClient, path: str) -> str:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.text


# ---------------------------------------------------------------------------
# Icons
# ---------------------------------------------------------------------------


def test_an_unknown_icon_renders_nothing_rather_than_raising() -> None:
    """An icon is decoration. A mistyped name must not be able to take down the
    page it decorates."""
    assert icon("no-such-icon") == ""
    assert icon(None) == ""


def test_a_known_icon_references_the_sprite() -> None:
    assert 'href="#ff-i-users"' in icon("users")


def test_the_sprite_defines_every_registered_icon() -> None:
    markup = str(sprite())
    for name in icon_names():
        assert f'id="ff-i-{name}"' in markup, name


def test_every_icon_is_drawn_on_the_same_grid() -> None:
    """Mixed viewBoxes are how an icon set ends up with one glyph visibly larger
    than its neighbours."""
    assert str(sprite()).count('viewBox="0 0 24 24"') == len(ICONS)


#: How many numbers each path command takes, per repetition.
_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _line_runs(d: str) -> list[list[tuple[float, float]]]:
    """The straight-line runs in a path, as lists of steps taken.

    Steps are absolute offsets whichever form the path is written in, so `H`
    and `h` are comparable. Only moves and lines are followed: a curve or an arc
    ends the run being collected, because "the pen came back to where it was"
    says nothing about a shape that got there along a different route.
    """
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+", d)
    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    command = "M"
    x = y = 0.0
    index = 0

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in "Zz":
                runs.append(current)
                current = []
            continue

        upper = command.upper()
        relative = command.islower()
        numbers = [float(n) for n in tokens[index : index + _ARITY[upper]]]
        index += _ARITY[upper]

        if upper in {"M", "L"}:
            step = (numbers[0], numbers[1]) if relative else (numbers[0] - x, numbers[1] - y)
        elif upper == "H":
            step = (numbers[0], 0.0) if relative else (numbers[0] - x, 0.0)
        elif upper == "V":
            step = (0.0, numbers[0]) if relative else (0.0, numbers[0] - y)
        else:
            # A curve or an arc: the run stops here, and the pen lands on the
            # last coordinate pair the command carries.
            runs.append(current)
            current = []
            end = numbers[-2:] if upper != "A" else numbers[5:7]
            x, y = (x + end[0], y + end[1]) if relative else (end[0], end[1])
            continue

        x, y = x + step[0], y + step[1]

        if upper == "M":
            # A move starts a new subpath -- and after the first pair, further
            # pairs are implicit lines, which is how the chevron below was
            # written and how a parser that ignores them misses the bug.
            runs.append(current)
            current = []
            command = "l" if relative else "L"
        else:
            current.append(step)

    runs.append(current)
    return runs


@pytest.mark.parametrize("name", icon_names())
def test_an_icon_never_retraces_the_line_it_just_drew(name: str) -> None:
    """Two consecutive steps that cancel out paint over the segment before them.

    `chevron-left` shipped as `m15 18-6-6 6 6`: down-left, then back up-right
    along the same line. It drew one diagonal stroke rather than a chevron, and
    it was the "previous page" arrow on every list -- visible on every screen
    with more than one page of rows, and still invisible to every test, because
    the markup was perfectly well-formed and the sprite defined the symbol.
    """
    # Only the paths: an icon's `<circle>` and `<rect>` carry attributes whose
    # letters and numbers read as perfectly plausible path commands.
    for d in re.findall(r'\bd="([^"]+)"', ICONS[name]):
        for run in _line_runs(d):
            for before, after in itertools.pairwise(run):
                assert (before[0] + after[0], before[1] + after[1]) != (0.0, 0.0), (
                    f"{name} retraces {before} with {after} in {d!r}"
                )


def test_is_icon_rejects_the_empty_name() -> None:
    assert not is_icon("")
    assert not is_icon(None)
    assert is_icon("users")


def test_a_misspelled_icon_is_a_configuration_error(spec: ModelSpec) -> None:
    """Caught at declaration time. The alternative is a blank slot in the sidebar
    that nobody notices and nothing explains."""

    class Broken(admin.ModelAdmin):
        icon = "definitely-not-an-icon"

    with pytest.raises(ConfigurationError, match="definitely-not-an-icon"):
        Broken(spec)


def test_the_icon_error_says_what_the_choices_are(spec: ModelSpec) -> None:
    class Broken(admin.ModelAdmin):
        icon = "nope"

    with pytest.raises(ConfigurationError) as raised:
        Broken(spec)

    assert "users" in str(raised.value)


@pytest.mark.parametrize("path", ["/admin/", "/admin/shop.product/", "/admin/shop.product/add"])
async def test_a_page_references_only_icons_it_ships(client: httpx.AsyncClient, path: str) -> None:
    """The failure this catches is silent: `<use>` pointing at a symbol that is
    not there renders an empty box, and nothing anywhere reports it."""
    body = await page(client, path)
    defined = set(re.findall(r'<symbol id="ff-i-([\w-]+)"', body))
    used = set(re.findall(r'<use href="#ff-i-([\w-]+)"', body))

    assert used, f"{path} draws no icons at all"
    assert used <= defined, sorted(used - defined)


async def test_the_sprite_is_emitted_exactly_once(client: httpx.AsyncClient) -> None:
    """Twice would duplicate every symbol id, and which one a `<use>` resolves to
    then depends on document order."""
    assert (await page(client, "/admin/")).count('class="ff-sprite"') == 1


async def test_the_sign_in_page_carries_its_own_sprite(backend: SQLAlchemyBackend) -> None:
    """It does not extend the shell, so it cannot inherit one -- and without it
    every icon on the page is an empty box. Signed out on purpose: signed in,
    /login redirects away and there is nothing to check."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as anonymous:
        body = (await anonymous.get("/admin/login")).text

    assert 'class="ff-sprite"' in body


async def test_a_declared_icon_reaches_the_sidebar(client: httpx.AsyncClient) -> None:
    assert '<use href="#ff-i-box"/>' in await page(client, "/admin/")


async def test_a_model_without_an_icon_still_lines_up(client: httpx.AsyncClient) -> None:
    """Every entry gets an icon slot. Half a sidebar indented and half not is
    worse than a default glyph."""
    assert (await page(client, "/admin/")).count('class="ff-nav-item__icon"') >= 2


# ---------------------------------------------------------------------------
# Theme control
# ---------------------------------------------------------------------------


async def test_the_theme_control_offers_all_three_modes(client: httpx.AsyncClient) -> None:
    """Two would be a trap: choosing either pins the admin, and there is then no
    way back to following the operating system."""
    body = await page(client, "/admin/")
    for mode in ("light", "dark", "system"):
        assert f'data-ff-theme-set="{mode}"' in body, mode


async def test_the_density_control_offers_both_spacings(client: httpx.AsyncClient) -> None:
    """`UISettings.density` drove every `--ff-space-*` token, and `boot.js` read
    `ff:density` before the first paint, long before anything on screen could
    set it -- the stylesheet, the storage key and the pre-paint application all
    existed and the control did not.

    Two states, not three: there is no system preference for spacing to follow,
    so "comfortable" and "compact" are the whole choice.
    """
    body = await page(client, "/admin/")
    for spacing in ("comfortable", "compact"):
        assert f'data-ff-density-set="{spacing}"' in body, spacing


async def test_the_stored_theme_is_applied_before_the_first_paint(
    client: httpx.AsyncClient,
) -> None:
    """The anti-flash script must block, and must run before the stylesheets.

    Everything else on the page is deferred, correctly -- but a deferred script
    runs *after* the first paint, so anything it sets is a visible change. The
    admin flashed dark and then turned light on every load, and the sidebar
    opened before snapping shut. Only a blocking script in the head can set the
    theme early enough for the first paint to be the right one.
    """
    body = await page(client, "/admin/")
    head = body[: body.index("</head>")]

    boot = re.search(r"<script src=\"([^\"]*boot\.js)\"([^>]*)>", head)
    assert boot, "boot.js must be in the head"
    assert "defer" not in boot.group(2), "boot.js must not be deferred"
    assert "async" not in boot.group(2), "boot.js must not be async"

    # Before the stylesheets, so the attributes it sets are already in place when
    # the first rule is matched.
    assert head.index(boot.group(0)) < head.index('<link rel="stylesheet"')

    # And the main script stays deferred: it must not block the page.
    main = re.search(r"<script src=\"([^\"]*fastfort\.js)\"([^>]*)>", head)
    assert main, "the main script should be in the head"
    assert "defer" in main.group(2)


async def test_the_boot_script_is_served(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/static/js/boot.js")
    assert response.status_code == 200
    assert "ff:theme" in response.text


async def test_the_current_model_can_be_found_in_the_nav_by_the_boot_script(
    client: httpx.AsyncClient,
) -> None:
    """`.ff-nav` scrolls by itself, so a browser starts it at zero on every
    navigation and the current model was highlighted below the visible band.
    `boot.js` scrolls it back into view before the first paint.

    It finds its way there through four selectors that live in the template, not
    in the script: `.ff-sidebar`, `.ff-nav`, `[aria-current="page"]`, and
    `.ff-sidebar__footer` -- the last of which is how it knows the parser is past
    `</nav>` and the items have real heights. Rename any of them and the sidebar
    quietly goes back to marking "you are here" off screen, with every other test
    still green. This is the check that makes that a failure.
    """
    boot = (await client.get("/admin/static/js/boot.js")).text
    body = await page(client, "/admin/shop.product/")

    for selector in (".ff-sidebar .ff-nav", ".ff-sidebar__footer", 'aria-current="page"'):
        assert selector in boot, f"boot.js no longer looks for {selector}"

    nav = re.search(r'<nav class="ff-nav[^"]*"[\s\S]*?</nav>', body)
    assert nav, "the sidebar should render a .ff-nav"
    assert 'aria-current="page"' in nav.group(0), "the current model should be marked in the nav"
    assert '<div class="ff-sidebar__footer">' in body
    assert body.index("</nav>") < body.index('<div class="ff-sidebar__footer">')


async def test_the_script_route_serves_only_what_it_ships(
    client: httpx.AsyncClient,
) -> None:
    """The name comes from the URL, so it is an allow-list rather than a path."""
    for name in ("../../admin/site.py", "nope.js", "%2e%2e%2fsite.py"):
        response = await client.get(f"/admin/static/js/{name}", follow_redirects=True)
        assert response.status_code == 404, name
        assert "build_admin_router" not in response.text, name


async def test_the_theme_control_is_hidden_without_javascript(client: httpx.AsyncClient) -> None:
    """It writes to local storage, so with scripting off it is a button that does
    nothing when clicked. Better absent than broken."""
    body = await page(client, "/admin/")
    segment = re.search(r'<div class="([^"]*)"[^>]*role="group"[^>]*aria-label="Theme"', body)

    assert segment, "the theme segment should be a labelled group"
    assert "ff-js-only" in segment.group(1)


# ---------------------------------------------------------------------------
# Account card
# ---------------------------------------------------------------------------


async def test_the_account_card_sits_in_the_sidebar(client: httpx.AsyncClient) -> None:
    body = await page(client, "/admin/")
    sidebar = body[body.index('id="ff-sidebar"') : body.index("<header")]

    assert "ff-account" in sidebar


async def test_the_account_card_names_who_is_signed_in(client: httpx.AsyncClient) -> None:
    """A card that says only "admin" cannot tell two accounts apart."""
    assert ADMIN_EMAIL in await page(client, "/admin/")


async def test_signing_out_still_carries_a_csrf_token(client: httpx.AsyncClient) -> None:
    """Moving the menu must not have left the one destructive form in it
    unprotected."""
    body = await page(client, "/admin/")
    menu = body[body.index("ff-account") : body.index("</details>")]

    assert 'name="_csrf"' in menu


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------


async def test_the_environment_badge_is_absent_by_default(client: httpx.AsyncClient) -> None:
    assert "ff-env" not in await page(client, "/admin/")


async def test_the_environment_badge_appears_in_the_header(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """In the header rather than the footer: which of two open windows is
    production has to be answerable without scrolling."""
    app = build(backend, environment_label="Production", environment_tone="danger")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        body = await page(opened, "/admin/")

    header = body[body.index("<header") : body.index("</header>")]
    assert "ff-badge--danger" in header
    assert "Production" in header
