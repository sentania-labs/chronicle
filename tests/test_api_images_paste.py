"""Issue 85: a second pasted image attaches with a derived unique filename.

Browsers name every pasted clipboard image ``image.png``. The paste route
(`POST /content/drafts/<id>/images`) used to fail the second paste with a
409 ``image_filename_conflict`` because two different images with the same
stored filename would collide when convert places them at
``static/images/<slug>/<filename>``.

Now ``put_and_attach_image`` derives a unique filename
(``image-2.png``, ``image-3.png``, and so on) when there is a collision, so
the paste succeeds and the build places both files.  A direct call to
``attach_image`` (the ``/v1`` route) still returns 409, because that is an
explicit rename by the consumer.

Re-attaching the same ``image_id`` (changing role) stays a no-op.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.errors import ApiError
from chronicle.api.store import Store

from .conftest import auth, png_bytes


def paste(
    client: TestClient,
    draft_id: str,
    data: bytes,
    name: str = "image.png",
    role: str = "inline",
):
    return client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": (name, data, "image/png")},
        data={"role": role},
        headers={"Accept": "application/json"},
    )


# --- AC1: two clipboard pastes attach as image.png and image-2.png ----------


def test_two_pastes_attach_with_derived_filenames(client: TestClient, services: Services) -> None:
    """Two pasted images with the same browser-given name attach successfully.

    The first gets ``image.png``. The second gets ``image-2.png`` because
    the first already uses the base name on the draft.
    """
    draft_id = _make_draft_id(client, services)
    first = paste(client, draft_id, png_bytes((10, 20, 30)))
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True
    assert first_body["filename"] == "image.png"

    second = paste(client, draft_id, png_bytes((20, 30, 40)))
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["ok"] is True
    assert second_body["filename"] == "image-2.png"

    # Both images are attached.
    draft = services.store.get_draft(draft_id)
    attached_names = {item.filename for item in draft.images}
    assert attached_names == {"image.png", "image-2.png"}

    # The markdown references the derived filenames.
    assert first_body["markdown"] == "![image](image.png)"
    assert second_body["markdown"] == "![image 2](image-2.png)"


def test_three_pastes_derive_image_3_png(client: TestClient, services: Services) -> None:
    """A third paste with the same name gets image-3.png."""
    draft_id = _make_draft_id(client, services)
    for color in ((1, 2, 3), (4, 5, 6), (7, 8, 9)):
        resp = paste(client, draft_id, png_bytes(color))
        assert resp.status_code == 200

    draft = services.store.get_draft(draft_id)
    attached_names = sorted(item.filename for item in draft.images)
    assert attached_names == ["image-2.png", "image-3.png", "image.png"]


# --- AC2: re-attach is a no-op; explicit rename is 409 --------------------


def test_re_attaching_the_same_image_is_a_no_op(client: TestClient, agent_token: str) -> None:
    """Attaching an already-attached image under a different role updates
    that role but does not create a second entry.
    """
    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]

    # Attach an image directly via store (no auth needed).
    store = client.app.state.services.store  # type: ignore[attr-defined]
    img1, _ = store.put_and_attach_image(
        draft_id, png_bytes((10, 20, 30)), "image.png", "inline", "scott"
    )
    assert len(store.get_draft(draft_id).images) == 1

    # Re-attach under feature role via the v1 route.
    resp2 = client.put(
        f"/v1/drafts/{draft_id}/images/{img1.image_id}",
        json={"role": "feature"},
        headers=auth(agent_token),
    )
    assert resp2.status_code == 200
    draft = store.get_draft(draft_id)
    assert len(draft.images) == 1
    assert draft.images[0].role == "feature"


def test_explicit_rename_onto_existing_name_returns_409(
    client: TestClient, agent_token: str
) -> None:
    """Direct attach of a second image whose filename collides with the
    first returns 409.  This is the /v1 route used by consumers for
    explicit renames, and must stay strict.
    """
    first_id = client.post(
        "/v1/images",
        files={"file": ("image.png", png_bytes((10, 20, 30)), "image/png")},
        headers=auth(agent_token),
    ).json()["image_id"]
    second_id = client.post(
        "/v1/images",
        files={"file": ("image.png", png_bytes((20, 30, 40)), "image/png")},
        headers=auth(agent_token),
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
    assert "image.png" in conflict.json()["message"]

    # The first image is still attached.
    draft = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert len(draft["images"]) == 1


# --- Store-level tests: _would_collide and _unique_filename ----------------


def test_would_collide_returns_true_when_another_image_has_same_name(store: Store) -> None:
    """_would_collide is true only when a different image_id already holds
    the same filename.
    """
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_image(png_bytes((1, 2, 3)), "photo.png")
    img2, _ = store.put_image(png_bytes((4, 5, 6)), "photo.png")

    store.attach_image(draft.id, img1.image_id, "inline", "scott")
    assert store._would_collide(store.get_draft(draft.id), img2.image_id, "photo.png") is True
    # Same image_id is not a collision.
    assert store._would_collide(store.get_draft(draft.id), img1.image_id, "photo.png") is False


def test_unique_filename_derives_successive_suffixes(store: Store) -> None:
    """_unique_filename finds the lowest free suffix."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_image(png_bytes((1, 2, 3)), "photo.png")
    store.attach_image(draft.id, img1.image_id, "inline", "scott")

    result = store._unique_filename(store.get_draft(draft.id), "fake", "photo.png")
    assert result == "photo-2.png"


def test_unique_filename_skips_existing_suffixes(store: Store) -> None:
    """When photo-2.png is already taken, the next candidate is photo-3.png."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_image(png_bytes((1, 2, 3)), "photo.png")
    img2, _ = store.put_image(png_bytes((4, 5, 6)), "photo-2.png")
    img3, _ = store.put_image(png_bytes((7, 8, 9)), "photo-3.png")
    store.attach_image(draft.id, img1.image_id, "inline", "scott")
    store.attach_image(draft.id, img2.image_id, "feature", "scott")
    store.attach_image(draft.id, img3.image_id, "inline", "scott")

    result = store._unique_filename(store.get_draft(draft.id), "fake", "photo.png")
    assert result == "photo-4.png"


def test_unique_filename_is_case_insensitive(store: Store) -> None:
    """Image.PNG and image.png cannot both be on the draft."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_image(png_bytes((1, 2, 3)), "Image.PNG")
    store.attach_image(draft.id, img1.image_id, "inline", "scott")

    result = store._unique_filename(store.get_draft(draft.id), "fake", "image.png")
    assert result == "image-2.png"


# --- Store-level paste: put_and_attach_image auto-derives ------------------


def test_put_and_attach_image_derives_on_collision(store: Store) -> None:
    """put_and_attach_image auto-derives a unique filename when there is a
    collision, instead of raising 409.
    """
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((10, 20, 30)), "image.png", "inline", "scott"
    )
    assert img1.filename == "image.png"

    img2, _ = store.put_and_attach_image(
        draft.id, png_bytes((20, 30, 40)), "image.png", "inline", "scott"
    )
    assert img2.filename == "image-2.png"

    draft = store.get_draft(draft.id)
    filenames = {item.filename for item in draft.images}
    assert filenames == {"image.png", "image-2.png"}


def test_put_and_attach_image_re_attach_same_image_is_no_op(store: Store) -> None:
    """Re-pasting the same content (same image_id) updates the role but
    does not create a second entry or change the filename.
    """
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((10, 20, 30)), "image.png", "inline", "scott"
    )
    assert img1.filename == "image.png"

    # Re-attach same image under feature role.
    img2, created = store.put_and_attach_image(
        draft.id, png_bytes((10, 20, 30)), "image.png", "feature", "scott"
    )
    assert not created  # same content, already stored
    assert img2.image_id == img1.image_id

    draft = store.get_draft(draft.id)
    assert len(draft.images) == 1
    assert draft.images[0].role == "feature"
    assert draft.images[0].filename == "image.png"


def test_put_and_attach_image_refuses_unreferenceable_inline(store: Store) -> None:
    """An inline reference to an already-stored image whose name is not a
    plain filename is still refused.
    """
    draft, _ = store.create_draft("scott")
    with pytest.raises(ApiError) as caught:
        store.put_and_attach_image(
            draft.id,
            png_bytes((10, 20, 30)),
            "my photo.png",  # space not allowed in markdown ref
            "inline",
            "scott",
        )
    assert caught.value.code == "image_filename_unreferenceable"


def _make_draft_id(client: TestClient, services: Services) -> str:
    """Create a draft in ``drafting`` status for the client+services combo."""
    store = services.store
    draft, _warnings = store.create_draft("scott")
    store.save_draft(draft.id, "scott", draft.version_no, {"title": "Test"}, "body")
    return draft.id
