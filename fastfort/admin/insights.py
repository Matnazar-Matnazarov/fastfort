"""How many rows arrived, and when.

The dashboard's counts answer "how much is there"; this answers "is it growing",
which is the question anyone opening an admin every morning is actually asking.

Deliberately narrow. There is no chart builder here, no aggregation language and
no per-model configuration: one series, over one column, for one model, drawn as
bars the server renders into the page. A dashboard nobody configured should
already show something true, and everything past that belongs to whatever the
project already uses to look at its data.

The column is found by name. A project that calls it something else says so with
`UISettings.signup_field` rather than being told its schema is wrong.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastfort.spec import Filter, FilterOperator, ListQuery, ModelSpec

__all__ = ["Bar", "Series", "build_series", "signup_field"]

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

    def height(self, bar: Bar) -> int:
        """`bar` as a percentage of the peak, rounded away from invisible.

        A day with one signup next to a day with four hundred is still a day
        with a signup: floored at 2% so it draws as a sliver rather than as
        nothing, which would read as "no data" rather than "very little".
        """
        if not bar.count:
            return 0
        return max(2, round(bar.count * 100 / self.peak))


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
