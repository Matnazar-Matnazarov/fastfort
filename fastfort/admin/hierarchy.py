"""`date_hierarchy`: drilling a list down by year, then month, then day.

Django's bar above the change list, rebuilt on what the list already has. A
level is not a new kind of query-string parameter: it is the pair of filters
`<field>__gte` and `<field>__lt` that `ListQuery.from_params` already validates,
so a drill-down link is an ordinary filtered URL, the active-filter chips can
clear it, and the export of a drilled list is the drilled list. Which level is
showing is read back off that pair -- a year-long span offers its months, a
month offers its days, a single day is the bottom.

Cost: two queries, the earliest and the latest value under the list's other
filters, so the links on offer stop where the data does rather than listing
every month since year one. Buckets inside that span are not counted -- a month
with nothing in it is still a link -- because counting would be one query per
link, up to thirty-one on a day level, on the page opened most.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from fastfort.spec import FieldType, Filter, FilterOperator, ListQuery, SortSpec

__all__ = ["DATE_TYPES", "MONTHS", "Drilldown", "Step", "Window", "build", "read_window"]

DATE_TYPES = frozenset({FieldType.DATE, FieldType.DATETIME})

#: English month names, passed through the translator one by one. Literal here
#: so the catalogue test can find them; `calendar.month_name` follows the
#: process locale, which is the server's, not the person reading the page.
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class Window:
    """The span the list is drilled into, as the two filter bounds say."""

    start: dt.date
    end: dt.date

    @property
    def level(self) -> str:
        """`"year"`, `"month"` or `"day"` -- what this span is one of."""
        if self.end == _next_day(self.start):
            return "day"
        if self.start.day == 1 and self.end == _next_month(self.start):
            return "month"
        return "year"


@dataclass(frozen=True, slots=True)
class Step:
    label: str
    url: str
    current: bool = False


@dataclass(frozen=True, slots=True)
class Drilldown:
    """What the template draws: the path so far, and the links one level down."""

    trail: tuple[Step, ...]
    links: tuple[Step, ...]


def read_window(params: dict[str, str], field: str) -> Window | None:
    """The drilled span, when the two bounds form one the bar could have built.

    Anything else -- one bound alone, a span of nine days, a value that is not a
    date -- is an ordinary range filter someone typed, and the bar shows the top
    level rather than pretending to understand it.
    """
    try:
        start = dt.date.fromisoformat(params.get(f"{field}__gte", "")[:10])
        end = dt.date.fromisoformat(params.get(f"{field}__lt", "")[:10])
    except ValueError:
        return None
    window = Window(start, end)
    if window.level == "day" or window.level == "month":
        return window
    if start.month == 1 and start.day == 1 and end == dt.date(start.year + 1, 1, 1):
        return window
    return None


async def build(
    adapter: Any,
    query: ListQuery,
    field: str,
    params: dict[str, str],
    *,
    url: Callable[[dict[str, str | None]], str],
    translate: Callable[..., str],
) -> Drilldown | None:
    """Resolve the bar for one list request. `None` when the column is empty."""
    window = read_window(params, field)
    # The bounds come from the list as it would be *without* the drill, so the
    # years offered do not collapse to the one already chosen.
    undrilled = replace(
        query,
        filters=tuple(f for f in query.filters if f.field != field),
        page=1,
        page_size=1,
    )
    first = await _edge(adapter, undrilled, field, descending=False)
    if first is None:
        return None
    last = await _edge(adapter, undrilled, field, descending=True)
    assert last is not None  # a column with a first value has a last one

    def link(start: dt.date, end: dt.date) -> str:
        return url(
            {
                # A bare date for a datetime column too, as the dashboard's
                # trend does: both ORMs read it as midnight, which is the
                # boundary a calendar day has.
                f"{field}__gte": start.isoformat(),
                f"{field}__lt": end.isoformat(),
                "p": None,
            }
        )

    def month_name(month: int) -> str:
        return translate(MONTHS[month - 1])

    trail = [Step(translate("All dates"), url({f"{field}__gte": None, f"{field}__lt": None}))]
    links: list[Step] = []

    if window is None:
        for year in range(first.year, last.year + 1):
            links.append(Step(str(year), link(dt.date(year, 1, 1), dt.date(year + 1, 1, 1))))
        return Drilldown(tuple(trail), tuple(links))

    year = window.start.year
    trail.append(Step(str(year), link(dt.date(year, 1, 1), dt.date(year + 1, 1, 1))))

    if window.level == "year":
        for month in range(1, 13):
            start = dt.date(year, month, 1)
            if (year, month) < (first.year, first.month) or (year, month) > (
                last.year,
                last.month,
            ):
                continue
            links.append(Step(month_name(month), link(start, _next_month(start))))
        return Drilldown(tuple(trail), tuple(links))

    month_start = window.start.replace(day=1)
    trail.append(
        Step(
            f"{month_name(month_start.month)} {year}",
            link(month_start, _next_month(month_start)),
        )
    )

    if window.level == "month":
        day = month_start
        while day < _next_month(month_start):
            if first <= day <= last:
                links.append(Step(str(day.day), link(day, _next_day(day))))
            day = _next_day(day)
        return Drilldown(tuple(trail), tuple(links))

    # A single day is the bottom: nothing below it, the day itself as the last
    # crumb, marked current so it reads as where you are rather than a link.
    trail.append(Step(str(window.start.day), link(window.start, window.end), current=True))
    return Drilldown(tuple(trail), ())


async def _edge(adapter: Any, query: ListQuery, field: str, *, descending: bool) -> dt.date | None:
    """The earliest or latest value of `field`, as a date.

    `isnull=false` because PostgreSQL sorts NULL first in a descending order,
    and "the latest date" being nothing would end the bar before it began.
    """
    ranged = replace(
        query,
        filters=(*query.filters, Filter(field, FilterOperator.ISNULL, "false")),
        ordering=(SortSpec(field, descending=descending),),
    )
    page = await adapter.list(ranged)
    if not page.items:
        return None
    value = getattr(page.items[0], field)
    if isinstance(value, dt.datetime):
        return value.date()
    return value if isinstance(value, dt.date) else None


def _next_day(day: dt.date) -> dt.date:
    return day + dt.timedelta(days=1)


def _next_month(day: dt.date) -> dt.date:
    if day.month == 12:
        return dt.date(day.year + 1, 1, 1)
    return dt.date(day.year, day.month + 1, 1)
