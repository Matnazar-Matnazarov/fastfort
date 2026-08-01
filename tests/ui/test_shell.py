"""The admin shell: icons, the account card, the theme control and the header.

These guard the parts of the chrome that fail quietly. An icon whose name is
misspelled renders an empty box, a control that only works with JavaScript looks
identical to one that is broken, and a theme control that cannot express "follow
the system" silently pins the admin the first time it is touched.
"""

from __future__ import annotations

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
