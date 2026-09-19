"""Issue 23: a submission's image ids are checked when it is created, and a
record that already carries a bad one still renders."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chronicle.api.errors import ApiError
from chronicle.api.store import Store

from .conftest import auth, png_bytes

UNKNOWN = "f" * 64


@pytest.mark.parametrize("image_id", ["deadbeef", "../../etc/passwd", UNKNOWN])
def test_create_submission_refuses_an_unknown_image_id_and_names_it(
    store: Store, image_id: str
) -> None:
    with pytest.raises(ApiError) as caught:
        store.create_submission("ghostwriter", "brief", [], [image_id])

    assert caught.value.status_code == 422
    assert caught.value.code == "image_not_found"
    assert caught.value.extra["image_id"] == image_id
    assert image_id in caught.value.message
    assert store.list_submissions() == []


def test_create_submission_names_the_first_bad_id_among_good_ones(store: Store) -> None:
    image, _ = store.put_image(png_bytes(), "shot.png")
    with pytest.raises(ApiError) as caught:
        store.create_submission("ghostwriter", "brief", [], [image.image_id, UNKNOWN])
    assert caught.value.extra["image_id"] == UNKNOWN


def test_create_submission_accepts_an_uploaded_image(store: Store) -> None:
    image, _ = store.put_image(png_bytes(), "shot.png")
    record = store.create_submission("ghostwriter", "brief", [], [image.image_id])
    assert record.image_ids == [image.image_id]


def test_post_submission_with_an_unknown_image_id_is_422(
    client: TestClient, agent_token: str
) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "b", "materials": [], "image_ids": [UNKNOWN]},
        headers=auth(agent_token),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "image_not_found"
    assert UNKNOWN in response.json()["message"]
    listing = client.get("/v1/submissions", headers=auth(agent_token)).json()
    assert listing["submissions"] == []


def _legacy_submission_with(client: TestClient, token: str, image_ids: list[str]) -> str:
    """A record written before create validated: the bad ids go in on disk."""
    created = client.post(
        "/v1/submissions", json={"brief": "legacy brief", "materials": []}, headers=auth(token)
    ).json()
    store: Store = client.app.state.services.store  # type: ignore[attr-defined]
    record = store.get_submission(created["id"])
    record.image_ids = image_ids
    store._write_json(
        store._submission_path(record.id), record.model_dump(mode="json", by_alias=True)
    )
    return str(created["id"])


def test_detail_page_of_a_submission_with_a_missing_image_still_renders(
    client: TestClient, agent_token: str
) -> None:
    submission_id = _legacy_submission_with(client, agent_token, [UNKNOWN])

    page = client.get(f"/content/submissions/{submission_id}")

    assert page.status_code == 200
    assert "legacy brief" in page.text
    assert UNKNOWN in page.text  # named, not silently dropped
    assert f'action="/content/submissions/{submission_id}/edit"' in page.text
    # The edit form no longer carries the bad id, so saving it heals the record.
    assert f'name="image_id" value="{UNKNOWN}"' not in page.text


def test_detail_page_lists_the_images_that_do_exist_beside_a_missing_one(
    client: TestClient, agent_token: str
) -> None:
    store: Store = client.app.state.services.store  # type: ignore[attr-defined]
    image, _ = store.put_image(png_bytes(), "kept.png")
    submission_id = _legacy_submission_with(client, agent_token, [image.image_id, UNKNOWN])

    page = client.get(f"/content/submissions/{submission_id}")

    assert page.status_code == 200
    assert "kept.png" in page.text
    assert f'name="image_id" value="{image.image_id}"' in page.text


def test_saving_the_detail_form_of_a_healed_page_succeeds(
    client: TestClient, agent_token: str
) -> None:
    submission_id = _legacy_submission_with(client, agent_token, [UNKNOWN])
    response = client.post(
        f"/content/submissions/{submission_id}/edit",
        data={
            "base_version": "1",
            "brief": "fixed brief",
            "material_name": [""],
            "material_url": [""],
            "material_text": [""],
        },
    )
    assert response.status_code in (200, 303)
    record = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert record["brief"] == "fixed brief"
    assert record["image_ids"] == []
