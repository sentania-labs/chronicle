"""Submissions are mutable while new or claimed, with history in git."""

from __future__ import annotations

import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from chronicle.api import gitrepo
from chronicle.api.errors import ApiError
from chronicle.api.models import Material, Submission
from chronicle.api.store import Store
from chronicle.api.transitions import (
    SUBMISSION_TRANSITIONS,
    resolve_submission,
    resolve_submission_revise,
)

from .conftest import auth


def git(store: Store, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(store.repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"},
    ).stdout


def create(client: TestClient, token: str, brief: str = "first brief") -> dict[str, object]:
    response = client.post(
        "/v1/submissions",
        json={"brief": brief, "materials": [{"name": "notes", "text": "original notes"}]},
        headers=auth(token),
    )
    assert response.status_code == 201
    body: dict[str, object] = response.json()
    return body


def revise_body(base_version: int, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "base_version": base_version,
        "brief": "second brief",
        "materials": [
            {"name": "notes", "text": "original notes"},
            {"name": "link", "url": "https://example.com/thread"},
        ],
        "image_ids": [],
    }
    body.update(overrides)
    return body


def test_a_new_submission_is_version_one_with_a_version_record(
    client: TestClient, agent_token: str
) -> None:
    created = create(client, agent_token)
    assert created["version_no"] == 1
    services = client.app.state.services  # type: ignore[attr-defined]
    version = services.store.get_submission_version(created["id"], 1)
    assert (version.author, version.brief, version.base_version) == (
        "ghostwriter",
        "first brief",
        0,
    )


def test_put_bumps_the_version_writes_a_version_record_and_an_event(
    client: TestClient, agent_token: str
) -> None:
    created = create(client, agent_token)

    response = client.put(
        f"/v1/submissions/{created['id']}",
        json=revise_body(1, message="added the thread"),
        headers=auth(agent_token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["version_no"] == 2
    assert body["brief"] == "second brief"
    assert [m["name"] for m in body["materials"]] == ["notes", "link"]
    assert body["status"] == "new"

    store: Store = client.app.state.services.store  # type: ignore[attr-defined]
    version = store.get_submission_version(created["id"], 2)  # type: ignore[arg-type]
    assert (version.author, version.base_version, version.message) == (
        "ghostwriter",
        1,
        "added the thread",
    )
    assert version.brief == "second brief"
    assert [v.version_no for v in store.list_submission_versions(created["id"])] == [1, 2]  # type: ignore[arg-type]

    events = [e for e in store.events_since(0)[0] if e.submission_id == created["id"]]
    assert [e.type for e in events] == ["submission.created", "submission.revise"]
    assert (events[1].actor, events[1].from_status, events[1].to_status) == (
        "ghostwriter",
        "new",
        "new",
    )

    fresh = client.get(f"/v1/submissions/{created['id']}", headers=auth(agent_token)).json()
    assert fresh["version_no"] == 2


def test_a_revision_is_a_git_commit_by_the_consumer_with_the_diff(
    client: TestClient, agent_token: str
) -> None:
    created = create(client, agent_token)
    client.put(f"/v1/submissions/{created['id']}", json=revise_body(1), headers=auth(agent_token))
    store: Store = client.app.state.services.store  # type: ignore[attr-defined]

    assert gitrepo.log_authors(store.repo_dir, limit=1) == ["ghostwriter"]
    patch = git(store, "log", "-1", "-p", "--format=%s")
    assert f"submission {created['id']}: version 2 by ghostwriter" in patch
    assert '+  "brief": "second brief"' in patch
    assert '-  "brief": "first brief"' in patch
    assert "https://example.com/thread" in patch


def test_stale_base_version_is_409_with_a_diff_summary(
    client: TestClient, agent_token: str
) -> None:
    created = create(client, agent_token)
    url = f"/v1/submissions/{created['id']}"
    client.put(url, json=revise_body(1), headers=auth(agent_token))

    stale = client.put(url, json=revise_body(1, brief="lost edit"), headers=auth(agent_token))

    assert stale.status_code == 409
    body = stale.json()
    assert body["error"] == "stale_base_version"
    assert body["current_version"] == 2
    assert body["base_version"] == 1
    assert "-first brief" in body["diff_summary"]
    assert "+second brief" in body["diff_summary"]
    assert "+url: https://example.com/thread" in body["diff_summary"]
    # Nothing was overwritten.
    assert client.get(url, headers=auth(agent_token)).json()["brief"] == "second brief"


def test_a_base_version_ahead_of_current_is_still_a_409(
    client: TestClient, agent_token: str
) -> None:
    created = create(client, agent_token)
    stale = client.put(
        f"/v1/submissions/{created['id']}", json=revise_body(9), headers=auth(agent_token)
    )
    assert stale.status_code == 409
    assert "does not exist" in stale.json()["diff_summary"]


@pytest.mark.parametrize("action", ["draft", "discard"])
def test_a_frozen_submission_refuses_edits_by_name(
    client: TestClient, agent_token: str, action: str
) -> None:
    created = create(client, agent_token)
    url = f"/v1/submissions/{created['id']}"
    client.post(f"{url}/claim", headers=auth(agent_token))
    if action == "draft":
        client.post(
            "/v1/drafts", json={"from_submission": created["id"]}, headers=auth(agent_token)
        )
        status = "drafted"
    else:
        client.post(f"{url}/discard", headers=auth(agent_token))
        status = "discarded"

    refused = client.put(url, json=revise_body(1), headers=auth(agent_token))

    assert refused.status_code == 409
    body = refused.json()
    assert body["error"] == "submission_frozen"
    assert status in body["message"]
    assert "edit the draft instead" in body["message"]
    assert client.get(url, headers=auth(agent_token)).json()["version_no"] == 1


@pytest.mark.parametrize("missing", ["materials", "image_ids", "brief"])
def test_put_that_omits_a_content_field_is_422_and_changes_nothing(
    client: TestClient, agent_token: str, missing: str
) -> None:
    created = create(client, agent_token)
    url = f"/v1/submissions/{created['id']}"
    body = revise_body(1)
    del body[missing]

    response = client.put(url, json=body, headers=auth(agent_token))

    assert response.status_code == 422
    fresh = client.get(url, headers=auth(agent_token)).json()
    assert fresh["version_no"] == 1
    assert [m["name"] for m in fresh["materials"]] == ["notes"]


@pytest.mark.parametrize("image_id", ["deadbeef", "../../etc/passwd", "f" * 64])
def test_put_with_an_unknown_image_id_is_422_and_changes_nothing(
    client: TestClient, agent_token: str, image_id: str
) -> None:
    created = create(client, agent_token)
    url = f"/v1/submissions/{created['id']}"

    response = client.put(url, json=revise_body(1, image_ids=[image_id]), headers=auth(agent_token))

    assert response.status_code == 422
    assert response.json()["error"] == "image_not_found"
    assert client.get(url, headers=auth(agent_token)).json()["version_no"] == 1


def test_put_with_an_uploaded_image_id_is_accepted(client: TestClient, agent_token: str) -> None:
    from .conftest import png_bytes

    created = create(client, agent_token)
    image = client.post(
        "/v1/images",
        files={"file": ("shot.png", png_bytes(), "image/png")},
        headers=auth(agent_token),
    ).json()
    response = client.put(
        f"/v1/submissions/{created['id']}",
        json=revise_body(1, image_ids=[image["image_id"]]),
        headers=auth(agent_token),
    )
    assert response.status_code == 200
    assert response.json()["image_ids"] == [image["image_id"]]


def test_a_claimed_submission_can_still_be_edited(client: TestClient, agent_token: str) -> None:
    created = create(client, agent_token)
    url = f"/v1/submissions/{created['id']}"
    client.post(f"{url}/claim", headers=auth(agent_token))
    response = client.put(url, json=revise_body(1), headers=auth(agent_token))
    assert response.status_code == 200
    assert response.json()["status"] == "claimed"


def test_put_requires_a_consumer_token(client: TestClient, agent_token: str) -> None:
    created = create(client, agent_token)
    response = client.put(f"/v1/submissions/{created['id']}", json=revise_body(1))
    assert response.status_code == 401


def test_a_submission_written_before_versioning_still_loads_and_can_be_revised(
    store: Store,
) -> None:
    legacy = {
        "id": "legacy0001",
        "created_at": "2026-09-01T10:00:00-05:00",
        "from": "ghostwriter",
        "brief": "an old brief",
        "materials": [{"name": "notes", "text": "old notes", "url": None}],
        "image_ids": [],
        "status": "claimed",
        "claimed_by": "ghostwriter",
        "draft_id": None,
    }
    path = store.submissions_dir / "legacy0001.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    gitrepo.commit_all(store.repo_dir, "legacy submission", "ghostwriter")

    assert Submission.model_validate(legacy).version_no == 1
    record = store.get_submission("legacy0001")
    assert (record.version_no, record.brief) == (1, "an old brief")

    revised = store.revise_submission(
        "legacy0001",
        "scott",
        1,
        "a new brief",
        [Material(name="notes", text="old notes")],
        [],
    )

    assert revised.version_no == 2
    # The first revision backfills version 1 so the history has a start.
    assert store.get_submission_version("legacy0001", 1).brief == "an old brief"
    assert store.get_submission_version("legacy0001", 1).author == "ghostwriter"
    with pytest.raises(ApiError) as caught:
        store.revise_submission("legacy0001", "scott", 1, "x", [], [])
    assert "-an old brief" in caught.value.extra["diff_summary"]


def test_an_unrevised_legacy_submission_has_no_diff_for_a_stale_base(store: Store) -> None:
    legacy = {
        "id": "legacy0002",
        "created_at": "2026-09-01T10:00:00-05:00",
        "from": "ghostwriter",
        "brief": "an old brief",
        "materials": [],
        "image_ids": [],
        "status": "new",
    }
    (store.submissions_dir / "legacy0002.json").write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(ApiError) as caught:
        store.revise_submission("legacy0002", "scott", 0, "x", [], [])
    assert caught.value.status_code == 409
    assert "does not exist" in caught.value.extra["diff_summary"]


def test_reindex_survives_version_files_beside_the_records(store: Store) -> None:
    created = store.create_submission("ghostwriter", "b", [], [])
    store.revise_submission(created.id, "ghostwriter", 1, "b2", [], [])
    counts = store.reindex()
    assert counts["submissions"] == 1
    assert [s.brief for s in store.list_submissions()] == ["b2"]


def test_the_transition_table_decides_who_may_be_revised() -> None:
    assert resolve_submission_revise("new", "s").to_status == "new"
    assert resolve_submission_revise("claimed", "s").to_status == "claimed"
    for status in ("drafted", "discarded"):
        with pytest.raises(ApiError) as caught:
            resolve_submission_revise(status, "s")
        assert caught.value.code == "submission_frozen"
    assert ("new", "revise") in SUBMISSION_TRANSITIONS
    assert resolve_submission("claimed", "revise").to_status == "claimed"
