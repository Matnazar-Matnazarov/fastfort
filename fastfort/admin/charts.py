"""The geometry a chart is drawn from: numbers in, coordinates out.

Pure arithmetic, no queries and no markup, because the shapes are the part worth
testing and a test that has to build a page first is a test nobody writes.

Everything here works in a normalised 100x100 space. The templates hand that to
an `<svg viewBox="0 0 100 100" preserveAspectRatio="none">`, so one set of
coordinates fits a card of any width; strokes carry `vector-effect:
non-scaling-stroke` so the line stays 2px wide however far the box is stretched.
Nothing inside the plot is text -- stretched text would be a different width in
every card -- so the labels are HTML around the SVG rather than `<text>` in it.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "PAD",
    "SIZE",
    "area_path",
    "bar_height",
    "gridlines",
    "line_points",
    "share",
]

#: The side of the square the coordinates are expressed in.
SIZE = 100.0

#: Breathing room, top and bottom, in the same units. Without it a peak sits on
#: y=0 and the root SVG -- which clips -- takes the top half of its own stroke
#: off with it, so the tallest point in the chart is the one drawn thinnest.
PAD = 3.0


def _y(value: float, peak: float, *, height: float, pad: float) -> float:
    usable = height - pad * 2
    return round(pad + (1 - value / peak) * usable, 2)


def _n(value: float) -> str:
    """A coordinate, as short as it can be written without changing it.

    `%g` drops the trailing zeros, so a path reads `M0,50` rather than
    `M0.0,50.0`. Two characters a point, thirty points a chart and several
    charts a page: it is the difference between a chart costing a line of HTML
    and costing a paragraph of it.
    """
    return f"{value:g}"


def line_points(
    values: Sequence[float],
    *,
    peak: float,
    width: float = SIZE,
    height: float = SIZE,
    pad: float = PAD,
) -> str:
    """`values` as `x,y` pairs for a `<polyline points>`.

    A single value has no line to draw, so it becomes a flat one across the box:
    a chart that renders nothing looks broken, and "one day of data" is a real
    state a week-old install is in.
    """
    if not values or peak <= 0:
        return ""
    if len(values) == 1:
        y = _n(_y(values[0], peak, height=height, pad=pad))
        return f"0,{y} {_n(width)},{y}"

    step = width / (len(values) - 1)
    return " ".join(
        f"{_n(round(index * step, 2))},{_n(_y(value, peak, height=height, pad=pad))}"
        for index, value in enumerate(values)
    )


def area_path(
    values: Sequence[float],
    *,
    peak: float,
    width: float = SIZE,
    height: float = SIZE,
    pad: float = PAD,
) -> str:
    """The same line, closed down to the baseline, for the wash underneath it.

    Closed on `height` rather than on the padded baseline: the fill should reach
    the bottom edge of the box, where the axis rule is drawn, or it floats.
    """
    points = line_points(values, peak=peak, width=width, height=height, pad=pad)
    if not points:
        return ""
    first_x = points.split(",", 1)[0]
    return f"M{points.replace(' ', ' L')} L{_n(width)},{_n(height)} L{first_x},{_n(height)} Z"


def gridlines(count: int = 3, *, height: float = SIZE, pad: float = PAD) -> tuple[float, ...]:
    """`count` evenly spaced y positions, from the top of the plot to the axis.

    The last one lands on the axis itself and is left to the axis rule, so a
    hairline is never drawn twice at the same place and printed at double weight.
    """
    if count < 1:
        return ()
    usable = height - pad * 2
    return tuple(round(pad + usable * index / count, 2) for index in range(count))


def bar_height(value: float, peak: float) -> int:
    """`value` as a percentage of `peak`, rounded away from invisible.

    A day with one row next to a day with four hundred is still a day with a
    row: floored at 2% so it draws as a sliver rather than as nothing, which
    would read as "no data" rather than as "very little".
    """
    if value <= 0 or peak <= 0:
        return 0
    return max(2, round(value * 100 / peak))


def share(value: float, total: float) -> float:
    """`value` as a percentage of `total`, to one decimal place.

    Zero when there is no total: a breakdown of an empty table is every slice at
    nothing, not a division by zero on the first dashboard someone opens.
    """
    if total <= 0:
        return 0.0
    return round(value * 100 / total, 1)
