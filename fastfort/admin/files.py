"""Storing what a file or image field is given, and refusing what it should not be.

The database column holds a relative path -- never the bytes, and never the
browser's own filename unmodified. Two uploads named `photo.jpg` must not
collide with each other, and a filename is attacker-controlled input that must
not become a path segment verbatim: `../../etc/passwd` is a perfectly ordinary
thing for a multipart request to claim a file is called.

`UploadedFile` is what a submitted value looks like by the time it reaches
`Form.bind`. It is read into memory once, in `site.py`, so neither this module
nor `forms.py` has to know the request was `multipart/form-data` -- that stays
a detail of the transport, not of what a file field means.

## What an upload has to get past

Uploaded files are served back from `/admin/media/…`, which is the *admin's own
origin* -- the origin holding the session cookie. So a file that the browser can
be persuaded to execute as a document is a stored cross-site scripting hole with
a session-theft payload, and the three ways to get one are all closed here:

**The last extension has to be one the field accepts**, from an allow-list, and
it additionally has to not be in `DANGEROUS_EXTENSIONS`. The allow-list is what
decides the field is for pictures; the deny-list is what stops a project widening
it into a stored cross-site scripting hole by hand.

**A dangerous extension in the middle of a name stops being one.** `hack.exe.png`
is a name a server reading left to right sees a program in, which is the classic
`AddHandler` bug -- so `safe_filename` writes it to disk as `hack_exe.png` and
there is no `.exe` in the name any more. Neutralised rather than refused, because
refusing every name with an extension buried in it also refuses
`example.com.pdf`, and a rule that produces a mystery error for an ordinary file
is a rule people work around. `archive.tar.gz` is untouched: `tar` is not
dangerous, so there is nothing to neutralise.

**The bytes are checked against the name.** An extension is a claim by whoever
uploaded the file. `sniff()` reads the leading magic numbers, and a `.png` whose
content is an ELF binary, a shell script or an HTML document is refused with the
mismatch named -- the interesting attack is exactly the one where the name and
the content disagree.

**SVG is not an image here, and cannot be made into one.** It is a document that
can carry a `<script>` element, not a picture. It is in `DANGEROUS_EXTENSIONS`,
and `MediaSettings` refuses at start-up to allow-list anything in that set, so
there is no configuration that results in FastFort storing one.

**And nothing is served on trust.** `content_type_for` decides the response's
`Content-Type` from the stored bytes rather than from the stored name, hands out
`text/plain` for anything it cannot positively identify as a raster image, and
`site.py` sends `nosniff` and an attachment disposition with it. A file that got
onto disk before any of the above existed is still served harmlessly.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import dataclass

from fastfort.core.settings import MediaSettings

__all__ = [
    "DANGEROUS_EXTENSIONS",
    "INLINE_SAFE_TYPES",
    "UploadedFile",
    "check_upload",
    "content_type_for",
    "delete_upload",
    "extensions_of",
    "safe_filename",
    "save_upload",
    "sniff",
    "stored_path",
]


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

    One extension survives: the last. `hack.exe.png` is written down as
    `hack_exe.png`, because a name carrying `.exe` in the middle is a name some
    server somewhere reads left to right and hands to an interpreter, and the
    stored file has no reason to keep the ambiguity. Only *dangerous* middles are
    flattened, so `archive.tar.gz` stays `archive.tar.gz` -- flattening every
    name with two dots in it would rewrite ordinary filenames for no gain.
    """
    tail = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = _UNSAFE.sub("_", tail).lstrip(".") or "upload"

    head, dot, final = cleaned.rpartition(".")
    if dot:
        # `partition` on each remaining dot, so only the separators in front of a
        # dangerous segment are replaced and the rest of the name is untouched.
        segments = head.split(".")
        rebuilt = segments[0]
        for segment in segments[1:]:
            rebuilt += ("_" if segment.lower() in DANGEROUS_EXTENSIONS else ".") + segment
        cleaned = f"{rebuilt}.{final}"

    return cleaned[-200:]  # generous for any real filename, bounded for every filesystem


#: Extensions refused wherever they appear in a filename, not only at the end.
#:
#: Scripts a server might execute, documents a browser might execute, and the
#: archive and shortcut formats whose whole purpose is to carry one of those.
#: This is deliberately a *deny*-list working alongside the allow-list rather
#: than instead of it: the allow-list decides what the field is for, and this
#: decides what may not be smuggled through the middle of a name that ends in
#: something the allow-list likes.
DANGEROUS_EXTENSIONS = frozenset(
    {
        # Executed by a web server
        "php", "php3", "php4", "php5", "php7", "phps", "phtml", "pht",
        "asp", "aspx", "ascx", "ashx", "asmx", "cer", "cshtml", "vbhtml",
        "jsp", "jspx", "jsw", "jsv", "jspf", "cgi", "pl", "py", "pyc", "pyo",
        "rb", "erb", "lua", "sh", "bash", "zsh", "csh", "ksh", "fish",
        # Executed by the operating system
        "exe", "dll", "so", "dylib", "com", "bat", "cmd", "msi", "msp", "scr",
        "vb", "vbs", "vbe", "js", "mjs", "cjs", "jse", "ws", "wsf", "wsh",
        "ps1", "psm1", "psd1", "ps1xml", "hta", "cpl", "jar", "app", "deb",
        "rpm", "apk", "dmg", "pkg", "run", "bin", "elf", "out",
        # Executed by a browser, on this origin, with this session's cookie
        "html", "htm", "xhtml", "xht", "shtml", "svg", "svgz", "xml", "xsl",
        "xslt", "mhtml", "mht", "swf", "wasm",
        # Configuration a server reads and obeys
        "htaccess", "htpasswd", "ini", "conf", "config", "env",
        # Shortcuts, which are a command with an icon
        "lnk", "url", "desktop", "webloc", "reg", "inf", "scf",
    }
)  # fmt: skip

#: Magic numbers, longest first so a prefix never shadows a longer signature.
#: Only formats worth being sure about are listed: the point is to catch a
#: `.png` that is really a program, not to identify every file on earth.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"%PDF-", "pdf"),
    (b"PK\x03\x04", "zip"),  # also docx, xlsx, pptx, odt -- all zip containers
    (b"PK\x05\x06", "zip"),
    (b"PK\x07\x08", "zip"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"Rar!\x1a\x07", "rar"),
    (b"\x00\x00\x01\x00", "ico"),
    (b"OggS", "ogg"),
    (b"fLaC", "flac"),
    (b"ID3", "mp3"),
    (b"\x1aE\xdf\xa3", "matroska"),  # mkv and webm
    (b"MZ", "executable"),  # DOS/Windows
    (b"\x7fELF", "executable"),  # Linux
    (b"\xca\xfe\xba\xbe", "executable"),  # Mach-O fat, and Java class
    (b"\xcf\xfa\xed\xfe", "executable"),  # Mach-O 64-bit
    (b"#!", "script"),
)

#: Signatures that only make sense a few bytes in. RIFF and ISO base media both
#: put a length or a size before the tag that names the format.
_OFFSET_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (8, b"WEBP", "webp"),
    (8, b"WAVE", "wav"),
    (8, b"AVI ", "avi"),
    (4, b"ftyp", "mp4"),
)

#: Which sniffed kinds a given extension is allowed to turn out to be. An
#: extension that is not here is not sniffable -- `.txt` and `.csv` have no magic
#: number, and refusing them for lack of one would refuse every plain text file.
_EXPECTED: dict[str, frozenset[str]] = {
    "png": frozenset({"png"}),
    "jpg": frozenset({"jpeg"}),
    "jpeg": frozenset({"jpeg"}),
    "gif": frozenset({"gif"}),
    "webp": frozenset({"webp"}),
    "avif": frozenset({"mp4"}),  # AVIF is an ISO base media container
    "heic": frozenset({"mp4"}),
    "ico": frozenset({"ico"}),
    "pdf": frozenset({"pdf"}),
    "zip": frozenset({"zip"}),
    "gz": frozenset({"gzip"}),
    "tgz": frozenset({"gzip"}),
    "bz2": frozenset({"bzip2"}),
    "7z": frozenset({"7z"}),
    "rar": frozenset({"rar"}),
    "docx": frozenset({"zip"}),
    "xlsx": frozenset({"zip"}),
    "pptx": frozenset({"zip"}),
    "odt": frozenset({"zip"}),
    "ods": frozenset({"zip"}),
    "odp": frozenset({"zip"}),
    # `.mp3` is deliberately absent: a bare MPEG frame begins with a sync word
    # rather than a signature, so "no magic number" is the normal case for one
    # without an ID3 tag and requiring a match here would refuse valid audio.
    "mp4": frozenset({"mp4"}),
    "m4a": frozenset({"mp4"}),
    "mov": frozenset({"mp4"}),
    "webm": frozenset({"matroska"}),
    "mkv": frozenset({"matroska"}),
    "ogg": frozenset({"ogg"}),
    "wav": frozenset({"wav"}),
    "avi": frozenset({"avi"}),
    "flac": frozenset({"flac"}),
}

#: The only types ever sent with a `Content-Type` a browser will render in place,
#: and every one of them is a raster image -- a format with no way to express a
#: script. Everything else is served as a download, which is why this map is an
#: allow-list rather than `mimetypes.guess_type`: guessing is how `.svg` becomes
#: `image/svg+xml` and an upload becomes script running on the admin's origin.
INLINE_SAFE_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/vnd.microsoft.icon",
}

#: Bytes `sniff` needs. The deepest signature ends at 12, but the markup check
#: skips leading whitespace before matching, so it is given room to do that.
SNIFF_LENGTH = 512


def extensions_of(filename: str) -> list[str]:
    """Every extension in `filename`, lowercased, in the order they appear.

    `archive.tar.gz` is `["tar", "gz"]`; `hack.exe.png` is `["exe", "png"]`,
    which is the whole reason this returns a list rather than a suffix.

    A dotfile's name *is* treated as an extension, on purpose: `.htaccess` is
    precisely the upload that has to be refused, and letting it through on the
    grounds that it is technically a name rather than a suffix would be a rule
    written for the benefit of the attack.
    """
    tail = filename.replace("\\", "/").rsplit("/", 1)[-1]
    parts = tail.split(".")
    # A segment with a space or of unreasonable length is part of the name, not
    # an extension: "Q3 report.final version.pdf" has one extension, not two.
    return [
        part.lower()
        for part in parts[1:]
        if part and len(part) <= 12 and part.isalnum() and not part.isdigit()
    ]


def sniff(content: bytes) -> str | None:
    """What `content` actually is, from its leading bytes, or `None` if unknown.

    "Unknown" is a real answer and not a failure: plain text, CSV and SVG have no
    magic number at all, so a caller has to decide what to do about silence
    rather than being handed a guess.
    """
    for offset, marker, kind in _OFFSET_SIGNATURES:
        if content[offset : offset + len(marker)] == marker:
            return kind
    for marker, kind in _SIGNATURES:
        if content.startswith(marker):
            return kind

    # Not a signature, but worth naming: a document beginning with markup is a
    # document a browser will happily execute if it is ever served as one.
    head = content[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html", b"<?xml", b"<svg", b"<script")):
        return "markup"
    return None


def check_upload(
    filename: str,
    content: bytes,
    *,
    allowed: frozenset[str],
    kind: str = "file",
) -> str | None:
    """`None` if this upload may be stored, otherwise the reason it may not.

    A message rather than an exception: this is a validation error belonging on
    the field the reader was filling in, alongside whatever else on the form was
    also wrong, not a 500 and not a bare 400 with the rest of their work lost.

    Every message names both what was refused and what would be accepted --
    "that file is not allowed" tells someone holding a `.dwg` nothing about
    whether to rename it, convert it, or ask an administrator.
    """
    extensions = extensions_of(filename)
    if not extensions:
        return f"That file has no extension, so it cannot be checked. Accepted: {_listed(allowed)}."

    # Only the last one. An earlier `.exe` is handled by `safe_filename`, which
    # writes it to disk with the dot replaced -- see this module's docstring for
    # why that is better here than a refusal.
    final = extensions[-1]
    if final in DANGEROUS_EXTENSIONS:
        return (
            f"A .{final} file is never accepted here. Uploads are served back from the "
            "admin's own address, so a file a browser or a server might execute would "
            f"run as part of the admin. Accepted: {_listed(allowed)}."
        )
    if final not in allowed:
        return f"{kind.capitalize()}s must be one of: {_listed(allowed)}. This one is .{final}."

    sniffed = sniff(content)
    if sniffed in {"executable", "script", "markup"}:
        return (
            f"{filename!r} is named .{final} but its contents are "
            f"{'a program' if sniffed == 'executable' else 'a script or a document'}. "
            "Uploads have to be what they say they are."
        )

    expected = _EXPECTED.get(final)
    if expected is not None and sniffed is not None and sniffed not in expected:
        return (
            f"{filename!r} is named .{final} but its contents are {sniffed}. "
            "Rename it to match, or convert it."
        )
    if expected is not None and sniffed is None:
        return (
            f"{filename!r} does not begin like a .{final} file. "
            "It may be empty, or truncated, or not the format its name claims."
        )

    return None


def content_type_for(content: bytes) -> tuple[str, bool]:
    """`(content_type, inline)` for a stored file, decided by its bytes.

    By its bytes and never by its name, because the name is the part an attacker
    chose. Anything not recognised as a raster image is `text/plain` and a
    download -- `text/plain` rather than `application/octet-stream` so that a
    browser which ignores `Content-Disposition` still renders the file as text
    rather than as whatever it would have liked to sniff it into.

    Re-read at serve time rather than trusted from upload time, so files written
    by a version of FastFort that predates `check_upload` are covered too.
    """
    kind = sniff(content)
    if kind in INLINE_SAFE_TYPES:
        return INLINE_SAFE_TYPES[kind], True
    return "text/plain; charset=utf-8", False


def _listed(extensions: frozenset[str]) -> str:
    return ", ".join("." + extension for extension in sorted(extensions))


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
