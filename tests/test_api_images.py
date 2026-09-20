"""Image upload: the ceiling, the sniff, the dedupe, and attaching to a draft."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api import gitrepo
from chronicle.api.errors import ApiError
from chronicle.api.images import MAX_IMAGE_BYTES
from chronicle.api.store import Store

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


def test_attaching_a_second_image_with_the_same_filename_is_409(
    client: TestClient, agent_token: str
) -> None:
    first_id = upload(client, agent_token, png_bytes(), name="shot.png").json()["image_id"]
    second_id = upload(
        client, agent_token, png_bytes(color=(200, 100, 50)), name="shot.png"
    ).json()["image_id"]
    assert first_id != second_id
    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]

    attached = client.put(
        f"/v1/drafts/{draft_id}/images/{first_id}",
        json={"role": "inline"},
        headers=auth(agent_token),
    )
    assert attached.status_code == 200

    conflict = client.put(
        f"/v1/drafts/{draft_id}/images/{second_id}",
        json={"role": "inline"},
        headers=auth(agent_token),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"] == "image_filename_conflict"
    assert "shot.png" in conflict.json()["message"]

    # Re-attaching the same image under its own filename is still fine: it
    # is an update, not a second image trying to claim the same output path.
    reattached = client.put(
        f"/v1/drafts/{draft_id}/images/{first_id}",
        json={"role": "feature"},
        headers=auth(agent_token),
    )
    assert reattached.status_code == 200


def test_images_are_not_tracked_by_git(client: TestClient, agent_token: str) -> None:
    upload(client, agent_token, png_bytes())
    store = client.app.state.services.store  # type: ignore[attr-defined]
    assert not any(path.startswith("images/") for path in gitrepo.tracked_files(store.repo_dir))


# Issue 47: `get_image` builds `images/<id[:2]>/<id>.json` from the id it is
# handed. The router already refuses an id carrying a slash, but a bare `..`
# (or `%2e%2e`) is one segment and used to read `data/...json`, outside the
# images directory, and to be stored on a draft as an image id. The store
# checks the shape itself so that never depends on URL handling.
BAD_IMAGE_IDS = ["..", "...", ".", "..x", "deadbeef", "A" * 64, "g" * 64, ("a" * 63) + "\\"]


@pytest.mark.parametrize("image_id", BAD_IMAGE_IDS)
def test_get_image_refuses_an_id_that_is_not_a_sha256(store: Store, image_id: str) -> None:
    with pytest.raises(ApiError) as caught:
        store.get_image(image_id)
    assert caught.value.status_code == 404
    assert caught.value.code == "image_not_found"


def test_get_image_does_not_read_a_file_outside_the_images_directory(
    store: Store, data_dir: Path
) -> None:
    (data_dir / "...json").write_text(
        '{"image_id":"x","sha256":"0","filename":"dots.png","bytes":1,"mime":"image/png"}',
        encoding="utf-8",
    )
    with pytest.raises(ApiError) as caught:
        store.get_image("..")
    assert caught.value.code == "image_not_found"


def test_image_routes_refuse_an_encoded_dotdot_id(
    client: TestClient, agent_token: str, data_dir: Path
) -> None:
    # `%2e%2e` because the test client normalises a literal `..` away before
    # it is sent; the literal form is the store-level test above and the live
    # probe in docs/pr-bodies/lane-g-image-id-evidence.txt (`curl --path-as-is`).
    image_id = "%2e%2e"
    (data_dir / "...json").write_text(
        '{"image_id":"x","sha256":"0","filename":"dots.png","bytes":1,"mime":"image/png"}',
        encoding="utf-8",
    )
    got = client.get(f"/v1/images/{image_id}", headers=auth(agent_token))
    assert got.status_code == 404
    assert got.json()["error"] == "image_not_found"

    draft = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()
    attached = client.put(
        f"/v1/drafts/{draft['id']}/images/{image_id}",
        json={"role": "inline"},
        headers=auth(agent_token),
    )
    assert attached.status_code == 404
    assert attached.json()["error"] == "image_not_found"
    fresh = client.get(f"/v1/drafts/{draft['id']}", headers=auth(agent_token)).json()
    assert fresh["images"] == []
