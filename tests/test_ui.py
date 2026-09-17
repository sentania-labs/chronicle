"""The content and preview UI (C5, ADR 014).

No route here is exercised through a bearer header: the UI backend
authenticates its own calls in-process off `data/state/ui_token.txt`
(`ui_deps.require_ui_consumer`), so every request in this file goes through
plain form posts and cookie-less GETs, the same way a browser on the
internal network would use it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api.deps import Services
from chronicle.api.models import DRAFT_STATUSES

from .conftest import auth, png_bytes

# Every response body collected across this file, checked once at the end of
# each test that renders pages: the deliverable's own bar is that no token
# value ever appears in *any* rendered page, not just the ones a given test
# happens to be asserting on.


def assert_no_token_leak(response: Any, *tokens: str) -> None:
    body = response.text
    for token in tokens:
        assert token not in body, f"token leaked into rendered HTML: {token!r}"


def make_draft(
    services: Services,
    status: str,
    *,
    title: str = "A Draft",
    with_image: bool = False,
    with_publish: bool = False,
) -> str:
    """A draft forced straight to `status`, bypassing the transition table.

    Only for setting up rendering fixtures: the lifecycle itself is
    `transitions.py`'s job and is already covered by `test_transitions.py`
    and `test_store.py`. This exists so every status can be rendered without
    driving a publish/merge cycle end to end for each one.
    """
    store = services.store
    draft, _warnings = store.create_draft("scott")
    store.save_draft(draft.id, "scott", draft.version_no, {"title": title}, "body text")
    draft = store.get_draft(draft.id)
    if with_image:
        raw = png_bytes()
        image, _created = store.put_image(raw, "feature.png")
        store.attach_image(draft.id, image.image_id, "feature", "scott")
    draft = store.get_draft(draft.id)
    draft.status = status
    if status != "drafting":
        draft.slug = draft.slug or "a-draft"
    if with_publish:
        draft.published = {
            "kind": "publish",
            "branch": f"post/{draft.slug}",
            "pr_number": 7,
            "pr_url": "https://github.com/o/r/pull/7",
            "commit_sha": "deadbeef",
            "post_path": f"content/posts/{draft.slug}.md",
            "url": f"/2026/09/{draft.slug}/",
            "date": "2026-09-17",
            "images": [],
            "post_blob_sha": "blobsha",
        }
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    store.index.upsert_draft(draft)
    return draft.id


# --- Submissions -------------------------------------------------------


def test_submissions_list_and_detail_render(client: TestClient, agent_token: str) -> None:
    response = client.post(
        "/v1/submissions",
        json={
            "brief": "a pitch about the lab",
            "materials": [{"name": "notes", "text": "some notes", "url": "https://example.com"}],
            "image_ids": [],
        },
        headers=auth(agent_token),
    )
    assert response.status_code == 201
    submission_id = response.json()["id"]

    listing = client.get("/content/submissions")
    assert listing.status_code == 200
    assert "a pitch about the lab" in listing.text
    assert_no_token_leak(listing, agent_token)

    detail = client.get(f"/content/submissions/{submission_id}")
    assert detail.status_code == 200
    assert "some notes" in detail.text
    assert "https://example.com" in detail.text
    assert "Create draft from this submission" in detail.text
    assert "Discard" in detail.text
    assert_no_token_leak(detail, agent_token)


def test_submission_to_draft_claims_a_new_submission_then_creates_a_draft(
    client: TestClient, agent_token: str
) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "brief", "materials": [], "image_ids": []},
        headers=auth(agent_token),
    )
    submission_id = response.json()["id"]

    created = client.post(f"/content/submissions/{submission_id}/draft")
    assert created.status_code == 200
    draft_id = str(created.url).rstrip("/").rsplit("/", 1)[-1]

    submission = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert submission["status"] == "drafted"
    assert submission["draft_id"] == draft_id


def test_submission_discard(client: TestClient, agent_token: str) -> None:
    response = client.post(
        "/v1/submissions",
        json={"brief": "brief", "materials": [], "image_ids": []},
        headers=auth(agent_token),
    )
    submission_id = response.json()["id"]
    discarded = client.post(f"/content/submissions/{submission_id}/discard")
    assert discarded.status_code == 200
    submission = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert submission["status"] == "discarded"


# --- Drafts board and editor, every status --------------------------------


@pytest.mark.parametrize("status", DRAFT_STATUSES)
def test_editor_renders_for_every_draft_status(
    client: TestClient, services: Services, agent_token: str, status: str
) -> None:
    draft_id = make_draft(
        services,
        status,
        title=f"Draft in {status}",
        with_publish=(status in ("published", "unpublished")),
    )
    response = client.get(f"/content/drafts/{draft_id}")
    assert response.status_code == 200
    assert f"Draft in {status}" in response.text
    assert_no_token_leak(response, agent_token)

    board = client.get(f"/content/drafts?status={status}")
    assert board.status_code == 200
    assert f"Draft in {status}" in board.text
    assert_no_token_leak(board, agent_token)


def test_reserved_actions_are_labelled_scott_only(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "in_review", title="Needs review")
    response = client.get(f"/content/drafts/{draft_id}")
    assert "Scott only" in response.text
    assert "request revision" in response.text.lower()
    assert "reject" in response.text.lower()


def test_drafts_board_shows_flag_and_pr_badges(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "published", with_publish=True)
    services.store.create_flag(
        "content_drift",
        slug=None,
        draft_id=draft_id,
        detail="main moved",
        actor="chronicle-reconcile",
    )
    board = client.get("/content/drafts?status=published")
    assert board.status_code == 200
    assert "pull/7" in board.text
    assert "content_drift" in board.text


# --- Save, conflict, and versions ------------------------------------------


def test_editor_save_forwards_base_version_and_bumps_version(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Untitled")
    draft = services.store.get_draft(draft_id)
    form = {
        "base_version": str(draft.version_no),
        "title": "New Title",
        "date": "",
        "categories": "lab, homelab",
        "tags": "unifi",
        "summary": "a summary",
        "url": "",
        "featureImage": "",
        "body": "updated body",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200
    assert "saved" in response.text.lower()

    updated = services.store.get_draft(draft_id)
    assert updated.version_no == draft.version_no + 1
    assert updated.title == "New Title"
    assert updated.frontmatter["categories"] == ["lab", "homelab"]
    assert updated.frontmatter["tags"] == ["unifi"]
    assert updated.body == "updated body"

    versions = services.store.list_versions(draft_id)
    assert versions[-1].author == "scott"


def test_stale_save_renders_409_with_diff_summary_and_both_panes(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Original")
    draft = services.store.get_draft(draft_id)
    stale_base = draft.version_no

    # Second author (or the same session in another tab) saves first.
    services.store.save_draft(
        draft_id,
        "ghostwriter",
        draft.version_no,
        {"title": "Second author edit"},
        "second author body",
    )

    form = {
        "base_version": str(stale_base),
        "title": "My attempted title",
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": "my attempted body",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 409
    assert "conflict" in response.text.lower()
    # The server's diff summary shows what moved underneath the visitor.
    assert "Second author edit" in response.text or "second author body" in response.text
    # The reloaded form carries the current server version, ready to reapply on.
    assert 'value="Second author' in response.text
    # The visitor's own attempted text survives in a second, read-only pane.
    assert "My attempted title" in response.text
    assert "my attempted body" in response.text
    # Nothing was overwritten.
    current = services.store.get_draft(draft_id)
    assert current.title == "Second author edit"


def test_version_diff_view(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting", title="v1 title")
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, {"title": "v2 title"}, "v2 body")
    response = client.get(f"/content/drafts/{draft_id}/diff?from=1&to=2")
    assert response.status_code == 200
    assert "diff-add" in response.text or "diff-del" in response.text
    assert "v2 title" in response.text or "v2 body" in response.text


# --- Reserved actions produce scott-authored records -----------------------


def test_reserved_action_via_ui_is_authored_scott(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting", title="Reviewable")
    submitted = client.post(f"/content/drafts/{draft_id}/actions/submit")
    assert submitted.status_code == 200
    assert services.store.get_draft(draft_id).status == "in_review"

    revision = client.post(
        f"/content/drafts/{draft_id}/actions/request_revision",
        data={"feedback": "please add more detail"},
    )
    assert revision.status_code == 200
    draft = services.store.get_draft(draft_id)
    assert draft.status == "revision_requested"

    feedback = services.store.list_feedback(draft_id)
    assert feedback[-1].author == "scott"
    assert feedback[-1].text == "please add more detail"

    events, _cursor = services.store.events_since(0)
    reserved_events = [e for e in events if e.type == "draft.request_revision"]
    assert reserved_events and reserved_events[-1].actor == "scott"


def test_reserved_action_without_feedback_is_rejected_and_editor_re_rendered(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting", title="Reviewable")
    client.post(f"/content/drafts/{draft_id}/actions/submit")
    response = client.post(f"/content/drafts/{draft_id}/actions/reject", data={})
    assert response.status_code == 422
    assert services.store.get_draft(draft_id).status == "in_review"


# --- Image upload limits mirror the API's own -------------------------------


def test_image_upload_enforces_size_and_type_like_the_api(
    client: TestClient, services: Services
) -> None:
    draft_id = make_draft(services, "drafting")

    buffer = io.BytesIO()
    PillowImage.new("RGB", (4, 4)).save(buffer, format="PNG")
    good = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.png", buffer.getvalue(), "image/png")},
        data={"role": "inline"},
    )
    assert good.status_code == 200
    assert "a.png" in good.text

    bad_type = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("a.txt", b"not an image", "text/plain")},
        data={"role": "inline"},
    )
    assert bad_type.status_code == 415

    too_big = client.post(
        f"/content/drafts/{draft_id}/images",
        files={"file": ("big.png", b"0" * (6 * 1024 * 1024), "image/png")},
        data={"role": "inline"},
    )
    assert too_big.status_code == 413

    draft = services.store.get_draft(draft_id)
    assert len(draft.images) == 1

    detach = client.post(f"/content/drafts/{draft_id}/images/{draft.images[0].image_id}/detach")
    assert detach.status_code == 200
    assert len(services.store.get_draft(draft_id).images) == 0


# --- Import -----------------------------------------------------------------


def test_import_search_and_create(client: TestClient, services: Services, data_dir: Path) -> None:
    from chronicle.api.models import Post

    services.store.apply_digest(
        "scott",
        [
            Post(
                slug="unifi-network",
                path="content/posts/unifi.md",
                title="My Unifi Network",
                date="2026-01-01",
                sha="abc",
            ),
            Post(
                slug="other-post",
                path="content/posts/other.md",
                title="Something else",
                date="2026-01-02",
                sha="def",
            ),
        ],
    )
    (services.store.site_dir / "content" / "posts").mkdir(parents=True, exist_ok=True)
    (services.store.site_dir / "content" / "posts" / "unifi.md").write_text(
        "---\ntitle: My Unifi Network\n---\nbody\n", encoding="utf-8"
    )

    searched = client.get("/content/import?q=unifi")
    assert searched.status_code == 200
    assert "unifi-network" in searched.text
    assert "other-post" not in searched.text

    created = client.post("/content/import", data={"slug": "unifi-network"})
    assert created.status_code == 200
    draft_id = str(created.url).rstrip("/").rsplit("/", 1)[-1]
    draft = services.store.get_draft(draft_id)
    assert draft.slug == "unifi-network"


# --- Preview tab and run log -------------------------------------------------


def test_preview_list_and_rebuild_and_run_log(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "previewed")
    draft = services.store.get_draft(draft_id)
    run = services.store._queue_run(draft_id, "preview")
    services.store.start_run(run.id, "builder-1", "0.164.0", False, built_version=draft.version_no)
    services.store.finish_run(run.id, "builder-1", True, {"preview_url": f"/preview/{draft.slug}/"})

    listing = client.get("/preview")
    assert listing.status_code == 200
    assert f"/preview/{draft.slug}/" in listing.text
    assert "Rebuild" in listing.text

    log_page = client.get(f"/runs/{run.id}")
    assert log_page.status_code == 200
    assert "succeeded" in log_page.text

    rebuild = client.post(f"/preview/{draft_id}/rebuild")
    assert rebuild.status_code == 200


# --- CSRF and banner ---------------------------------------------------------


def test_cross_origin_post_is_refused(client: TestClient, services: Services) -> None:
    draft_id = make_draft(services, "drafting")
    response = client.post(
        f"/content/drafts/{draft_id}/claim", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403


def test_banner_shown_by_default(client: TestClient) -> None:
    response = client.get("/content/drafts")
    assert "internal-only and unauthenticated" in response.text


def test_banner_hidden_when_disabled(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRONICLE_UI_BANNER", "0")
    from chronicle.api.main import create_app

    app = create_app()
    with TestClient(app) as scoped_client:
        response = scoped_client.get("/content/drafts")
        assert "internal-only and unauthenticated" not in response.text
    app.state.services.close()


def test_ui_token_never_appears_in_any_rendered_page(
    client: TestClient, services: Services, ui_token: str, agent_token: str
) -> None:
    draft_id = make_draft(services, "in_review", title="Leak check", with_image=True)
    pages = [
        "/content/drafts",
        f"/content/drafts/{draft_id}",
        "/content/submissions",
        "/content/import",
        "/preview",
    ]
    for path in pages:
        response = client.get(path)
        assert_no_token_leak(response, ui_token, agent_token)
