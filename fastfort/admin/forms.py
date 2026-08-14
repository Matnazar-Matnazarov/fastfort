"""Turning a `ModelSpec` into a form, and a submitted form back into values.

The write path is where an admin panel gets breached, so two rules hold here
without exception.

*The allow-list is the spec.* A field is writable only if `FieldSpec.editable` is
true and the `ModelAdmin` has not marked it read-only. A value submitted for
anything else is dropped and never reaches the adapter, whatever the request looked
like.

*Nothing is coerced silently.* A value that cannot be parsed into the column's
type is an error shown against that field, not a `None` written to the database.

Split into four modules once the column-types phase pushed this file past what
one should carry: this one keeps `Form`/`FormField`/`bind()`/`commit_files()` --
the two rules above and the state machine that enforces them. `widgets.py`
picks which control renders a field; `values.py` parses and renders the text
that control holds; `geo.py` is the geometry codec `values.py` calls into.
`widget_for` and `WIDGET_NAMES` are re-exported below so `fastfort.admin.forms`
stays a working import path -- this is a published library and those two names
are semi-public.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any

from fastfort.auth.passwords import hash_password, validate_password
from fastfort.spec import Choice, FieldSpec, FieldType

from .files import UploadedFile, check_upload, delete_upload, save_upload, stored_path
from .values import _identity, check_bounds, parse_value, range_parts, render_value
from .widgets import (
    CLEAR_SUFFIX,
    CONFIRM_SUFFIX,
    FILE_WIDGETS,
    READONLY_WIDGET,
    UPGRADED_WIDGETS,
    WIDGET_NAMES,
    bound_widget,
    canonical_widget,
    format_help,
    widget_for,
)

if TYPE_CHECKING:
    from fastfort.core.settings import MediaSettings
    from fastfort.spec import ModelSpec

    from .options import ModelAdmin

__all__ = ["WIDGET_NAMES", "Form", "FormField", "widget_for"]


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

    #: The three pieces a `RANGE` field's two-input control renders: the lower
    #: box's text, the upper box's text, and the bracket notation the bounds
    #: selector shows. Unused by every other widget -- `raw` alone is enough
    #: for a control that is one box, not two.
    range_lower: str = ""
    range_upper: str = ""
    range_bounds: str = "[)"

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
        return self.help_override is not None and self.widget in UPGRADED_WIDGETS

    @property
    def confirm_name(self) -> str:
        return f"{self.spec.name}{CONFIRM_SUFFIX}"

    @property
    def clear_name(self) -> str:
        return f"{self.spec.name}{CLEAR_SUFFIX}"

    @property
    def range_widget(self) -> str:
        """Which control renders each of a range's two boxes.

        A `daterange` gets `type="date"` from this, through the same table
        `widget_for` reads, so its boxes carry `data-ff-date` and pick up the
        calendar exactly the way a plain `DATE` column's box does.
        """
        if self.spec.bounds is None:
            return "text"
        return bound_widget(self.spec.bounds.bound)


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
        locked: frozenset[str] = frozenset(),
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
        # Names this one request may not write, on top of everything the spec
        # and the ModelAdmin already withheld. Narrowing only -- it can take a
        # name out of the writable set and can never put one in, which is what
        # keeps `FieldSpec.editable` the single source of truth for
        # mass assignment. See `admin/protection.py`.
        self._locked = locked
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

    def writable_field_names(self) -> frozenset[str]:
        """What this form may write: the admin's allow-list, minus the locked."""
        return self.admin.editable_field_names() - self._locked

    def _build(self, spec: FieldSpec) -> FormField:
        writable = spec.name in self.writable_field_names()
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
            # colour, not a string". Canonicalised once here, so an old name
            # from either source ("list", "point") never reaches the template
            # or `format_help` -- both of those only ever handle the current
            # name.
            widget = canonical_widget(self.admin.widget_override(spec) or widget_for(spec))
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

        # `MULTIRANGE` shares the "range" widget but stays a plain textarea (see
        # `range_control` in `_widgets.html`), so only a true `RANGE` needs its
        # value split into the two boxes and the bounds selector read it.
        range_lower, range_upper, range_bounds = (
            range_parts(value, spec) if spec.type is FieldType.RANGE else ("", "", "[)")
        )

        return FormField(
            spec=spec,
            widget=widget,
            value=value,
            raw=render_value(value, spec),
            choices=choices,
            selected=_selection(value, spec),
            remote=remote,
            # Some controls are a plain text box because the browser has nothing
            # better, so the box has to say what shape it wants. Without this a
            # duration field is an empty rectangle and the only way to learn the
            # format is to guess wrong and read the error.
            help_override=format_help(spec, widget) if not spec.help_text else None,
            range_lower=range_lower,
            range_upper=range_upper,
            range_bounds=range_bounds,
        )

    # -- binding ------------------------------------------------------------

    def bind(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse a submitted form, returning the values that may be written.

        Errors are collected per field so the form can be re-rendered with every
        problem visible at once, rather than one per round trip.
        """
        writable = self.writable_field_names()
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

            if spec.type is FieldType.RANGE:
                self._bind_range(form_field, data, cleaned)
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
                parsed = parse_value(raw, spec)
            except ValueError as exc:
                form_field.errors.append(str(exc))
                continue

            error = check_bounds(parsed, spec)
            if error:
                form_field.errors.append(error)
                continue

            cleaned[spec.name] = parsed
            form_field.value = parsed

        return cleaned

    def _bind_range(
        self, form_field: FormField, data: dict[str, Any], cleaned: dict[str, Any]
    ) -> None:
        """Reassemble the two boxes and the bounds selector into the single
        `[1, 10)`-shaped string `values.parse_value` actually parses.

        The form never submits `spec.name` itself for a `RANGE` -- the control
        is three fields, `{name}__lower`, `{name}__upper` and `{name}__bounds`
        -- so this runs instead of the generic scalar path above, not after it.
        Errors are still appended to `form_field.errors`, the same list the
        template reads off `spec.name`, so a bad range lands on the one field a
        project's own `form.add_error(name, ...)` would also reach.
        """
        spec = form_field.spec
        lower_key, upper_key, bounds_key = (
            f"{spec.name}__lower",
            f"{spec.name}__upper",
            f"{spec.name}__bounds",
        )
        if lower_key not in data and upper_key not in data:
            # Neither box was on the request at all -- the same "leave it
            # alone" rule the generic path applies when `spec.name` is absent.
            return

        lower_raw = str(data.get(lower_key) or "").strip()
        upper_raw = str(data.get(upper_key) or "").strip()
        notation = str(data.get(bounds_key) or "[)")
        form_field.range_lower = lower_raw
        form_field.range_upper = upper_raw
        form_field.range_bounds = notation

        if not lower_raw and not upper_raw:
            if spec.required:
                form_field.errors.append("This field is required.")
                return
            cleaned[spec.name] = None
            form_field.value = None
            return

        # `notation` came from a <select> offering exactly "[)", "(]", "[]" and
        # "()", but it is still request data -- a tampered value that is not
        # one of those simply fails `_RANGE_RE` inside `parse_value` below and
        # reports the same "Enter a range" error a typo would, rather than
        # reassembling into something the parser was not expecting.
        reassembled = f"{notation[:1]}{lower_raw}, {upper_raw}{notation[-1:]}"

        try:
            parsed = parse_value(reassembled, spec)
        except ValueError as exc:
            form_field.errors.append(str(exc))
            return

        error = check_bounds(parsed, spec)
        if error:
            form_field.errors.append(error)
            return

        cleaned[spec.name] = parsed
        form_field.value = parsed

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

            # Before anything is staged, so a refused upload is a message beside
            # the field rather than bytes that `commit_files` later writes.
            # An image field gets the narrower list: `photo` meaning "a picture"
            # and meaning "a zip archive" are different promises to the reader
            # looking at the thumbnail column.
            image = spec.type is FieldType.IMAGE
            problem = check_upload(
                upload.filename,
                upload.content,
                allowed=(
                    self._media.allowed_image_extensions
                    if image
                    else self._media.allowed_extensions
                ),
                kind="image" if image else "file",
            )
            if problem is not None:
                form_field.errors.append(problem)
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
# Relation helpers -- only `Form` itself needs these, unlike `values.py`'s
# scalar parse/render pair, which `options.py` and the tests reach for too.
# ---------------------------------------------------------------------------


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
