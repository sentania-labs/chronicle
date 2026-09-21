"""Per-draft social announcements (ADR 021).

The field lives on the draft and on every version, is saved through the one
save path, and never reaches the converted post. These tests cover all three
of those claims, plus what an older record on disk (written before the field
existed) loads as.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chronicle import backup
from chronicle.api import convert
from chronicle.api.deps import Services
from chronicle.api.errors import ApiError
from chronicle.api.models import Draft, Version, now_stamp
from chronicle.api.store import Store

from .conftest import auth

FRONTMATTER = {"title": "Announcing Things", "tags": ["lab"]}
THREE = {
    "x": "short and sharp",
    "bluesky": "the same, with more room",
    "linkedin": "the long, professional one",
}


def _new_draft(client: TestClient, token: str) -> str:
    response = client.post("/v1/drafts", json={}, headers=auth(token))
    assert response.status_code == 201
    draft_id: str = response.json()["id"]
    return draft_id


def _put(
    client: TestClient,
    token: str,
    draft_id: str,
    base_version: int,
    **extra: Any,
) -> Any:
    payload: dict[str, Any] = {
        "base_version": base_version,
        "frontmatter": FRONTMATTER,
        "body": "body",
    }
    payload.update(extra)
    return client.put(f"/v1/drafts/{draft_id}", json=payload, headers=auth(token))


# --- Over HTTP ----------------------------------------------------------


def test_put_stores_all_three_channels_and_get_reads_them_back(
    client: TestClient, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    saved = _put(client, agent_token, draft_id, 0, announcements=THREE)
    assert saved.status_code == 200
    assert saved.json()["announcements"] == THREE

    fetched = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert fetched["announcements"] == THREE


def test_the_version_record_carries_the_announcements(client: TestClient, agent_token: str) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    version = client.get(f"/v1/drafts/{draft_id}/versions/1", headers=auth(agent_token)).json()
    assert version["announcements"] == THREE


def test_a_later_save_that_omits_announcements_keeps_them(
    client: TestClient, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    second = _put(client, agent_token, draft_id, 1, body="a second body")
    assert second.status_code == 200
    assert second.json()["announcements"] == THREE
    assert second.json()["version_no"] == 2

    fetched = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert fetched["announcements"] == THREE


def test_an_explicit_empty_mapping_clears_them(client: TestClient, agent_token: str) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    cleared = _put(client, agent_token, draft_id, 1, announcements={})
    assert cleared.status_code == 200
    assert cleared.json()["announcements"] == {}


def test_a_stale_base_version_is_409_and_leaves_announcements_untouched(
    client: TestClient, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    stale = _put(client, agent_token, draft_id, 0, announcements={"x": "overwritten"})
    assert stale.status_code == 409
    assert stale.json()["error"] == "stale_base_version"

    fetched = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert fetched["announcements"] == THREE
    assert fetched["version_no"] == 1


def test_a_stale_base_version_wins_over_an_invalid_announcement(
    client: TestClient, agent_token: str
) -> None:
    # Ordering matters: the conflict is the more useful thing to report, and
    # it is what this save would have been before the field existed.
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    both_wrong = _put(client, agent_token, draft_id, 0, announcements={"instagram": "no"})
    assert both_wrong.status_code == 409


def test_an_unknown_channel_is_422(client: TestClient, agent_token: str) -> None:
    draft_id = _new_draft(client, agent_token)
    refused = _put(client, agent_token, draft_id, 0, announcements={"instagram": "nope"})
    assert refused.status_code == 422
    assert refused.json()["error"] == "announcement_channel_not_allowed"
    assert refused.json()["unknown_channels"] == ["instagram"]


def test_a_non_string_value_is_422(client: TestClient, agent_token: str) -> None:
    draft_id = _new_draft(client, agent_token)
    refused = _put(client, agent_token, draft_id, 0, announcements={"x": 5})
    assert refused.status_code == 422
    assert refused.json()["error"] == "announcement_wrong_type"


def test_a_non_mapping_is_refused_by_the_framework_in_the_same_envelope(
    client: TestClient, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    refused = _put(client, agent_token, draft_id, 0, announcements="just a string")
    assert refused.status_code == 422
    assert refused.json()["error"] == "invalid_request"


def test_announcement_text_has_no_length_limit(client: TestClient, agent_token: str) -> None:
    # Deliberate: the API's own body-size guard is the only ceiling, the same
    # one the draft body itself lives under. See ADR 021.
    draft_id = _new_draft(client, agent_token)
    long_text = "x" * 20000
    saved = _put(client, agent_token, draft_id, 0, announcements={"x": long_text})
    assert saved.status_code == 200
    assert saved.json()["announcements"]["x"] == long_text


# --- Through the store --------------------------------------------------


def test_save_draft_keeps_announcements_when_none_is_passed(store: Store) -> None:
    draft, _warnings = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body", announcements=THREE)
    again = store.save_draft(draft.id, "ghostwriter", 1, FRONTMATTER, "body two")
    assert again.announcements == THREE
    assert store.get_version(draft.id, 2).announcements == THREE


def test_save_draft_refuses_an_unknown_channel(store: Store) -> None:
    draft, _warnings = store.create_draft("ghostwriter")
    with pytest.raises(ApiError) as caught:
        store.save_draft(
            draft.id, "ghostwriter", 0, FRONTMATTER, "body", announcements={"mastodon": "hi"}
        )
    assert caught.value.status_code == 422


# --- On disk ------------------------------------------------------------


def test_a_draft_written_before_this_field_loads_with_an_empty_mapping(store: Store) -> None:
    draft, _warnings = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body", announcements=THREE)

    # Rewrite both records the way the previous release wrote them: with no
    # `announcements` key at all.
    for path in (
        store.drafts_dir / draft.id / "draft.json",
        store.drafts_dir / draft.id / "versions" / "1.json",
    ):
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("announcements", None)
        path.write_text(json.dumps(record), encoding="utf-8")

    assert store.get_draft(draft.id).announcements == {}
    assert store.get_version(draft.id, 1).announcements == {}


def test_the_models_default_without_the_key(store: Store) -> None:
    stamp = now_stamp()
    draft = Draft.model_validate({"id": "d1", "created_at": stamp, "updated_at": stamp})
    assert draft.announcements == {}
    version = Version.model_validate(
        {"draft_id": "d1", "version_no": 1, "author": "a", "created_at": stamp, "base_version": 0}
    )
    assert version.announcements == {}


def test_a_backup_made_before_this_field_restores_with_an_empty_mapping(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    store = Store.open(data_dir)
    draft, _warnings = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    draft_path = store.drafts_dir / draft.id / "draft.json"
    record = json.loads(draft_path.read_text(encoding="utf-8"))
    record.pop("announcements", None)
    draft_path.write_text(json.dumps(record), encoding="utf-8")
    store.reindex()
    store.close()
    assert "announcements" not in draft_path.read_text(encoding="utf-8")

    bundle = backup.create_backup(data_dir, tmp_path / "out")
    fresh_dir = tmp_path / "fresh"
    Store.open(fresh_dir).close()
    backup.restore_backup(fresh_dir, bundle)

    restored = Store.open(fresh_dir)
    drafts = restored.list_drafts()
    assert len(drafts) == 1
    assert drafts[0].announcements == {}
    restored.close()


# --- Never in the post --------------------------------------------------


def test_convert_output_is_byte_identical_with_and_without_announcements() -> None:
    stamp = now_stamp()
    base: dict[str, Any] = {
        "id": "d1",
        "created_at": stamp,
        "updated_at": stamp,
        "slug": "my-post",
        "title": "My Post",
        "frontmatter": {"title": "My Post", "date": "2026-08-01T09:00:00-05:00"},
        "body": "hello ![a](rack.png)",
        "images": [],
    }
    plain = convert.convert(Draft.model_validate(base))
    loud = convert.convert(Draft.model_validate({**base, "announcements": THREE}))

    assert plain.text == loud.text
    assert plain.images == loud.images
    assert plain.post_path == loud.post_path
    assert plain.url == loud.url
    for value in THREE.values():
        assert value not in loud.text


def test_no_announcement_text_reaches_a_converted_post_through_the_store(store: Store) -> None:
    draft, _warnings = store.create_draft("ghostwriter")
    store.save_draft(
        draft.id,
        "ghostwriter",
        0,
        {"title": "Announcing Things", "date": "2026-08-01T09:00:00-05:00"},
        "body text",
        announcements={"x": "DO-NOT-PUBLISH-THIS"},
    )
    # A slug is pinned at the first preview or publish; this record never gets
    # there, so pin it here to reach the converter at all.
    stored = store.get_draft(draft.id).model_copy(update={"slug": "announcing-things"})
    converted = convert.convert(stored)
    assert "DO-NOT-PUBLISH-THIS" not in converted.text


# --- The edit page ------------------------------------------------------


def _editor_html(client: TestClient, draft_id: str) -> str:
    response = client.get(f"/content/drafts/{draft_id}")
    assert response.status_code == 200
    return response.text


def test_the_editor_renders_three_named_textareas_joined_to_the_edit_form(
    client: TestClient, services: Services, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    html = _editor_html(client, draft_id)

    for channel in ("x", "bluesky", "linkedin"):
        assert f'name="announcement_{channel}"' in html
        assert f'id="announcement_{channel}"' in html
        assert f'data-announce-copy="{channel}"' in html
    assert html.count('form="edit-form"') >= 3
    for value in THREE.values():
        assert value in html


def test_announcement_text_is_escaped_not_rendered(client: TestClient, agent_token: str) -> None:
    draft_id = _new_draft(client, agent_token)
    nasty = '</textarea><img src=x onerror="alert(1)"> & "quoted"'
    _put(client, agent_token, draft_id, 0, announcements={"x": nasty})
    html = _editor_html(client, draft_id)

    assert nasty not in html
    assert "&lt;/textarea&gt;" in html
    assert 'onerror="alert(1)"' not in html


def test_the_published_url_shows_only_once_a_draft_has_one(
    client: TestClient, services: Services, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    assert "announce-url" not in _editor_html(client, draft_id)

    store = services.store
    path = store.drafts_dir / draft_id / "draft.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["published"] = {"url": "/2026/08/announcing-things/"}
    path.write_text(json.dumps(record), encoding="utf-8")

    html = _editor_html(client, draft_id)
    assert "announce-url" in html
    assert "/2026/08/announcing-things/" in html


def test_the_ui_save_replaces_the_mapping_from_the_three_fields(
    client: TestClient, services: Services, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    draft = services.store.get_draft(draft_id)
    form = {
        "base_version": str(draft.version_no),
        "title": draft.title,
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": draft.body,
        "announcement_x": "  edited from the page  ",
        # Blank and whitespace-only both mean the channel is absent, not
        # present and empty.
        "announcement_bluesky": "   ",
        "announcement_linkedin": "",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 200

    saved = services.store.get_draft(draft_id)
    assert saved.announcements == {"x": "edited from the page"}
    assert saved.version_no == draft.version_no + 1


def test_a_ui_conflict_keeps_the_attempted_announcement_text(
    client: TestClient, services: Services, agent_token: str
) -> None:
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, FRONTMATTER, "moved on")

    form = {
        "base_version": str(draft.version_no),
        "title": draft.title,
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": "mine",
        "announcement_x": "typed before the clash",
        "announcement_bluesky": "",
        "announcement_linkedin": "",
    }
    response = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert response.status_code == 409
    # The conflict page keeps every posted field for the editor's Restore,
    # with no special case for these three.
    assert "typed before the clash" in response.text
    # Round of review: the attempted pane rendered only frontmatter and body,
    # so a visitor whose browser could not hold the local backup was told to
    # copy from a pane that never showed their announcement edits. The visible
    # summary list must name the channel and carry the attempted text.
    assert "<li>X: typed before the clash</li>" in response.text


def test_the_conflict_page_reload_form_carries_the_current_announcements(
    client: TestClient, services: Services, agent_token: str
) -> None:
    # The reload form has no announcements panel of its own; without hidden
    # fields carrying the current values, resubmitting it would send empty
    # strings for all three and silently wipe them (found in review, ADR 021).
    draft_id = _new_draft(client, agent_token)
    _put(client, agent_token, draft_id, 0, announcements=THREE)
    draft = services.store.get_draft(draft_id)
    services.store.save_draft(draft_id, "scott", draft.version_no, FRONTMATTER, "moved on")

    form = {
        "base_version": str(draft.version_no),
        "title": draft.title,
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": "mine",
        "announcement_x": "",
        "announcement_bluesky": "",
        "announcement_linkedin": "",
    }
    conflict = client.post(f"/content/drafts/{draft_id}/save", data=form)
    assert conflict.status_code == 409
    for value in THREE.values():
        assert value in conflict.text

    reapply = {
        "base_version": str(services.store.get_draft(draft_id).version_no),
        "title": draft.title,
        "date": "",
        "categories": "",
        "tags": "",
        "summary": "",
        "url": "",
        "featureImage": "",
        "body": "reapplied",
        "announcement_x": THREE["x"],
        "announcement_bluesky": THREE["bluesky"],
        "announcement_linkedin": THREE["linkedin"],
    }
    reloaded = client.post(f"/content/drafts/{draft_id}/save", data=reapply)
    assert reloaded.status_code == 200
    assert services.store.get_draft(draft_id).announcements == THREE
