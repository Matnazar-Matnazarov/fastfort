"""A rich-text editor the project loads, started by FastFort.

`richtext_url` names the editor's script and the CSP admits its origin; with
`richtext_editor` set, FastFort serves the call that starts it, from its own
origin, after the editor's script. These are the server's half: the tags, the
order they arrive in, the policy that lets them run, and the textarea that is
still what submits.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.core.settings import UISettings
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"
EDITOR = "https://cdn.jsdelivr.net/npm/ckeditor5@49.0.0/dist/browser/ckeditor5.umd.js"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    formfield_overrides: ClassVar[dict[Any, str]] = {"description": "richtext"}


async def opened(backend: SQLAlchemyBackend, **ui: Any) -> AsyncIterator[httpx.AsyncClient]:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            security={"cookie_secure": False},  # type: ignore[arg-type]
            ui=ui,  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await sign_in(client)
        yield client


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async for ready in opened(backend, richtext_url=EDITOR, richtext_editor="ckeditor5"):
        yield ready


async def test_the_starter_follows_the_editor(client: httpx.AsyncClient) -> None:
    """Deferred scripts run in document order, so the starter must come after
    the editor it looks for, or it runs before `CKEDITOR` exists."""
    body = (await client.get("/admin/shop.product/1/")).text

    editor_at = body.index(f'src="{EDITOR}"')
    starter = re.search(r'<script\s+src="[^"]*/js/fastfort-richtext\.js"[^>]*>', body)
    assert starter, "the page should load the starter"
    assert starter.start() > editor_at
    assert 'data-editor="ckeditor5"' in starter.group(0)


async def test_the_textarea_is_still_what_submits(client: httpx.AsyncClient) -> None:
    body = (await client.get("/admin/shop.product/1/")).text

    assert re.search(r'<textarea[^>]*name="description"[^>]*data-ff-richtext', body, re.S)


async def test_the_starter_is_served_from_the_admin_itself(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/static/js/fastfort-richtext.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_only_the_editors_origin_is_admitted(client: httpx.AsyncClient) -> None:
    policy = (await client.get("/admin/")).headers["content-security-policy"]
    script_src = next(d for d in policy.split(";") if d.strip().startswith("script-src"))

    assert script_src.split() == ["script-src", "'self'", "https://cdn.jsdelivr.net"]


async def test_without_an_editor_name_nothing_is_started(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """The old contract: the script at `richtext_url` starts itself."""
    async for client in opened(backend, richtext_url=EDITOR):
        body = (await client.get("/admin/shop.product/1/")).text
    assert "fastfort-richtext.js" not in body


def test_an_editor_with_no_script_is_a_start_up_error() -> None:
    with pytest.raises(ValidationError, match="no editor script to start"):
        UISettings(richtext_editor="ckeditor5")
