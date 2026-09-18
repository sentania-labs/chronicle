"""The editor's image upload: what the endpoint returns to `editor.js`.

A plain form post still gets the editor page back; a request that asks for
JSON gets the stored filename, the markdown to insert at the cursor, and the
URL the live render loads the bytes from.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.images import alt_text_for, safe_upload_filename

from .conftest import png_bytes
from .test_ui import make_draft

JSON = {"Accept": "application/json"}


def upload(client: TestClient, draft_id: str, name: str, data: bytes, role: str = "inline"):
    return client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": (name, data, "image/png")},
        data={"role": role},
        headers=JSON,
    )


def test_inline_upload_returns_the_markdown_to_insert(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    response = upload(client, draft_id, "rack photo.png", png_bytes())
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True
    assert result["role"] == "inline"
    assert result["filename"] == "rack-photo.png"
    assert result["markdown"] == "![rack photo](rack-photo.png)"
    assert result["url"] == f"/content/drafts/{draft_id}/images/{result['image_id']}/file"
    attached = services.store.get_draft(draft_id).images
    assert [(i.filename, i.role) for i in attached] == [("rack-photo.png", "inline")]


def test_feature_upload_has_no_markdown_to_insert(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    result = upload(client, draft_id, "hero.png", png_bytes(), role="feature").json()
    assert result["ok"] is True and result["markdown"] is None and result["role"] == "feature"


def test_the_inserted_reference_is_the_stored_filename_after_a_dedup(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    first = upload(client, draft_id, "a.png", png_bytes()).json()
    again = upload(client, draft_id, "b.png", png_bytes()).json()
    # Same bytes: the store keeps the first record, so the reference must use
    # its filename, not the name of the second upload.
    assert again["image_id"] == first["image_id"]
    assert again["filename"] == first["filename"]
    assert again["markdown"] == first["markdown"]


def test_errors_are_json_with_the_api_status_code(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    bad_type = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.txt", b"not an image", "text/plain")},
        data={"role": "inline"},
        headers=JSON,
    )
    assert bad_type.status_code == 415
    body = bad_type.json()
    assert body["ok"] is False and body["message"] and body["code"]

    too_big = upload(client, draft_id, "big.png", b"0" * (6 * 1024 * 1024))
    assert too_big.status_code == 413 and too_big.json()["ok"] is False

    missing = upload(client, "no-such-draft", "a.png", png_bytes())
    assert missing.status_code == 404 and missing.json()["ok"] is False


def test_a_plain_form_post_still_gets_the_editor_page(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("plain.png", png_bytes(), "image/png")},
        data={"role": "inline"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "plain.png" in response.text


def test_json_upload_is_still_guarded_by_the_same_origin_check(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.png", png_bytes(), "image/png")},
        data={"role": "inline"},
        headers={**JSON, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert services.store.get_draft(draft_id).images == []


def test_attached_image_bytes_are_served_only_for_the_draft_that_attached_them(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    other_id = make_draft(services, "drafting", title="Other")
    result = upload(client, draft_id, "a.png", png_bytes()).json()

    served = client.get(result["url"])
    assert served.status_code == 200
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.content == services.store.image_blob(result["image_id"]).read_bytes()

    elsewhere = client.get(f"/content/drafts/{other_id}/images/{result['image_id']}/file")
    assert elsewhere.status_code == 404


def test_editor_page_exposes_attached_images_to_the_live_render(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")
    result = upload(client, draft_id, "a.png", png_bytes()).json()
    html = client.get(f"/content/drafts/{draft_id}").text
    assert 'data-image-filename="a.png"' in html
    assert f'data-image-src="{result["url"]}"' in html


def test_filename_helpers() -> None:
    assert safe_upload_filename("My Photo (1).PNG") == "My-Photo-1.PNG"
    assert safe_upload_filename("../../etc/passwd") == "passwd"
    assert safe_upload_filename("C:\\pics\\a b.png") == "a-b.png"
    assert safe_upload_filename("...") == "upload"
    assert alt_text_for("rack-photo_2.png") == "rack photo 2"
    assert alt_text_for("[x].png") == "x"
    assert alt_text_for(".png") == "image"
