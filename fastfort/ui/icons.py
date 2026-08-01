"""The admin's icon set.

Drawn here rather than pulled from an icon font or a CDN. A font would be a
second network request, a third-party dependency to keep audited, and a flash of
missing glyphs before it loads; the text glyphs this replaces (`☰`, `◐`, `⌕`)
are worse still, because every platform draws them differently and some do not
draw them at all.

The paths live in Python rather than in a template so that one dictionary is both
what gets rendered and what `ModelAdmin.icon` is validated against -- a name that
is not here fails at declaration time instead of rendering an empty box.

Every path is drawn on a 24x24 grid with no fill and a 2px round stroke, so an
icon inherits its colour from `currentColor` and stays legible at 16px.
"""

from __future__ import annotations

__all__ = ["ICONS", "icon_names", "is_icon"]

#: name -> the inner markup of one 24x24 symbol.
#:
#: Kept alphabetical. Additions are cheap; the cost is per page, and only for the
#: sprite, which is emitted once.
ICONS: dict[str, str] = {
    # -- navigation ---------------------------------------------------------
    "menu": '<path d="M4 6h16M4 12h16M4 18h16"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-up": '<path d="m18 15-6-6-6 6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "chevron-left": '<path d="m15 18-6-6 6 6"/>',
    "expand": '<path d="m8 9 4-4 4 4M8 15l4 4 4-4"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v8H3V6h8"/>',
    # -- actions ------------------------------------------------------------
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "filter": '<path d="M3 5h18l-7 8v6l-4 2v-8Z"/>',
    "check": '<path d="m4 12 5 5L20 6"/>',
    "close": '<path d="M6 6 18 18M18 6 6 18"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    "save": '<path d="M5 3h11l3 3v15H5Z"/><path d="M8 3v6h7V3M8 21v-7h8v7"/>',
    "logout": (
        '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>'
    ),
    # -- state --------------------------------------------------------------
    "sun": (
        '<circle cx="12" cy="12" r="4"/>'
        '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4'
        'M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'
    ),
    "moon": '<path d="M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10Z"/>',
    "monitor": '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "globe": (
        '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18"/>'
    ),
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "warning": '<path d="M12 3 2 20h20Z"/><path d="M12 10v4M12 17h.01"/>',
    "shield": '<path d="M12 3l8 3v6c0 5-3.4 8.2-8 9-4.6-.8-8-4-8-9V6Z"/>',
    # -- objects, for ModelAdmin.icon --------------------------------------
    "dashboard": '<path d="M3 3h8v8H3ZM13 3h8v5h-8ZM13 10h8v11h-8ZM3 13h8v8H3Z"/>',
    "users": (
        '<circle cx="9" cy="8" r="4"/><path d="M2 21a7 7 0 0 1 14 0"/>'
        '<path d="M17 4.5a4 4 0 0 1 0 7M18 14.5a7 7 0 0 1 4 6.5"/>'
    ),
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
    "key": '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9 2 2-2 2 2 2-3 3-2-2-2 2"/>',
    "tag": '<path d="M3 12V4h8l10 10-8 8Z"/><path d="M7.5 7.5h.01"/>',
    "folder": (
        '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>'
    ),
    "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5Z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
    "cart": (
        '<circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>'
        '<path d="M2 3h3l2.5 12h11L21 7H6"/>'
    ),
    "file": (
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/>'
        '<path d="M14 3v5h5"/>'
    ),
    "list": '<path d="M9 6h12M9 12h12M9 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    "calendar": (
        '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>'
    ),
    "chart": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "settings": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M12 2v3M12 19v3M2 12h3M19 12h3'
        'M4.9 4.9 7 7M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"/>'
    ),
    "database": (
        '<ellipse cx="12" cy="6" rx="8" ry="3"/>'
        '<path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>'
        '<path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>'
    ),
    "mail": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="m2 7 10 6 10-6"/>',
    "bell": (
        '<path d="M18 9a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7"/><path d="M10.5 20a2 2 0 0 0 3 0"/>'
    ),
    "star": '<path d="m12 3 2.7 5.7 6.3.9-4.5 4.4 1 6.3-5.5-3-5.5 3 1-6.3L3 9.6l6.3-.9Z"/>',
}


def icon_names() -> tuple[str, ...]:
    """Every icon that can be referenced, sorted."""
    return tuple(sorted(ICONS))


def is_icon(name: str | None) -> bool:
    """Whether `name` can be drawn. Used to validate a declared `icon`."""
    return bool(name) and name in ICONS
