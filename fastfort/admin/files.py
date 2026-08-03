"""Storing what a file or image field is given.

The database column holds a relative path -- never the bytes, and never the
browser's own filename unmodified. Two uploads named `photo.jpg` must not
collide with each other, and a filename is attacker-controlled input that must
not become a path segment verbatim: `../../etc/passwd` is a perfectly ordinary
thing for a multipart request to claim a file is called.

`UploadedFile` is what a submitted value looks like by the time it reaches
`Form.bind`. It is read into memory once, in `site.py`, so neither this module
nor `forms.py` has to know the request was `multipart/form-data` -- that stays
a detail of the transport, not of what a file field means.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass

from fastfort.core.settings import MediaSettings

__all__ = ["UploadedFile", "delete_upload", "safe_filename", "save_upload", "stored_path"]


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """One file as it arrived in a submitted form.

    An empty filename is what a browser sends for a file input nobody touched --
    "keep whatever is already there" -- and is a different thing from choosing
    to clear the field, which is its own checkbox.
    """

    filename: str
    content: bytes

    @property
    def chosen(self) -> bool:
        return bool(self.filename)


#: Everything outside this set becomes an underscore. Conservative on purpose:
#: the result is a path segment, not a document users read the name of.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """`name`, reduced to a segment safe to use in a stored path.

    Only the part after the last slash (forward or back) survives, and only a
    conservative character set within it. What is kept is decoration -- the
    stored path is already unique without it -- so replacing the rest is free.
    """
    tail = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _UNSAFE.sub("_", tail).lstrip(".") or "upload"
    return cleaned[-200:]  # generous for any real filename, bounded for every filesystem


def stored_path(model_key: str, field_name: str, filename: str) -> str:
    """A collision-proof relative path, e.g. `shop.product/photo/<uuid>-cat.jpg`.

    Namespaced by model and field so two unrelated uploads never share a
    directory, and a project can point a backup or a CDN at one field's files
    without the rest.
    """
    return f"{model_key}/{field_name}/{uuid.uuid4().hex}-{safe_filename(filename)}"


def save_upload(media: MediaSettings, relative: str, content: bytes) -> None:
    """Write `content` to `media.root / relative`, creating directories as needed.

    The size limit is `Form`'s to enforce, before this is ever called: a
    rejected upload should be a message on the field, not bytes written to disk
    and then thrown away.
    """
    destination = media.root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def delete_upload(media: MediaSettings, relative: str) -> None:
    """Remove a stored file. Missing is not an error -- it may already be gone,
    or may never have been written by this installation at all."""
    with contextlib.suppress(FileNotFoundError):
        (media.root / relative).unlink()
