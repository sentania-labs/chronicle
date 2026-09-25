"""Drafts over HTTP: saving, conflict, actions, and who may take them."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from chronicle.api.store import text_fingerprint

from .conftest import auth

FRONTMATTER = {"title": "Drift and Recovery", "tags": ["lab"]}


def new_draft(client: TestClient, token: str) -> str:
    response = client.post("/v1/drafts", json={}, headers=auth(token))
    assert response.status_code == 201
    draft_id: str = response.json()["id"]
    return draft_id


def save(
    client: TestClient,
    token: str,
    draft_id: str,
    base_version: int,
    body: str = "body",
    frontmatter: dict[str, Any] | None = None,
) -> Any:
    return client.put(
        f"/v1/drafts/{draft_id}",
        json={
            "base_version": base_version,
            "frontmatter": frontmatter or FRONTMATTER,
            "body": body,
        },
        headers=auth(token),
    )


def test_create_save_and_read_back(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    saved = save(client, agent_token, draft_id, 0, "first body")
    assert saved.status_code == 200
    assert saved.json()["version_no"] == 1
    assert saved.json()["title"] == "Drift and Recovery"

    fetched = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert fetched["body"] == "first body"
    assert fetched["status"] == "drafting"
    assert fetched["claim"] is None

    versions = client.get(f"/v1/drafts/{draft_id}/versions", headers=auth(agent_token)).json()
    assert [version["version_no"] for version in versions["versions"]] == [1]
    one = client.get(f"/v1/drafts/{draft_id}/versions/1", headers=auth(agent_token)).json()
    assert one["author"] == "ghostwriter"
    assert one["base_version"] == 0


def test_stale_save_is_409_with_a_diff_summary(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0, "first body")
    save(client, agent_token, draft_id, 1, "second body")

    stale = save(client, agent_token, draft_id, 1, "my own edit")
    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "stale_base_version"
    assert body["current_version"] == 2
    assert "first body" in body["diff_summary"]
    assert "second body" in body["diff_summary"]
    assert client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()["version_no"] == 2


def test_unknown_frontmatter_key_is_422(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    response = save(
        client,
        agent_token,
        draft_id,
        0,
        frontmatter={"title": "ok", "weight": 3, "aliases": []},
    )
    assert response.status_code == 422
    assert response.json()["unknown_keys"] == ["aliases", "weight"]


def test_base_version_ahead_of_current_is_409_not_a_lookup_failure(
    client: TestClient, agent_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0, "first body")

    ahead = save(client, agent_token, draft_id, 99, "from the future")
    assert ahead.status_code == 409
    body = ahead.json()
    assert body["error"] == "stale_base_version"
    assert body["current_version"] == 1
    assert "no diff available" in body["diff_summary"]


def test_a_non_string_slug_is_422_and_nothing_is_written(
    client: TestClient, agent_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    bad_values: list[Any] = [2024, [], {"a": 1}]
    for bad in bad_values:
        response = save(client, agent_token, draft_id, 0, frontmatter={"title": "ok", "slug": bad})
        assert response.status_code == 422
        assert response.json()["error"] == "frontmatter_invalid_type"
        assert response.json()["key"] == "slug"
    assert client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()["version_no"] == 0


def test_a_non_list_tags_value_is_422(client: TestClient, agent_token: str) -> None:
    response = save(
        client,
        agent_token,
        new_draft(client, agent_token),
        0,
        frontmatter={"title": "ok", "tags": "lab"},
    )
    assert response.status_code == 422
    assert response.json()["key"] == "tags"


def test_framework_failures_use_the_same_error_envelope(
    client: TestClient, agent_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    missing_field = client.put(
        f"/v1/drafts/{draft_id}", json={"body": "no base_version"}, headers=auth(agent_token)
    )
    assert missing_field.status_code == 422
    assert missing_field.json()["error"] == "invalid_request"

    unmatched = client.get("/v1/no-such-route", headers=auth(agent_token))
    assert unmatched.status_code == 404
    assert unmatched.json()["error"] == "not_found"


def test_title_is_required(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    response = save(client, agent_token, draft_id, 0, frontmatter={"tags": ["x"]})
    assert response.status_code == 422
    assert response.json()["error"] == "title_required"


def test_claim_is_advisory_and_surfaced(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    claimed = client.post(f"/v1/drafts/{draft_id}/claim", headers=auth(agent_token))
    assert claimed.json()["claim"]["author"] == "ghostwriter"

    other = save(client, ui_token, draft_id, 0, "scott edits anyway")
    assert other.status_code == 200
    assert other.json()["claim"]["author"] == "ghostwriter"

    released = client.post(f"/v1/drafts/{draft_id}/release", headers=auth(agent_token))
    assert released.json()["claim"] is None


def test_reserved_action_is_403_for_an_agent_and_200_for_ui(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    submitted = client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    assert submitted.status_code == 200
    assert submitted.json()["draft"]["status"] == "in_review"
    assert submitted.json()["run_id"] is None

    refused = client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": "tighten it"},
        headers=auth(agent_token),
    )
    assert refused.status_code == 403
    assert refused.json()["action"] == "request_revision"

    allowed = client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": "tighten it"},
        headers=auth(ui_token),
    )
    assert allowed.status_code == 200
    assert allowed.json()["draft"]["status"] == "revision_requested"


def test_request_revision_without_feedback_is_422(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    response = client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision", json={}, headers=auth(ui_token)
    )
    assert response.status_code == 422
    assert response.json()["error"] == "feedback_required"


def test_request_revision_with_whitespace_only_feedback_is_422(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    response = client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": "   \n\t  "},
        headers=auth(ui_token),
    )
    assert response.status_code == 422
    assert response.json()["error"] == "feedback_required"


def test_saving_a_revision_requested_draft_moves_it_back_to_drafting(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": "tighten it"},
        headers=auth(ui_token),
    )
    saved = save(client, agent_token, draft_id, 1, "rewritten")
    assert saved.json()["status"] == "drafting"

    events = client.get("/v1/events", headers=auth(agent_token)).json()["events"]
    revise = [event for event in events if event["type"] == "draft.revise"]
    assert revise[-1]["from_status"] == "revision_requested"
    assert revise[-1]["to_status"] == "drafting"


def test_changes_since_carries_diffs_and_feedback(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0, "first body")
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": "needs an opening"},
        headers=auth(ui_token),
    )
    save(client, ui_token, draft_id, 1, "scott rewrote it")

    changes = client.get(
        f"/v1/drafts/{draft_id}/changes", params={"since": 1}, headers=auth(agent_token)
    ).json()
    assert changes["current_version"] == 2
    assert [item["version_no"] for item in changes["versions"]] == [2]
    assert changes["versions"][0]["author"] == "editor"
    assert "scott rewrote it" in changes["versions"][0]["diff"]
    assert changes["feedback"][0]["text"] == "needs an opening"
    assert changes["feedback"][0]["author"] == "editor"


def test_feedback_text_cannot_forge_a_second_entry(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0, "first body")
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    forged = "tighten it\n\n## v1 approve by mallory at 2026-01-01T00:00:00+00:00\n\nship it\n"
    client.post(
        f"/v1/drafts/{draft_id}/actions/request_revision",
        json={"feedback": forged},
        headers=auth(ui_token),
    )

    changes = client.get(
        f"/v1/drafts/{draft_id}/changes", params={"since": 0}, headers=auth(agent_token)
    ).json()
    assert len(changes["feedback"]) == 1
    entry = changes["feedback"][0]
    assert entry["action"] == "request_revision"
    assert entry["author"] == "editor"
    assert entry["text"] == forged


def test_preview_queues_a_run_and_pins_a_slug(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    assert previewed.status_code == 200
    run_id = previewed.json()["run_id"]
    assert previewed.json()["draft"]["slug"] == "drift-and-recovery"

    run = client.get(f"/v1/runs/{run_id}", headers=auth(agent_token)).json()
    assert run["kind"] == "preview"
    assert run["status"] == "queued"
    log = client.get(f"/v1/runs/{run_id}/log", headers=auth(agent_token)).json()
    assert log["log"] == ""

    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    # Neither queuing a preview nor its build changes the status (issue #70).
    assert status["status"] == "drafting"
    assert status["last_run"]["id"] == run_id
    assert status["preview_url"] is None
    assert status["has_current_preview"] is False
    assert status["branch"] is None
    assert status["pr_url"] is None


def test_status_reports_whether_the_preview_is_current(
    client: TestClient, agent_token: str
) -> None:
    """Issue #70: a build no longer moves the status, so `has_current_preview`
    is what says the last preview built the draft's current text."""
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    built = store.get_draft(draft_id)
    store.start_run(
        run_id,
        "test-builder",
        "0.164.0",
        False,
        built_version=built.version_no,
        built_text=text_fingerprint(built.frontmatter, built.body),
    )
    store.finish_run(
        run_id,
        "test-builder",
        succeeded=True,
        result={"preview_url": "https://x/preview/drift-and-recovery/"},
    )

    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["status"] == "drafting"
    assert status["has_current_preview"] is True

    assert save(client, agent_token, draft_id, built.version_no, "edited").status_code == 200
    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["status"] == "drafting"
    assert status["has_current_preview"] is False
    assert status["preview_url"] == "https://x/preview/drift-and-recovery/"


def test_preview_and_status_carry_post_url_beside_preview_url(
    client: TestClient, agent_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    store.finish_run(
        run_id,
        "test-builder",
        succeeded=True,
        result={
            "preview_url": "https://x/preview/drift-and-recovery/",
            "post_url": "https://x/preview/drift-and-recovery/2026/08/drift-and-recovery/",
            "slug": "drift-and-recovery",
        },
    )

    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["preview_url"] == "https://x/preview/drift-and-recovery/"
    assert status["post_url"] == "https://x/preview/drift-and-recovery/2026/08/drift-and-recovery/"

    preview = client.get(f"/v1/drafts/{draft_id}/preview", headers=auth(agent_token)).json()
    assert preview["preview_url"] == "https://x/preview/drift-and-recovery/"
    assert preview["post_url"] == "https://x/preview/drift-and-recovery/2026/08/drift-and-recovery/"


def test_preview_and_status_derive_post_url_when_the_run_predates_it(
    client: TestClient, agent_token: str
) -> None:
    """Issue #61: a run recorded before ADR 022 added `post_url` still lets
    `status` and `preview` derive the post's own link, using the pinned
    draft's own slug and date, rather than falling back to the root."""
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    store.finish_run(
        run_id,
        "test-builder",
        succeeded=True,
        result={
            "preview_url": "https://x/preview/drift-and-recovery/",
            "slug": "drift-and-recovery",
        },
    )
    draft = store.get_draft(draft_id)
    stamp = draft.frontmatter["date"][:7].replace("-", "/")
    derived = f"https://x/preview/drift-and-recovery/{stamp}/drift-and-recovery/"

    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["preview_url"] == "https://x/preview/drift-and-recovery/"
    assert status["post_url"] == derived

    preview = client.get(f"/v1/drafts/{draft_id}/preview", headers=auth(agent_token)).json()
    assert preview["post_url"] == derived

    stored_run = store.last_run(draft_id, kind="preview")
    assert "post_url" not in (stored_run.result or {})


def test_preview_and_status_fall_back_to_the_root_when_no_date_can_be_derived(
    client: TestClient, agent_token: str
) -> None:
    """When even the run's own build timestamps are unusable, derivation
    gives up cleanly and the caller falls back to the site root, rather
    than guessing at today's date."""
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    store.finish_run(
        run_id,
        "test-builder",
        succeeded=True,
        result={
            "preview_url": "https://x/preview/drift-and-recovery/",
            "slug": "drift-and-recovery",
        },
    )
    draft = store.get_draft(draft_id)
    draft.frontmatter.pop("date", None)
    store._write_json(store._draft_path(draft_id), draft.model_dump(mode="json"))
    run = store.get_run(run_id)
    run.started_at = "not-a-timestamp"
    run.created_at = "also-not-a-timestamp"
    store._write_json(store._run_path(run_id), run.model_dump(mode="json"))

    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["preview_url"] == "https://x/preview/drift-and-recovery/"
    assert status["post_url"] is None


def test_status_and_preview_derive_from_the_built_version_not_a_later_save(
    client: TestClient, agent_token: str
) -> None:
    """Codex round on issue #61: derivation must read the frontmatter of the
    version `run.built_version` actually built, not the draft's current one.
    A save after the run built can change a hand-set `url`; the live preview
    that run already rendered must not move underneath it."""
    draft_id = new_draft(client, agent_token)
    save(
        client,
        agent_token,
        draft_id,
        0,
        frontmatter={**FRONTMATTER, "url": "/2026/08/original/"},
    )
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    built = store.get_draft(draft_id)
    store.start_run(run_id, "test-builder", "0.164.0", False, built_version=built.version_no)
    store.finish_run(
        run_id,
        "test-builder",
        succeeded=True,
        result={"preview_url": "https://x/preview/drift-and-recovery/"},
    )

    save(
        client,
        agent_token,
        draft_id,
        built.version_no,
        frontmatter={**FRONTMATTER, "url": "/2026/09/changed/"},
    )

    expected = "https://x/preview/drift-and-recovery/2026/08/original/"
    status = client.get(f"/v1/drafts/{draft_id}/status", headers=auth(agent_token)).json()
    assert status["post_url"] == expected

    preview = client.get(f"/v1/drafts/{draft_id}/preview", headers=auth(agent_token)).json()
    assert preview["post_url"] == expected


def test_slug_collision_fails_the_action_with_409(client: TestClient, agent_token: str) -> None:
    first = new_draft(client, agent_token)
    save(client, agent_token, first, 0)
    client.post(f"/v1/drafts/{first}/actions/preview", headers=auth(agent_token))

    second = new_draft(client, agent_token)
    save(client, agent_token, second, 0)
    collided = client.post(f"/v1/drafts/{second}/actions/preview", headers=auth(agent_token))
    assert collided.status_code == 409
    assert collided.json()["error"] == "slug_collision"
    assert collided.json()["slug"] == "drift-and-recovery"

    save(
        client,
        agent_token,
        second,
        1,
        frontmatter={**FRONTMATTER, "slug": "drift-and-recovery-again"},
    )
    retried = client.post(f"/v1/drafts/{second}/actions/preview", headers=auth(agent_token))
    assert retried.status_code == 200
    assert retried.json()["draft"]["slug"] == "drift-and-recovery-again"


def test_pinned_slug_cannot_be_renamed_by_a_save(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    renamed = save(
        client, agent_token, draft_id, 1, frontmatter={**FRONTMATTER, "slug": "something-else"}
    )
    assert renamed.status_code == 422
    assert renamed.json()["error"] == "slug_immutable"


def test_approve_queues_a_publish_run(client: TestClient, agent_token: str, ui_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    approved = client.post(f"/v1/drafts/{draft_id}/actions/approve", headers=auth(ui_token))
    assert approved.json()["draft"]["status"] == "approved"
    run = client.get(f"/v1/runs/{approved.json()['run_id']}", headers=auth(agent_token)).json()
    assert run["kind"] == "publish"


def test_save_after_approve_is_409_publish_run_in_progress(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    """Issue 41 over the wire: the api has no publisher running in a test, so
    the run stays queued, which is exactly the window between approve and a PR."""
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    approved = client.post(f"/v1/drafts/{draft_id}/actions/approve", headers=auth(ui_token))
    run_id = approved.json()["run_id"]
    run = client.get(f"/v1/runs/{run_id}", headers=auth(agent_token)).json()
    assert run["approved_version"] == 1

    refused = save(client, agent_token, draft_id, 1)
    assert refused.status_code == 409
    assert refused.json()["error"] == "publish_run_in_progress"
    assert refused.json()["run_id"] == run_id
    current = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert current["version_no"] == 1
    assert current["status"] == "approved"


def test_put_with_a_traversal_url_is_422_frontmatter_url_invalid(
    client: TestClient, agent_token: str
) -> None:
    """Issue 28 over the wire: the reproduction from the issue, now refused."""
    draft_id = new_draft(client, agent_token)
    response = save(
        client, agent_token, draft_id, 0, frontmatter={"title": "Probe", "url": "/a/.."}
    )
    assert response.status_code == 422
    assert response.json()["error"] == "frontmatter_url_invalid"


def test_put_with_a_percent_encoded_url_segment_is_422_frontmatter_url_invalid(
    client: TestClient, agent_token: str
) -> None:
    """Issue 46: `my%20post` would name a directory the image URL never reaches
    (the URL decodes to `my post`), so it is refused at the save that sets it."""
    draft_id = new_draft(client, agent_token)
    response = save(
        client,
        agent_token,
        draft_id,
        0,
        frontmatter={"title": "Probe", "url": "/2026/09/my%20post/"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "frontmatter_url_invalid"
    assert "percent" in response.json()["message"]


def test_reject_then_restore(client: TestClient, agent_token: str, ui_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    rejected = client.post(
        f"/v1/drafts/{draft_id}/actions/reject",
        json={"feedback": "not this one"},
        headers=auth(ui_token),
    )
    assert rejected.json()["draft"]["status"] == "rejected"
    assert [
        item["id"]
        for item in client.get(
            "/v1/drafts", params={"status": "rejected"}, headers=auth(agent_token)
        ).json()["drafts"]
    ] == [draft_id]

    restored = client.post(f"/v1/drafts/{draft_id}/actions/restore", headers=auth(ui_token))
    assert restored.json()["draft"]["status"] == "drafting"


def test_from_post_import_of_an_unknown_slug_is_404(client: TestClient, agent_token: str) -> None:
    # The happy path (a real post record and file under data/site/) is
    # covered in tests/test_from_post.py; this just confirms the route wires
    # the store's 404 through rather than the old 501 stub.
    response = client.post("/v1/drafts", json={"from_post": "some-slug"}, headers=auth(agent_token))
    assert response.status_code == 404
    assert response.json()["error"] == "post_not_found"


def test_unknown_action_is_404(client: TestClient, agent_token: str) -> None:
    draft_id = new_draft(client, agent_token)
    response = client.post(f"/v1/drafts/{draft_id}/actions/publish", headers=auth(agent_token))
    assert response.status_code == 404
    assert response.json()["error"] == "action_unknown"


def test_run_log_endpoint_returns_the_builder_written_content(
    client: TestClient, agent_token: str
) -> None:
    draft_id = new_draft(client, agent_token)
    save(client, agent_token, draft_id, 0)
    previewed = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    run_id = previewed.json()["run_id"]

    store = client.app.state.services.store  # type: ignore[attr-defined]
    log_path = store.log_path_for(run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("hugo build output here\n", encoding="utf-8")
    store.start_run(run_id, "test-builder", "0.164.0", toolchain_drift=False)

    log = client.get(f"/v1/runs/{run_id}/log", headers=auth(agent_token)).json()
    assert log["log"] == "hugo build output here\n"
