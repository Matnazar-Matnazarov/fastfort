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
from fastfort.admin.security import LANGUAGE_COOKIE
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


#: Chrome strings that reach the translator as data rather than as a literal at
#: the call site -- date presets, accent names, the built-in action label. The
#: scanner cannot see these, so they are listed once here instead.
_INDIRECT = frozenset(
    {
        "Today",
        "Yesterday",
        "Last 7 days",
        "Last 30 days",
        "This week",
        "This month",
        "Last month",
        "This year",
        "Violet",
        "Indigo",
        "Blue",
        "Sky",
        "Teal",
        "Green",
        "Lime",
        "Amber",
        "Orange",
        "Red",
        "Pink",
        "Magenta",
        "Add",
        "Delete selected",
        "Leave blank to keep the current password.",
        # Dashboard widgets carry their own default titles, which reach the
        # translator as `Card.context["title"]` rather than as a literal in a
        # template. A project that passes `title=` supplies its own words and
        # FastFort does not translate those, the same as a model's name.
        "New accounts",
        "Browse",
    }
)


def _translated_literals() -> set[str]:
    """Every string the admin's own interface passes through the translator.

    Templates call `_("…")`; the Python layer calls a `Translator` bound to a
    local name. Both are scanned, because a string added in either place and not
    added to the catalogues is invisible until someone switches language and
    finds one English sentence in the middle of a translated page.

    Model and field names are deliberately absent: those are the project's words
    for its own domain, and FastFort does not translate them.
    """
    import pathlib

    package = pathlib.Path(__file__).resolve().parents[2] / "fastfort"
    found: set[str] = set(_INDIRECT)

    for path in (package / "ui" / "templates").rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'_\(\s*"((?:[^"\\]|\\.)*)"', text))
        found |= set(re.findall(r"_\(\s*'((?:[^'\\]|\\.)*)'", text))

    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        found |= set(re.findall(r'(?:translate|label_of)\(\s*"((?:[^"\\]|\\.)*)"', text))

    return found


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
def test_every_interface_string_is_translated(code: str) -> None:
    """A string added without a catalogue entry renders English and looks broken.

    This is the check that keeps the admin from drifting back into half a page
    in one language: it fails on the commit that adds the string, not months
    later when someone switches to Uzbek and finds "20 per page" in the middle
    of an otherwise translated list.
    """
    catalog = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    missing = sorted(text for text in _translated_literals() if text not in catalog)

    assert missing == [], f"{code} is missing: {missing}"


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
def test_a_catalogue_carries_nothing_the_interface_does_not_use(code: str) -> None:
    """A catalogue that accumulates dead entries stops being reviewable.

    Model names used to live in here. They do not any more: a model's name is the
    project's word for its own domain, and translating it was never FastFort's
    job -- Django does not translate your model names either.
    """
    catalog = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    stale = sorted(set(catalog) - _translated_literals())

    assert stale == [], f"{code} carries entries nothing renders: {stale}"


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
def test_a_translation_keeps_its_placeholders(code: str) -> None:
    """A translation that drops `{count}` renders a sentence missing its number;
    one that invents a placeholder raises at render time."""
    loaded = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    pattern = re.compile(r"\{(\w+)\}")

    for source, translated in loaded.items():
        assert set(pattern.findall(translated)) == set(pattern.findall(source)), source


#: The two catalogue entries that are lists rather than sentences, and how many
#: names each has to hold. The calendar indexes straight into them.
NAME_LISTS = (
    ("January,February,March,April,May,June,July,August,September,October,November,December", 12),
    ("Sunday,Monday,Tuesday,Wednesday,Thursday,Friday,Saturday", 7),
)


@pytest.mark.parametrize("code", [c for c in LANGUAGES if c != DEFAULT_LANGUAGE])
@pytest.mark.parametrize(("source", "count"), NAME_LISTS)
def test_a_list_of_names_keeps_its_length_and_its_order(code: str, source: str, count: int) -> None:
    """The calendar reads `t("Months").split(",")[date.getMonth()]`.

    So a catalogue that translated eleven months, or reordered them to put the
    week's first day first, would not fail here as a missing string -- it would
    silently label September as August, or every Tuesday as Monday. Nothing else
    in the admin indexes into a translation, which is why this check exists only
    for these two.
    """
    catalog = json.loads((LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
    names = catalog[source].split(",")

    assert len(names) == count, f"{code}: {len(names)} names, expected {count}"
    assert all(name.strip() for name in names), f"{code}: a name is blank"
    assert len(set(names)) == count, f"{code}: two names are the same"


def test_the_calendars_month_names_are_sent_to_the_browser() -> None:
    """`Intl` names the months in ten of the eleven languages, and in the
    eleventh -- Uzbek, which Chromium ships no date symbols for -- it answers
    "M09". The script decides which source to use, so the server always sends
    these; if it stopped, the calendar would show "M09" with nothing failing."""
    from fastfort.admin.site import _ui_text

    sent = _ui_text(Translator("uz"))

    assert sent["months"].split(",")[8] == "Sentabr"
    assert sent["weekdays"].split(",")[1] == "Dushanba"


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
    # One button per language, however many there are.
    assert len(re.findall(r'<button\b[^>]*?name="language"', body, re.DOTALL)) == len(LANGUAGES)


@pytest.mark.parametrize(
    ("language", "direction"),
    [("ar", "rtl"), ("en", "ltr"), ("ja", "ltr"), ("uz", "ltr")],
)
async def test_the_page_declares_which_way_it_reads(
    client: httpx.AsyncClient, language: str, direction: str
) -> None:
    """One attribute turns the whole admin around. The stylesheet is written in
    logical properties throughout, so `dir` is the only thing standing between a
    right-to-left language and a layout laid out backwards.
    """
    await sign_in(client)
    client.cookies.set(LANGUAGE_COOKIE, language)
    tag = re.search(r"<html[^>]*>", await page(client, "/admin/"))

    assert tag
    assert f'dir="{direction}"' in tag.group(0)


async def test_the_sign_in_page_reads_the_same_way(client: httpx.AsyncClient) -> None:
    """It is not built on the shell, so it is the page that gets forgotten --
    and it is the only one an anonymous visitor ever sees."""
    client.cookies.set(LANGUAGE_COOKIE, "ar")
    tag = re.search(r"<html[^>]*>", await page(client, "/admin/login"))

    assert tag
    assert 'dir="rtl"' in tag.group(0)


async def test_the_switcher_can_be_filtered(client: httpx.AsyncClient) -> None:
    """Nine languages is past the point where a list is scanned rather than read.

    The filter matches the name and the code, because someone who knows "de" is
    not going to guess "Deutsch".
    """
    body = await page(client, "/admin/login")

    assert "data-ff-lang-filter" in body
    # The endonym, the English name and the code all match, because someone
    # might reach for any of the three.
    assert 'data-ff-lang="deutsch german de"' in body


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

    # Tolerant of attribute order and of attributes added between them: this is
    # checking which row is marked, not how the button is spelled.
    marked = [
        re.search(r'value="(\w+)"', button).group(1)
        for button in re.findall(r"<button\b[^>]*?name=\"language\"[^>]*>", body, re.DOTALL)
        if 'aria-current="true"' in button
    ]
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


# ---------------------------------------------------------------------------
# The strings script produces on its own
# ---------------------------------------------------------------------------


def test_every_string_the_script_asks_for_is_one_the_server_sends() -> None:
    """The script reads them off `<html>`, so the two sides have to agree.

    `t("ZoomIn")` reads `dataset.ffTZoomIn`, which is the attribute
    `data-ff-t-zoom-in` -- so the server's key has to be `zoom-in` and not
    `zoomin`. Get that wrong and the control keeps its English fallback in every
    language, forever, without anything failing: the string is in all nine
    catalogues, the test that checks they are complete passes, and the label on
    screen is still in English.
    """
    from fastfort.admin.site import _ui_text

    script = (
        Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static" / "js" / "fastfort.js"
    ).read_text(encoding="utf-8")

    block = re.search(r"const FALLBACK_TEXT = \{(.*?)\n  \};", script, re.S)
    assert block, "the script should declare its fallbacks in one place"

    asked = re.findall(r"^\s{4}(\w+):", block.group(1), re.M)
    assert asked, "no fallback keys found -- has the block moved?"

    # dataset `ffTNoResults` <- attribute `data-ff-t-no-results`.
    def attribute(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()

    sent = set(_ui_text(Translator()))
    missing = sorted(attribute(name) for name in asked if attribute(name) not in sent)

    assert not missing, f"the script asks for strings the server never sends: {missing}"


def test_the_on_demand_bundles_only_ask_for_strings_that_exist() -> None:
    """`fastfort-geo.js` and `fastfort-data.js` call the same `t()`.

    They reach it through the kit `fastfort.js` publishes, so it reads the same
    `FALLBACK_TEXT` and the same attributes on `<html>` -- one table of strings
    for every bundle. That also means the check above, which only reads the main
    script, cannot see a `t("Format")` added to one of these: the key would be
    missing from both sides and the control would show the literal key. Hence
    this, which starts from the calls rather than from the table.
    """
    from fastfort.admin.site import _ui_text

    js = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static" / "js"
    main = (js / "fastfort.js").read_text(encoding="utf-8")
    block = re.search(r"const FALLBACK_TEXT = \{(.*?)\n  \};", main, re.S)
    assert block, "the script should declare its fallbacks in one place"
    declared = set(re.findall(r"^\s{4}(\w+):", block.group(1), re.M))

    def attribute(name: str) -> str:
        return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()

    sent = set(_ui_text(Translator()))
    problems: list[str] = []

    for bundle in sorted(js.glob("fastfort-*.js")):
        for key in sorted(set(re.findall(r'\bt\("(\w+)"\)', bundle.read_text(encoding="utf-8")))):
            if key not in declared:
                problems.append(f"{bundle.name}: t({key!r}) has no fallback")
            elif attribute(key) not in sent:
                problems.append(f"{bundle.name}: t({key!r}) is never sent as {attribute(key)!r}")

    assert not problems, problems
