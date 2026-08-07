"""The Tortoise half of the conformance suite's model set.

Deliberately the same shape as `tests/orm/models.py`: the same names, the same
columns, the same relations. That is what makes `test_conformance.py` able to
ask both backends the same questions and compare the answers -- if the two model
sets differed, a disagreement between the backends would be indistinguishable
from a disagreement between the fixtures.

Portability rules are followed here too. Every `CharField` has a length because
MySQL rejects one without, and the enum is a `CharEnumField` so the stored value
is the member's value on every database rather than an index that shifts when a
member is inserted.
"""

from __future__ import annotations

import enum

from tortoise import fields, models


class Status(enum.StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Category(models.Model):
    class Meta:
        table = "t_category"

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=120, unique=True)

    def __str__(self) -> str:
        return self.name


class Tag(models.Model):
    class Meta:
        table = "t_tag"

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=60, unique=True)

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    """Named `t_product` in the database to prove the key comes from the class,
    the same point `tests/orm/models.py` makes with `category_product`."""

    class Meta:
        table = "t_product"

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=200)
    description = fields.TextField(null=True)

    # Money is Decimal, never float; rendering it must not lose precision.
    price = fields.DecimalField(max_digits=12, decimal_places=2, default=0)
    stock = fields.BigIntField(default=0)
    weight = fields.FloatField(null=True)

    is_active = fields.BooleanField(default=True)
    status = fields.CharEnumField(Status, max_length=20, default=Status.DRAFT)

    # Every browser has a control for a time, and each of them draws a different
    # one. This is the column that proves the admin draws its own.
    opens_at = fields.TimeField(null=True)

    # Nullable so NULLS-last ordering can be exercised on every database.
    released_on = fields.DateField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    #: Detected as sensitive by name, and therefore never echoed into a form.
    api_secret = fields.CharField(max_length=64, null=True)

    category = fields.ForeignKeyField(
        "models.Category", null=True, related_name="products", on_delete=fields.SET_NULL
    )
    tags = fields.ManyToManyField("models.Tag", related_name="products")

    def __str__(self) -> str:
        return self.name


class Listing(models.Model):
    """A one-to-one whose key is on this side.

    The half that matters is the other one: `Product.listing` has no column on
    the product's own table, so the spec must offer neither sorting nor
    filtering by it.
    """

    class Meta:
        table = "t_listing"

    id = fields.IntField(primary_key=True)
    code = fields.CharField(max_length=40, unique=True)
    product = fields.OneToOneField("models.Product", null=True, related_name="listing")

    def __str__(self) -> str:
        return self.code


class StaffUser(models.Model):
    class Meta:
        table = "t_staff"

    id = fields.IntField(primary_key=True)
    email = fields.CharField(max_length=200, unique=True)
    hashed_password = fields.CharField(max_length=255, default="")
    is_active = fields.BooleanField(default=True)
    is_staff = fields.BooleanField(default=True)
    is_superuser = fields.BooleanField(default=False)

    def __str__(self) -> str:
        return self.email
