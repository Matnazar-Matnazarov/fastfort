"""User interface: Jinja2 templates, the CSS design system and JavaScript.

The stylesheet is hand-written and ships pre-built, so neither a user nor a
contributor needs Node.js. Everything configurable is applied at render time as
custom properties on the root element -- see `theming.Theme`.
"""

from __future__ import annotations

from .theming import Theme

__all__ = ["Theme"]
