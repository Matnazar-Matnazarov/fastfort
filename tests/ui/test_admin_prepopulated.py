"""`prepopulated_fields` and `empty_value_display`: two Django conveniences.

The slug box is filled by the browser, so what the server owes it is the
attribute naming its sources -- on the add form, and never on a change form,
where rewriting a published slug would break every link to the row. The script
side is exercised by `slugify` being run under Node when Node is present.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Category, Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.exceptions import ConfigurationError
from fastfort.core.registry import default_model_key
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.orm.sqlalchemy.introspect import introspect_model
from fastfort.ui.renderer import EMPTY, display_value

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"
SCRIPT = Path(__file__).resolve().parents[2] / "fastfort/ui/static/js/fastfort.js"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name", "description", "released_on")
    readonly_fields = ("released_on",)
    # Back to front on purpose -- the test models have no slug column, and the
    # option cares only that the target is a writable text box and the source
    # is a field. A textarea as the source is the case worth proving.
    prepopulated_fields: ClassVar[dict[str, tuple[str, ...]]] = {"name": ("description",)}
    empty_value_display = "Not yet"


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            project_name="Test Shop",
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    fort.register(Category, admin.ModelAdmin, key="shop.category")

    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


def control(body: str, name: str) -> str:
    found = re.search(rf'<(?:input|textarea)[^>]*\bname="{name}"[^>]*>', body, re.S)
    assert found, f"the form should render {name!r}"
    return found.group(0)


# ---------------------------------------------------------------------------
# prepopulated_fields
# ---------------------------------------------------------------------------


async def test_the_add_form_names_the_sources(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/add")).text

    assert 'data-ff-prepopulate="description"' in control(body, "name")


async def test_a_change_form_never_rewrites_a_published_slug(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/")).text

    assert "data-ff-prepopulate" not in body


@pytest.mark.parametrize(
    ("declared", "message"),
    [
        ({"nope": ("name",)}, "prepopulated_fields names 'nope'"),
        ({"description": ("nope",)}, "reads 'nope'"),
        ({"description": "name"}, "should be a tuple of field names"),
        ({"price": ("name",)}, "not a writable text field"),
        ({"description": ("name",)}, "not a writable text field"),
    ],
)
def test_a_wrong_declaration_is_a_start_up_error(declared: dict[str, object], message: str) -> None:
    spec = introspect_model(Product, key=default_model_key(Product))
    wrong = type("Wrong", (admin.ModelAdmin,), {"prepopulated_fields": declared})

    with pytest.raises(ConfigurationError, match=re.escape(message)):
        wrong(spec)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
@pytest.mark.parametrize(
    ("typed", "limit", "slug"),
    [
        ("O'zbekiston Respublikasi", 0, "ozbekiston-respublikasi"),
        ("Crème Brûlée — 2026!", 0, "creme-brulee-2026"),
        ("  Ring B (2026-11-08) ", 0, "ring-b-2026-11-08"),
        ("Привет мир", 0, "привет-мир"),
        ("aaaa bbbb cccc", 9, "aaaa-bbbb"),
    ],
)
def test_the_script_slugifies_like_a_person_would(typed: str, limit: int, slug: str) -> None:
    """Run the script's own `slugify` rather than a Python copy of it: a copy
    would pass while the shipped function drifted."""
    source = SCRIPT.read_text(encoding="utf-8")
    found = re.search(r"const slugify = [\s\S]*?\n  };\n", source)
    assert found, "fastfort.js should define slugify"
    program = found.group(0) + f"process.stdout.write(slugify({json.dumps(typed)}, {limit}));"
    result = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True, timeout=20
    )

    assert result.stdout == slug


# ---------------------------------------------------------------------------
# empty_value_display
# ---------------------------------------------------------------------------


async def test_the_list_prints_the_admins_word_for_nothing(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/")).text

    assert "Not yet" in body


async def test_a_read_only_field_prints_it_too(client: httpx.AsyncClient) -> None:
    """Product 3 was never released, and `released_on` is read-only here."""
    body = (await client.get("/admin/shop.product/3/")).text

    assert re.search(r'id="ff-released_on">\s*Not yet\s*<', body)


def test_the_filter_falls_back_to_the_dash() -> None:
    assert display_value(None) == EMPTY
    assert display_value("", "Never") == "Never"
    assert display_value([], "None") == "None"
    assert display_value(0, "Never") == "0"
