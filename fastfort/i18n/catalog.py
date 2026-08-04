"""Loading catalogues and choosing a language.

The translator is a plain callable so templates can use it as `_("Save")`, which
keeps the source string visible at the call site. Reading a template should not
require looking a key up in another file to find out what the page says.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "LANGUAGE_ENGLISH_NAMES",
    "LANGUAGE_FLAGS",
    "RTL_LANGUAGES",
    "LanguageChoice",
    "Translator",
    "available_languages",
    "clear_catalog_cache",
    "is_rtl",
    "negotiate_language",
]

CATALOG_DIR = Path(__file__).resolve().parent / "locale"

#: The source language. Its "catalogue" is the source strings themselves.
DEFAULT_LANGUAGE = "en"

#: Display names, in the language itself -- a Russian speaker looking for their
#: language scans for "Русский", not for "Russian".
#:
#: Shown alongside `LANGUAGE_ENGLISH_NAMES` rather than instead of it. Either one
#: alone fails somebody: a list of endonyms is unreadable to anyone who does not
#: know the script ("中文" says nothing if you cannot read it), and a list of
#: English names makes a speaker hunt for the word English happens to use for
#: their language. Both, and the switcher filters on either.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "uz": "O'zbekcha",
    "ru": "Русский",
    "tr": "Türkçe",
    "de": "Deutsch",
    "fr": "Français",
    "es": "Español",
    "zh": "中文",
    "ja": "日本語",
    "ko": "한국어",
    "ar": "العربية",
}

#: The same languages named in English, for anyone who cannot read the script a
#: language is written in -- which is most people, for most scripts.
LANGUAGE_ENGLISH_NAMES: dict[str, str] = {
    "en": "English",
    "uz": "Uzbek",
    "ru": "Russian",
    "tr": "Turkish",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
}

#: Languages written right to left. The page's `dir` follows this, which is what
#: turns the whole admin around: the stylesheet is written with logical
#: properties throughout -- `inset-inline-end`, `padding-inline`, `margin-inline`
#: -- so the sidebar, the table alignment, the chevrons and every popover flip
#: without a second stylesheet or a single mirrored rule.
RTL_LANGUAGES: frozenset[str] = frozenset({"ar"})

#: Regional-indicator pairs, shown beside the name in the switcher. Decoration
#: only: several platforms draw them as two letters instead of a flag, and a flag
#: is a country rather than a language in any case. The name and the code carry
#: the meaning, so a switcher stays readable wherever these fail to render.
LANGUAGE_FLAGS: dict[str, str] = {
    "en": "\U0001f1ec\U0001f1e7",
    "uz": "\U0001f1fa\U0001f1ff",
    "ru": "\U0001f1f7\U0001f1fa",
    "tr": "\U0001f1f9\U0001f1f7",
    "de": "\U0001f1e9\U0001f1ea",
    "fr": "\U0001f1eb\U0001f1f7",
    "es": "\U0001f1ea\U0001f1f8",
    "zh": "\U0001f1e8\U0001f1f3",
    "ja": "\U0001f1ef\U0001f1f5",
    "ko": "\U0001f1f0\U0001f1f7",
    # No country owns Arabic. The League of Arab States' flag is the closest
    # thing to a neutral mark for it, and unlike a regional-indicator pair it is
    # a single codepoint that either renders or does not.
    "ar": "\U0001f1f8\U0001f1e6",
}


@dataclass(frozen=True, slots=True)
class LanguageChoice:
    """One row in a language switcher."""

    code: str
    name: str
    flag: str
    #: The same language named in English. Equal to `name` for English itself,
    #: and the template drops the duplicate rather than printing it twice.
    english: str = ""

    @property
    def short(self) -> str:
        """The code as a badge, e.g. ``EN``."""
        return self.code.upper()


def _read(path: Path) -> dict[str, str]:
    """Read one catalogue file, or nothing.

    A missing or malformed file is not an error: every string falls back to its
    English source, so the admin stays usable rather than failing to render.
    """
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items() if value}


@lru_cache(maxsize=64)
def _catalog(language: str, project_dir: str | None = None) -> dict[str, str]:
    """The strings for one language, with a project's own taking precedence.

    Model and field names come from the project, not from here, so a project has
    to be able to translate them -- otherwise the chrome appears in Uzbek while
    every column header stays in English, which reads as broken rather than as
    untranslated.
    """
    merged = _read(CATALOG_DIR / f"{language}.json")
    if project_dir:
        merged |= _read(Path(project_dir) / f"{language}.json")
    return merged


def available_languages() -> tuple[str, ...]:
    """Languages with a catalogue on disk, plus the source language."""
    found = [DEFAULT_LANGUAGE]
    found.extend(code for code in LANGUAGES if code != DEFAULT_LANGUAGE and _catalog(code))
    return tuple(found)


def clear_catalog_cache() -> None:
    """Forget loaded catalogues. Used by tests and by a development reload."""
    _catalog.cache_clear()


def is_rtl(language: str) -> bool:
    """Whether a language is written right to left.

    Read off the base code, so a regional variant is not a language the admin
    suddenly lays out the wrong way round.
    """
    return language.split("-", 1)[0].lower() in RTL_LANGUAGES


def negotiate_language(
    *, chosen: str | None = None, configured: str | None = None, accept: str = ""
) -> str:
    """Pick a language: an explicit choice, then configuration, then the browser.

    A person who picked a language always wins. A project that pinned one wins
    over the browser, because an admin configured in Uzbek should not appear in
    English merely because a laptop was bought abroad. `configured=None` means
    the project did not pin anything, so the browser decides.
    """
    supported = available_languages()

    if chosen and chosen in supported:
        return chosen
    if configured and configured in supported:
        return configured

    for part in accept.split(","):
        # "uz-UZ;q=0.9" -> "uz"
        code = part.split(";")[0].strip().lower().split("-")[0]
        if code in supported:
            return code

    return DEFAULT_LANGUAGE


@dataclass(frozen=True, slots=True)
class Translator:
    """Translates source strings into one language."""

    language: str = DEFAULT_LANGUAGE
    #: A project's own catalogue directory, searched before ours.
    project_dir: str | None = None

    def __call__(self, text: str, /, **placeholders: Any) -> str:
        """Translate `text`, filling any `{named}` placeholders.

        A placeholder that a translation forgot, or invented, must not take the
        page down -- the untranslated source is rendered instead.
        """
        if not text:
            return text
        translated = _catalog(self.language, self.project_dir).get(text, text)
        if not placeholders:
            return translated
        try:
            return translated.format(**placeholders)
        except (KeyError, IndexError, ValueError):
            try:
                return text.format(**placeholders)
            except (KeyError, IndexError, ValueError):
                return text

    @property
    def label(self) -> str:
        return LANGUAGES.get(self.language, self.language)

    @property
    def is_default(self) -> bool:
        return self.language == DEFAULT_LANGUAGE

    @property
    def choice(self) -> LanguageChoice:
        """This translator's own language, for a switcher's summary."""
        return LanguageChoice(
            self.language,
            self.label,
            LANGUAGE_FLAGS.get(self.language, ""),
            LANGUAGE_ENGLISH_NAMES.get(self.language, ""),
        )

    def choices(self) -> tuple[LanguageChoice, ...]:
        """Every offered language, for a switcher."""
        return tuple(
            LanguageChoice(
                code,
                LANGUAGES.get(code, code),
                LANGUAGE_FLAGS.get(code, ""),
                LANGUAGE_ENGLISH_NAMES.get(code, ""),
            )
            for code in available_languages()
        )
