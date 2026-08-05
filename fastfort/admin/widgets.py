"""Which control renders a field, and the vocabulary of control names.

Split out of `forms.py` when the column-types phase needed to add nine more
`FieldType`s to the map it already held -- see that module's docstring for the
two rules the split did not change. Everything here is presentation-only: no
parsing, no validation. `values.py` owns the shape a control's text has to
satisfy; this module only decides which control shows it.
"""

from __future__ import annotations

from fastfort.spec import FieldSpec, FieldType

__all__ = [
    "CLEAR_SUFFIX",
    "CONFIRM_SUFFIX",
    "FILE_WIDGETS",
    "READONLY_WIDGET",
    "UPGRADED_WIDGETS",
    "WIDGET_NAMES",
    "format_help",
    "register_widget",
    "widget_for",
]

#: A field type with no widget is shown but never written, so an exotic column
#: degrades to a read-only row instead of blocking the whole form.
READONLY_WIDGET = "readonly"

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
    # -- Column types (Phase 2) ---------------------------------------------
    # Step 5 of the phase names a dedicated control for each of these --
    # "inet", "mac", "keyvalue", "money", "bits", "range" -- for Phase 3 to
    # draw, along with the CSS and JS that go with it. They cannot be used as
    # the widget name yet: form.html's generic-input branch does
    # `type="{{ input_types[field.widget] }}"`, and under Jinja's
    # `StrictUndefined` that raises `UndefinedError` for any name the
    # `INPUT_TYPES` dict in `site.py` does not carry -- confirmed empirically
    # against a live render, not assumed. Naming the widget "inet" here today
    # would 500 the page the first time anyone opened a column of that type.
    # So each routes through a control the template already renders safely:
    # "text" for the single-line shapes, "textarea" for the two that are
    # naturally multi-line (one hstore pair, or one range, per line) -- a
    # single-line `<input>` cannot even hold a newline a person typed, since
    # Enter submits the form instead. `format_help` below gives each its own
    # hint despite the shared widget name, keyed by `FieldType` rather than by
    # widget so a plain STRING or TEXT column sharing the same control does
    # not get someone else's format hint.
    FieldType.INET: "text",
    FieldType.MACADDR: "text",
    FieldType.MONEY: "text",
    FieldType.BITS: "text",
    FieldType.RANGE: "text",
    FieldType.HSTORE: "textarea",
    FieldType.MULTIRANGE: "textarea",
    # Never editable -- see `types.py` for why (a raster or a full-text index
    # is not something a form can round-trip), so the widget only has to name
    # that honestly rather than fall back to "unknown".
    FieldType.BINARY: READONLY_WIDGET,
    FieldType.SEARCH_VECTOR: READONLY_WIDGET,
}

#: Every control the form template can render, plus whatever a project has
#: added with `register_widget`. `ModelAdmin.formfield_overrides` is checked
#: against this at declaration time, so a typo is a start-up error rather than
#: a field that quietly renders read-only. A `set`, not a `frozenset`: mirrors
#: `types.py`'s `_RULES` list, which `register_type` grows the same way, for a
#: project that ships its own control and the template partial to go with it.
WIDGET_NAMES: set[str] = {
    *_WIDGETS.values(),
    "nullboolean",
    "color",
    "richtext",
    "readonly",
    "file",
    "image",
}

#: Widgets whose submitted value is a file rather than text.
FILE_WIDGETS = frozenset({"file", "image"})

#: Suffix of the confirmation input paired with a password control.
CONFIRM_SUFFIX = "__confirm"

#: Suffix of the checkbox that removes a file or image without replacing it.
CLEAR_SUFFIX = "__clear"

#: Help text for the controls the browser has no native input for, keyed by
#: widget name. Safe only while a widget is still unique to one field type --
#: `_TYPE_FORMAT_HELP` below is the equivalent for the types Phase 2 could not
#: give a widget name of their own without crashing the template (see
#: `_WIDGETS`), and has to be keyed by type instead for exactly that reason.
_FORMAT_HELP: dict[str, str] = {
    "duration": "Length of time, as HH:MM:SS — or 2d HH:MM:SS for more than a day.",
    "list": "Separate values with a comma.",
    "point": "Latitude and longitude, as 41.2995, 69.2401.",
}

#: Keyed by `FieldType` rather than by widget name -- INET, MACADDR, MONEY,
#: BITS and RANGE all share the plain "text" widget, and HSTORE and
#: MULTIRANGE both share "textarea", so a widget-keyed lookup would give every
#: field on the shared control the same hint, including the STRING and TEXT
#: fields that should get none at all.
_TYPE_FORMAT_HELP: dict[FieldType, str] = {
    FieldType.INET: "An IP address or network, as 192.168.1.5 or 10.0.0.0/8.",
    FieldType.MACADDR: "A MAC address, as aa:bb:cc:dd:ee:ff.",
    FieldType.HSTORE: "One key: value pair per line.",
    FieldType.MONEY: "An amount, as 1234.56.",
    FieldType.BITS: "0s and 1s only.",
    FieldType.RANGE: "A range, as [1, 10) — or (, 5] for an unbounded low end.",
    FieldType.MULTIRANGE: "One range per line, as [1, 10).",
}

#: Widgets whose format hint is script-only, because script replaces the box
#: that needed explaining with a control that cannot be filled in wrongly.
#: Telling somebody to type `HH:MM:SS` next to four labelled number boxes is
#: instructions for a control that is no longer on the page.
UPGRADED_WIDGETS = frozenset({"duration"})


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


def format_help(spec: FieldSpec, widget: str) -> str | None:
    """The format hint for a control with no native input, or `None`.

    `Form._build` only asks when the admin declared no `help_text` of its own
    -- a project's own words always win over this fallback.
    """
    return _FORMAT_HELP.get(widget) or _TYPE_FORMAT_HELP.get(spec.type)


def register_widget(name: str) -> None:
    """Let a project add a widget name `ModelAdmin.formfield_overrides` will
    accept.

    Mirrors `types.py`'s `register_type`: a project that ships its own control
    -- and the template partial to go with it, since `renderer.py` searches a
    project's own template directory before the package's -- needs a way past
    the declaration-time check without forking this module.
    """
    WIDGET_NAMES.add(name)
