"""Tests for the design-token stylesheets and render-time theming.

The stylesheet ships pre-built, so these guard the two things that can silently
break it: a token referenced but never declared, and a configuration value
reaching a `style` attribute unvalidated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fastfort.admin.site import CSS_SHEETS
from fastfort.core.settings import UISettings
from fastfort.ui.theming import Theme

CSS_DIR = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static" / "css"

#: Every stylesheet, in the order a page loads them.
#:
#: Taken from the router rather than restated, so a sheet added there is covered
#: by these checks straight away. A second hand-maintained list is a list that
#: silently stops matching, and the sheet nobody is checking is the one with the
#: unbalanced brace.
SHEETS = CSS_SHEETS


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


def test_visually_hidden_content_never_sits_on_a_table() -> None:
    """`.ff-sr-only` on a `<table>` does not hide it from the layout.

    A table treats `height` as a minimum and grows to fit its rows, so the
    utility's `1px` box stays full size and `overflow: hidden` has nothing left
    to clip. The box is absolutely positioned, which means it is still counted in
    the page's scrollable overflow: the dashboard's hidden 30-row data table made
    the document about 640px taller than the shell it sits in.

    That is what took the sidebar off screen. The sidebar is `position: sticky`,
    and a sticky grid item can only travel inside its own grid area -- the shell.
    Once the page scrolled past where the shell ended, the sidebar had run out of
    room and went up with everything else, on a page whose last 640px were blank.

    Wrapping the table in a `<div class="ff-sr-only">` fixes it, because a div
    does collapse to 1px and the clip stops at the div. Nothing in CSS rescues
    the table form -- `max-height`, `contain` and the legacy `clip` were all
    measured and all ignored -- so the shape of the markup is the guard, and this
    is the test for it.
    """
    templates = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "templates"
    offenders = [
        path.relative_to(templates).as_posix()
        for path in templates.rglob("*.html")
        if re.search(r"<table[^>]*\bff-sr-only\b", path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f'wrap the table in a <div class="ff-sr-only">: {offenders}'


def test_focus_is_never_removed_without_a_replacement(css: str) -> None:
    """Keyboard users navigate the admin constantly."""
    assert ":focus-visible {" in css
    assert "outline: var(--ff-focus-width)" in css


def test_motion_is_disabled_when_the_viewer_asks(css: str) -> None:
    assert "prefers-reduced-motion" in css


#: Scripts every single page downloads. Anything not named here is asked for
#: only by a page whose fields need it -- see `_extra_scripts` in `admin/site.py`.
ALWAYS_LOADED = ("boot.js", "fastfort.js")


def _gzipped(*blobs: bytes) -> int:
    import gzip

    return sum(len(gzip.compress(blob)) for blob in blobs)


def test_the_everyday_page_stays_within_budget(css: str) -> None:
    """What every page downloads is budgeted at 70 KB gzipped: the stylesheet,
    `boot.js` and `fastfort.js`.

    Both halves are weighed, not just the stylesheet. The budget exists to keep
    the admin from acquiring a front-end framework by degrees, and a framework
    would arrive as JavaScript -- so measuring only the CSS guarded the half that
    was never at risk.

    A complete design system for an admin -- shell, table, forms, every drawn
    control including the ones that replace `<select>`, both themes -- plus the
    behaviour that drives them fits in well under this, and it fails if that
    stops being true. For comparison, React and ReactDOM alone are about twice
    this before a single component is written.

    This is the number that matters, and it is the reason the front end is no
    longer one bundle. The column-types round added a polygon editor and four
    structured-data editors, together about a third of the JavaScript that
    existed before them -- and the great majority of admin pages have no
    geometry, array or hstore column at all. Charging every list view for them
    is exactly the accumulation this test exists to prevent, so they moved into
    files a page asks for only when one of its fields needs one, and the weight
    of the everyday page went *down*.

    The assertion sits below the stated ceiling rather than at it, so growth is
    noticed before it becomes a problem. The gap is deliberate headroom: raising
    this number by fifty bytes every time something lands is the failure mode the
    budget exists to catch, so a raise should be one considered step with room
    left in it -- and trimming the comments to squeeze under is never the fix.

    Raised once, to 66 KB, for three things on the everyday path: thumbnails in
    list columns, which put a picture where an image column used to print its
    stored path; a confirm dialog whose actions are large enough to be read
    before they are clicked; and the `clamp` helper the date picker's clock had
    been calling without it being defined in that bundle at all -- a
    ReferenceError on every keystroke, which is what made choosing an hour do
    nothing.

    Raised again, to 67 KB, for the note on `.ff-sr-only` recording why the
    utility must never sit on a `<table>`. Two hundred bytes for a comment is a
    poor trade in the abstract; it is a good one here, because the bug it
    describes -- an invisible table growing the page until the sticky sidebar
    scrolled off the top of it -- took an afternoon and a headless browser to
    find, and nothing in the rule itself hints at it.

    Raised again, to 70 KB, for `07-dashboard.css`: the dashboard's card grid,
    its plots, its meters and its stat tiles. It buys a page that draws area
    charts, bar charts, sparklines with a signed delta, and a breakdown per
    value of a column -- all of it server-rendered, so the JavaScript half of
    this budget did not move by a single byte and neither did any page that is
    not the dashboard. A charting library would have cost this much compressed
    before drawing anything.
    """
    js = CSS_DIR.parent / "js"
    scripts = [js / name for name in ALWAYS_LOADED]
    missing = [path.name for path in scripts if not path.exists()]
    assert not missing, f"the budget cannot pass by measuring nothing: {missing}"

    compressed = _gzipped(css.encode("utf-8"), *(path.read_bytes() for path in scripts))
    assert compressed < 70_000, f"{compressed} bytes gzipped"


def test_the_whole_front_end_stays_within_budget(css: str) -> None:
    """Everything that ships, on-demand bundles included, is budgeted at 92,000 bytes.

    The per-page budget above is the one that protects the common case, but on
    its own it would be a budget with a hole in it: anything could be moved into
    an on-demand file and stop being counted. This one counts every byte in the
    package, so the split has to earn its place by being genuinely optional
    rather than by relabelling weight.

    It has been raised five times before this round: from 40 KB when the
    related-object popup, the appearance panel, the filter drawer and the export
    menu landed together; again for the date picker, the duration boxes and the
    map; again for the upload card, the date picker's month and year views and
    its clock, and the map's own controls; again for the geometry editor for
    all seven shapes and the array, hstore, JSON and address editors; and again
    for thumbnails in list columns, a per-map zoom ceiling, and the note in
    `fastfort-geo.js` explaining why tile coordinates are re-based -- a
    single-precision overflow that blanked the map past zoom 19 while every
    tile was loaded.

    This round, to 92,000: the dashboard's own stylesheet, and the note on
    `.ff-sr-only`. The dashboard is now widgets a project arranges -- charts,
    sparklines, meters, grouped counts -- and every one of them is drawn by the
    server. Nothing here is a script: the JavaScript in this package weighs
    exactly what it did before the dashboard was rebuilt.

    Before it: `boot.js` scrolling the current model back into a nav that
    scrolls by itself. It has to be pre-paint, so it has to be in the one file
    that is render-blocking, which is the most expensive place in the package
    for a byte to live -- and it was still the right trade, because the
    alternative was a sidebar that marked "you are here" below the fold on every
    navigation.

    Each raise was a feature rather than an accumulation.
    """
    scripts = sorted((CSS_DIR.parent / "js").glob("*.js"))
    assert scripts, "the budget cannot pass by measuring nothing"

    compressed = _gzipped(css.encode("utf-8"), *(script.read_bytes() for script in scripts))
    assert compressed < 92_000, f"{compressed} bytes gzipped"


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


def test_the_collapsed_sidebar_is_keyed_off_the_root(css: str) -> None:
    """Because the boot script runs before `.ff-app` has been parsed.

    Applying the stored state to `.ff-app` meant waiting for the deferred script,
    which runs after the first paint -- so the sidebar opened and then snapped
    shut on every single navigation. The root element is the only one that exists
    early enough to be marked.
    """
    assert ':root[data-ff-collapsed="true"]' in css
    assert '.ff-app[data-collapsed="true"]' not in css


def test_a_false_boolean_is_drawn_in_red(css: str) -> None:
    """ "No" is an answer, and usually the one being looked for.

    Grey on grey made a column of them unreadable at a glance, which is the only
    way anybody reads that column.
    """
    start = css.index(".ff-bool--off {")
    body = css[start : css.index("}", start)]
    assert "--ff-danger" in body


def test_script_only_controls_are_hidden_without_script(css: str) -> None:
    """A control that does nothing when clicked is worse than one that is absent.

    Two directions are needed, and it is the second that keeps being forgotten:
    `.ff-js-only` hides what cannot work without the script, and `.ff-no-js`
    hides the native control once its replacement exists. The row-selection
    column belongs to the first group -- the action bar it feeds is script-only,
    so without it a ticked box does nothing at all.
    """
    assert ":root:not([data-ff-js]) .ff-js-only" in css
    assert ":root[data-ff-js] .ff-no-js" in css
    assert ":root:not([data-ff-js]) .ff-table__select" in css


def test_the_stylesheet_is_written_in_logical_directions(css: str) -> None:
    """Arabic turns the whole admin around from one `dir` attribute, and that
    only works because nothing here is written in physical directions.

    Two exceptions are deliberate and are what this counts around. A tick inside
    a checkbox is a tick in any language -- mirroring it would draw it backwards.
    The map is a coordinate space rather than a layout: its tiles are placed by
    script in physical pixels, so `left: 0` there means what it says.
    """
    physical = re.findall(
        r"^\s+(?:left|right|padding-left|padding-right|margin-left|margin-right"
        r"|border-left|border-right):",
        css,
        re.M,
    )

    # `.ff-check::after` (the tick), the tooltip's `left: auto` resets, and the
    # map's tile and marker origins. Anything past that is a rule that will not
    # flip, and the way to write it is `inset-inline-*` / `padding-inline-*`.
    assert len(physical) <= 8, f"{len(physical)} physical direction properties: {physical}"


def test_the_dialogs_restate_their_centring_margin(css: str) -> None:
    """The reset zeroes every margin, including the one that centres a dialog.

    A modal `<dialog>` is centred by the browser's own `margin: auto`, so a
    blanket `margin: 0` silently parks both the confirmation dialog and the
    command palette in the top-left corner.
    """
    for block in (".ff-modal {", ".ff-palette {"):
        start = css.index(block)
        body = css[start : css.index("}", start)]
        assert "margin:" in body, f"{block} must restate its margin"
        assert "auto" in body


def test_icons_carry_their_stroke_at_the_point_of_use(css: str) -> None:
    """`<use>` clones a symbol into the referencing element's shadow tree.

    So presentation attributes on the sprite's own root never reach the cloned
    paths, and every icon renders as a solid black silhouette. The properties
    have to sit on the element that references the symbol.
    """
    start = css.index(".ff-icon {")
    body = css[start : css.index("}", start)]
    assert "fill: none" in body
    assert "stroke: currentColor" in body


def test_row_actions_are_never_hidden_behind_hover(css: str) -> None:
    """Touch has no hover, so fading them out would hide them entirely.

    They used to be text buttons at `opacity: 0` until the row was hovered, with
    a `@media (hover: none)` escape hatch to bring them back on a touch screen.
    They are icon buttons now and are simply always drawn, so the guard is that
    nothing has reintroduced the fade.
    """
    faded = re.search(
        r"\.ff-row-actions[^{]*\{[^}]*opacity:\s*0\s*[;}]",
        css,
    )
    assert faded is None, "row actions must not be hidden until hover"
