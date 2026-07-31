"""Translation for the admin interface.

Source strings are English and are used as their own keys. A missing translation
therefore renders the English rather than a bare identifier like `admin.save`,
which means a half-translated catalogue is usable and a typo in a key shows up as
English text instead of as nothing at all.

Catalogues are small JSON files loaded once per process. There is no gettext
toolchain, no `.mo` compilation and no build step: adding a language is adding
one file.
"""

from __future__ import annotations

from .catalog import (
    DEFAULT_LANGUAGE,
    LANGUAGES,
    Translator,
    available_languages,
    clear_catalog_cache,
    negotiate_language,
)

__all__ = [
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "Translator",
    "available_languages",
    "clear_catalog_cache",
    "negotiate_language",
]
