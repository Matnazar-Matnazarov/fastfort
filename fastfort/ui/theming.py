"""Turning `UISettings` into the attributes and custom properties a page needs.

The stylesheet is static and ships pre-built. Everything configurable is applied
at render time as `data-` attributes and inline custom properties on the root
element, which is why rebranding needs no build step and no forked stylesheet.

Values are validated here rather than trusted, because they end up inside a
`style` attribute: a hue that could carry arbitrary text would be a CSS injection
straight into every admin page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastfort.core.settings import UISettings

__all__ = ["Theme"]

#: Only these characters can reach a `style` attribute from configuration. Hue and
#: chroma are numbers, so anything else is a bug or an attack.
_NUMERIC = "0123456789.-"


def _number(value: float, *, low: float, high: float) -> str:
    """Render a bounded number, refusing anything that is not one.

    Clamping instead of raising: a hue of 400 is a typo, not a reason to take the
    admin down, and 400 mod 360 is very likely what was meant.
    """
    clamped = min(max(float(value), low), high)
    text = f"{clamped:g}"
    if any(character not in _NUMERIC for character in text):  # pragma: no cover
        raise ValueError(f"refusing to emit {text!r} as a CSS value")
    return text


@dataclass(frozen=True, slots=True)
class Theme:
    """The render-time appearance of one admin page."""

    hue: str
    chroma: str
    theme: str
    density: str
    logo_url: str | None
    favicon_url: str | None
    richtext_url: str | None
    custom_css_url: str | None
    environment_label: str | None
    environment_tone: str

    @classmethod
    def from_settings(cls, settings: UISettings) -> Theme:
        return cls(
            hue=_number(settings.accent_hue, low=0, high=360),
            chroma=_number(settings.accent_chroma, low=0, high=0.37),
            theme=settings.theme,
            density=settings.density,
            logo_url=settings.logo_url,
            favicon_url=settings.favicon_url,
            richtext_url=settings.richtext_url,
            custom_css_url=settings.custom_css_url,
            environment_label=settings.environment_label,
            environment_tone=settings.environment_tone,
        )

    def root_attributes(self) -> dict[str, str]:
        """Attributes for the `<html>` element.

        ``theme`` is omitted when it is ``system``, so the media query in the
        stylesheet decides. Writing ``data-ff-theme="system"`` would instead match
        neither branch and pin the page to light.
        """
        attributes = {"data-ff-density": self.density, "style": self.style()}
        if self.theme != "system":
            attributes["data-ff-theme"] = self.theme
        return attributes

    def style(self) -> str:
        """The inline custom properties that carry the brand."""
        return f"--ff-h:{self.hue};--ff-c:{self.chroma}"

    def stylesheets(self, static_url: str) -> tuple[str, ...]:
        """Stylesheet URLs in load order.

        A project's own sheet comes last so it can override tokens without
        fighting specificity.
        """
        sheets = [f"{static_url}/fastfort.css"]
        if self.custom_css_url:
            sheets.append(self.custom_css_url)
        return tuple(sheets)
