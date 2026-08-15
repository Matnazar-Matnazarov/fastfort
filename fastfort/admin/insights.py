"""What the dashboard's widgets ask the database, and what comes back.

The counts answer "how much is there"; these answer "is it growing", "what is it
made of" and "what happened lately" -- the questions anyone opening an admin
every morning is actually asking.

Deliberately narrow. There is no aggregation language here and no query builder:
a series is one count per day, a breakdown is one count per choice, and recent
rows are one ordered page. Everything is expressed through `ListQuery` and the
adapter protocol, which is what keeps this module free of SQL and of any one
ORM -- and what bounds the cost, since each shape states in its own docstring
how many queries it runs.

The date column is found by name. A project that calls it something else says so
with `AdminSettings.signup_field`, or names it on the widget, rather than being
told its schema is wrong.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from fastfort.spec import Choice, FieldType, Filter, FilterOperator, ListQuery, ModelSpec, SortSpec

from . import charts

__all__ = [
    "Bar",
    "Distribution",
    "RecentRow",
    "Series",
    "Slice",
    "build_distribution",
    "build_recent",
    "build_series",
    "date_field",
    "signup_field",
]

#: Column names that conventionally record when a row was created, in the order
#: they are preferred. `date_joined` first because a project coming from Django
#: has one, and on a user model it means exactly this and nothing else.
SIGNUP_FIELD_NAMES = (
    "date_joined",
    "joined_at",
    "registered_at",
    "signed_up_at",
    "created_at",
    "created",
)

_DATE_TYPES = {"date", "datetime"}


def signup_field(spec: ModelSpec, configured: str = "") -> str:
    """The column recording when a row was created, or `""` if there is none.

    A configured name is honoured only if the column exists and holds a date, so
    a typo leaves the chart off rather than taking the dashboard down with it.
    """
    if configured:
        field = next((f for f in spec if f.name == configured), None)
        return field.name if field is not None and field.type in _DATE_TYPES else ""

    by_name = {field.name: field for field in spec}
    for name in SIGNUP_FIELD_NAMES:
        field = by_name.get(name)
        if field is not None and field.type in _DATE_TYPES:
            return field.name
    return ""


#: The same detection under the name a widget on any model reads better with.
#: `signup_field` is what the user-model setting has always been called, and
#: renaming a public function to improve a sentence is not worth the break.
date_field = signup_field


@dataclass(frozen=True, slots=True)
class Bar:
    """One day of the series."""

    day: dt.date
    count: int

    @property
    def iso(self) -> str:
        return self.day.isoformat()


@dataclass(frozen=True, slots=True)
class Series:
    """A counted-per-day series, plus what a chart needs to draw it."""

    field: str
    bars: tuple[Bar, ...]

    @property
    def total(self) -> int:
        return sum(bar.count for bar in self.bars)

    @property
    def peak(self) -> int:
        """The tallest bar, floored at 1 so heights never divide by zero.

        An all-zero week is a real answer -- a new install has one -- and it
        should draw as a flat baseline rather than as no chart at all.
        """
        return max((bar.count for bar in self.bars), default=0) or 1

    @property
    def empty(self) -> bool:
        """True when nothing happened in the window.

        Worth asking, because the honest drawing of an all-zero series and the
        drawing of a chart that failed are the same picture: a flat rule across
        the bottom of the card. "New accounts · 0" under one was the first thing
        a new installation ever showed, and it read as the dashboard being
        broken rather than as the week having been quiet. The templates ask this
        and say so in words instead.
        """
        return self.total == 0

    def height(self, bar: Bar) -> int:
        """`bar` as a percentage of the peak, rounded away from invisible."""
        return charts.bar_height(bar.count, self.peak)

    # -- what the numbers say ------------------------------------------------

    @property
    def best(self) -> Bar | None:
        """The busiest day, or None on an empty series.

        Worth naming on the card: "the best day was the 14th" is the sentence a
        peak in a chart makes someone squint at the axis to work out.
        """
        return max(self.bars, key=lambda bar: bar.count, default=None)

    @property
    def average(self) -> float:
        """Rows per day across the window, to one decimal place."""
        if not self.bars:
            return 0.0
        return round(self.total / len(self.bars), 1)

    @property
    def half(self) -> int:
        """How many days each side of the comparison below covers."""
        return len(self.bars) // 2

    @property
    def delta(self) -> float | None:
        """Percentage change between the two halves of the window.

        The second half against the first, rather than this month against last:
        those are two windows and so twice the queries, and the window is
        already the shape the counts arrived in. None when the earlier half is
        empty, because "up from nothing" is a division by zero pretending to be
        a percentage -- the card says "no earlier activity" instead of printing
        an infinity.
        """
        half = self.half
        if half < 1:
            return None
        earlier = sum(bar.count for bar in self.bars[:half])
        later = sum(bar.count for bar in self.bars[-half:])
        if not earlier:
            return None
        return round((later - earlier) * 100 / earlier, 1)

    # -- what a chart is drawn from -----------------------------------------

    @property
    def counts(self) -> tuple[int, ...]:
        return tuple(bar.count for bar in self.bars)

    @property
    def line(self) -> str:
        return charts.line_points(self.counts, peak=self.peak)

    @property
    def area(self) -> str:
        return charts.area_path(self.counts, peak=self.peak)

    @property
    def gridlines(self) -> tuple[float, ...]:
        return charts.gridlines()


@dataclass(frozen=True, slots=True)
class Slice:
    """One value of a column, and how many rows hold it."""

    label: str
    count: int
    #: True for the row that stands for everything past the widget's limit. The
    #: template gives it the translated word and a neutral swatch; it is not a
    #: value anybody can click through to.
    other: bool = False


@dataclass(frozen=True, slots=True)
class Distribution:
    """What a column is made of, biggest first."""

    field: str
    label: str
    slices: tuple[Slice, ...]

    @property
    def total(self) -> int:
        """Rows that hold one of these values.

        Not the table's row count: a nullable column leaves rows in neither
        slice, and a percentage against the whole table would then never reach
        100% with no visible reason why.
        """
        return sum(part.count for part in self.slices)

    @property
    def peak(self) -> int:
        return max((part.count for part in self.slices), default=0) or 1

    def share(self, part: Slice) -> float:
        return charts.share(part.count, self.total)

    def width(self, part: Slice) -> int:
        """The meter fill, against the biggest slice rather than the total.

        Against the total, a column split forty ways draws forty invisible
        slivers; against the biggest, the shape of the distribution is legible
        and the percentage beside it still says what the real share is.
        """
        return charts.bar_height(part.count, self.peak)


@dataclass(frozen=True, slots=True)
class RecentRow:
    """One row of a "latest" list: what it is called, and when it arrived."""

    label: str
    when: Any
    url: str = ""


#: The most values a breakdown will count, since each one is a query. A column
#: with ninety distinct values is a table rather than a chart, and quietly
#: firing ninety counts to draw ninety unreadable bars is the kind of cost that
#: only shows up in production.
BREAKDOWN_CHOICE_CAP = 12

#: What a boolean column is broken down into. Its `choices` are empty -- there is
#: nothing to introspect -- but "how many are active" is one of the questions
#: most worth asking of an admin, so the two values are supplied here.
_BOOLEAN_CHOICES = (Choice(value="true", label="Yes"), Choice(value="false", label="No"))


async def build_distribution(
    counter: object, spec: ModelSpec, field: str, *, limit: int = 6
) -> Distribution:
    """Count rows per value of `field`, biggest first.

    One count per value, so the cost is the number of choices the column
    declares and never the number of rows. Columns without a fixed set of values
    are refused rather than sampled: counting the distinct values of a free-text
    column is a full scan, and this runs on the page everyone opens first.

    Everything past `limit` is folded into a single "Other" slice, which keeps
    its share honest -- dropping the tail would leave percentages that do not
    add up.
    """
    column = next((f for f in spec if f.name == field), None)
    if column is None:
        return Distribution(field="", label="", slices=())

    choices = column.choices or (_BOOLEAN_CHOICES if column.type is FieldType.BOOLEAN else ())
    if not choices or len(choices) > BREAKDOWN_CHOICE_CAP:
        return Distribution(field="", label="", slices=())

    counted: list[Slice] = []
    for choice in choices:
        count = await counter.count(  # type: ignore[attr-defined]
            ListQuery(filters=(Filter(field, FilterOperator.EXACT, str(choice.value)),))
        )
        if count:
            counted.append(Slice(label=choice.label, count=count))

    counted.sort(key=lambda part: part.count, reverse=True)
    if len(counted) > limit:
        tail = counted[limit:]
        counted = [
            *counted[:limit],
            Slice(label="Other", count=sum(p.count for p in tail), other=True),
        ]

    return Distribution(field=field, label=column.label, slices=tuple(counted))


async def build_recent(
    adapter: object, *, field: str, limit: int = 5, url_for: Any = None
) -> tuple[RecentRow, ...]:
    """The newest rows by `field`, newest first.

    One query. Values are read through the adapter's own snapshot rather than
    off the object, so this stays true for any ORM and cannot touch a lazy
    relation on the way past -- the usual way a dashboard turns into a hundred
    queries.
    """
    page = await adapter.list(  # type: ignore[attr-defined]
        ListQuery(ordering=(SortSpec(field, descending=True),), page_size=limit)
    )
    rows: list[RecentRow] = []
    for obj in page.items:
        snapshot = adapter.snapshot(obj)  # type: ignore[attr-defined]
        rows.append(
            RecentRow(
                label=adapter.label_for(obj),  # type: ignore[attr-defined]
                when=snapshot.get(field),
                url=url_for(adapter.primary_key_of(obj)) if url_for else "",  # type: ignore[attr-defined]
            )
        )
    return tuple(rows)


async def build_series(counter: object, spec: ModelSpec, *, days: int, field: str = "") -> Series:
    """Count rows per day for the last `days` days, ending today.

    `counter` is anything with an async `count(ListQuery)` -- the adapter, in
    practice; typed loosely so this module does not depend on the ORM layer.

    One count per day rather than one grouped query, because grouping a
    timestamp into days is written differently on every database and this layer
    does not get to write SQL. The cost is a fixed, small number of indexed
    counts, bounded by `days` and not by how big the table is.
    """
    column = field or signup_field(spec)
    if not column:
        return Series(field="", bars=())

    today = dt.date.today()  # noqa: DTZ011
    start = today - dt.timedelta(days=days - 1)

    bars: list[Bar] = []
    for offset in range(days):
        day = start + dt.timedelta(days=offset)
        # Half-open, so a row at exactly midnight belongs to one day only.
        bars.append(
            Bar(
                day=day,
                count=await counter.count(  # type: ignore[attr-defined]
                    ListQuery(
                        filters=(
                            Filter(column, FilterOperator.GTE, day.isoformat()),
                            Filter(
                                column,
                                FilterOperator.LT,
                                (day + dt.timedelta(days=1)).isoformat(),
                            ),
                        )
                    )
                ),
            )
        )

    return Series(field=column, bars=tuple(bars))
