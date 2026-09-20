"""Upload validation and metadata stripping for the image store.

The ceiling and the allowed types are spec section 17: 5 MB, png/jpg/webp/gif.
The type is decided by decoding the bytes with Pillow, never by the filename
the uploader chose, and the stored bytes are a re-encode with no metadata
carried over, so an upload cannot smuggle EXIF location data onto the public
site.
"""

from __future__ import annotations

import hashlib
import io
import re
from posixpath import basename

from PIL import Image as PillowImage

from .errors import ApiError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_FORMATS = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}


class NormalisedImage:
    def __init__(self, data: bytes, mime: str, extension: str) -> None:
        self.data = data
        self.mime = mime
        self.extension = extension
        self.sha256 = hashlib.sha256(data).hexdigest()


def normalise(raw: bytes) -> NormalisedImage:
    if len(raw) > MAX_IMAGE_BYTES:
        raise ApiError(
            413,
            "image_too_large",
            f"image is {len(raw)} bytes, ceiling is {MAX_IMAGE_BYTES}",
            limit_bytes=MAX_IMAGE_BYTES,
        )
    try:
        source = PillowImage.open(io.BytesIO(raw))
        source.load()
    except Exception as exc:
        raise ApiError(415, "image_unsupported", f"not a decodable image: {exc}") from exc

    fmt = source.format or ""
    if fmt not in ALLOWED_FORMATS:
        raise ApiError(
            415,
            "image_unsupported",
            f"image format {fmt or 'unknown'} is not one of png, jpg, webp, gif",
            allowed=sorted(extension for _, extension in ALLOWED_FORMATS.values()),
        )

    # Rebuilding from raw pixels rather than copying the image is what drops
    # the metadata: EXIF, PNG text chunks, and comments all live in `info`,
    # which a copy would carry along.
    stripped = PillowImage.frombytes(source.mode, source.size, source.tobytes())
    palette = source.getpalette()
    if palette is not None:
        stripped.putpalette(palette)
    buffer = io.BytesIO()
    stripped.save(buffer, format=fmt)

    mime, extension = ALLOWED_FORMATS[fmt]
    return NormalisedImage(buffer.getvalue(), mime, extension)


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_EXTENSION_CHARS = re.compile(r"[^A-Za-z0-9]+")
_PLAIN_FILENAME = re.compile(r"[A-Za-z0-9._-]+")
# A word as a person reads it: a run of letters and digits in any script.
_STEM_WORD = re.compile(r"[^\W_]+")
_CONTENT_HASH_LENGTH = 10


def _has_fragment_word(stem: str) -> bool:
    """True when cleaning cut a word of `stem` in half.

    A word that mixes ASCII with characters cleaning drops (`写真2`) survives
    only as a fragment (`2`) that says nothing about the file and is what two
    different images named in the same script both reduce to. A word that was
    wholly ASCII, or wholly dropped, is not a fragment: the first is what the
    author typed and the second leaves nothing behind.
    """
    for word in _STEM_WORD.findall(stem):
        if not word.isascii() and any(char.isascii() for char in word):
            return True
    return False


def safe_upload_filename(name: str, content: bytes = b"") -> str:
    """A filename a markdown reference and a Hugo static path can carry.

    A body refers to an attached image by its bare filename, and a reference
    with a space or a parenthesis in it is not one markdown parses as an image
    (nor one `convert.py` matches). The editor's uploads are named here, once,
    so what it inserts at the cursor is what the conversion later resolves.
    Only the UI route calls this: the API stores the name it is given.

    A stem with nothing left after cleaning (a name written entirely outside
    ASCII) keeps its extension and takes a short hash of `content` as the stem,
    so two such images on one post stay two different files. A stem that keeps
    only a fragment of a word (`写真2` cleans to `2`, and `猫2` to the same)
    keeps that fragment and gets the same hash after it (`2-3f9a1c0b7d`), so
    it is still recognisable and never collides with another image's.
    """
    base = basename(name.replace("\\", "/"))
    stem, _, extension = base.rpartition(".") if "." in base else (base, "", "")
    fragment = _has_fragment_word(stem)
    stem = _UNSAFE_FILENAME_CHARS.sub("-", stem).strip(".-")
    extension = _UNSAFE_EXTENSION_CHARS.sub("", extension)
    if not stem:
        if not content:
            return "upload"
        stem = hashlib.sha256(content).hexdigest()[:_CONTENT_HASH_LENGTH]
    elif fragment and content:
        stem = f"{stem}-{hashlib.sha256(content).hexdigest()[:_CONTENT_HASH_LENGTH]}"
    return f"{stem}.{extension}" if extension else stem


def is_plain_filename(filename: str) -> bool:
    """True if a bare markdown reference to `filename` parses and resolves.

    `convert.py` finds an image reference as a run of characters with no
    whitespace and no closing parenthesis, so a name outside this set cannot
    be written into a body in a form both markdown and the conversion accept.
    """
    return _PLAIN_FILENAME.fullmatch(filename) is not None


def alt_text_for(filename: str) -> str:
    """Alt text for the reference the editor inserts: the filename's stem."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[-_]+", " ", re.sub(r"[\[\]]", "", stem)).strip() or "image"
