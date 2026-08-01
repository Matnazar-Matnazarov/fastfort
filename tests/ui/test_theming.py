"""Tests for the design-token stylesheets and render-time theming.

The stylesheet ships pre-built, so these guard the two things that can silently
break it: a token referenced but never declared, and a configuration value
reaching a `style` attribute unvalidated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fastfort.core.settings import UISettings
from fastfort.ui.theming import Theme

CSS_DIR = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static" / "css"

#: Every stylesheet, in the order a page loads them.
SHEETS = (
    "00-reset.css",
    "01-tokens.css",
    "02-base.css",
    "03-layout.css",
    "04-components.css",
    "05-admin.css",
)


@pytest.fixture(scope="module")
def css() -> str:
    return "\n".join((CSS_DIR / name).read_text(encoding="utf-8") for name in SHEETS)


# ---------------------------------------------------------------------------
# Stylesheets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SHEETS)
def test_every_stylesheet_ships(name: str) -> None:
    assert (CSS_DIR / name).is_file()


def test_braces_are_balanced(css: str) -> None:
    """A missing brace silently disables everything after it."""
    assert css.count("{") == css.count("}")


def test_every_declaration_ends_with_a_semicolon(css: str) -> None:
    """A dropped semicolon swallows the next declaration without any error.

    Multi-line declarations are common -- `transition` and `grid-template-areas`
    both read better broken up -- so the check follows a declaration to the line
    that actually terminates it rather than judging each line alone.
    """
    offenders: list[str] = []
    continuing = False

    for line in css.splitlines():
        stripped = line.strip()

        if continuing:
            # A brace means the previous line was a selector after all, not an
            # unterminated declaration.
            if stripped.endswith((";", "{", "}")):
                continuing = False
            continue

        if (
            not stripped
            or stripped.startswith(("/*", "*", "@", "}", "{"))
            # A trailing comma is a selector list or a multi-value declaration;
            # either way the line that terminates it is the one to judge.
            or stripped.endswith(("{", "}", ";", ",", "*/"))
        ):
            continue

        if ":" in stripped:
            continuing = True
            continue

        offenders.append(stripped)

    assert offenders == [], offenders


def test_every_referenced_token_is_declared(css: str) -> None:
    """`var(--ff-typo)` resolves to nothing and fails silently at runtime."""
    declared = set(re.findall(r"^\s*(--ff-[a-z0-9-]+)\s*:", css, flags=re.MULTILINE))
    used = set(re.findall(r"var\((--ff-[a-z0-9-]+)", css))
    assert used - declared == set()


def test_no_literal_colour_outside_the_token_file() -> None:
    """A hard-coded colour is a value a hue change cannot reach.

    The print block is the one exception: paper has no theme.
    """
    base = (CSS_DIR / "02-base.css").read_text(encoding="utf-8")
    before_print = base.split("@media print")[0]
    literals = re.findall(r"(#[0-9a-fA-F]{3,8}\b|\b(?:rgb|hsl|oklch)\()", before_print)
    assert literals == []


def test_the_palette_derives_from_a_single_hue() -> None:
    """Rebranding has to be one number, or it is a rebuild in disguise."""
    tokens = (CSS_DIR / "01-tokens.css").read_text(encoding="utf-8")
    # Matched to end of line rather than to the first ")", because the ramp
    # nests calc() inside oklch().
    accent_declarations = re.findall(r"--ff-accent[a-z-]*:\s*oklch\(.*", tokens)
    assert accent_declarations
    for declaration in accent_declarations:
        assert "var(--ff-h)" in declaration or "0 0" in declaration, declaration


def test_dark_mode_redefines_the_same_token_names() -> None:
    """Dark mode is the same tokens re-valued, not a second palette.

    If a token exists only in the light block, every component using it is
    unstyled in dark mode.
    """
    tokens = (CSS_DIR / "01-tokens.css").read_text(encoding="utf-8")
    light_block = tokens.split("@media (prefers-color-scheme: dark)")[0]
    dark_block = tokens.split(':root[data-ff-theme="dark"]')[1]

    def names(block: str) -> set[str]:
        return set(re.findall(r"^\s*(--ff-[a-z0-9-]+)\s*:", block, flags=re.MULTILINE))

    assert names(dark_block) - names(light_block) == set()


def test_an_explicit_theme_choice_beats_the_system_preference() -> None:
    """The media query must not win over a viewer who picked a theme."""
    tokens = (CSS_DIR / "01-tokens.css").read_text(encoding="utf-8")
    assert ':root:not([data-ff-theme="light"])' in tokens
    assert ':root[data-ff-theme="dark"]' in tokens


def test_focus_is_never_removed_without_a_replacement(css: str) -> None:
    """Keyboard users navigate the admin constantly."""
    assert ":focus-visible {" in css
    assert "outline: var(--ff-focus-width)" in css


def test_motion_is_disabled_when_the_viewer_asks(css: str) -> None:
    assert "prefers-reduced-motion" in css


def test_the_stylesheets_stay_within_budget(css: str) -> None:
    """The whole front end is budgeted at 60 KB gzipped, CSS and JavaScript.

    A complete design system for an admin -- shell, table, forms, every drawn
    control, both themes -- fits in a fraction of that, and this fails if it
    stops doing so.
    """
    import gzip

    compressed = len(gzip.compress(css.encode("utf-8")))
    assert compressed < 16_000, f"{compressed} bytes gzipped"


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------


def test_brand_settings_become_custom_properties() -> None:
    theme = Theme.from_settings(UISettings(accent_hue=145, accent_chroma=0.12))
    assert theme.style() == "--ff-h:145;--ff-c:0.12"


def test_out_of_range_values_are_clamped_not_fatal() -> None:
    """A hue of 400 is a typo, not a reason to take the admin down."""
    assert Theme.from_settings(UISettings(accent_hue=360)).hue == "360"
    assert Theme.from_settings(UISettings(accent_chroma=0.37)).chroma == "0.37"


def test_the_style_attribute_can_only_contain_numbers() -> None:
    """It is interpolated into a `style` attribute, so it is an injection point."""
    style = Theme.from_settings(UISettings(accent_hue=255, accent_chroma=0.16)).style()
    assert re.fullmatch(r"--ff-h:[\d.]+;--ff-c:[\d.]+", style)


def test_system_theme_leaves_the_attribute_off() -> None:
    """`data-ff-theme="system"` would match neither branch and pin the page light."""
    attributes = Theme.from_settings(UISettings(theme="system")).root_attributes()
    assert "data-ff-theme" not in attributes


@pytest.mark.parametrize("choice", ["light", "dark"])
def test_an_explicit_theme_is_written_to_the_root(choice: str) -> None:
    attributes = Theme.from_settings(UISettings(theme=choice)).root_attributes()  # type: ignore[arg-type]
    assert attributes["data-ff-theme"] == choice


def test_density_is_written_to_the_root() -> None:
    attributes = Theme.from_settings(UISettings(density="compact")).root_attributes()
    assert attributes["data-ff-density"] == "compact"


def test_a_project_stylesheet_loads_last() -> None:
    """So it can override tokens without fighting specificity."""
    theme = Theme.from_settings(UISettings(custom_css_url="/static/mine.css"))
    sheets = theme.stylesheets("/admin/static")
    assert sheets[-1] == "/static/mine.css"
    assert len(sheets) == 2


def test_without_a_custom_sheet_only_ours_loads() -> None:
    assert Theme.from_settings(UISettings()).stylesheets("/admin/static") == (
        "/admin/static/fastfort.css",
    )


# ---------------------------------------------------------------------------
# Controls are drawn, not left to the browser
# ---------------------------------------------------------------------------


def test_no_control_is_left_native(css: str) -> None:
    """A native select beside a styled input is the clearest tell that an
    interface was not finished, and it changes shape between platforms."""
    assert ".ff-select {" in css
    assert "appearance: none" in css
    assert '.ff-checkbox input[type="checkbox"]' in css
    assert ".ff-switch__track" in css


def test_drawn_controls_carry_their_own_glyphs(css: str) -> None:
    """The chevron and the tick are inline SVG data URIs, which the content
    security policy allows under `img-src data:`."""
    assert css.count("data:image/svg+xml") >= 2


def test_a_data_uri_glyph_is_swapped_for_dark_mode(css: str) -> None:
    """A data URI cannot read a custom property, so a white tick would stay white
    on the near-white box dark mode gives the checkbox."""
    assert ':root[data-ff-theme="dark"] .ff-checkbox input[type="checkbox"]:checked' in css


def test_the_primary_action_is_neutral_not_the_brand_colour(css: str) -> None:
    """A blue Save beside a blue Add beside a blue link gives a page three equally
    loud things and no focal point."""
    assert "--ff-primary:" in css
    assert ".ff-btn--primary" in css
    assert "--ff-focus-color" in css


def test_radius_derives_from_one_value(css: str) -> None:
    """So a project can round the whole interface by changing one number."""
    assert "--ff-radius-base:" in css
    for name in ("--ff-radius-sm", "--ff-radius-md", "--ff-radius-lg", "--ff-radius-xl"):
        assert (
            f"{name}: calc(var(--ff-radius-base)" in css or f"{name}: var(--ff-radius-base)" in css
        )


def test_row_actions_stay_visible_without_hover(css: str) -> None:
    """Touch has no hover, so fading them out would hide them entirely."""
    assert "@media (hover: none)" in css
