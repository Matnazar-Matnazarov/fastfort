"""Tests for `FieldSpec`, `ModelSpec` and `ChangeSet`."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest

from fastfort.spec import (
    ChangeSet,
    Choice,
    FieldSpec,
    FieldType,
    ModelSpec,
    RelationSpec,
)
from fastfort.spec._json import jsonify
from fastfort.spec.changes import MASK


def string(name: str, **kwargs: object) -> FieldSpec:
    return FieldSpec(name=name, type=FieldType.STRING, label=name.title(), **kwargs)  # type: ignore[arg-type]


PRODUCT = ModelSpec(
    key="shop.product",
    name="Product",
    verbose_name="product",
    verbose_name_plural="products",
    primary_key=("id",),
    fields=(
        FieldSpec(name="id", type=FieldType.INTEGER, label="ID", primary_key=True, editable=False),
        string("name", searchable=True, required=True, nullable=False),
        FieldSpec(name="price", type=FieldType.DECIMAL, label="Price", decimal_places=2),
        FieldSpec(name="is_active", type=FieldType.BOOLEAN, label="Active", filterable=True),
        FieldSpec(
            name="category",
            type=FieldType.FOREIGN_KEY,
            label="Category",
            filterable=True,
            relation=RelationSpec(target="shop.category"),
        ),
        FieldSpec(
            name="tags",
            type=FieldType.MANY_TO_MANY,
            label="Tags",
            relation=RelationSpec(target="shop.tag", is_list=True),
        ),
        FieldSpec(name="secret", type=FieldType.PASSWORD, label="Secret", sensitive=True),
    ),
)


# ---------------------------------------------------------------------------
# FieldSpec
# ---------------------------------------------------------------------------


def test_relation_type_requires_a_relation_spec() -> None:
    with pytest.raises(ValueError, match="no RelationSpec"):
        FieldSpec(name="category", type=FieldType.FOREIGN_KEY, label="Category")


def test_relation_spec_requires_a_relation_type() -> None:
    with pytest.raises(ValueError, match="RelationSpec but type"):
        string("name", relation=RelationSpec(target="shop.tag"))


def test_required_and_nullable_is_rejected() -> None:
    """A contradiction here produces forms that reject values the database accepts."""
    with pytest.raises(ValueError, match="required and nullable"):
        string("name", required=True, nullable=True)


def test_required_and_nullable_is_allowed_with_a_database_default() -> None:
    assert string("name", required=True, nullable=True, has_db_default=True).required


def test_empty_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        string("")


def test_field_is_immutable_but_replaceable() -> None:
    original = string("name")
    with pytest.raises(AttributeError):
        original.editable = False  # type: ignore[misc]

    derived = original.replace(editable=False, label="Title")
    assert derived.editable is False
    assert derived.label == "Title"
    assert original.editable is True  # the adapter's spec is untouched


@pytest.mark.parametrize(
    ("field_type", "expected"),
    [
        (FieldType.FOREIGN_KEY, True),
        (FieldType.MANY_TO_MANY, True),
        (FieldType.STRING, False),
        (FieldType.JSON, False),
    ],
)
def test_relation_classification(field_type: FieldType, expected: bool) -> None:
    assert field_type.is_relation is expected


def test_multi_valued_classification() -> None:
    assert FieldType.MANY_TO_MANY.is_multi_valued
    assert FieldType.REVERSE_FK.is_multi_valued
    assert not FieldType.FOREIGN_KEY.is_multi_valued


def test_field_to_dict_is_json_safe() -> None:
    spec = FieldSpec(
        name="price",
        type=FieldType.DECIMAL,
        label="Price",
        min_value=Decimal("0.00"),
        default=Decimal("9.99"),
        choices=(Choice(value=Decimal("1.5"), label="One and a half"),),
    )
    payload = spec.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    # Precision is preserved by serialising decimals as strings, not floats.
    assert payload["default"] == "9.99"


# ---------------------------------------------------------------------------
# ModelSpec
# ---------------------------------------------------------------------------


def test_lookup_helpers() -> None:
    assert PRODUCT.field("name").label == "Name"
    assert PRODUCT.get("missing") is None
    assert "price" in PRODUCT
    assert [f.name for f in PRODUCT.select(("price", "name"))] == ["price", "name"]


def test_unknown_field_error_lists_what_is_available() -> None:
    with pytest.raises(KeyError, match="Available fields"):
        PRODUCT.field("nope")


def test_editable_fields_exclude_the_primary_key() -> None:
    assert "id" not in [f.name for f in PRODUCT.editable_fields]


def test_sortable_fields_exclude_multi_valued_relations() -> None:
    """Ordering by a to-many field multiplies rows; it is never what was meant."""
    assert "tags" not in PRODUCT.sortable_fields
    assert "category" in PRODUCT.sortable_fields


def test_searchable_and_filterable_views() -> None:
    assert PRODUCT.searchable_fields == ("name",)
    assert PRODUCT.filterable_fields == ("is_active", "category")


def test_sensitive_fields_are_collected() -> None:
    assert PRODUCT.sensitive_fields == frozenset({"secret"})


def test_duplicate_fields_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate fields"):
        ModelSpec(
            key="a.b",
            name="B",
            verbose_name="b",
            verbose_name_plural="bs",
            primary_key=("id",),
            fields=(string("id"), string("id")),
        )


def test_primary_key_must_reference_real_fields() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        ModelSpec(
            key="a.b",
            name="B",
            verbose_name="b",
            verbose_name_plural="bs",
            primary_key=("pk",),
            fields=(string("id"),),
        )


def test_model_without_a_primary_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="no primary key"):
        ModelSpec(
            key="a.b",
            name="B",
            verbose_name="b",
            verbose_name_plural="bs",
            primary_key=(),
            fields=(string("id"),),
        )


def test_composite_primary_keys_are_supported() -> None:
    spec = ModelSpec(
        key="shop.stock",
        name="Stock",
        verbose_name="stock",
        verbose_name_plural="stock",
        primary_key=("warehouse_id", "sku"),
        fields=(string("warehouse_id"), string("sku")),
    )
    assert spec.is_composite_key


def test_model_to_dict_is_json_safe() -> None:
    payload = PRODUCT.to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# ChangeSet
# ---------------------------------------------------------------------------


def test_only_changed_fields_are_recorded() -> None:
    changes = ChangeSet.between({"name": "a", "price": 1}, {"name": "b", "price": 1})
    assert changes.fields == ("name",)
    assert bool(changes)
    assert len(changes) == 1


def test_untouched_fields_are_not_invented() -> None:
    """A partial update records exactly the fields it submitted."""
    changes = ChangeSet.between({"name": "a", "price": 1}, {"name": "b"})
    assert changes.fields == ("name",)


def test_identical_states_produce_no_changes() -> None:
    assert not ChangeSet.between({"a": 1}, {"a": 1})


def test_sensitive_values_are_masked_but_still_reported() -> None:
    changes = ChangeSet.between(
        {"password": "old"}, {"password": "new"}, sensitive=frozenset({"password"})
    )
    entry = changes.to_dict()["changes"][0]
    assert entry == {"field": "password", "old": MASK, "new": MASK, "masked": True}


def test_field_order_can_be_pinned() -> None:
    changes = ChangeSet.between({"a": 1, "b": 2}, {"b": 3, "a": 4}, fields=("a", "b", "missing"))
    assert changes.fields == ("a", "b")


def test_changeset_to_dict_is_json_safe() -> None:
    changes = ChangeSet.between(
        {"when": None, "id": None},
        {"when": dt.datetime(2026, 7, 29, 12, 0, tzinfo=dt.UTC), "id": uuid.uuid4()},
    )
    payload = changes.to_dict()
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# jsonify
# ---------------------------------------------------------------------------


def test_jsonify_handles_unknown_types_without_raising() -> None:
    class Exotic:
        def __repr__(self) -> str:
            return "<exotic>"

    assert jsonify(Exotic()) == "<exotic>"


def test_jsonify_preserves_decimal_precision() -> None:
    assert jsonify(Decimal("0.10")) == "0.10"


def test_jsonify_recurses_into_containers() -> None:
    assert jsonify({"a": [Decimal("1"), {"b": uuid.UUID(int=0)}]}) == {
        "a": ["1", {"b": "00000000-0000-0000-0000-000000000000"}]
    }


def test_changeset_supports_iteration_and_membership() -> None:
    changes = ChangeSet.between({"a": 1}, {"a": 2})
    assert [c.field for c in changes] == ["a"]
    assert "a" in changes
    assert "b" not in changes


def test_model_spec_is_iterable() -> None:
    assert [f.name for f in PRODUCT][:2] == ["id", "name"]


def test_model_spec_rejects_an_empty_key_or_no_fields() -> None:
    with pytest.raises(ValueError, match="key must not be empty"):
        ModelSpec("", "B", "b", "bs", (string("id"),), ("id",))
    with pytest.raises(ValueError, match="no fields"):
        ModelSpec("a.b", "B", "b", "bs", (), ("id",))


def test_field_derived_properties() -> None:
    assert PRODUCT.field("category").is_relation
    assert PRODUCT.field("name").is_text_like
    assert not PRODUCT.field("price").is_text_like
    # Passwords, files and JSON are never dumped straight into a list column.
    assert not PRODUCT.field("secret").displayable
    assert PRODUCT.field("name").displayable


def test_jsonify_covers_the_remaining_scalar_types() -> None:
    assert jsonify(1.5) == 1.5
    assert jsonify(dt.timedelta(minutes=2)) == 120.0
    assert jsonify(b"hi") == "hi"
    assert jsonify(dt.date(2026, 7, 29)) == "2026-07-29"
    assert jsonify(FieldType.STRING) == "string"
