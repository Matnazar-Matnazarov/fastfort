"""The Jinja2 environment the admin renders through.

Two settings here are security controls rather than preferences.

`autoescape` is on for every template. An admin displays data the site's own
users typed, so a product name containing `<script>` is not a hypothetical.

`undefined=StrictUndefined` makes a misspelled variable raise instead of
rendering an empty string. A permission check that silently evaluates to nothing
is the failure mode that matters: `{% if can_delete %}` on a typo would hide the
button, but `{% if not can_delet %}` would show it.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    StrictUndefined,
    select_autoescape,
)
from markupsafe import Markup

from fastfort.i18n import Translator
from fastfort.ui.icons import ICONS

if TYPE_CHECKING:
    from pathlib import Path

    from fastfort.core.settings import FastFortSettings

__all__ = ["Renderer"]

#: How a missing value is shown. An empty cell is ambiguous -- it could equally be
#: an empty string, so NULL gets a visible mark.
EMPTY = "—"


class Renderer:
    """Renders admin templates, with a project's overrides taking precedence."""

    def __init__(
        self,
        settings: FastFortSettings,
        *,
        template_dirs: tuple[Path | str, ...] = (),
    ) -> None:
        self.settings = settings
        self.environment = Environment(
            # A project's directory is searched first, so overriding one template
            # does not mean vendoring all of them.
            loader=ChoiceLoader(
                [
                    *(FileSystemLoader(str(directory)) for directory in template_dirs),
                    PackageLoader("fastfort.ui", "templates"),
                ]
            ),
            autoescape=select_autoescape(default_for_string=True, default=True),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=settings.debug,
        )
        self.environment.filters["ff_display"] = display_value
        self.environment.filters["ff_bool"] = boolean_mark
        self.environment.filters["step"] = decimal_step
        self.environment.globals["EMPTY"] = EMPTY
        self.environment.globals["ff_icon"] = icon
        self.environment.globals["ff_sprite"] = sprite

    def render(self, template: str, /, **context: Any) -> str:
        # A translator is always present, so a template never has to guard for it.
        context.setdefault("_", Translator())
        return self.environment.get_template(template).render(**context)


def display_value(value: Any) -> Any:
    """Format a cell value for a list or detail view.

    Returns plain text, never markup: whatever comes back is escaped by the
    template. Rendering HTML from data is an opt-in that goes through an explicit
    helper, not through this filter.
    """
    if value is None or value == "":
        return EMPTY
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, Decimal):
        return f"{value:,}"
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, int | float):
        return f"{value:,}"
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value) or EMPTY
    return str(value)


def boolean_mark(value: Any) -> Markup:
    """A tick or a dash, which reads faster in a table than the word "True".

    The only place the admin emits markup from a value, and it emits a fixed
    string chosen by a boolean -- the value itself never reaches the output.
    """
    if value is None:
        return Markup('<span class="ff-bool ff-bool--off" title="Not set">&mdash;</span>')
    if value:
        return Markup('<span class="ff-bool ff-bool--on" title="Yes">&check;</span>')
    return Markup('<span class="ff-bool ff-bool--off" title="No">&times;</span>')


def icon(name: str | None, *, size: int = 16) -> Markup:
    """One icon, referencing a symbol in the sprite.

    An unknown name renders nothing rather than raising: an icon is decoration,
    and a mistyped `ModelAdmin.icon` should not be able to take a page down. The
    name is checked against the registry before it reaches the output, so it can
    never carry markup of its own.
    """
    if not name or name not in ICONS:
        return Markup("")
    # Trusted markup: the only two values interpolated are a key that was just
    # checked against the registry and an int. Nothing from a request, and
    # nothing from the database, can reach this string.
    return Markup(  # noqa: S704
        f'<svg class="ff-icon" width="{int(size)}" height="{int(size)}" '
        f'aria-hidden="true" focusable="false"><use href="#ff-i-{name}"/></svg>'
    )


def sprite() -> Markup:
    """Every symbol, emitted once per page.

    Inline rather than a separate file: `<use>` pointing at an external document
    costs a request that blocks the first paint of every icon, and browsers have
    a long history of getting it wrong. Inline, it is about 1 KB compressed and
    it cannot fail.
    """
    symbols = "".join(
        f'<symbol id="ff-i-{name}" viewBox="0 0 24 24">{markup}</symbol>'
        for name, markup in ICONS.items()
    )
    # Trusted markup: every part of this comes from the icon registry, which is a
    # constant in this package. No request data passes through it.
    return Markup(  # noqa: S704
        '<svg class="ff-sprite" aria-hidden="true" fill="none" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{symbols}</svg>"
    )


def decimal_step(places: int | None) -> str:
    """The `step` attribute for a numeric input.

    Without it a browser rejects "19.50" on a field it assumes is an integer,
    which reads as the form being broken rather than as a validation rule.
    """
    if not places or places <= 0:
        return "1"
    return f"0.{'0' * (places - 1)}1"
