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
    "bound_widget",
    "canonical_widget",
    "format_help",
    "register_widget",
    "widget_for",
]

#: A field type with no widget is shown but never written, so an exotic column
#: degrades to a read-only row instead of blocking the whole form.
READONLY_WIDGET = "readonly"

#: Which control renders each field type. The template switches on this name, so
#: adding a widget is a template branch plus one entry here.
#:
#: `RANGE` and `MULTIRANGE` share one name: both need the bound type to pick
#: their input type, and `MULTIRANGE` additionally collapses to a plain
#: textarea inside that one control -- see `_widgets.html`'s `range_control`
#: macro, which is what actually tells the two apart, by `FieldType` rather
#: than by widget name.
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
    FieldType.ARRAY: "tags",
    FieldType.VECTOR: "textarea",
    FieldType.GEOMETRY: "geometry",
    FieldType.UUID: "text",
    FieldType.JSON: "json",
    FieldType.ENUM: "select",
    FieldType.EMAIL: "email",
    FieldType.URL: "url",
    FieldType.PASSWORD: "password",
    FieldType.FOREIGN_KEY: "relation",
    FieldType.ONE_TO_ONE: "relation",
    FieldType.MANY_TO_MANY: "relations",
    # -- Column types (Phase 3) ----------------------------------------------
    # Phase 2 parked all seven of these on "text"/"textarea" because the
    # template's generic-input branch did `input_types[field.widget]` under
    # Jinja's `StrictUndefined`, which raises for any name `INPUT_TYPES` in
    # `site.py` does not carry -- confirmed empirically against a live render.
    # That branch now falls back to a plain text box for an unrecognised name
    # instead of raising (`input_types.get(field.widget, "text")`), so these
    # can carry their real names and their own controls in `_widgets.html`.
    FieldType.INET: "inet",
    FieldType.MACADDR: "mac",
    FieldType.MONEY: "money",
    FieldType.BITS: "bits",
    FieldType.RANGE: "range",
    FieldType.HSTORE: "keyvalue",
    FieldType.MULTIRANGE: "range",
    # Never editable -- see `types.py` for why (a raster or a full-text index
    # is not something a form can round-trip), so the widget only has to name
    # that honestly rather than fall back to "unknown".
    FieldType.BINARY: READONLY_WIDGET,
    FieldType.SEARCH_VECTOR: READONLY_WIDGET,
}

#: Old widget names accepted from `formfield_overrides` (or from a `FieldSpec`
#: an adapter built before this phase) so a project's `{"keywords": "list"}`,
#: written before `ARRAY` had a widget of its own, keeps working rather than
#: raising a `ConfigurationError` on upgrade. Resolved once, by
#: `canonical_widget`, so the template and `format_help` only ever see the
#: current name.
_ALIASES: dict[str, str] = {"list": "tags", "point": "geometry"}


#: Every control the form template can render, plus whatever a project has
#: added with `register_widget`. `ModelAdmin.formfield_overrides` is checked
#: against this at declaration time, so a typo is a start-up error rather than
#: a field that quietly renders read-only. A `set`, not a `frozenset`: mirrors
#: `types.py`'s `_RULES` list, which `register_type` grows the same way, for a
#: project that ships its own control and the template partial to go with it.
#: The old alias names stay members too -- they have to keep validating even
#: though nothing downstream is ever asked to render one.
WIDGET_NAMES: set[str] = {
    *_WIDGETS.values(),
    *_ALIASES,
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
#: `_TYPE_FORMAT_HELP` below is the equivalent for a type sharing its widget
#: with others (`RANGE`/`MULTIRANGE` both render "range"), and geometry's hint
#: additionally depends on the column's `kind`, so it is computed by
#: `_geometry_help` instead of living in either dict.
_FORMAT_HELP: dict[str, str] = {
    "duration": "Length of time, as HH:MM:SS — or 2d HH:MM:SS for more than a day.",
    "tags": "Separate values with a comma.",
}

#: Keyed by `FieldType` rather than by widget name -- HSTORE and MULTIRANGE
#: both share a widget with something else, so a widget-keyed lookup would
#: give every field on the shared control the same hint. `RANGE` itself is
#: absent on purpose: its two boxes and its bounds selector are self-explaining
#: the way a labelled `<input type="date">` is, and repeating "[1, 10)" next to
#: a control nobody has to type a bracket into would be explaining a syntax
#: the field no longer has.
_TYPE_FORMAT_HELP: dict[FieldType, str] = {
    FieldType.INET: "An IP address or network, as 192.168.1.5 or 10.0.0.0/8.",
    FieldType.MACADDR: "A MAC address, as aa:bb:cc:dd:ee:ff.",
    FieldType.HSTORE: "One key: value pair per line.",
    FieldType.MONEY: "An amount, as 1234.56.",
    FieldType.BITS: "0s and 1s only.",
    FieldType.MULTIRANGE: "One range per line, as [1, 10) — or (, 5] for an unbounded low end.",
}

#: The geometry hint depends on the column's `kind`: "latitude and longitude"
#: is exactly right for a POINT and actively misleading above a POLYGON, whose
#: WKT does not fit the shape that sentence describes at all. Keyed by the
#: upper-cased kind string rather than by a widget or a `FieldType`, since
#: every kind shares both.
_GEOMETRY_HELP: dict[str, str] = {
    "POINT": "Latitude and longitude, as 41.2995, 69.2401 — or WKT or GeoJSON.",
    "LINESTRING": "A line, as WKT — LINESTRING(69.2 41.3, 69.3 41.2) — or GeoJSON.",
    "POLYGON": "A polygon, as WKT — POLYGON((69.2 41.3, 69.3 41.3, 69.3 41.2, 69.2 41.3)) — "
    "or GeoJSON.",
    "MULTIPOINT": "Several points, as WKT — MULTIPOINT((69.2 41.3), (69.3 41.2)) — or GeoJSON.",
    "MULTILINESTRING": "Several lines, as WKT — MULTILINESTRING((...), (...)) — or GeoJSON.",
    "MULTIPOLYGON": "Several polygons, as WKT — MULTIPOLYGON(((...)), ((...))) — or GeoJSON.",
    "GEOMETRYCOLLECTION": "A mix of shapes, as WKT — GEOMETRYCOLLECTION(POINT(...), ...) — "
    "or GeoJSON.",
    "GEOMETRY": "A shape as WKT, EWKT or GeoJSON — or, for a point, latitude and longitude.",
}


def _geometry_help(spec: FieldSpec) -> str:
    kind = spec.geometry.kind.upper() if spec.geometry else "GEOMETRY"
    return _GEOMETRY_HELP.get(kind, _GEOMETRY_HELP["GEOMETRY"])


#: Widgets whose format hint is script-only, because script replaces the box
#: that needed explaining with a control that cannot be filled in wrongly.
#: Telling somebody to type `HH:MM:SS` next to four labelled number boxes is
#: instructions for a control that is no longer on the page.
UPGRADED_WIDGETS = frozenset({"duration"})


def canonical_widget(name: str) -> str:
    """Resolve an old widget name to its current one; anything else is
    returned unchanged.

    Applied once, where a widget name is decided (`Form._build`), so the
    template, `format_help` and every other consumer downstream only ever see
    the name a widget renders under today. `WIDGET_NAMES` still contains the
    old names so `formfield_overrides = {"keywords": "list"}` keeps passing
    its declaration-time check -- only the *rendering* is redirected.
    """
    return _ALIASES.get(name, name)


def bound_widget(field_type: FieldType) -> str:
    """The control name for one endpoint of a `RANGE`/`MULTIRANGE`.

    Reuses the same table `widget_for` reads from, so a `daterange`'s two
    boxes render `type="date"` and pick up the calendar exactly the way a
    plain `DATE` column's box does -- one table of "which control for which
    type" rather than a second one that could drift from it.
    """
    return _WIDGETS.get(field_type, "text")


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
    -- a project's own words always win over this fallback. `widget` is
    assumed already canonical (`Form._build` resolves aliases before calling
    this), so an old name like "point" is never looked up here.
    """
    if spec.type is FieldType.GEOMETRY:
        return _geometry_help(spec)
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
