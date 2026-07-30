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

if TYPE_CHECKING:
    from fastfort.spec import ModelSpec

    from .options import ModelAdmin

__all__ = ["Form", "FormField", "widget_for"]

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

#: A field type with no widget is shown but never written, so an exotic column
#: degrades to a read-only row instead of blocking the whole form.
READONLY_WIDGET = "readonly"

#: Suffix of the confirmation input paired with a password control.
CONFIRM_SUFFIX = "__confirm"


def widget_for(spec: FieldSpec) -> str:
    """The control name for a field, honouring an explicit override."""
    if spec.widget:
        return spec.widget
    if spec.choices and spec.type is not FieldType.BOOLEAN:
        return "select"
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
    def confirm_name(self) -> str:
        return f"{self.spec.name}{CONFIRM_SUFFIX}"


class Form:
    """A model form derived from the spec and the admin's declarations."""

    def __init__(
        self,
        spec: ModelSpec,
        admin: ModelAdmin,
        *,
        instance: Any = None,
        relation_choices: dict[str, tuple[Choice, ...]] | None = None,
        auth_settings: Any = None,
    ) -> None:
        self.spec = spec
        self.admin = admin
        self.instance = instance
        self._relation_choices = relation_choices or {}
        self._auth_settings = auth_settings
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
        widget = widget_for(spec) if writable else READONLY_WIDGET
        value = (
            getattr(self.instance, spec.name, None) if self.instance is not None else spec.default
        )
        # Blanked here, not only in `raw`: the read-only control renders `value`,
        # so masking one and not the other would print a password hash onto the
        # page for any field marked read-only.
        if spec.sensitive:
            value = None

        choices = spec.choices
        if spec.is_relation:
            choices = self._relation_choices.get(spec.name, ())

        return FormField(
            spec=spec,
            widget=widget,
            value=value,
            raw=_render(value, spec),
            choices=choices,
            selected=_selection(value, spec),
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

            if spec.type.is_multi_valued:
                if f"{spec.name}[]" not in data and spec.name not in data:
                    continue
                submitted = data.get(f"{spec.name}[]") or data.get(spec.name) or []
                values = submitted if isinstance(submitted, list) else [submitted]
                form_field.selected = tuple(str(item) for item in values)
                cleaned[spec.name] = [item for item in values if item not in ("", None)]
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


def _render(value: Any, spec: FieldSpec) -> str:
    """The string a control shows for a value.

    A sensitive field renders empty: echoing a stored secret back into a form is
    how it ends up in a browser cache or a screenshot.
    """
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
    return str(value)


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
