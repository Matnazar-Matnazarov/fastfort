"""The controls the browser does not ship, drawn by FastFort instead.

A date input opens whatever calendar the platform feels like, which is a
different control on every operating system and, on a desktop Firefox, a rather
plain one. A duration input does not exist at all, so the field was a text box
with the format written underneath it -- instructions the reader had to follow
correctly or be told off by a validation error.

Both are upgraded in the page. What is tested here is the half the server owns:
that it marks the fields, that it hides the instructions that only apply while
the plain box is on screen, and that what the upgraded control writes is a value
the server accepts. The behaviour of the controls themselves is script, and this
suite has no browser -- but a marker that stops being rendered, or a format the
parser stops accepting, breaks them just as thoroughly and is invisible without
these.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.admin.forms import _duration_text, _parse_duration
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def form_body(client: httpx.AsyncClient) -> str:
    response = await client.get("/admin/shop.product/1/")
    assert response.status_code == 200, response.text
    return response.text


def input_for(body: str, name: str) -> str:
    match = re.search(rf"<input[^>]*\bname=\"{name}\"[^>]*>", body)
    assert match, f"no input named {name}"
    return match.group(0)


# ---------------------------------------------------------------------------
# Marking the fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["released_on", "created_at"])
async def test_a_date_field_is_marked_for_the_calendar(
    client: httpx.AsyncClient, name: str
) -> None:
    assert "data-ff-date" in input_for(await form_body(client), name)


async def test_a_duration_field_is_marked_for_the_segments(client: httpx.AsyncClient) -> None:
    assert "data-ff-duration" in input_for(await form_body(client), "warranty")


async def test_an_ordinary_field_is_marked_for_neither(client: httpx.AsyncClient) -> None:
    """The markers are what the enhancers look for; on the wrong field they
    would replace a working control with one for a value it does not hold."""
    box = input_for(await form_body(client), "name")

    assert "data-ff-date" not in box
    assert "data-ff-duration" not in box


async def test_the_date_field_keeps_its_name_and_value(client: httpx.AsyncClient) -> None:
    """The calendar writes into this input rather than replacing it, so the
    field has to stay the one that submits -- with script off it is exactly the
    control it has always been."""
    box = input_for(await form_body(client), "released_on")

    assert 'type="date"' in box
    assert 'value="2026-01-15"' in box


# ---------------------------------------------------------------------------
# The picker's own machinery
#
# There is no browser here, so what these check is that the script still ships
# the parts and that the server still sends the words for them. A view that gets
# renamed out from under the click handler, or a string the server stops
# sending, breaks the control exactly as thoroughly as a logic error and is
# invisible from the Python side without this.
# ---------------------------------------------------------------------------


async def test_the_calendar_can_zoom_out_to_months_and_years(client: httpx.AsyncClient) -> None:
    """Paging a month at a time is fine for next Tuesday and useless for a date
    of birth: 1987 is four hundred clicks from here on one arrow."""
    script = (await client.get("/admin/static/js/fastfort.js")).text

    assert "data-ff-cal-zoom" in script
    assert "data-ff-cal-month" in script
    assert "data-ff-cal-year" in script


async def test_a_datetime_field_gets_a_clock(client: httpx.AsyncClient) -> None:
    """A `datetime-local` column stores a time, and the panel had no way to set
    one -- the calendar wrote the date and left the clock at whatever it was."""
    script = (await client.get("/admin/static/js/fastfort.js")).text

    assert "ff-datepicker__clock" in script
    assert "writeTime" in script


async def test_the_clock_is_made_of_the_same_dropdowns_as_everything_else(
    client: httpx.AsyncClient,
) -> None:
    """Two number boxes were what this started as, and they read as a form to
    fill in rather than a time to pick: spinners drawn differently by every
    browser, no way to see the options, and nothing saying whether 9 meant
    morning or evening.
    """
    script = (await client.get("/admin/static/js/fastfort.js")).text

    # Real `<select>` elements, upgraded by the combobox the rest of the admin
    # uses -- rather than a second dropdown of the picker's own.
    assert 'class: "ff-select ff-datepicker__unit"' in script
    assert "enhance(this.panel)" in script


async def test_the_clock_follows_the_locale_on_twelve_or_twenty_four_hours(
    client: httpx.AsyncClient,
) -> None:
    """9pm is "9 PM" in English and "21:00" in most of the world. Hard-coding
    either is wrong for most people in one direction or the other, and the
    AM/PM names themselves come from `Intl` rather than from a catalogue."""
    script = (await client.get("/admin/static/js/fastfort.js")).text

    assert "hourCycle" in script
    assert "dayPeriod" in script


async def test_the_server_sends_the_words_the_picker_and_uploader_need() -> None:
    from fastfort.admin.site import _ui_text
    from fastfort.i18n import Translator

    sent = _ui_text(Translator())

    for key in ("now", "done", "choose-file", "or-drop-it", "remove", "undo", "too-large"):
        assert key in sent, key


# ---------------------------------------------------------------------------
# The format hint
# ---------------------------------------------------------------------------


async def test_the_duration_format_hint_is_script_only(client: httpx.AsyncClient) -> None:
    """Four labelled number boxes do not need "as HH:MM:SS" written under them,
    and the sentence describes a control that is no longer on the page."""
    body = await form_body(client)
    hint = re.search(r'<span class="([^"]*)">[^<]*HH:MM:SS[^<]*</span>', body)

    assert hint, "the plain box still needs the format written down"
    assert "ff-no-js" in hint.group(1)


# ---------------------------------------------------------------------------
# What the segments write, the server has to accept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        # Exactly the two shapes `Duration.sync` produces: a bare clock, and a
        # day count in front of one.
        ("00:00:00", dt.timedelta()),
        ("01:30:00", dt.timedelta(hours=1, minutes=30)),
        ("23:59:59", dt.timedelta(hours=23, minutes=59, seconds=59)),
        ("2d 04:15:00", dt.timedelta(days=2, hours=4, minutes=15)),
        ("1d 03:46:40", dt.timedelta(days=1, seconds=13_600)),
    ],
)
def test_the_server_parses_what_the_segments_write(written: str, expected: dt.timedelta) -> None:
    assert _parse_duration(written) == expected


@pytest.mark.parametrize(
    "value",
    [
        dt.timedelta(),
        dt.timedelta(minutes=90),
        dt.timedelta(days=2, hours=4, minutes=15),
        dt.timedelta(days=400, seconds=1),
    ],
)
def test_a_rendered_duration_is_a_value_the_segments_can_read(value: dt.timedelta) -> None:
    """The boxes are filled by parsing what the server rendered. A shape the
    script cannot read leaves the plain text box on screen -- correct, but not
    the control this is supposed to be.

    Kept in step with the regex in `Duration.parseDuration`.
    """
    rendered = _duration_text(value)

    assert re.match(r"^(?:\d+d )?\d+:[0-5]\d:[0-5]\d$", rendered), rendered


def test_a_sub_second_duration_renders_a_shape_the_segments_refuse() -> None:
    """And so keeps the plain text box, on purpose.

    Whole-second boxes cannot show a fraction. Rounding one to fit would drop
    part of a stored value the moment somebody opened the row and saved it,
    without ever showing them the digits they were losing.
    """
    rendered = _duration_text(dt.timedelta(seconds=1, milliseconds=500))

    assert rendered == "00:00:01.5"
    assert not re.match(r"^(?:\d+d )?\d+:[0-5]\d(?::[0-5]\d)?$", rendered)
    # Still a value the server round-trips; only the upgraded control declines.
    assert _parse_duration(rendered) == dt.timedelta(seconds=1, milliseconds=500)


async def test_a_duration_saves_and_comes_back(client: httpx.AsyncClient) -> None:
    """The round trip the widget actually performs."""
    body = await form_body(client)
    csrf = re.search(r'name="_csrf" value="([^"]+)"', body)
    assert csrf

    fields = {
        name: value
        for name, value in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', body)
        if name not in {"_csrf", "warranty"}
    }
    response = await client.post(
        "/admin/shop.product/1/",
        data={**fields, "name": "Kept", "warranty": "2d 04:15:00", "_csrf": csrf.group(1)},
    )
    assert response.status_code == 303, response.text

    assert 'value="2d 04:15:00"' in await form_body(client)
