"""The dashboard, as widgets a project can rearrange.

The page used to be one hard-coded layout: every model's row count, and a chart
of new accounts. Both are still what a project that configures nothing gets --
the default costs exactly the queries it always did -- but they are now two
widgets in a list, and the list is `fort.set_dashboard(...)`::

    from fastfort.admin import Counts, Metric, Recent, Trend

    fort.set_dashboard(
        Metric(Shipment, title="Shipments booked", days=14),
        Metric(Invoice, title="Invoices raised", days=14),
        Trend(Shipment, days=30, span=FULL),
        Recent(Invoice, limit=5),
        Counts(),
    )

Two rules hold this together.

*A widget declares what it wants, not how to get it.* Each one resolves against a
`Context` that owns the unit of work, so every widget on the page shares one
transaction and one connection, and a widget cannot reach past the adapter
protocol into an ORM.

*A widget states its cost.* Every class below says in its docstring how many
queries it runs, because a dashboard is the page that gets opened most and the
easy mistake is a card that quietly costs a hundred of them. `Counts` is one per
model, `Trend` and `Metric` are one per day, `Breakdown` is one per value of the
column, `Recent` is one.

A project that wants something else entirely subclasses `Widget`, returns a
`Card` naming its own template, and puts that template in a directory it passed
as `template_dirs`. Nothing here is special-cased for the built-in five.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import NoneType
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastfort.core.registry import default_model_key
from fastfort.spec import ListQuery

from . import insights

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from fastfort.spec import ModelSpec

__all__ = [
    "FULL",
    "HALF",
    "THIRD",
    "Breakdown",
    "Card",
    "Context",
    "Counts",
    "Dashboard",
    "Metric",
    "Recent",
    "Signups",
    "Trend",
    "Widget",
]

#: How wide a card sits in the dashboard's six-column grid. Named rather than
#: numbered at the call site: `span=2` says nothing about what it will look like,
#: and the number that means "half" changes if the grid ever does.
THIRD = 1
HALF = 2
FULL = 3


@dataclass(frozen=True, slots=True)
class Card:
    """A resolved widget: a template, and what to render it with."""

    template: str
    context: dict[str, Any]
    span: int = FULL


@dataclass(frozen=True, slots=True)
class Context:
    """What a widget is allowed to reach, and the transaction it reaches through.

    Handed to every widget on the page. It holds the unit of work rather than
    creating one, so the whole dashboard is a single transaction: eleven widgets
    that each opened their own would be eleven connections for one page load.
    """

    fort: Any
    uow: Any
    days: int
    list_url: Callable[[str], str]
    #: Resolves a key to the *instance* of its ModelAdmin. The registry holds
    #: the class, and a class's `title` is the property object rather than the
    #: string it computes -- which is how a card ended up titled
    #: "<property object at 0x7f6cbe8ebf10>".
    admin_for: Callable[[str], Any]
    #: True when the request named a window itself, through the range control.
    #: It is what tells `window()` apart from the page's default, and without it
    #: a widget that pinned its own `days` would ignore the control -- half the
    #: cards moving to the chosen range and half staying where they were, which
    #: reads as the control being broken rather than as the widget being
    #: deliberate.
    chosen: bool = False

    def window(self, preferred: int) -> int:
        """How many days a widget should cover.

        A widget's own `days` is its *default*, not its instruction: it wins
        while the page is showing the window it was configured with, and loses
        the moment somebody asks for a different one. The alternative -- letting
        a pinned widget keep its window -- was tried on paper and is worse than
        having no control at all: pressing "7 days" would move the trend and
        leave the metric beside it on fourteen, with both cards still captioned
        truthfully and the page as a whole saying nothing coherent.
        """
        return self.days if self.chosen else (preferred or self.days)

    def key_for(self, model: type) -> str:
        """The registry key for `model`, or the one introspection would derive.

        A model does not have to have an admin page to be charted -- an events
        table nobody edits is a good thing to plot -- so an unregistered model
        gets the derived key and no link.
        """
        entry = self.fort.registry.get(model)
        return str(entry.key) if entry is not None else default_model_key(model)

    def url_for(self, model: type) -> str:
        """The list page for `model`, or `""` when it has no admin."""
        return self.list_url(self.key_for(model)) if self.fort.registry.get(model) else ""

    def spec_for(self, model: type) -> ModelSpec:
        spec: ModelSpec = self.fort.backend.introspect(model, key=self.key_for(model))
        return spec

    def adapter_for(self, model: type) -> Any:
        return self.fort.backend.adapter(model, self.uow, key=self.key_for(model))

    def title_for(self, model: type) -> str:
        """What the admin calls this model, plural, falling back to the spec."""
        entry = self.fort.registry.get(model)
        if entry is not None:
            return str(self.admin_for(entry.key).title)
        return str(self.spec_for(model).verbose_name_plural)


class Widget:
    """One card on the dashboard.

    Subclass this to add a card of your own: return a `Card` naming a template
    your project ships, or `None` to render nothing at all. Returning `None` is
    the right answer whenever the data a card is about is missing -- an empty
    card that explains it has nothing to say is worse than no card.
    """

    span: int = FULL

    async def resolve(self, context: Context) -> Card | None:  # pragma: no cover - interface
        raise NotImplementedError


# ---------------------------------------------------------------------------
# The built-in widgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Counts(Widget):
    """Every registered model, with its row count, grouped by application.

    One count per registered model, which is what the dashboard has always cost.
    `only` narrows it to the keys named, for a project with sixty models that
    wants six of them on the front page.
    """

    only: Sequence[str] = ()
    title: str = "Browse"
    span: int = FULL

    async def resolve(self, context: Context) -> Card | None:
        groups: dict[str, list[dict[str, Any]]] = {}
        for entry in context.fort.registry:
            if self.only and entry.key not in self.only:
                continue
            adapter = context.fort.backend.adapter(entry.model, context.uow, key=entry.key)
            group = entry.key.split(".", 1)[0]
            groups.setdefault(group, []).append(
                {
                    "key": entry.key,
                    "title": context.admin_for(entry.key).title,
                    "url": context.list_url(entry.key),
                    "count": await adapter.count(_ALL),
                }
            )

        if not groups:
            return None
        return Card(
            template="dashboard/counts.html",
            context={"title": self.title, "groups": groups},
            span=self.span,
        )


@dataclass(frozen=True, slots=True)
class Trend(Widget):
    """How many rows arrived per day, as an area chart or as bars.

    One count per day in the window: `days` queries, each of them an indexed
    range on one column, and none of them growing with the size of the table.

    The card carries the window's own summary -- total, busiest day, average,
    and the second half against the first -- because all four fall out of counts
    already in hand and each one answers a question the chart only implies.
    """

    model: type
    title: str = ""
    on: str = ""
    days: int = 0
    kind: str = "area"
    span: int = FULL

    async def resolve(self, context: Context) -> Card | None:
        days = context.window(self.days)
        if not days:
            return None

        spec = context.spec_for(self.model)
        column = insights.date_field(spec, self.on)
        if not column:
            return None

        series = await insights.build_series(
            context.adapter_for(self.model), spec, days=days, field=column
        )
        return Card(
            template="dashboard/trend.html",
            context={
                "title": self.title or context.title_for(self.model),
                "series": series,
                "bars": self.kind == "bars",
                "url": context.url_for(self.model),
            },
            span=self.span,
        )


@dataclass(frozen=True, slots=True)
class Signups(Trend):
    """The user model's trend, which is the chart a project gets for free.

    Separate from `Trend` only because the model it plots is the one FastFort
    already knows about, and because it honours `AdminSettings.signup_field`.
    Nothing to configure, and nothing rendered when there is no user model.
    """

    #: Never read: `resolve` below asks the installation for its user model. It
    #: is here because a dataclass cannot drop a field its base declared.
    model: type = NoneType
    title: str = "New accounts"

    async def resolve(self, context: Context) -> Card | None:
        if not context.fort.has_user_model:
            return None
        settings = context.fort.settings.admin
        widget = Trend(
            model=context.fort.user_config.model,
            title=self.title,
            on=self.on or settings.signup_field,
            days=self.days or settings.dashboard_days,
            kind=self.kind,
            span=self.span,
        )
        return await widget.resolve(context)


@dataclass(frozen=True, slots=True)
class Metric(Widget):
    """One number, how it moved, and a sparkline of the window behind it.

    The same `days` queries a `Trend` costs, drawn small: a row of these across
    the top of a dashboard is the shape most people mean by one. Pair it with a
    `Trend` on the same model only if the detail is worth paying twice for.
    """

    model: type
    title: str = ""
    on: str = ""
    days: int = 0
    icon: str = ""
    span: int = THIRD

    async def resolve(self, context: Context) -> Card | None:
        days = context.window(self.days)
        if not days:
            return None

        spec = context.spec_for(self.model)
        column = insights.date_field(spec, self.on)
        if not column:
            return None

        series = await insights.build_series(
            context.adapter_for(self.model), spec, days=days, field=column
        )
        return Card(
            template="dashboard/metric.html",
            context={
                "title": self.title or context.title_for(self.model),
                "series": series,
                "icon": self.icon,
                "url": context.url_for(self.model),
            },
            span=self.span,
        )


@dataclass(frozen=True, slots=True)
class Breakdown(Widget):
    """What a column is made of, as a meter per value.

    One count per value the column declares, so it is offered only for columns
    with a fixed set of them -- an enumeration, or a boolean. Counting the
    distinct values of a free-text column is a scan of the table, and this is
    the page everyone opens first.
    """

    model: type
    on: str
    title: str = ""
    limit: int = 6
    span: int = HALF

    async def resolve(self, context: Context) -> Card | None:
        distribution = await insights.build_distribution(
            context.adapter_for(self.model),
            context.spec_for(self.model),
            self.on,
            limit=self.limit,
        )
        if not distribution.slices:
            return None
        return Card(
            template="dashboard/breakdown.html",
            context={
                # The title is composed in the template, not here: a sentence
                # built with an f-string is a sentence no catalogue can hold.
                "title": self.title,
                "subject": context.title_for(self.model),
                "distribution": distribution,
                "url": context.url_for(self.model),
            },
            span=self.span,
        )


@dataclass(frozen=True, slots=True)
class Recent(Widget):
    """The newest rows, by whichever column records when they arrived.

    One query. The cheapest useful card there is, and on most admins the one
    people actually read: "what happened since I last looked".
    """

    model: type
    title: str = ""
    on: str = ""
    limit: int = 5
    span: int = HALF

    async def resolve(self, context: Context) -> Card | None:
        spec = context.spec_for(self.model)
        column = insights.date_field(spec, self.on)
        if not column:
            return None

        key = context.key_for(self.model)
        registered = context.fort.registry.get(self.model) is not None
        rows = await insights.build_recent(
            context.adapter_for(self.model),
            field=column,
            limit=self.limit,
            url_for=(lambda pk: _row_url(context, key, pk)) if registered else None,
        )
        if not rows:
            return None
        return Card(
            template="dashboard/recent.html",
            context={
                "title": self.title,
                "subject": context.title_for(self.model),
                "rows": rows,
                "url": context.url_for(self.model),
            },
            span=self.span,
        )


# ---------------------------------------------------------------------------
# The list of them
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Dashboard:
    """The widgets a dashboard is made of, in the order they appear."""

    widgets: tuple[Widget, ...] = field(default_factory=lambda: (Signups(), Counts()))

    async def resolve(self, context: Context) -> tuple[Card, ...]:
        """Every widget's card, in order, skipping the ones with nothing to say.

        Sequentially rather than gathered: they share one unit of work, and a
        session is not something two coroutines may hold at once.
        """
        cards = [await widget.resolve(context) for widget in self.widgets]
        return tuple(card for card in cards if card is not None)


#: The whole table, unfiltered and unpaginated -- what a count is asked for.
#: Built once: `ListQuery` is frozen, and a dashboard of forty widgets would
#: otherwise build forty identical ones.
_ALL = ListQuery()


def _row_url(context: Context, key: str, pk: tuple[Any, ...]) -> str:
    """The page for one row, built the same way the list's own links are.

    Composite keys travel joined by `~` and the whole thing is quoted, because a
    primary key can be a slug and a slug can contain a slash.
    """
    return context.list_url(key) + quote("~".join(str(part) for part in pk), safe="") + "/"
