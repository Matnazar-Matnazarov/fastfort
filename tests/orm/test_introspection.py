"""Deriving a `ModelSpec` from a SQLAlchemy model.

Introspection needs no database connection, so these run once rather than per
backend. What they pin down is the translation itself: get a type or a constraint
wrong here and every layer above inherits the mistake.
"""

from __future__ import annotations

import pytest

from fastfort.core.exceptions import AdapterError
from fastfort.orm.sqlalchemy import introspect_model, is_sqlalchemy_model
from fastfort.spec import FieldType, ModelSpec

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
    import json

    payload = spec.to_dict()
    assert json.loads(json.dumps(payload)) == payload
