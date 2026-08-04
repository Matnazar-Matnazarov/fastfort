"""How the admin's own CSS and JavaScript come down the wire.

Brotli where the browser reads it, gzip where it does not, and the bytes
themselves for anything that asks for neither. The stylesheet and the script are
the two largest things the admin serves and they are on every page, so this is
where compression is worth anything at all.

The HTML deliberately does not go through it, and that is what the last test
here is about: a page carrying a CSRF token *and* text the request chose is the
BREACH side channel, and the way not to have it is not to compress those.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.ui.compression import available_encodings

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

ASSETS = ["/admin/static/fastfort.css", "/admin/static/js/fastfort.js"]


def build(backend: SQLAlchemyBackend, *, debug: bool = False) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            debug=debug,
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, admin.ModelAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


# ---------------------------------------------------------------------------
# Over the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ASSETS)
async def test_an_asset_is_compressed_and_still_arrives_intact(
    client: httpx.AsyncClient, path: str
) -> None:
    """httpx decodes the body, so `text` being right is the round trip."""
    plain = await client.get(path, headers={"Accept-Encoding": "identity"})
    compressed = await client.get(path, headers={"Accept-Encoding": "gzip, deflate, br"})

    assert compressed.headers["content-encoding"] in available_encodings()
    assert compressed.text == plain.text
    assert "content-encoding" not in plain.headers


@pytest.mark.parametrize("path", ASSETS)
async def test_an_asset_varies_on_the_encoding(client: httpx.AsyncClient, path: str) -> None:
    """Without this a cache in front of the admin serves one browser's Brotli to
    another browser that cannot read it."""
    response = await client.get(path, headers={"Accept-Encoding": "gzip"})

    assert response.headers["vary"] == "Accept-Encoding"


async def test_an_old_client_gets_gzip_rather_than_nothing(client: httpx.AsyncClient) -> None:
    response = await client.get(ASSETS[0], headers={"Accept-Encoding": "gzip, deflate"})

    assert response.headers["content-encoding"] == "gzip"


async def test_debug_serves_them_uncompressed(
    backend: SQLAlchemyBackend, staff_user: StaffUser
) -> None:
    """The files are changing under a reload, and the saving is on a request to
    localhost."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, debug=True)),
        base_url="http://testserver",
    ) as client:
        response = await client.get(ASSETS[0], headers={"Accept-Encoding": "br, gzip"})

    assert "content-encoding" not in response.headers


async def test_a_page_is_not_compressed(client: httpx.AsyncClient) -> None:
    """A rendered page carries a CSRF token and text the request chose -- a
    search term, a filter value. Compressing a response holding both is the
    BREACH side channel: the compressed length leaks how much of a guess at the
    secret matched. The admin's routes do not offer that opportunity, and a
    proxy in front of one should be configured the same way.
    """
    response = await client.get(
        "/admin/shop.product/", headers={"Accept-Encoding": "gzip, deflate, br"}
    )

    assert response.status_code == 200
    assert "content-encoding" not in response.headers
