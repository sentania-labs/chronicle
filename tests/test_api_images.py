"""Image upload: the ceiling, the sniff, the dedupe, and attaching to a draft."""

from __future__ import annotations

import hashlib
import io

import httpx
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api import gitrepo
from chronicle.api.images import MAX_IMAGE_BYTES

from .conftest import auth, png_bytes


def upload(client: TestClient, token: str, data: bytes, name: str = "shot.png") -> httpx.Response:
    return client.post("/v1/images", files={"file": (name, data, "image/png")}, headers=auth(token))


def test_new_upload_is_201_and_a_repeat_is_200(client: TestClient, agent_token: str) -> None:
    first = upload(client, agent_token, png_bytes())
    assert first.status_code == 201
    image_id = first.json()["image_id"]
    assert first.json()["sha256"] == image_id
    assert first.json()["mime"] == "image/png"

    again = upload(client, agent_token, png_bytes(), name="different-name.png")
    assert again.status_code == 200
    assert again.json()["image_id"] == image_id

    other = upload(client, agent_token, png_bytes(color=(200, 100, 50)))
    assert other.status_code == 201
    assert other.json()["image_id"] != image_id


def test_upload_over_the_ceiling_is_413(client: TestClient, agent_token: str) -> None:
    response = upload(client, agent_token, b"\x00" * (MAX_IMAGE_BYTES + 1))
    assert response.status_code == 413


def test_request_body_over_the_middleware_ceiling_is_413_before_parsing(
    client: TestClient, agent_token: str
) -> None:
    # Bigger than MAIN_REQUEST_BODY_BYTES (8 MB): the Content-Length guard in
    # main.py must reject this before FastAPI ever spools it into an
    # UploadFile, so the response is fast and the body is never decoded.
    response = upload(client, agent_token, b"\x00" * (9 * 1024 * 1024))
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_content_type_is_sniffed_not_taken_from_the_name(
    client: TestClient, agent_token: str
) -> None:
    response = upload(client, agent_token, b"#!/bin/sh\necho not an image\n", name="payload.png")
    assert response.status_code == 415
    assert response.json()["error"] == "image_unsupported"


def test_disallowed_image_format_is_415(client: TestClient, agent_token: str) -> None:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (4, 4)).save(buffer, format="BMP")
    response = upload(client, agent_token, buffer.getvalue(), name="shot.bmp")
    assert response.status_code == 415


def test_metadata_is_stripped_by_re_encoding(client: TestClient, agent_token: str) -> None:
    source = PillowImage.new("RGB", (8, 8), (1, 2, 3))
    exif = source.getexif()
    exif[0x010E] = "a secret location note"
    buffer = io.BytesIO()
    source.save(buffer, format="JPEG", exif=exif)
    raw = buffer.getvalue()
    assert b"a secret location note" in raw

    response = client.post(
        "/v1/images", files={"file": ("shot.jpg", raw, "image/jpeg")}, headers=auth(agent_token)
    )
    assert response.status_code == 201
    stored = client.get(f"/v1/images/{response.json()['image_id']}", headers=auth(agent_token))
    assert stored.json()["mime"] == "image/jpeg"
    assert stored.json()["image_id"] != hashlib.sha256(raw).hexdigest()


def test_attach_and_detach_on_a_draft(client: TestClient, agent_token: str) -> None:
    image_id = upload(client, agent_token, png_bytes()).json()["image_id"]
    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]

    attached = client.put(
        f"/v1/drafts/{draft_id}/images/{image_id}",
        json={"role": "feature"},
        headers=auth(agent_token),
    )
    assert attached.status_code == 200
    assert attached.json()["images"] == [
        {"image_id": image_id, "filename": "shot.png", "role": "feature", "source_ref": None}
    ]

    bad_role = client.put(
        f"/v1/drafts/{draft_id}/images/{image_id}",
        json={"role": "banner"},
        headers=auth(agent_token),
    )
    assert bad_role.status_code == 422

    detached = client.delete(f"/v1/drafts/{draft_id}/images/{image_id}", headers=auth(agent_token))
    assert detached.json()["images"] == []
    assert (
        client.delete(
            f"/v1/drafts/{draft_id}/images/{image_id}", headers=auth(agent_token)
        ).status_code
        == 404
    )


def test_images_are_not_tracked_by_git(client: TestClient, agent_token: str) -> None:
    upload(client, agent_token, png_bytes())
    store = client.app.state.services.store  # type: ignore[attr-defined]
    assert not any(path.startswith("images/") for path in gitrepo.tracked_files(store.repo_dir))
