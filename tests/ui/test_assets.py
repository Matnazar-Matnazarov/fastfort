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
from fastfort._version import __version__
from fastfort.orm.sqlalchemy import SQLAlchemyBackend
from fastfort.ui.compression import available_encodings

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

#: What a page actually asks for. The version is in the path so that a release
#: cannot be served against a cached stylesheet from the one before it.
ASSETS = [
    f"/admin/static/{__version__}/fastfort.css",
    f"/admin/static/{__version__}/js/fastfort.js",
]

#: The addresses pages used before the version moved into the path. Still
#: served, because a project may have overridden `base.html`.
LEGACY_ASSETS = ["/admin/static/fastfort.css", "/admin/static/js/fastfort.js"]


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


# ---------------------------------------------------------------------------
# Cache busting
# ---------------------------------------------------------------------------


async def test_the_page_asks_for_the_stylesheet_by_version(client: httpx.AsyncClient) -> None:
    """The bug this exists for: an upgrade changed the HTML and left the URL of
    the stylesheet alone, so every browser and CDN holding yesterday's copy
    rendered the new page with the old rules -- for a day, on a live site, with
    nothing in any log.

    A version in the path is what makes the year-long cache below safe.
    """
    body = (await client.get("/admin/")).text

    assert f"/admin/static/{__version__}/fastfort.css" in body
    assert f"/admin/static/{__version__}/js/fastfort.js" in body
    assert 'href="/admin/static/fastfort.css"' not in body


async def test_the_sign_in_page_asks_by_version_too(backend: SQLAlchemyBackend) -> None:
    """The page built by `auth_views.py` rather than `site.py`, and the one the
    check above did not cover.

    Both files compose the static URL themselves, the version moved into it in
    only one of them, and nothing noticed: this test read `/admin/` and every
    other page in the suite is behind the gate. So the sign-in page went on
    asking for `/admin/static/fastfort.css` -- the one address whose bytes
    change underneath it -- and paid a conditional request for its stylesheet
    and its script on every load, on the page an admin serves to anyone who is
    not signed in yet.

    Signed *out*, deliberately: the shared client has a session, and the sign-in
    route redirects somebody who already has one away from a form they do not
    need -- which is a 303 with an empty body and an assertion that passes
    against nothing.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend)), base_url="http://testserver"
    ) as anonymous:
        body = (await anonymous.get("/admin/login")).text

    assert f"/admin/static/{__version__}/fastfort.css" in body
    assert 'href="/admin/static/fastfort.css"' not in body


@pytest.mark.parametrize("path", ASSETS)
async def test_a_versioned_asset_is_cached_for_a_year(client: httpx.AsyncClient, path: str) -> None:
    """`immutable`, because the bytes behind this exact URL can never change:
    the next release asks for a different one."""
    response = await client.get(path)

    assert response.status_code == 200
    assert "max-age=31536000" in response.headers["cache-control"]
    assert "immutable" in response.headers["cache-control"]


@pytest.mark.parametrize("path", LEGACY_ASSETS)
async def test_an_unversioned_asset_still_answers_but_is_never_reused_blindly(
    client: httpx.AsyncClient, path: str
) -> None:
    """This is the address whose contents change underneath it, so it has to be
    revalidated. `no-cache` costs one conditional request per page and cannot
    serve a stylesheet from before an upgrade."""
    response = await client.get(path)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_a_version_that_is_not_this_one_still_serves_this_one(
    client: httpx.AsyncClient,
) -> None:
    """The segment is a cache key, never a lookup. A stale page asking for an
    older version gets the current bytes rather than a 404 -- an admin with no
    stylesheet at all is worse than one with the wrong one, and nothing on disk
    is found by that string."""
    response = await client.get("/admin/static/0.0.1/fastfort.css")

    assert response.status_code == 200
    assert ".ff-app" in response.text
