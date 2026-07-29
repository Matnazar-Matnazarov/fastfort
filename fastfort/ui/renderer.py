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
        self.environment.globals["EMPTY"] = EMPTY

    def render(self, template: str, /, **context: Any) -> str:
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
