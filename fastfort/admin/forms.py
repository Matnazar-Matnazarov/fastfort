"""Turning a `ModelSpec` into a form, and a submitted form back into values.

The write path is where an admin panel gets breached, so two rules hold here
without exception.

*The allow-list is the spec.* A field is writable only if `FieldSpec.editable` is
true and the `ModelAdmin` has not marked it read-only. A value submitted for
anything else is dropped and never reaches the adapter, whatever the request looked
like.

*Nothing is coerced silently.* A value that cannot be parsed into the column's
type is an error shown against that field, not a `None` written to the database.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from fastfort.auth.passwords import hash_password, validate_password
from fastfort.spec import Choice, FieldSpec, FieldType

from .files import UploadedFile, delete_upload, save_upload, stored_path

if TYPE_CHECKING:
    from fastfort.core.settings import MediaSettings
    from fastfort.spec import ModelSpec

    from .options import ModelAdmin

__all__ = ["WIDGET_NAMES", "Form", "FormField", "widget_for"]

#: Which control renders each field type. The template switches on this name, so
#: adding a widget is a template branch plus one entry here.
_WIDGETS: dict[FieldType, str] = {
    FieldType.STRING: "text",
    FieldType.TEXT: "textarea",
    FieldType.INTEGER: "number",
    FieldType.BIGINT: "number",
    FieldType.FLOAT: "number",
    FieldType.DECIMAL: "decimal",
    FieldType.BOOLEAN: "checkbox",
    FieldType.DATE: "date",
    FieldType.DATETIME: "datetime",
    FieldType.TIME: "time",
    FieldType.DURATION: "duration",
    FieldType.ARRAY: "list",
    FieldType.GEOMETRY: "point",
    FieldType.UUID: "text",
    FieldType.JSON: "json",
    FieldType.ENUM: "select",
    FieldType.EMAIL: "email",
    FieldType.URL: "url",
    FieldType.PASSWORD: "password",
    FieldType.FOREIGN_KEY: "relation",
    FieldType.ONE_TO_ONE: "relation",
    FieldType.MANY_TO_MANY: "relations",
}

#: Every control the form template can render. `ModelAdmin.formfield_overrides`
#: is checked against this at declaration time, so a typo is a start-up error
#: rather than a field that quietly renders read-only.
WIDGET_NAMES: frozenset[str] = frozenset(
    {
        *_WIDGETS.values(),
        "nullboolean",
        "color",
        "richtext",
        "readonly",
        "file",
        "image",
    }
)

#: Widgets whose submitted value is a file rather than text.
FILE_WIDGETS = frozenset({"file", "image"})

#: A field type with no widget is shown but never written, so an exotic column
#: degrades to a read-only row instead of blocking the whole form.
READONLY_WIDGET = "readonly"

#: Suffix of the confirmation input paired with a password control.
CONFIRM_SUFFIX = "__confirm"

#: Suffix of the checkbox that removes a file or image without replacing it.
CLEAR_SUFFIX = "__clear"

#: Help text for the controls the browser has no native input for. Both are a
#: plain text box, so the box has to state the shape it accepts.
_FORMAT_HELP = {
    "duration": "Length of time, as HH:MM:SS — or 2d HH:MM:SS for more than a day.",
    "list": "Separate values with a comma.",
    "point": "Latitude and longitude, as 41.2995, 69.2401.",
}

#: Widgets whose format hint is script-only, because script replaces the box
#: that needed explaining with a control that cannot be filled in wrongly.
#: Telling somebody to type `HH:MM:SS` next to four labelled number boxes is
#: instructions for a control that is no longer on the page.
_UPGRADED_WIDGETS = frozenset({"duration"})


def widget_for(spec: FieldSpec) -> str:
    """The control name for a field, honouring an explicit override."""
    if spec.widget:
        return spec.widget
    if spec.choices and spec.type is not FieldType.BOOLEAN:
        return "select"
    if spec.type is FieldType.BOOLEAN and spec.nullable:
        # A checkbox has two states and the column has three. Rendered as one,
        # a NULL ("not checked yet") became False ("checked, and it failed") the
        # first time anyone opened the form and pressed Save -- a silent change
        # to data nobody asked to change.
        return "nullboolean"
    return _WIDGETS.get(spec.type, READONLY_WIDGET)


@dataclass(slots=True)
class FormField:
    """One rendered control, with its current value and any errors."""

    spec: FieldSpec
    widget: str
    value: Any = None
    #: Rendered into the control. Kept separate from `value` because a date input
    #: needs "2026-07-30" while the value is a `date`.
    raw: str = ""
    errors: list[str] = dataclass_field(default_factory=list)
    choices: tuple[Choice, ...] = ()
    #: Multi-valued relations submit a list, so the template needs the selection.
    selected: tuple[str, ...] = ()
    #: Replaces the spec's help text when the control needs its own explanation.
    help_override: str | None = None
    #: True when the relation has more rows than can be sent to the browser, so
    #: the control searches the server as you type instead of listing everything.
    #: `choices` then holds only what is currently selected -- enough to render
    #: the field, not enough to browse it.
    remote: bool = False

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def label(self) -> str:
        return self.spec.label

    @property
    def required(self) -> bool:
        return self.spec.required

    @property
    def editable(self) -> bool:
        return self.widget != READONLY_WIDGET

    @property
    def help_text(self) -> str | None:
        return self.help_override or self.spec.help_text

    @property
    def help_is_for_no_script(self) -> bool:
        """Whether the help only applies when the control has not been upgraded."""
        return self.help_override is not None and self.widget in _UPGRADED_WIDGETS

    @property
    def confirm_name(self) -> str:
        return f"{self.spec.name}{CONFIRM_SUFFIX}"

    @property
    def clear_name(self) -> str:
        return f"{self.spec.name}{CLEAR_SUFFIX}"


class Form:
    """A model form derived from the spec and the admin's declarations."""

    def __init__(
        self,
        spec: ModelSpec,
        admin: ModelAdmin,
        *,
        instance: Any = None,
        relation_choices: dict[str, tuple[Choice, ...]] | None = None,
        remote_relations: frozenset[str] | None = None,
        auth_settings: Any = None,
        media: MediaSettings | None = None,
    ) -> None:
        self.spec = spec
        self.admin = admin
        self.instance = instance
        self._relation_choices = relation_choices or {}
        self._remote_relations = remote_relations or frozenset()
        self._auth_settings = auth_settings
        self._media = media
        # What `bind()` decided about file fields, not yet acted on. See
        # `commit_files`.
        self._pending_writes: list[tuple[str, bytes]] = []
        self._pending_deletes: list[str] = []
        self._passwords = admin.password_field_names()
        self.non_field_errors: list[str] = []
        self.fields: list[FormField] = [
            self._build(field_spec) for field_spec in self._visible_specs()
        ]

    # -- construction -------------------------------------------------------

    def _visible_specs(self) -> list[FieldSpec]:
        """Fields the form shows, in declaration order.

        Primary keys the database generates are omitted: offering the box invites
        someone to collide with an existing row. Reverse relations are omitted
        because editing them belongs on the other model's page.
        """
        writable = self.admin.editable_field_names()
        shown: list[FieldSpec] = []

        for field_spec in self.spec:
            if field_spec.type is FieldType.REVERSE_FK:
                continue
            if field_spec.primary_key and not field_spec.editable:
                continue
            if field_spec.name in writable or field_spec.name in self.admin.readonly_fields:
                shown.append(field_spec)
        return shown

    def _build(self, spec: FieldSpec) -> FormField:
        writable = spec.name in self.admin.editable_field_names()
        if spec.name in self._passwords and writable:
            # A password column is never rendered as text. The control takes a new
            # password plus a confirmation and hashes it; the stored hash is never
            # sent to the browser.
            return FormField(
                spec=spec,
                widget="password",
                value=None,
                raw="",
                help_override=(
                    "Leave blank to keep the current password."
                    if self.instance is not None
                    else None
                ),
            )
        if writable:
            # The admin's own choice wins over the type's default:
            # `formfield_overrides` exists precisely to say "this column is a
            # colour, not a string".
            widget = self.admin.widget_override(spec) or widget_for(spec)
        else:
            widget = READONLY_WIDGET
        value = (
            getattr(self.instance, spec.name, None) if self.instance is not None else spec.default
        )
        # Blanked here, not only in `raw`: the read-only control renders `value`,
        # so masking one and not the other would print a password hash onto the
        # page for any field marked read-only.
        if spec.sensitive:
            value = None

        remote = spec.name in self._remote_relations
        choices = spec.choices
        if spec.is_relation:
            # A relation the browser cannot be sent in full renders with only
            # what is already chosen, so the control shows the current value
            # rather than an empty box; everything else arrives from the
            # autocomplete endpoint as the person types.
            choices = (
                _current_choices(value) if remote else self._relation_choices.get(spec.name, ())
            )

        return FormField(
            spec=spec,
            widget=widget,
            value=value,
            raw=_render(value, spec),
            choices=choices,
            selected=_selection(value, spec),
            remote=remote,
            # Two controls are a plain text box because the browser has nothing
            # better, so the box has to say what shape it wants. Without this a
            # duration field is an empty rectangle and the only way to learn the
            # format is to guess wrong and read the error.
            help_override=_FORMAT_HELP.get(widget) if not spec.help_text else None,
        )

    # -- binding ------------------------------------------------------------

    def bind(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse a submitted form, returning the values that may be written.

        Errors are collected per field so the form can be re-rendered with every
        problem visible at once, rather than one per round trip.
        """
        writable = self.admin.editable_field_names()
        cleaned: dict[str, Any] = {}

        for form_field in self.fields:
            spec = form_field.spec
            if spec.name not in writable:
                # Not an error: a read-only field's control is not rendered, so a
                # value here was added by hand. Dropped without comment.
                continue

            if spec.name in self._passwords:
                self._bind_password(form_field, data, cleaned)
                continue

            if form_field.widget in FILE_WIDGETS:
                self._bind_file(form_field, data, cleaned)
                continue

            if spec.type.is_multi_valued:
                if f"{spec.name}[]" not in data and spec.name not in data:
                    continue
                submitted = data.get(f"{spec.name}[]") or data.get(spec.name) or []
                values = submitted if isinstance(submitted, list) else [submitted]
                form_field.selected = tuple(str(item) for item in values)
                cleaned[spec.name] = [item for item in values if item not in ("", None)]
                continue

            if spec.type is FieldType.BOOLEAN and form_field.widget == "nullboolean":
                # Three states, so the control always submits something and the
                # empty string is a real answer: "unknown".
                answer = str(data.get(spec.name, ""))
                cleaned[spec.name] = {"1": True, "0": False}.get(answer)
                form_field.value = cleaned[spec.name]
                form_field.raw = answer
                continue

            if spec.type is FieldType.BOOLEAN:
                # An unchecked checkbox submits nothing at all, which is the one
                # case where a missing key means False rather than "unchanged".
                cleaned[spec.name] = spec.name in data
                form_field.value = cleaned[spec.name]
                continue

            if spec.name not in data:
                # Absent and submitted-empty are different things. A browser sends
                # every rendered control, so a missing key means the request did
                # not include that field -- and writing None for it would null a
                # NOT NULL column that has a database default.
                continue

            raw = data.get(spec.name)
            raw = "" if raw is None else str(raw)
            form_field.raw = raw

            if raw == "":
                if spec.required:
                    form_field.errors.append("This field is required.")
                    continue
                cleaned[spec.name] = None
                form_field.value = None
                continue

            try:
                parsed = _parse(raw, spec)
            except ValueError as exc:
                form_field.errors.append(str(exc))
                continue

            error = _check_bounds(parsed, spec)
            if error:
                form_field.errors.append(error)
                continue

            cleaned[spec.name] = parsed
            form_field.value = parsed

        return cleaned

    def _bind_password(
        self, form_field: FormField, data: dict[str, Any], cleaned: dict[str, Any]
    ) -> None:
        """Read a new password, or leave the stored one alone.

        Blank means unchanged, which is the only sane default for an edit form:
        anything else would either clear the password or force the person editing
        an unrelated field to retype it.
        """
        spec = form_field.spec
        entered = str(data.get(spec.name, ""))
        confirm = str(data.get(form_field.confirm_name, ""))

        if not entered and not confirm:
            if self.instance is None:
                # Always required when creating, whatever the column's default
                # says. A `default=""` on a password column is a placeholder, not
                # a password, and an account created without one is an account
                # nobody can sign in to.
                form_field.errors.append("Set a password for the new account.")
            return

        if entered != confirm:
            form_field.errors.append("The two passwords do not match.")
            return

        if self._auth_settings is not None:
            identity = self._identity_hint()
            try:
                validate_password(entered, self._auth_settings, identity=identity)
            except Exception as exc:
                problems = getattr(exc, "field_errors", {}).get("password") or [str(exc)]
                form_field.errors.extend(problems)
                return

        # Hashed here rather than in the view, so no code path can store a
        # plaintext password by forgetting to call something.
        cleaned[spec.name] = hash_password(entered)

    def _bind_file(
        self, form_field: FormField, data: dict[str, Any], cleaned: dict[str, Any]
    ) -> None:
        """Stage a new upload, clear a stored one, or leave it alone.

        Three outcomes, in that priority: a chosen file replaces whatever was
        there; failing that, a ticked "clear" checkbox removes it; failing that,
        the column is left untouched. A file input renders empty for a value
        that already exists -- browsers refuse to prefill one, for good reason
        -- so "nothing was chosen this time" must mean "keep it", not "there is
        nothing".

        Nothing touches disk here. `bind()` runs before the rest of the form is
        known to be valid and before the write it feeds even reaches the
        database, and a field on the other side of the same form failing its
        own check must not have already deleted a file the row still points at.
        `commit_files()` is what actually writes and deletes, and it runs only
        once the save it belongs to has gone through.
        """
        spec = form_field.spec
        upload = data.get(spec.name)
        current = getattr(self.instance, spec.name, None) if self.instance is not None else None

        if isinstance(upload, UploadedFile) and upload.chosen:
            if self._media is None:
                form_field.errors.append("File uploads are not configured for this admin.")
                return
            if len(upload.content) > self._media.upload_limit:
                form_field.errors.append(
                    f"That file is too large. The limit is {self._media.upload_limit:,} bytes."
                )
                return
            relative = stored_path(self.spec.key, spec.name, upload.filename)
            self._pending_writes.append((relative, upload.content))
            if current:
                self._pending_deletes.append(str(current))
            cleaned[spec.name] = relative
            form_field.value = relative
            form_field.raw = relative
            return

        if str(data.get(form_field.clear_name, "")) in ("1", "true", "on"):
            if current:
                self._pending_deletes.append(str(current))
            cleaned[spec.name] = None
            form_field.value = None
            form_field.raw = ""
            return

        # Neither a replacement nor a clear: the stored value is left exactly as
        # it was, which is why this never appears in `cleaned` at all.

    def commit_files(self) -> None:
        """Write and delete whatever `bind()` staged.

        Called once, after the row this form describes has actually been
        created or updated -- not before, and not at all if that write failed or
        the form was invalid. Writing first and deleting on save would mean a
        row that fails to save has already lost the file it still points at;
        writing after guarantees the filesystem only ever changes once the
        database agrees.
        """
        if self._media is None:
            return
        for relative, content in self._pending_writes:
            save_upload(self._media, relative, content)
        for relative in self._pending_deletes:
            delete_upload(self._media, relative)

    def _identity_hint(self) -> str:
        """The email or username, so the policy can reject a password containing it."""
        for candidate in ("email", "username", "login"):
            field = self.spec.get(candidate)
            if field is None:
                continue
            for form_field in self.fields:
                if form_field.name == candidate and form_field.raw:
                    return form_field.raw
            value = getattr(self.instance, candidate, None) if self.instance else None
            if isinstance(value, str):
                return value
        return ""

    def add_error(self, name: str, message: str) -> None:
        """Attach a message from outside the form, e.g. a unique-key violation."""
        for form_field in self.fields:
            if form_field.name == name:
                form_field.errors.append(message)
                return
        self.non_field_errors.append(message)

    @property
    def needs_multipart(self) -> bool:
        """Whether the rendered `<form>` has to be `multipart/form-data`.

        Only when it does: every other form stays URL-encoded, which keeps a
        submission that could never contain a file simple to read off in a
        request log.
        """
        return any(f.widget in FILE_WIDGETS for f in self.fields)

    @property
    def is_valid(self) -> bool:
        return not self.non_field_errors and all(not f.errors for f in self.fields)

    @property
    def error_count(self) -> int:
        return len(self.non_field_errors) + sum(len(f.errors) for f in self.fields)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse(raw: str, spec: FieldSpec) -> Any:
    """Turn a submitted string into the column's Python type.

    Messages name what was expected, because "invalid value" tells the person
    filling the form nothing they can act on.
    """
    text = raw.strip()

    if spec.is_relation:
        # The identity of the related row, as the dropdown submitted it. The
        # adapter resolves it to an object and reports a missing target itself.
        return text

    if spec.type in {FieldType.INTEGER, FieldType.BIGINT}:
        try:
            return int(text)
        except ValueError:
            raise ValueError("Enter a whole number.") from None

    if spec.type is FieldType.FLOAT:
        try:
            return float(text)
        except ValueError:
            raise ValueError("Enter a number.") from None

    if spec.type is FieldType.DECIMAL:
        try:
            return Decimal(text)
        except InvalidOperation:
            raise ValueError("Enter a number, for example 1234.56.") from None

    if spec.type is FieldType.DATE:
        try:
            return dt.date.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a date as YYYY-MM-DD.") from None

    if spec.type is FieldType.DATETIME:
        try:
            parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Enter a date and time as YYYY-MM-DD HH:MM.") from None
        # A naive value from a datetime-local input is read as UTC, so the column
        # never receives a mix of aware and naive values.
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)

    if spec.type is FieldType.TIME:
        try:
            return dt.time.fromisoformat(text)
        except ValueError:
            raise ValueError("Enter a time as HH:MM.") from None

    if spec.type is FieldType.DURATION:
        return _parse_duration(text)

    if spec.type is FieldType.GEOMETRY:
        return _parse_point(text)

    if spec.type is FieldType.ARRAY:
        # Comma-separated, which is what people type and what the read side
        # renders. Blank entries are dropped rather than stored as empty
        # strings: "a, b," is a typo, not a three-element list.
        return [part.strip() for part in text.split(",") if part.strip()]

    if spec.type is FieldType.UUID:
        try:
            return uuid.UUID(text)
        except ValueError:
            raise ValueError("Enter a valid UUID.") from None

    if spec.type is FieldType.JSON:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Enter valid JSON ({exc.msg} at position {exc.pos}).") from None

    if spec.type is FieldType.EMAIL and "@" not in text:
        raise ValueError("Enter an email address.")

    if spec.choices and not any(str(choice.value) == text for choice in spec.choices):
        allowed = ", ".join(str(choice.value) for choice in spec.choices)
        raise ValueError(f"Choose one of: {allowed}.")

    return raw if spec.type is FieldType.TEXT else text


def _parse_point(text: str) -> str:
    """`lat, lng` into the EWKT a spatial column accepts.

    Returned as text rather than as a geometry object: constructing one would
    mean importing GeoAlchemy2, and the write path must not depend on a package
    the project may not have installed. Every spatial backend accepts EWKT, so
    the string is the portable answer.

    Latitude first, because that is the order every map, phone and street sign
    uses -- even though WKT itself is longitude first, which is exactly the
    trap this converts away from.
    """
    parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if len(parts) != 2:
        raise ValueError("Enter a latitude and a longitude, as 41.2995, 69.2401.")
    try:
        latitude, longitude = (float(part) for part in parts)
    except ValueError:
        raise ValueError("Enter a latitude and a longitude, as 41.2995, 69.2401.") from None

    if not -90 <= latitude <= 90:
        raise ValueError("Latitude runs from -90 to 90.")
    if not -180 <= longitude <= 180:
        raise ValueError("Longitude runs from -180 to 180.")
    return f"SRID=4326;POINT({longitude} {latitude})"


def _point_text(value: Any) -> str:
    """`lat, lng` from whatever a spatial column handed back.

    The value is normally a GeoAlchemy2 `WKBElement`, whose `str` is the raw
    hex -- which is what the read-only fallback used to print at people. The
    hex is decoded here rather than by importing Shapely: a POINT is a fixed
    21-byte layout, and it is the only geometry an admin form can meaningfully
    edit anyway. Anything else falls back to what the value says it is.
    """
    import binascii
    import struct

    raw = getattr(value, "desc", None) or str(value)
    try:
        data = binascii.unhexlify(raw)
    except (binascii.Error, ValueError, TypeError):
        return str(value)

    if len(data) < 21:
        return str(value)

    order = "<" if data[0] == 1 else ">"
    (kind,) = struct.unpack(f"{order}I", data[1:5])
    # The high bit of the type word marks an EWKB SRID prefix, which shifts the
    # coordinates along by four bytes.
    offset = 9 if kind & 0x20000000 else 5
    if kind & 0xFF != 1 or len(data) < offset + 16:
        return str(value)

    longitude, latitude = struct.unpack(f"{order}dd", data[offset : offset + 16])
    return f"{latitude:g}, {longitude:g}"


def _duration_text(value: dt.timedelta) -> str:
    """A duration in exactly the shape `_parse_duration` accepts.

    `str(timedelta)` renders "2 days, 4:15:00", which that parser rejects -- so
    opening a row with a multi-day duration and pressing Save produced a
    validation error on a field nobody had touched.
    """
    seconds = value.total_seconds()
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)

    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    # `%g` so a whole number of seconds does not render as "05.000000".
    clock = f"{int(hours):02d}:{int(minutes):02d}:{seconds:09.6f}".rstrip("0").rstrip(".")
    if len(clock) == 6:  # HH:MM: with the seconds stripped to nothing
        clock += "00"
    return f"{sign}{int(days)}d {clock}" if days else f"{sign}{clock}"


def _parse_duration(text: str) -> dt.timedelta:
    """A timedelta from `HH:MM:SS`, `MM:SS`, or `Nd HH:MM:SS`.

    There is no HTML control for a duration, so this is a text box and the shape
    it accepts has to be the one people already write. Seconds may carry a
    fraction, because that is what `str(timedelta)` prints and round-tripping
    the rendered value is the least surprising behaviour.
    """
    days = 0
    remainder = text
    if "d" in remainder:
        head, _, remainder = remainder.partition("d")
        try:
            days = int(head.strip())
        except ValueError:
            raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.") from None

    parts = remainder.strip().split(":") if remainder.strip() else ["0"]
    if len(parts) > 3:
        raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.")

    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        raise ValueError("Enter a duration as HH:MM:SS, or 2d HH:MM:SS.") from None

    # Right-aligned, so "90" is ninety seconds and "1:30" is a minute and a half.
    while len(numbers) < 3:
        numbers.insert(0, 0.0)
    hours, minutes, seconds = numbers
    return dt.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _check_bounds(value: Any, spec: FieldSpec) -> str | None:
    """Range and length checks the database would otherwise reject on write.

    Caught here so the person sees a field-level message instead of a 500 from a
    constraint violation.
    """
    if spec.max_length is not None and isinstance(value, str) and len(value) > spec.max_length:
        return f"Use at most {spec.max_length} characters (this is {len(value)})."
    if isinstance(value, int | float | Decimal):
        as_decimal = Decimal(str(value))
        if spec.min_value is not None and as_decimal < spec.min_value:
            return f"Must be at least {spec.min_value}."
        if spec.max_value is not None and as_decimal > spec.max_value:
            return f"Must be at most {spec.max_value}."
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _boolean_raw(value: Any) -> str:
    """The submitted form of a three-state boolean."""
    if value is None:
        return ""
    return "1" if value else "0"


def _render(value: Any, spec: FieldSpec) -> str:
    """The string a control shows for a value.

    A sensitive field renders empty: echoing a stored secret back into a form is
    how it ends up in a browser cache or a screenshot.
    """
    if spec.type is FieldType.BOOLEAN and spec.nullable:
        return _boolean_raw(value)
    if spec.sensitive or value is None:
        return ""
    if spec.is_relation:
        return str(_identity(value))
    if spec.type is FieldType.DATETIME and isinstance(value, dt.datetime):
        # datetime-local wants exactly this shape and rejects an offset.
        return value.strftime("%Y-%m-%dT%H:%M")
    if spec.type is FieldType.DATE and isinstance(value, dt.date):
        return value.isoformat()
    if spec.type is FieldType.TIME and isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if spec.type is FieldType.JSON:
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    if spec.type is FieldType.DURATION and isinstance(value, dt.timedelta):
        return _duration_text(value)
    if spec.type is FieldType.ARRAY and isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    if spec.type is FieldType.GEOMETRY:
        return _point_text(value)
    return str(value)


def _current_choices(value: Any) -> tuple[Choice, ...]:
    """The related row or rows already attached, as options.

    An autocomplete control needs these even though it does not need the rest:
    without them an edit form would show an empty picker for a field that is set,
    and saving without touching it would look like clearing it.
    """
    if value is None:
        return ()
    items = value if isinstance(value, list | tuple | set) else [value]
    return tuple(
        Choice(value=_identity(item), label=str(item)) for item in items if item is not None
    )


def _selection(value: Any, spec: FieldSpec) -> tuple[str, ...]:
    if not spec.type.is_multi_valued or value is None:
        return ()
    return tuple(str(_identity(item)) for item in value)


def _identity(obj: Any) -> Any:
    """A related object's primary key, or the value itself if it is already one."""
    for attribute in ("id", "pk"):
        found = getattr(obj, attribute, None)
        if found is not None:
            return found
    return obj
