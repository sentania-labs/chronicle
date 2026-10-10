"""Issue 86: rename an attachment and rewrite its references; publish refuses a
body reference with no staged image.

AC1: renaming an attachment rewrites every body reference in the same save,
with a 409 on a name conflict; the editor exposes it.

AC2: publish refuses a body reference with no matching attachment, naming it;
the editor shows the same lint before publish.

AC3: tests cover rename rewriting, the conflict, the publish refusal and the lint.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chronicle.api import convert, lint_renaming
from chronicle.api.deps import Services
from chronicle.api.errors import ApiError
from chronicle.api.publisher import PublishFailed, _check_missing_image_refs
from chronicle.api.store import Store

from .conftest import auth, png_bytes

# ---------------------------------------------------------------------------
# AC1: rename rewrites body references (store-level tests)
# ---------------------------------------------------------------------------


def test_rename_rewrites_bare_markdown_ref(store: Store) -> None:
    """Renaming an image rewrites bare ``![alt](old.png)`` references."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((1, 0, 0)), "photo.png", "inline", "scott"
    )
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        f"Some text ![alt]({img1.filename})",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "pic-test"
    draft.image_dir = "pic-test"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    draft = store.rename_image(draft.id, img1.image_id, "picture.png", "scott")
    assert "![alt](picture.png)" in draft.body
    assert "photo.png" not in draft.body


def test_rename_rewrites_slash_ref(store: Store) -> None:
    """Renaming rewrites ``/images/<slug>/old.png`` references too."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((2, 0, 0)), "photo.png", "inline", "scott"
    )
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        "Before /images/pic-test/photo.png after",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "pic-test"
    draft.image_dir = "pic-test"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    draft = store.rename_image(draft.id, img1.image_id, "picture.png", "scott")
    assert "/images/pic-test/picture.png" in draft.body
    assert "photo.png" not in draft.body


def test_rename_leaves_code_spans_untouched(store: Store) -> None:
    """Inline code and fenced code blocks are never rewritten."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((3, 0, 0)), "photo.png", "inline", "scott"
    )
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        "Normal ![alt](photo.png)\n`![alt](photo.png)`\n```\n![alt](photo.png)\n```\n",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "pic-test"
    draft.image_dir = "pic-test"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    draft = store.rename_image(draft.id, img1.image_id, "picture.png", "scott")
    assert "![alt](picture.png)" in draft.body
    assert "`![alt](photo.png)`" in draft.body
    assert "![alt](photo.png)" in draft.body


def test_rename_409_on_conflict(store: Store) -> None:
    """Renaming onto an existing name returns 409."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((4, 0, 0)), "photo.png", "inline", "scott"
    )
    img2, _ = store.put_and_attach_image(
        draft.id, png_bytes((5, 0, 0)), "other.png", "feature", "scott"
    )

    with pytest.raises(ApiError) as exc_info:
        store.rename_image(draft.id, img1.image_id, "other.png", "scott")
    assert exc_info.value.code == "image_filename_conflict"

    with pytest.raises(ApiError) as exc_info:
        store.rename_image(draft.id, img1.image_id, "OTHER.PNG", "scott")
    assert exc_info.value.code == "image_filename_conflict"


def test_rename_records_event(client: TestClient, services: Services, agent_token: str) -> None:
    """A rename writes a ``draft.image_rename`` event."""
    draft_id = _make_drafting(client, services)
    store = services.store
    resp = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("photo.png", png_bytes((6, 0, 0)), "image/png")},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200, f"attach failed: {resp.json()}"
    img1 = resp.json()

    draft = store.get_draft(draft_id)
    draft.slug = "pic-test"
    draft.image_dir = "pic-test"
    store._write_json(store._draft_path(draft_id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    events_before = _event_count(services, draft_id)

    resp = client.post(
        f"/content/drafts/{draft_id}/images/{img1['image_id']}/rename",
        data={"filename": "picture.png"},
        headers=auth(agent_token),
    )
    assert resp.status_code == 200, f"rename failed: {resp.json()}"

    draft = store.get_draft(draft_id)
    assert draft.images[0].filename == "picture.png"
    events_after = _event_count(services, draft_id)
    assert events_after == events_before + 1


# ---------------------------------------------------------------------------
# AC2: publish refuses missing image references
# ---------------------------------------------------------------------------


def test_publish_refuses_missing_image_reference(store: Store) -> None:
    """A body reference to /images/<slug>/<file> with no attachment raises."""
    draft, _ = store.create_draft("scott")
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        "Here is an image /images/no-image/missing.png in the body.",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "no-image"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))

    with pytest.raises(PublishFailed) as exc_info:
        _check_missing_image_refs(draft)

    assert exc_info.value.error_class == "missing_image"
    assert "missing.png" in str(exc_info.value)


def test_store_rename_rewrites_and_changes_filename(store: Store) -> None:
    """Store-level rename updates DraftImage and rewrites body."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((10, 20, 30)), "photo.png", "inline", "scott"
    )

    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        f"![alt]({img1.filename})",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "renamed-test"
    draft.image_dir = "renamed-test"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    store.rename_image(draft.id, img1.image_id, "picture.png", "scott")

    draft = store.get_draft(draft.id)
    assert draft.images[0].filename == "picture.png"
    assert "![alt](picture.png)" in draft.body
    assert "photo.png" not in draft.body


def test_store_rename_409_conflict(store: Store) -> None:
    """Store-level rename returns 409 when target name is taken."""
    draft, _ = store.create_draft("scott")
    img1, _ = store.put_and_attach_image(
        draft.id, png_bytes((7, 0, 0)), "photo.png", "inline", "scott"
    )
    img2, _ = store.put_and_attach_image(
        draft.id, png_bytes((8, 0, 0)), "other.png", "feature", "scott"
    )

    with pytest.raises(ApiError) as exc_info:
        store.rename_image(draft.id, img1.image_id, "other.png", "scott")

    assert exc_info.value.code == "image_filename_conflict"


def test_store_lint_body_image_refs_reports_missing(store: Store) -> None:
    """lint_body_image_refs returns warnings for dangling refs."""
    draft, _ = store.create_draft("scott")
    img, _ = store.put_and_attach_image(
        draft.id, png_bytes((9, 0, 0)), "good.png", "inline", "scott"
    )
    store.save_draft(
        draft.id,
        "scott",
        draft.version_no,
        {"title": "Test"},
        "Reference /images/dangling/missing.png /images/dangling/good.png",
    )
    draft = store.get_draft(draft.id)
    draft.slug = "dangling"
    draft.image_dir = "dangling"
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))

    warnings = store.lint_body_image_refs(draft.id, slug="dangling")
    # only missing.png is a warning; good.png matches an attachment
    assert len(warnings) >= 1
    assert "missing.png" in warnings[0]


def test_editor_lint_reports_missing_references(
    client: TestClient, services: Services, agent_token: str
) -> None:
    """GET /content/drafts/<id>/lint returns warnings for dangling refs."""
    draft_id = _make_drafting(client, services)
    store = services.store

    store.save_draft(
        draft_id,
        "scott",
        store.get_draft(draft_id).version_no,
        {"title": "Test"},
        "Reference /images/lint-test/ghost.png here",
    )

    resp = client.get(
        f"/content/drafts/{draft_id}/lint",
        headers=auth(agent_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert len(data["warnings"]) >= 1
    assert "ghost.png" in data["warnings"][0]


def test_editor_lint_empty_when_all_refs_exist(
    client: TestClient, services: Services, agent_token: str
) -> None:
    """Lint returns an empty list when every reference is attached."""
    draft_id = _make_drafting(client, services)
    store = services.store

    resp = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("ok.png", png_bytes(), "image/png")},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200, f"attach failed: {resp.json()}"
    filename = resp.json()["filename"]

    store.save_draft(
        draft_id,
        "scott",
        store.get_draft(draft_id).version_no,
        {"title": "Test"},
        f"Reference /images/ok-test/{filename}",
    )
    draft = store.get_draft(draft_id)
    draft.slug = "ok-test"
    draft.image_dir = "ok-test"
    store._write_json(store._draft_path(draft_id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    resp = client.get(
        f"/content/drafts/{draft_id}/lint",
        headers=auth(agent_token),
    )
    assert resp.status_code == 200
    assert resp.json()["warnings"] == []


def test_rename_body_and_convert(client: TestClient, services: Services, agent_token: str) -> None:
    """Rename then convert produces valid output with the new name."""
    draft_id = _make_drafting(client, services)
    store = services.store

    resp = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("photo.png", png_bytes((11, 0, 0)), "image/png")},
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200, f"attach failed: {resp.json()}"
    img1 = resp.json()

    store.save_draft(
        draft_id,
        "scott",
        store.get_draft(draft_id).version_no,
        {"title": "Test"},
        "![alt](photo.png)",
    )
    draft = store.get_draft(draft_id)
    draft.slug = "pic-test"
    draft.image_dir = "pic-test"
    store._write_json(store._draft_path(draft_id), draft.model_dump(mode="json"))
    store._commit("set slug", "scott")

    resp = client.post(
        f"/content/drafts/{draft_id}/images/{img1['image_id']}/rename",
        data={"filename": "picture.png"},
        headers=auth(agent_token),
    )
    assert resp.status_code == 200, f"rename failed: {resp.json()}"

    draft = store.get_draft(draft_id)
    converted = convert.convert(draft)
    assert "![alt](" in converted.text and "picture.png" in converted.text


# ---------------------------------------------------------------------------
# AC3: store-level rename via lint_renaming.rewrite_body_image_ref
# ---------------------------------------------------------------------------


def test_rewrite_body_image_ref_rewrites_bare(client: TestClient, services: Services) -> None:
    body = "![alt](photo.png) and ![other](photo.png)"
    new_body, replaced = lint_renaming.rewrite_body_image_ref(body, "photo.png", "pic.png", "test")
    assert new_body == "![alt](pic.png) and ![other](pic.png)"
    assert len(replaced) == 2


def test_rewrite_body_image_ref_rewrites_slash(client: TestClient, services: Services) -> None:
    body = "/images/test/photo.png"
    new_body, replaced = lint_renaming.rewrite_body_image_ref(body, "photo.png", "pic.png", "test")
    assert new_body == "/images/test/pic.png"
    assert len(replaced) == 1


def test_rewrite_body_image_ref_skips_code(client: TestClient, services: Services) -> None:
    body = "![alt](photo.png) ``![alt](photo.png)``"
    new_body, replaced = lint_renaming.rewrite_body_image_ref(body, "photo.png", "pic.png", "test")
    assert "![alt](pic.png)" in new_body
    assert "![alt](photo.png)" in new_body


def test_rewrite_body_image_ref_no_match(client: TestClient, services: Services) -> None:
    body = "![alt](other.png)"
    new_body, replaced = lint_renaming.rewrite_body_image_ref(body, "photo.png", "pic.png", "test")
    assert new_body == body
    assert replaced == []


def test_rewrite_body_image_ref_ignores_wrong_slug(client: TestClient, services: Services) -> None:
    body = "/images/other/photo.png"
    new_body, replaced = lint_renaming.rewrite_body_image_ref(body, "photo.png", "pic.png", "test")
    assert new_body == body
    assert replaced == []


def test_lint_staged_images_from_attachments_finds_missing(
    client: TestClient, services: Services
) -> None:
    body = "/images/test/ghost.png"
    warnings = lint_renaming.lint_staged_images_from_attachments(
        body,
        slug="test",
        attachments={"real.png"},
        attachments_lower={"real.png": "real.png"},
    )
    assert len(warnings) == 1
    assert "ghost.png" in warnings[0]


def test_lint_staged_images_from_attachments_passes_valid(
    client: TestClient, services: Services
) -> None:
    body = "/images/test/real.png"
    warnings = lint_renaming.lint_staged_images_from_attachments(
        body,
        slug="test",
        attachments={"real.png"},
        attachments_lower={"real.png": "real.png"},
    )
    assert warnings == []


def test_lint_staged_images_from_attachments_skips_code(
    client: TestClient, services: Services
) -> None:
    body = "/images/test/ghost.png ``/images/test/ghost.png``"
    warnings = lint_renaming.lint_staged_images_from_attachments(
        body,
        slug="test",
        attachments={"real.png"},
        attachments_lower={"real.png": "real.png"},
    )
    # Only the non-code segment produces warnings (2: spaces + missing).
    # The inline code span is skipped entirely.
    assert len(warnings) == 2
    assert all("ghost.png" in w for w in warnings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_drafting(client: TestClient, services: Services) -> str:
    """Create a draft at ``drafting`` status and return its id."""
    store = services.store
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", draft.version_no, {"title": "Test"}, "body")
    return draft.id


def _event_count(services: Services, draft_id: str) -> int:
    """Count events for a draft from the raw log file."""
    import json
    from pathlib import Path

    events_file = Path(services.store.events_file)
    count = 0
    if events_file.exists():
        with events_file.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        ev = json.loads(line)
                        if ev.get("draft_id") == draft_id:
                            count += 1
                    except json.JSONDecodeError:
                        pass
    return count
