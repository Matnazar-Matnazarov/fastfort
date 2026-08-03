"""A file or image field: the widget, the upload, and where it ends up.

`FieldType.FILE`/`FieldType.IMAGE` were declared in the spec layer with nothing
behind them -- no introspection, no widget, no route. This is that: a plain
string column (a path, never bytes, exactly like `brand_colour` holds a hex
string rather than being a colour column) opted into the widget through
`formfield_overrides`, the same mechanism `color`/`richtext`/`point` already use.

What is worth guarding carefully here is the part any file upload feature lives
or dies on: the stored path is server-generated, never the browser's own
filename, and the route that serves a file back is gated and cannot be walked
out of with `../`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import ClassVar

import httpx
import pytest
from fastapi import FastAPI
from tests.conftest import sign_in
from tests.orm.models import Product, StaffUser

from fastfort import FastFort, FastFortSettings, admin
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"

    # `attachment` is a plain `String` column, same as `brand_colour` elsewhere.
    # `widget_for(FieldType.FILE) == "file"` is covered in
    # tests/unit/test_exotic_columns.py; this override drives the same
    # template branch a project's own model would.
    formfield_overrides: ClassVar[dict[str, str]] = {"attachment": "file"}


def build(backend: SQLAlchemyBackend, media_root: Path, **media: object) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            media={"root": media_root, **media},  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, ProductAdmin, key="shop.product")

    app = FastAPI()
    fort.mount(app)
    return app


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    """Its own subdirectory, not `tmp_path` itself -- the `backend` fixture
    already keeps the sqlite file there, and stray files under `tmp_path`
    would otherwise be counted as uploads by any test that lists what has been
    stored."""
    root = tmp_path / "media"
    root.mkdir()
    return root


@pytest.fixture
async def client(
    backend: SQLAlchemyBackend, staff_user: StaffUser, media_root: Path
) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=build(backend, media_root)), base_url="http://testserver"
    ) as opened:
        await sign_in(opened)
        yield opened


async def form_body(client: httpx.AsyncClient, path: str = "/admin/shop.product/1/") -> str:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    return response.text


async def token(client: httpx.AsyncClient, path: str) -> str:
    match = re.search(r'name="_csrf" value="([^"]+)"', await form_body(client, path))
    assert match
    return match.group(1)


def other_fields(body: str) -> dict[str, str]:
    """Every other value already on the form, so a save can round-trip them.

    A checkbox's `value` attribute is what it submits *if* it is checked, not
    proof that it is -- an unchecked box contributes nothing to a real
    submission. The "Clear" checkbox beside a file field is exactly this shape,
    and picking up every `value=` on the page regardless would tick it on every
    resubmission this helper builds, silently clearing the file each time.
    """
    fields: dict[str, str] = {}
    for tag in re.findall(r"<input\b[^>]*>", body):
        name = re.search(r'name="([^"]+)"', tag)
        if not name:
            continue
        if 'type="checkbox"' in tag and "checked" not in tag:
            continue
        value = re.search(r'value="([^"]*)"', tag)
        if value:
            fields[name.group(1)] = value.group(1)
    return fields


def _stored_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


async def stored_files(root: Path) -> list[Path]:
    """Everything on disk under `root`, off the event loop -- these are test
    assertions, not application code, but the filesystem calls are still
    blocking ones made from inside a coroutine."""
    return await asyncio.to_thread(_stored_files, root)


# ---------------------------------------------------------------------------
# The widget
# ---------------------------------------------------------------------------


async def test_a_file_field_renders_a_file_input(client: httpx.AsyncClient) -> None:
    body = await form_body(client)
    box = re.search(r'<input[^>]*\bname="attachment"[^>]*>', body)

    assert box, "the field should render an input"
    assert 'type="file"' in box.group(0)


async def test_an_empty_file_field_has_no_current_value_shown(client: httpx.AsyncClient) -> None:
    """Nothing has been uploaded to the seeded row, so there is nothing to
    link to and no "Clear" checkbox for a value that does not exist."""
    body = await form_body(client)

    assert "ff-file-current" not in body


async def test_the_form_declares_multipart_when_it_has_a_file_field(
    client: httpx.AsyncClient,
) -> None:
    body = await form_body(client)
    tag = re.search(r'<form\b[^>]*action="[^"]*shop\.product[^"]*"[^>]*>', body)

    assert tag
    assert 'enctype="multipart/form-data"' in tag.group(0)


async def test_an_ordinary_form_stays_url_encoded(
    backend: SQLAlchemyBackend, staff_user: StaffUser, media_root: Path
) -> None:
    """Every other form on the site has nothing that could be a file, and
    should not carry the encoding that exists for the one that does."""

    class PlainAdmin(admin.ModelAdmin):
        list_display = ("id", "name")
        verbose_name_plural = "Products"

    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            media={"root": media_root},  # type: ignore[arg-type]
            security={"cookie_secure": False},  # type: ignore[arg-type]
        ),
        backend=backend,
    )
    fort.set_user_model(StaffUser)
    fort.register(Product, PlainAdmin, key="shop.product")
    app = FastAPI()
    fort.mount(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as plain_client:
        await sign_in(plain_client)
        body = await form_body(plain_client)

    tag = re.search(r'<form\b[^>]*action="[^"]*shop\.product[^"]*"[^>]*>', body)
    assert tag
    assert "enctype" not in tag.group(0)


# ---------------------------------------------------------------------------
# Uploading
# ---------------------------------------------------------------------------


async def test_uploading_a_file_stores_it_and_serves_it_back(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))
    csrf = await token(client, path)

    response = await client.post(
        path,
        data={**fields, "_csrf": csrf},
        files={"attachment": ("notes.txt", b"hello from a test", "text/plain")},
    )
    assert response.status_code == 303, response.text

    body = await form_body(client, path)
    link = re.search(r'href="([^"]*/media/[^"]+)"', body)
    assert link, "the stored file should be linked from the form"

    # And what is on disk is namespaced by model and field, not the filename
    # verbatim -- two uploads called `notes.txt` must not collide.
    stored = await stored_files(media_root)
    assert len(stored) == 1
    assert await asyncio.to_thread(stored[0].read_bytes) == b"hello from a test"
    assert "shop.product" in stored[0].as_posix()
    assert "attachment" in stored[0].as_posix()
    assert stored[0].name != "notes.txt"

    served = await client.get(link.group(1))
    assert served.status_code == 200
    assert served.content == b"hello from a test"


async def test_leaving_the_input_empty_keeps_the_stored_file(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """A file input cannot be prefilled, so an untouched one submits an empty
    file -- which must mean "nothing changed", not "remove it"."""
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))
    upload = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("first.txt", b"first", "text/plain")},
    )
    assert upload.status_code == 303, upload.text

    fields = other_fields(await form_body(client, path))
    response = await client.post(
        path,
        data={**fields, "name": "Renamed", "_csrf": await token(client, path)},
    )
    assert response.status_code == 303, response.text

    body = await form_body(client, path)
    print("BODY AFTER RENAME HAS ff-file-current:", "ff-file-current" in body)
    assert "Renamed" in await form_body(client, "/admin/shop.product/")
    link = re.search(r'href="([^"]*/media/[^"]+)"', body)
    assert link
    served = await client.get(link.group(1))
    assert served.content == b"first"


async def test_choosing_a_new_file_replaces_the_old_one(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))
    first = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("first.txt", b"first version", "text/plain")},
    )
    assert first.status_code == 303, first.text

    fields = other_fields(await form_body(client, path))
    second = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("second.txt", b"second version", "text/plain")},
    )
    assert second.status_code == 303, second.text

    body = await form_body(client, path)
    link = re.search(r'href="([^"]*/media/[^"]+)"', body)
    assert link
    served = await client.get(link.group(1))
    assert served.content == b"second version"

    # The first file is not left behind as an orphan on disk.
    assert len(await stored_files(media_root)) == 1


async def test_the_clear_checkbox_removes_the_file(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))
    uploaded = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("first.txt", b"first version", "text/plain")},
    )
    assert uploaded.status_code == 303, uploaded.text

    body = await form_body(client, path)
    assert "ff-file-current" in body
    fields = other_fields(body)
    cleared = await client.post(
        path,
        data={**fields, "attachment__clear": "1", "_csrf": await token(client, path)},
    )
    assert cleared.status_code == 303, cleared.text

    body = await form_body(client, path)
    assert "ff-file-current" not in body
    assert not await stored_files(media_root)


async def test_an_oversized_upload_is_refused_without_writing_anything(
    backend: SQLAlchemyBackend, staff_user: StaffUser, media_root: Path
) -> None:
    app = build(backend, media_root, upload_limit=10)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as small_client:
        await sign_in(small_client)
        path = "/admin/shop.product/1/"
        fields = other_fields(await form_body(small_client, path))
        response = await small_client.post(
            path,
            data={**fields, "_csrf": await token(small_client, path)},
            files={"attachment": ("big.txt", b"way more than ten bytes of content", "text/plain")},
        )

    assert response.status_code == 200  # re-rendered with an error, not redirected
    assert "too large" in response.text
    assert not await stored_files(media_root)


async def test_a_rejected_form_neither_writes_the_new_file_nor_deletes_the_old_one(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """A row that already has a file, edited with a valid replacement *and* an
    invalid value in some other field, must come out of that exactly as it
    went in: still pointing at the old file, and the old file still on disk.

    Writing the new file and deleting the old one as soon as `bind()` decides
    what to do with them -- rather than only once the whole form is confirmed
    valid and the row has actually saved -- would upload the replacement and
    discard the original over a save that never happened, leaving the row
    pointing at a file that no longer exists.
    """
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))
    first = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("original.txt", b"the original file", "text/plain")},
    )
    assert first.status_code == 303, first.text

    fields = other_fields(await form_body(client, path))
    rejected = await client.post(
        path,
        data={**fields, "name": "", "_csrf": await token(client, path)},
        files={"attachment": ("replacement.txt", b"a file that should not land", "text/plain")},
    )
    assert rejected.status_code == 200
    assert "This field is required." in rejected.text

    body = await form_body(client, path)
    link = re.search(r'href="([^"]*/media/[^"]+)"', body)
    assert link, "the original file should still be linked"

    served = await client.get(link.group(1))
    assert served.content == b"the original file"

    stored = await stored_files(media_root)
    assert len(stored) == 1
    assert await asyncio.to_thread(stored[0].read_bytes) == b"the original file"


# ---------------------------------------------------------------------------
# Serving a file back
# ---------------------------------------------------------------------------


async def test_the_media_route_requires_signing_in(
    backend: SQLAlchemyBackend, staff_user: StaffUser, media_root: Path
) -> None:
    """An upload is a record like any other, behind the same gate as everything
    else -- not a public static mount."""
    app = build(backend, media_root)
    (media_root / "shop.product" / "attachment").mkdir(parents=True)
    (media_root / "shop.product" / "attachment" / "secret.txt").write_bytes(b"private")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as anonymous:
        response = await anonymous.get(
            "/admin/media/shop.product/attachment/secret.txt", follow_redirects=False
        )

    assert response.status_code in (302, 303, 401, 403)


async def test_the_media_route_refuses_to_walk_out_of_its_root(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """`path` is a URL a request chose, not something FastFort generated --
    `../../etc/passwd` is a perfectly ordinary thing for one to contain."""
    outside = media_root.parent / "outside-the-media-root.txt"
    outside.write_text("should never be reachable")

    response = await client.get(f"/admin/media/%2e%2e%2f{outside.name}")

    assert response.status_code == 404
    outside.unlink()


async def test_the_media_route_404s_on_a_file_that_does_not_exist(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/admin/media/shop.product/attachment/nope.txt")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------


async def test_a_hostile_filename_does_not_become_a_path(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """A multipart filename is exactly as trustworthy as any other request
    header: a client can claim a file is called anything at all."""
    path = "/admin/shop.product/1/"
    fields = other_fields(await form_body(client, path))

    response = await client.post(
        path,
        data={**fields, "_csrf": await token(client, path)},
        files={"attachment": ("../../../etc/passwd", b"not actually passwd", "text/plain")},
    )
    assert response.status_code == 303, response.text

    for stored in await stored_files(media_root):
        assert ".." not in stored.as_posix()
        assert stored.is_relative_to(media_root)
