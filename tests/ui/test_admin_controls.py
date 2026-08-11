"""Regressions for controls that were rendered but did not work.

Every test here reproduces something a person hit in a browser. They are grouped
in one module because they share a cause worth naming: each was invisible from
the server's side. The markup was correct, the page was a 200, and the control
simply did nothing — which is the class of bug a request-level test suite is
structurally unable to see.

So these assert the *contract* between the server and the browser: that the
attribute the script binds to actually reaches the page, that the helper the
script calls is defined in the bundle that calls it, and that a value the page
sends is one the widget can use.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from fastfort.core.settings import UISettings

STATIC = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "static"
TEMPLATES = Path(__file__).resolve().parents[2] / "fastfort" / "ui" / "templates"


# ---------------------------------------------------------------------------
# Attribute separation
# ---------------------------------------------------------------------------


def _conditional_attribute_lines() -> list[tuple[Path, int, str]]:
    """Every `{% if ... %}attr{% endif %}` that is alone on its line."""
    pattern = re.compile(r"^\s*\{% if [^%]+%\}([^{].*?)\{% endif %\}\s*$")
    found: list[tuple[Path, int, str]] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = pattern.match(line)
            if match:
                found.append((path, number, match.group(1)))
    return found


def test_conditional_attributes_cannot_glue_to_the_next_one() -> None:
    """A conditional attribute ends in a space, or two of them become one.

    The renderer runs with `trim_blocks=True`, which drops the newline after
    `{% endif %}`. Two conditional attributes on consecutive lines were
    therefore emitted with nothing between them, and a required date column came
    out carrying the attribute `data-ff-daterequired` — so the picker's selector
    matched nothing and every required date field silently fell back to the
    browser's own control, next to fields that had the admin's.

    Checked here rather than in one rendered page because the failure is
    per-template and the next one to be added would reintroduce it.
    """
    offenders = [
        f"{path.relative_to(TEMPLATES)}:{number}  {body!r}"
        for path, number, body in _conditional_attribute_lines()
        if not body.endswith(" ")
    ]
    assert not offenders, "conditional attributes without a trailing space:\n" + "\n".join(
        offenders
    )


# ---------------------------------------------------------------------------
# The browser bundles
# ---------------------------------------------------------------------------


def _identifiers_defined_in(source: str) -> set[str]:
    return set(re.findall(r"\b(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", source))


def _identifiers_borrowed_in(source: str) -> set[str]:
    """Names taken out of `window.FastFort`, which is how the extra bundles get
    the main one's helpers. That is the documented route; the bug was a call
    that went through neither it nor a local definition."""
    borrowed: set[str] = set()
    for names in re.findall(r"const\s*\{([^}]*)\}\s*=\s*(?:kit|window\.FastFort)", source):
        borrowed.update(part.split(":")[-1].strip() for part in names.split(","))
    return {name for name in borrowed if name}


@pytest.mark.parametrize("script", ["fastfort.js", "fastfort-geo.js", "fastfort-data.js"])
def test_each_bundle_defines_the_helpers_it_calls(script: str) -> None:
    """A helper "obviously available" from another file is a ReferenceError.

    Each script is its own IIFE, so nothing crosses between them. `clamp` was
    used by the date picker's clock in `fastfort.js` and defined only in
    `fastfort-geo.js` — every call threw, which killed the change listener on
    each clock unit before it could write. Choosing an hour did nothing, "Done"
    closed the panel without applying anything, and clicking a day on a
    `datetime-local` field left the value untouched.

    Nothing failed loudly, because a listener that throws takes only itself
    down. This is the cheapest check that catches the next one.
    """
    source = (STATIC / "js" / script).read_text()
    available = _identifiers_defined_in(source) | _identifiers_borrowed_in(source)

    # The small shared-looking helpers. Not a general undefined-name check --
    # that needs a parser -- but these are the ones that actually moved between
    # files and would move again.
    shared = {"clamp", "pad", "el", "icon", "once", "uid", "t"}
    used = {name for name in shared if re.search(rf"\b{name}\(", source)}

    missing = sorted(used - available)
    assert not missing, (
        f"{script} calls {missing} without defining them or destructuring them from window.FastFort"
    )


def test_the_map_stops_where_the_tiles_do() -> None:
    """The request ceiling follows the source; the view goes two past it.

    OpenStreetMap's standard layer serves zoom 0 to 19 and 404s above it, and a
    failed tile is a blank square. The deepest level to *ask* for is therefore a
    property of the source rather than a constant — a project naming a layer
    that serves 22 got a map three levels blurrier than it needed to be.

    The view still zooms past the last level with pictures, scaling it rather
    than refetching. That is what every map application does past its own
    imagery, and it is better than a button that stops responding — so lowering
    the *view* ceiling to match the tiles would fix the blank square by removing
    the feature.
    """
    source = (STATIC / "js" / "fastfort-geo.js").read_text()

    # Two ceilings, answering different questions: how deep the server has
    # pictures, and how far the view may go past them.
    assert "const DEFAULT_TILE_ZOOM = 19;" in source
    assert "const VIEW_ZOOM_HEADROOM = 2;" in source

    # Per-map, because the tile URL is per-map. A constant was wrong for every
    # source that is not OpenStreetMap.
    assert "this.maxTileZoom = clamp(" in source
    assert "Math.min(this.zoom, this.maxTileZoom)" in source
    assert "MIN_ZOOM, this.maxZoom" in source


def test_tiles_are_positioned_relative_to_their_layer() -> None:
    """Absolute world coordinates overflow the compositor's float precision.

    At zoom 19 a tile's world coordinate is around 93 million pixels, which
    needs 27 bits. The transform pipeline is single-precision — 24 bits of
    mantissa — so anything past 2^24 is rounded. At scale 1 that was invisible,
    because the layer's translate and the tile's translate rounded the same way
    and the errors cancelled. The moment the layer was scaled they stopped
    cancelling and the error came out multiplied: at 2x every tile landed
    exactly 2^25 pixels off screen. The map went blank past the tile ceiling
    while every tile was loaded and reported as visible, which is why this went
    unnoticed — nothing failed, and no request 404ed.

    So the subtraction has to happen in JavaScript's doubles, with only the
    small result reaching CSS.
    """
    source = (STATIC / "js" / "fastfort-geo.js").read_text()

    # Each layer carries an origin, and tiles are placed against it.
    assert "originX: null," in source
    assert "layer.originX = first.x * TILE;" in source
    assert "x * TILE - layer.originX" in source

    # The layer's own transform closes the gap, computed here rather than left
    # for the compositor to cancel.
    assert "anchorX * scale - originX" in source

    # The shape this replaced: a bare world coordinate handed straight to CSS.
    assert "`translate(${-originX}px, ${-originY}px) scale(" not in source
    assert "`translate(${x * TILE}px, ${y * TILE}px)`" not in source


def test_opening_a_panel_does_not_scroll_the_page() -> None:
    """Focusing into a panel that was just unhidden scrolls it into view.

    The panel is positioned against a trigger that may be halfway down a long
    form, so "into view" moved the whole page — opening a dropdown jumped the
    layout by about 120 pixels. `preventScroll` keeps the keyboard behaviour and
    drops the scrolling, which was never the point of the call.
    """
    source = (STATIC / "js" / "fastfort.js").read_text()

    # Every focus call that happens while a panel is being opened.
    opening = re.findall(
        r"(?:searchEl|target|panel\.querySelector\([^)]*\))\?\.focus\([^)]*\)", source
    )
    assert opening, "the focus calls this protects have moved"
    for call in opening:
        assert "preventScroll" in call, call


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_map_max_zoom_is_configurable_and_bounded() -> None:
    """A source that serves deeper can say so; a typo cannot blank the map."""
    assert UISettings().map_max_zoom == 19

    assert UISettings(map_max_zoom=22).map_max_zoom == 22

    with pytest.raises(ValueError, match="map_max_zoom"):
        UISettings(map_max_zoom=0)
    with pytest.raises(ValueError, match="map_max_zoom"):
        UISettings(map_max_zoom=30)
