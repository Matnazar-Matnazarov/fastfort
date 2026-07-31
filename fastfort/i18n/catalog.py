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
    "Translator",
    "available_languages",
    "clear_catalog_cache",
    "negotiate_language",
]

CATALOG_DIR = Path(__file__).resolve().parent / "locale"

#: The source language. Its "catalogue" is the source strings themselves.
DEFAULT_LANGUAGE = "en"

#: Display names, in the language itself -- a Russian speaker looking for their
#: language scans for "Русский", not for "Russian".
LANGUAGES: dict[str, str] = {
    "en": "English",
    "uz": "O'zbekcha",
    "ru": "Русский",
}


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

    def choices(self) -> tuple[tuple[str, str], ...]:
        """Every offered language as (code, native name), for a switcher."""
        return tuple((code, LANGUAGES.get(code, code)) for code in available_languages())
