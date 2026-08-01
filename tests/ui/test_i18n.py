"""Translation: catalogues, language negotiation and the rendered admin."""

from __future__ import annotations

import html
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Translator,
    available_languages,
    negotiate_language,
)
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

# The rendered checks need rows: an empty table renders the empty state, whose
# wording differs from the table's.
pytestmark = pytest.mark.usefixtures("seeded")

LOCALE_DIR = Path(__file__).resolve().parents[2] / "fastfort" / "i18n" / "locale"


# ---------------------------------------------------------------------------
# Catalogues
# ---------------------------------------------------------------------------


def test_every_declared_language_has_a_catalogue() -> None:
    """Except English, which is the source language and needs no file."""
    for code in LANGUAGES:
        if code == DEFAULT_LANGUAGE:
            continue
        assert (LOCALE_DIR / f"{code}.json").is_file(), code


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
def test_a_catalogue_is_valid_json_of_strings(code: str) -> None:
    loaded = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))

    assert isinstance(loaded, dict)
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in loaded.items())


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
def test_a_translation_keeps_its_placeholders(code: str) -> None:
    """A translation that drops `{count}` renders a sentence missing its number;
    one that invents a placeholder raises at render time."""
    loaded = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    pattern = re.compile(r"\{(\w+)\}")

    for source, translated in loaded.items():
        assert set(pattern.findall(translated)) == set(pattern.findall(source)), source


def test_an_untranslated_string_falls_back_to_english() -> None:
    """A half-finished catalogue must stay usable, and a typo in a key must be
    visible as English text rather than as nothing."""
    translate = Translator("uz")
    assert translate("A string nobody has translated") == "A string nobody has translated"


def test_placeholders_are_filled() -> None:
    assert Translator("uz")("Page {page} of {pages}", page=2, pages=5) == "5 sahifadan 2-si"


def test_a_broken_placeholder_does_not_take_the_page_down() -> None:
    """Rendering the untranslated source beats raising during a request."""
    assert Translator("en")("Page {page} of {pages}", page=1) == "Page {page} of {pages}"


def test_the_default_language_needs_no_catalogue() -> None:
    assert Translator()("Sign in") == "Sign in"
    assert Translator().is_default


def test_available_languages_include_english_first() -> None:
    languages = available_languages()
    assert languages[0] == DEFAULT_LANGUAGE
    assert {"uz", "ru"} <= set(languages)


# ---------------------------------------------------------------------------
# Negotiation
# ---------------------------------------------------------------------------


def test_an_explicit_choice_wins_over_everything() -> None:
    chosen = negotiate_language(chosen="ru", configured="uz", accept="en-GB,en;q=0.9")
    assert chosen == "ru"


def test_a_pinned_language_wins_over_the_browser() -> None:
    """An admin configured in Uzbek should not appear in English because a laptop
    was bought abroad."""
    assert negotiate_language(configured="uz", accept="en-GB,en;q=0.9") == "uz"


def test_the_browser_decides_when_nothing_is_pinned() -> None:
    """`configured=None` is what "the project did not choose" looks like. Leaving
    it defaulted to English would make Accept-Language dead code."""
    assert negotiate_language(accept="ru-RU,ru;q=0.9") == "ru"
    assert negotiate_language(accept="uz-UZ,uz;q=0.8,en;q=0.5") == "uz"


def test_an_unsupported_language_is_ignored_at_every_level() -> None:
    assert negotiate_language(chosen="klingon") == DEFAULT_LANGUAGE
    assert negotiate_language(configured="klingon") == DEFAULT_LANGUAGE
    assert negotiate_language(accept="klingon,tlh;q=0.9") == DEFAULT_LANGUAGE


def test_a_regional_variant_matches_its_base_language() -> None:
    assert negotiate_language(accept="ru-BY") == "ru"


# ---------------------------------------------------------------------------
# The rendered admin
# ---------------------------------------------------------------------------


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "price")
    search_fields = ("name",)
    verbose_name_plural = "Products"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        yield opened


async def page(client: httpx.AsyncClient, path: str, **kwargs: object) -> str:
    response = await client.get(path, **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    # Unescaped, because an apostrophe in "O'chirish" arrives as &#39;.
    return html.unescape(response.text)


async def test_the_sign_in_page_can_be_translated(client: httpx.AsyncClient) -> None:
    """It has to be reachable before anyone has an account to store a preference
    against, so the switcher lives on the page itself."""
    assert "Sign in" in await page(client, "/admin/login")

    await client.post("/admin/language", data={"language": "uz", "next": "/admin/login"})
    assert "Kirish" in await page(client, "/admin/login")


async def test_a_language_choice_is_remembered(client: httpx.AsyncClient) -> None:
    await client.post("/admin/language", data={"language": "ru", "next": "/admin/login"})
    assert client.cookies.get("ff_language") == "ru"
    assert "Войти" in await page(client, "/admin/login")


async def test_the_whole_admin_follows_the_choice(client: httpx.AsyncClient) -> None:
    await sign_in(client)
    await client.post("/admin/language", data={"language": "uz", "next": "/admin/"})

    dashboard = await page(client, "/admin/")
    assert "Boshqaruv paneli" in dashboard
    assert 'lang="uz"' in dashboard

    listing = await page(client, "/admin/shop.product/")
    for expected in ("Tahrirlash", "O'chirish", "Qo'llash", "sahifadan"):
        assert expected in listing, expected

    assert "Bekor qilish" in await page(client, "/admin/shop.product/add")
    assert "Buni qaytarib bo'lmaydi" in await page(client, "/admin/shop.product/1/delete")


async def test_the_browser_language_is_honoured_without_a_choice(
    client: httpx.AsyncClient,
) -> None:
    body = await page(client, "/admin/login", headers={"Accept-Language": "ru-RU,ru;q=0.9"})
    assert "Войти" in body


async def test_an_unsupported_choice_leaves_the_admin_in_english(
    client: httpx.AsyncClient,
) -> None:
    await client.post("/admin/language", data={"language": "klingon", "next": "/admin/login"})
    assert "Sign in" in await page(client, "/admin/login")


async def test_the_switcher_cannot_be_used_to_redirect_off_site(
    client: httpx.AsyncClient,
) -> None:
    """Any endpoint taking a `next` is an open-redirect candidate."""
    response = await client.post(
        "/admin/language", data={"language": "ru", "next": "//evil.example.com"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"


async def test_the_switcher_offers_every_available_language(
    client: httpx.AsyncClient,
) -> None:
    body = await page(client, "/admin/login")
    for name in ("English", "O'zbekcha", "Русский"):
        assert name in body, name


async def test_the_switcher_is_not_a_native_select(client: httpx.AsyncClient) -> None:
    """A <select> opens the operating system's own popup: unstyleable, unlike
    anything else on the page, and on a phone it takes over the whole screen for
    three options."""
    body = await page(client, "/admin/login")

    assert not re.search(r'<select[^>]*name="language"', body)
    assert len(re.findall(r'<button\b[^>]*?name="language"', body, re.DOTALL)) == 3


async def test_each_language_carries_its_flag_and_its_code(client: httpx.AsyncClient) -> None:
    """The code is not decoration: several platforms draw a regional-indicator
    pair as two letters rather than a flag, and one that fails to render must not
    take the meaning of the row with it."""
    body = await page(client, "/admin/login")

    for flag, code in (("\U0001f1ec\U0001f1e7", "EN"), ("\U0001f1fa\U0001f1ff", "UZ")):
        assert flag in body, code
        assert f'class="ff-menu-item__code" aria-hidden="true">{code}<' in body, code


async def test_the_current_language_is_marked(client: httpx.AsyncClient) -> None:
    await client.post("/admin/language", data={"language": "uz", "next": "/admin/login"})
    body = await page(client, "/admin/login")

    marked = re.findall(
        r'name="language"\s+value="(\w+)"[^>]*lang="\w+"\s+aria-current="true"', body
    )
    assert marked == ["uz"]


async def test_choosing_a_language_returns_to_the_same_page(client: httpx.AsyncClient) -> None:
    """The point of the switcher is to read the page you are on in another
    language, not to be sent back to the dashboard."""
    await sign_in(client)
    response = await client.post(
        "/admin/language", data={"language": "ru", "next": "/admin/shop.product/"}
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/shop.product/"


async def test_switching_language_keeps_the_query_string(client: httpx.AsyncClient) -> None:
    """Switching from a filtered, sorted, paginated list and landing on page one
    of an unfiltered one loses real work."""
    await sign_in(client)
    # `page` unescapes, so the `&amp;` the template writes arrives as `&`.
    body = await page(client, "/admin/shop.product/?q=widget&o=-id")

    assert 'name="next" value="/admin/shop.product/?q=widget&o=-id"' in body
