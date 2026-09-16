"""Image upload: content-addressed, deduplicated, metadata stripped."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Response, UploadFile

from ..deps import Services, get_services, require_consumer
from ..errors import ApiError
from ..images import MAX_IMAGE_BYTES

router = APIRouter(prefix="/images", tags=["images"])

# One byte past the ceiling is enough to know the upload is oversized without
# ever holding the whole body in memory: read in capped chunks and bail the
# moment the running total would exceed MAX_IMAGE_BYTES.
_READ_CHUNK_BYTES = 64 * 1024


def _read_capped(file: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ApiError(
                413,
                "image_too_large",
                f"image exceeds the {MAX_IMAGE_BYTES} byte ceiling",
                limit_bytes=MAX_IMAGE_BYTES,
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("")
def upload_image(
    response: Response,
    file: UploadFile = File(...),
    services: Services = Depends(get_services),
    _: object = Depends(require_consumer),
) -> dict[str, Any]:
    raw = _read_capped(file)
    record, created = services.store.put_image(raw, file.filename or "upload")
    response.status_code = 201 if created else 200
    return record.model_dump(mode="json")


@router.get("/{image_id}")
def get_image(image_id: str, services: Services = Depends(get_services)) -> dict[str, Any]:
    return services.store.get_image(image_id).model_dump(mode="json")
