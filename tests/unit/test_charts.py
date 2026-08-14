"""The geometry the dashboard's charts are drawn from.

Pure arithmetic, which is why it is worth testing directly: a scaling mistake
draws a chart that is *wrong* rather than one that is missing, and a wrong chart
is believed. Each of these pins one number a reader would have to measure with a
ruler to catch.
"""

from __future__ import annotations

import pytest

from fastfort.admin import charts


def coordinates(points: str) -> list[tuple[float, float]]:
    return [(float(x), float(y)) for x, y in (pair.split(",") for pair in points.split())]


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------


def test_the_points_span_the_whole_width() -> None:
    """First on the left edge, last on the right, whatever the count.

    A chart inset from one end reads as a series that stopped early.
    """
    points = coordinates(charts.line_points([1, 2, 3, 4], peak=4))

    assert points[0][0] == 0
    assert points[-1][0] == charts.SIZE


def test_the_peak_sits_at_the_top_and_zero_on_the_baseline() -> None:
    """Inset by the padding at both ends: the stroke is two pixels wide and the
    root SVG clips, so a peak drawn at y=0 loses its top half."""
    points = coordinates(charts.line_points([0, 10], peak=10))

    assert points[1][1] == charts.PAD
    assert points[0][1] == charts.SIZE - charts.PAD


def test_a_single_day_draws_a_flat_line_rather_than_nothing() -> None:
    """A week-old install has one day of data, and an empty box reads as broken."""
    points = coordinates(charts.line_points([5], peak=5))

    assert len(points) == 2
    assert points[0][1] == points[1][1]


def test_no_values_draw_nothing_at_all() -> None:
    assert charts.line_points([], peak=1) == ""
    assert charts.area_path([], peak=1) == ""


def test_a_peak_of_zero_cannot_divide() -> None:
    """`Series.peak` floors at 1, but the helper is public and takes what it is
    given -- and a dashboard must not 500 over an empty table."""
    assert charts.line_points([0, 0], peak=0) == ""


# ---------------------------------------------------------------------------
# The area under it
# ---------------------------------------------------------------------------


def test_the_area_closes_on_the_bottom_edge() -> None:
    """On the edge, not on the padded baseline, or the wash floats above the
    axis rule the card draws under it."""
    path = charts.area_path([1, 2], peak=2)

    assert path.startswith("M0,")
    assert path.endswith("L100,100 L0,100 Z")


def test_the_coordinates_are_written_as_short_as_they_can_be() -> None:
    """`M0,50` rather than `M0.0,50.0`: two characters a point, thirty points a
    chart, several charts a page."""
    assert charts.line_points([0, 1], peak=1) == "0,97 100,3"


def test_the_area_follows_the_same_points_as_the_line() -> None:
    """The wash and the line have to be the same curve. Drawn from two
    calculations they would drift apart, and the fill would sit beside its own
    line rather than under it."""
    line = charts.line_points([3, 1, 4], peak=4)
    path = charts.area_path([3, 1, 4], peak=4)

    for point in line.split():
        assert point in path


# ---------------------------------------------------------------------------
# Bars, gridlines and shares
# ---------------------------------------------------------------------------


def test_the_tallest_bar_fills_the_plot() -> None:
    assert charts.bar_height(5, 5) == 100


@pytest.mark.parametrize(("value", "peak"), [(0, 5), (0, 0), (-1, 5)])
def test_nothing_draws_nothing(value: int, peak: int) -> None:
    assert charts.bar_height(value, peak) == 0


def test_a_very_small_value_still_draws() -> None:
    """One row beside four hundred is still a row. Rounded to zero it would read
    as "nothing happened" rather than as "very little did"."""
    assert charts.bar_height(1, 400) == 2


def test_gridlines_start_at_the_top_and_stop_before_the_axis() -> None:
    """The last one would land on the axis rule, which the card already draws --
    two hairlines in the same place print at double weight."""
    lines = charts.gridlines(3)

    assert len(lines) == 3
    assert lines[0] == charts.PAD
    assert max(lines) < charts.SIZE - charts.PAD


def test_no_gridlines_is_a_legal_answer() -> None:
    assert charts.gridlines(0) == ()


def test_a_share_is_a_percentage_to_one_place() -> None:
    assert charts.share(1, 3) == 33.3


def test_a_share_of_nothing_is_zero_rather_than_an_error() -> None:
    """A breakdown of an empty table is every slice at nothing, which is a real
    answer -- and the alternative is a division by zero on the first dashboard
    anyone opens."""
    assert charts.share(0, 0) == 0.0
