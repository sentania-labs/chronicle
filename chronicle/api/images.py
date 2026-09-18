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


def safe_upload_filename(name: str) -> str:
    """A filename a markdown reference and a Hugo static path can carry.

    A body refers to an attached image by its bare filename, and a reference
    with a space or a parenthesis in it is not one markdown parses as an image
    (nor one `convert.py` matches). The editor's uploads are named here, once,
    so what it inserts at the cursor is what the conversion later resolves.
    Only the UI route calls this: the API stores the name it is given.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("-", basename(name.replace("\\", "/")))
    cleaned = cleaned.replace("-.", ".").strip(".-")
    return cleaned or "upload"


def alt_text_for(filename: str) -> str:
    """Alt text for the reference the editor inserts: the filename's stem."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return re.sub(r"[-_]+", " ", re.sub(r"[\[\]]", "", stem)).strip() or "image"
