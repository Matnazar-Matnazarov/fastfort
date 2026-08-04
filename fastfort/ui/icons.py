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
    "chevron-left": '<path d="m15 18-6-6 6-6"/>',
    "chevrons-left": '<path d="m11 17-5-5 5-5M18 17l-5-5 5-5"/>',
    "chevrons-right": '<path d="m13 17 5-5-5-5M6 17l5-5-5-5"/>',
    "chevrons-up-down": '<path d="m7 15 5 5 5-5M7 9l5-5 5 5"/>',
    "arrow-left": '<path d="M19 12H5M12 19l-7-7 7-7"/>',
    "arrow-right": '<path d="M5 12h14M12 5l7 7-7 7"/>',
    "arrow-up": '<path d="M12 19V5M5 12l7-7 7 7"/>',
    "arrow-down": '<path d="M12 5v14M19 12l-7 7-7-7"/>',
    "expand": '<path d="m8 9 4-4 4 4M8 15l4 4 4-4"/>',
    "external": '<path d="M15 3h6v6M10 14 21 3M18 13v8H3V6h8"/>',
    "sidebar": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    "command": (
        '<path d="M9 6a3 3 0 1 0-3 3h12a3 3 0 1 0-3-3v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3Z"/>'
    ),
    # -- actions ------------------------------------------------------------
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "minus": '<path d="M5 12h14"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/>',
    "filter": '<path d="M3 5h18l-7 8v6l-4 2v-8Z"/>',
    "filter-off": '<path d="M3 5h18l-6 7M10 10l-7-5M9 13v6l4-2v-2M3 3l18 18"/>',
    "sliders": (
        '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h9M17 18h3"/>'
        '<circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/>'
        '<circle cx="15" cy="18" r="2"/>'
    ),
    "sort": '<path d="M7 4v16M7 20l-3-3M7 20l3-3M17 20V4M17 4l-3 3M17 4l3 3"/>',
    "sort-asc": '<path d="M6 16V4M6 4 3 7M6 4l3 3M12 6h9M12 12h6M12 18h3"/>',
    "sort-desc": '<path d="M6 4v12M6 16l-3-3M6 16l3-3M12 6h9M12 12h6M12 18h3"/>',
    "check": '<path d="m4 12 5 5L20 6"/>',
    "close": '<path d="M6 6 18 18M18 6 6 18"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5"/>',
    "edit": '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>',
    "save": '<path d="M5 3h11l3 3v15H5Z"/><path d="M8 3v6h7V3M8 21v-7h8v7"/>',
    "eye": (
        '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/>'
        '<circle cx="12" cy="12" r="3"/>'
    ),
    "eye-off": (
        '<path d="M10.6 6.2A9.9 9.9 0 0 1 12 6c6.4 0 10 6 10 6a17 17 0 0 1-3.2 4"/>'
        '<path d="M6.3 7.7A17 17 0 0 0 2 12s3.6 6 10 6a9.6 9.6 0 0 0 4.3-1"/>'
        '<path d="M10 10a3 3 0 0 0 4 4M3 3l18 18"/>'
    ),
    "copy": (
        '<rect x="9" y="9" width="12" height="12" rx="2"/>'
        '<path d="M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"/>'
    ),
    "link": (
        '<path d="M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1"/>'
        '<path d="M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1"/>'
    ),
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7M21 4v5h-5"/>',
    "download": '<path d="M12 3v12M7 11l5 5 5-5M4 21h16"/>',
    "upload": '<path d="M12 16V4M7 8l5-5 5 5M4 21h16"/>',
    "logout": (
        '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>'
    ),
    "more-horizontal": (
        '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/>'
        '<circle cx="19" cy="12" r="1.4"/>'
    ),
    "more-vertical": (
        '<circle cx="12" cy="5" r="1.4"/><circle cx="12" cy="12" r="1.4"/>'
        '<circle cx="12" cy="19" r="1.4"/>'
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
    "check-circle": '<circle cx="12" cy="12" r="9"/><path d="m8 12 3 3 5-6"/>',
    "close-circle": '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3 2.5v1M12 17h.01"/>',
    "lock": '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "loader": (
        '<path d="M12 3v4M12 17v4M3 12h4M17 12h4"/>'
        '<path d="M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8"/>'
    ),
    "inbox": (
        '<path d="M3 13h5l1.5 3h5L16 13h5"/>'
        '<path d="M5.5 5h13l2.5 8v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-6Z"/>'
    ),
    "trending-up": '<path d="M3 17 10 10l4 4 7-7"/><path d="M15 7h6v6"/>',
    "trending-down": '<path d="M3 7 10 14l4-4 7 7"/><path d="M15 17h6v-6"/>',
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
    #: A toothed gear, not a circle with eight spokes. The spoked version was
    #: indistinguishable from `sun` at 18px, which put two apparently identical
    #: icons next to each other in the topbar.
    "settings": (
        '<circle cx="12" cy="12" r="3.2"/>'
        '<path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1'
        "a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1"
        "A1.6 1.6 0 0 0 8 19.4a1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1"
        "a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1"
        "A1.6 1.6 0 0 0 4.6 8a1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1"
        "a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1"
        "a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1"
        "a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1"
        'a1.6 1.6 0 0 0-1.5 1Z"/>'
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
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    "image": (
        '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9.5" r="1.5"/>'
        '<path d="m4 17 5-5 4 4 3-2 4 4"/>'
    ),
    "map-pin": (
        '<path d="M12 21s7-6 7-11a7 7 0 1 0-14 0c0 5 7 11 7 11Z"/><circle cx="12" cy="10" r="2.5"/>'
    ),
    # The "where am I" control on a map. A ring with a dot and four ticks, which
    # is what every mapping application draws for it.
    "crosshair": (
        '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="1.5"/>'
        '<path d="M12 2v3M12 19v3M2 12h3M19 12h3"/>'
    ),
    "phone": (
        '<path d="M6 3h3l2 5-2.5 1.5a12 12 0 0 0 5 5L15 12l5 2v3'
        'a2 2 0 0 1-2.2 2A17 17 0 0 1 4 5.2 2 2 0 0 1 6 3Z"/>'
    ),
    "credit-card": '<rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20M6 15h4"/>',
    "truck": (
        '<path d="M3 6h11v10H3ZM14 9h4l3 3v4h-7"/>'
        '<circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/>'
    ),
    "layers": '<path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 13 9 5 9-5M3 17l9 5 9-5"/>',
    "grid": (
        '<rect x="3" y="3" width="7" height="7" rx="1"/>'
        '<rect x="14" y="3" width="7" height="7" rx="1"/>'
        '<rect x="3" y="14" width="7" height="7" rx="1"/>'
        '<rect x="14" y="14" width="7" height="7" rx="1"/>'
    ),
    "book": (
        '<path d="M4 4.5A2.5 2.5 0 0 1 6.5 2H20v18H6.5A2.5 2.5 0 0 0 4 22Z"/>'
        '<path d="M4 17.5A2.5 2.5 0 0 1 6.5 15H20"/>'
    ),
    "percent": (
        '<path d="M19 5 5 19"/><circle cx="7.5" cy="7.5" r="2.5"/>'
        '<circle cx="16.5" cy="16.5" r="2.5"/>'
    ),
    "wallet": (
        '<path d="M3 7a2 2 0 0 1 2-2h12v3"/>'
        '<path d="M3 7v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7a2 2 0 0 0-2-2H5"/>'
        '<path d="M17 12h.01"/>'
    ),
}


def icon_names() -> tuple[str, ...]:
    """Every icon that can be referenced, sorted."""
    return tuple(sorted(ICONS))


def is_icon(name: str | None) -> bool:
    """Whether `name` can be drawn. Used to validate a declared `icon`."""
    return bool(name) and name in ICONS
