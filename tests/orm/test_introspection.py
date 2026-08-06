"""Deriving a `ModelSpec` from a SQLAlchemy model.

Introspection needs no database connection, so these run once rather than per
backend. What they pin down is the translation itself: get a type or a constraint
wrong here and every layer above inherits the mistake.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from fastfort.core.exceptions import AdapterError
from fastfort.orm.sqlalchemy import introspect_model, is_sqlalchemy_model
from fastfort.spec import FieldType, ModelSpec

from .exotic_models import ExoticColumn, SpatialColumn
from .models import Category, Product, StockLevel


@pytest.fixture(scope="module")
def spec() -> ModelSpec:
    return introspect_model(Product, key="shop.product")


def test_a_non_mapped_class_is_reported() -> None:
    class Plain:
        pass

    assert not is_sqlalchemy_model(Plain)
    with pytest.raises(AdapterError, match="not a mapped SQLAlchemy class"):
        introspect_model(Plain, key="a.b")


def test_labels_come_from_the_class_not_the_table(spec: ModelSpec) -> None:
    """The table is `category_product`; the interface should still say "Product"."""
    assert spec.name == "Product"
    assert spec.verbose_name == "Product"
    assert spec.verbose_name_plural == "Products"


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("name", FieldType.STRING),
        ("description", FieldType.TEXT),
        ("price", FieldType.DECIMAL),
        ("stock", FieldType.BIGINT),
        ("weight", FieldType.FLOAT),
        ("is_active", FieldType.BOOLEAN),
        ("status", FieldType.ENUM),
        ("released_on", FieldType.DATE),
        ("created_at", FieldType.DATETIME),
        ("public_id", FieldType.UUID),
        ("attributes", FieldType.JSON),
        ("category", FieldType.FOREIGN_KEY),
        ("tags", FieldType.MANY_TO_MANY),
    ],
)
def test_column_types_are_classified(spec: ModelSpec, field: str, expected: FieldType) -> None:
    assert spec.field(field).type is expected


def test_decimal_scale_is_carried_through(spec: ModelSpec) -> None:
    """A price rendered without its scale is a bug report waiting to happen."""
    assert spec.field("price").decimal_places == 2


def test_string_length_is_carried_through(spec: ModelSpec) -> None:
    assert spec.field("name").max_length == 200


def test_enum_members_become_choices(spec: ModelSpec) -> None:
    choices = spec.field("status").choices
    assert [c.value for c in choices] == ["draft", "published", "archived"]
    assert [c.label for c in choices] == ["Draft", "Published", "Archived"]


def test_a_generated_primary_key_is_not_editable(spec: ModelSpec) -> None:
    """Offering the box invites someone to collide with an existing row."""
    identifier = spec.field("id")
    assert identifier.primary_key
    assert not identifier.editable
    assert "id" not in [f.name for f in spec.editable_fields]


def test_the_foreign_key_column_is_replaced_by_its_relation(spec: ModelSpec) -> None:
    """The admin should offer "Category", not a raw integer box."""
    assert spec.get("category_id") is None
    assert spec.field("category").relation is not None


def test_relations_point_at_registry_keys(spec: ModelSpec) -> None:
    assert spec.field("category").relation.target == "orm.category"  # type: ignore[union-attr]
    assert spec.field("tags").relation.is_list  # type: ignore[union-attr]


def test_relation_keys_follow_a_custom_resolver() -> None:
    """A project that overrides a model's key still gets working relations."""
    custom = introspect_model(
        Product, key="shop.product", resolve_key=lambda model: f"shop.{model.__name__.lower()}"
    )
    assert custom.field("category").relation.target == "shop.category"  # type: ignore[union-attr]


def test_nullability_drives_required(spec: ModelSpec) -> None:
    assert spec.field("name").required
    assert not spec.field("description").required
    # A column with a default is satisfied by the database, so the form need not
    # demand a value for it.
    assert not spec.field("price").required


def test_unique_constraints_are_visible() -> None:
    assert introspect_model(Category, key="shop.category").field("name").unique


def test_secret_looking_columns_are_marked_sensitive(spec: ModelSpec) -> None:
    """A safety net under an explicit declaration, not a replacement for one."""
    assert spec.field("api_secret").sensitive
    assert "api_secret" in spec.sensitive_fields
    assert not spec.field("name").sensitive


def test_long_text_gets_a_textarea(spec: ModelSpec) -> None:
    assert spec.field("description").widget == "textarea"
    assert spec.field("name").widget is None


def test_free_text_is_searchable_but_not_filterable(spec: ModelSpec) -> None:
    """Filtering on free text produces a dropdown with no useful facets."""
    assert "name" in spec.searchable_fields
    assert "name" not in spec.filterable_fields
    assert "is_active" in spec.filterable_fields
    assert "status" in spec.filterable_fields


def test_to_many_relations_are_not_sortable(spec: ModelSpec) -> None:
    assert "tags" not in spec.sortable_fields
    assert "category" in spec.sortable_fields


def test_composite_primary_keys_are_reported() -> None:
    spec = introspect_model(StockLevel, key="shop.stock_level")
    assert spec.primary_key == ("warehouse", "sku")
    assert spec.is_composite_key


def test_the_spec_is_json_serialisable(spec: ModelSpec) -> None:
    """A future JSON API renders from exactly these dictionaries."""
    payload = spec.to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# The type registry -- PostgreSQL-only types, introspected with no live database.
#
# `introspect_model` reads mapper metadata, not a connection, so a column
# declared with `postgresql.INET()` or a GeoAlchemy2 `Geometry` introspects the
# same on SQLite as it would against a real PostgreSQL server (confirmed by
# hand before relying on it here: these fixtures were never created in a
# database, and this whole module runs under the default SQLite backend).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def exotic() -> ModelSpec:
    return introspect_model(ExoticColumn, key="t.exotic")


@pytest.fixture(scope="module")
def spatial() -> ModelSpec:
    return introspect_model(SpatialColumn, key="t.spatial")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("blob", FieldType.BINARY),
        ("ip", FieldType.INET),
        ("network", FieldType.INET),  # CIDR shares INET's vocabulary slot.
        ("mac", FieldType.MACADDR),
        ("attributes", FieldType.HSTORE),
        ("price", FieldType.MONEY),
        ("flags", FieldType.BITS),
        ("permissions", FieldType.BITS),  # BIT(varying=True), i.e. VARBIT.
        ("search", FieldType.SEARCH_VECTOR),
        ("legacy_id", FieldType.INTEGER),  # OID, but a form still edits an int.
        ("nickname", FieldType.STRING),  # CITEXT, not TEXT -- see below.
        ("tags", FieldType.ARRAY),
        ("ratings", FieldType.ARRAY),  # postgresql.ARRAY, not sa.ARRAY.
        ("booking", FieldType.RANGE),
        ("bookings", FieldType.MULTIRANGE),
        ("page_numbers", FieldType.RANGE),
    ],
)
def test_new_column_types_are_classified(
    exotic: ModelSpec, field: str, expected: FieldType
) -> None:
    assert exotic.field(field).type is expected


def test_citext_is_not_rendered_as_a_textarea_by_type_alone() -> None:
    """CITEXT subclasses TEXT; without an explicit rule ahead of it, it would
    inherit TEXT's classification and get a 4-row box for one line of text."""
    assert ExoticColumn.__table__.columns["nickname"].type.__class__.__name__ == "CITEXT"


@pytest.mark.parametrize("field", ["blob", "search", "legacy_id"])
def test_read_only_types_override_the_widget(exotic: ModelSpec, field: str) -> None:
    """A raster, a search vector and an OID are not something a form can accept
    back -- the classification's widget wins over whatever the type would
    otherwise select."""
    assert exotic.field(field).widget == "readonly"


@pytest.mark.parametrize("field", ["blob", "search", "legacy_id"])
def test_read_only_types_are_also_not_editable(exotic: ModelSpec, field: str) -> None:
    """`widget="readonly"` alone used to leave `editable=True` -- harmless for
    a browser, since no `<input>` is ever drawn for a readonly widget, but
    `FieldSpec.editable` is documented as *the* mass-assignment boundary with
    "deliberately no second flag that could disagree with it" (`CLAUDE.md`),
    and `_writable()` only ever checks that flag. A hand-crafted POST for one
    of these field names reached `SQLAlchemyAdapter._apply` regardless of
    what the template drew -- not a data breach (the real column type
    rejects the string the write path hands it), but exactly the "control
    that 500s on save" the column-types phase's write path exists to avoid.
    """
    assert not exotic.field(field).editable
    assert not exotic.field(field).required


def test_a_raster_is_binary_and_read_only(spatial: ModelSpec) -> None:
    assert spatial.field("heatmap").type is FieldType.BINARY
    assert spatial.field("heatmap").widget == "readonly"
    assert not spatial.field("heatmap").editable


def test_a_geometry_column_carries_its_shape(spatial: ModelSpec) -> None:
    geometry = spatial.field("location").geometry
    assert geometry is not None
    assert geometry.kind == "POINT"
    assert geometry.srid == 4326
    assert geometry.geography is False
    assert geometry.spatial_index is True


def test_a_geography_column_is_flagged_as_such(spatial: ModelSpec) -> None:
    geometry = spatial.field("area").geometry
    assert geometry is not None
    assert geometry.kind == "POLYGON"
    assert geometry.geography is True
    assert geometry.spatial_index is False


@pytest.mark.parametrize(
    ("field", "bound", "multi"),
    [
        ("booking", FieldType.DATETIME, False),
        ("bookings", FieldType.DATETIME, True),
        ("page_numbers", FieldType.INTEGER, False),
    ],
)
def test_range_bounds_carry_the_endpoint_type(
    exotic: ModelSpec, field: str, bound: FieldType, multi: bool
) -> None:
    bounds = exotic.field(field).bounds
    assert bounds is not None
    assert bounds.bound is bound
    assert bounds.multi is multi


def test_an_array_item_spec_carries_the_element_type_and_a_humanised_label(
    exotic: ModelSpec,
) -> None:
    item = exotic.field("tags").item
    assert item is not None
    assert item.name == "tags[]"
    assert item.label == "Tags"
    assert item.type is FieldType.STRING
    assert item.max_length == 30


def test_an_identity_primary_key_is_not_editable(exotic: ModelSpec) -> None:
    """`Identity()` is Postgres's `GENERATED ... AS IDENTITY`; `column.computed`
    alone does not see it, and offering the box invites a collision same as any
    other database-generated key."""
    identifier = exotic.field("id")
    assert identifier.primary_key
    assert not identifier.editable


def test_a_check_constraint_becomes_a_min_and_a_max(exotic: ModelSpec) -> None:
    rating = exotic.field("rating")
    assert rating.min_value == 1
    assert rating.max_value == 5


def test_numeric_precision_and_scale_derive_a_max_value_with_no_check_constraint(
    exotic: ModelSpec,
) -> None:
    """`Numeric(6, 2)` holds at most 9999.99 -- catching that in the browser
    turns a database rejection into a field-level message."""
    price_cap = exotic.field("price_cap")
    assert price_cap.precision == 6
    assert price_cap.decimal_places == 2
    assert price_cap.min_value is None
    assert price_cap.max_value == Decimal("9999.99")


def test_the_exotic_spec_is_json_serialisable(exotic: ModelSpec, spatial: ModelSpec) -> None:
    """`GeometrySpec`, `RangeSpec` and a recursive `item` all have to survive a
    round trip through `json.dumps`, same as everything else in the spec."""
    for model_spec in (exotic, spatial):
        payload = model_spec.to_dict()
        assert json.loads(json.dumps(payload)) == payload
