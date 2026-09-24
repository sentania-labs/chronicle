"""The editor lock (issue #64, ADR 025): a draft open in the editor can't be
saved by anyone else, and leases lapse on their own."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chronicle.api.editor_lease import LEASE_SECONDS, EditorLeases
from chronicle.api.errors import ApiError

from .conftest import auth

FRONTMATTER = {"title": "Locked Post"}


class Clock:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 9, 24, 20, 41, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += dt.timedelta(seconds=seconds)


# --- The lease itself ---------------------------------------------------------


def test_a_lease_is_granted_renewed_and_keeps_its_start() -> None:
    clock = Clock()
    leases = EditorLeases(clock)
    first = leases.touch("d1", "editor")
    clock.advance(30)
    renewed = leases.touch("d1", "editor")
    assert renewed.holder == "editor"
    assert renewed.since == first.since
    assert renewed.heartbeat_at == clock.now


def test_another_identity_cannot_take_a_live_lease() -> None:
    clock = Clock()
    leases = EditorLeases(clock)
    leases.touch("d1", "editor")
    assert leases.touch("d1", "ghostwriter").holder == "editor"


def test_a_lease_lapses_two_minutes_after_its_last_heartbeat() -> None:
    clock = Clock()
    leases = EditorLeases(clock)
    leases.touch("d1", "editor")
    clock.advance(LEASE_SECONDS - 1)
    assert leases.current("d1") is not None
    clock.advance(1)
    assert leases.current("d1") is None
    assert leases.touch("d1", "ghostwriter").holder == "ghostwriter"


def test_check_save_refuses_only_another_identity() -> None:
    leases = EditorLeases(Clock())
    leases.touch("d1", "editor")
    leases.check_save("d1", "editor")  # same identity: allowed
    leases.check_save("d2", "ghostwriter")  # no lease: allowed
    with pytest.raises(ApiError) as caught:
        leases.check_save("d1", "ghostwriter")
    assert caught.value.status_code == 423
    assert caught.value.extra["holder"] == "editor"


def test_release_only_drops_the_holders_own_lease() -> None:
    leases = EditorLeases(Clock())
    leases.touch("d1", "editor")
    leases.release("d1", "ghostwriter")
    assert leases.current("d1") is not None
    leases.release("d1", "editor")
    assert leases.current("d1") is None


# --- Through the app ----------------------------------------------------------


def _leases(client: TestClient) -> EditorLeases:
    leases: EditorLeases = client.app.state.services.leases  # type: ignore[attr-defined]
    return leases


def _clocked(client: TestClient) -> Clock:
    clock = Clock()
    client.app.state.services.leases = EditorLeases(clock)  # type: ignore[attr-defined]
    return clock


def _draft(client: TestClient, token: str) -> str:
    draft_id: str = client.post("/v1/drafts", json={}, headers=auth(token)).json()["id"]
    saved = client.put(
        f"/v1/drafts/{draft_id}",
        json={"base_version": 0, "frontmatter": FRONTMATTER, "body": "body"},
        headers=auth(token),
    )
    assert saved.status_code == 200
    return draft_id


def _api_save(client: TestClient, token: str, draft_id: str, base: int) -> Any:
    return client.put(
        f"/v1/drafts/{draft_id}",
        json={"base_version": base, "frontmatter": FRONTMATTER, "body": f"edit {base}"},
        headers=auth(token),
    )


def test_opening_the_editor_refuses_an_api_save_until_the_lease_lapses(
    client: TestClient, agent_token: str
) -> None:
    clock = _clocked(client)
    draft_id = _draft(client, agent_token)
    assert client.get(f"/content/drafts/{draft_id}").status_code == 200  # editor opens

    refused = _api_save(client, agent_token, draft_id, 1)
    assert refused.status_code == 423
    body = refused.json()
    assert body["error"] == "draft_being_edited"
    assert body["holder"] == "editor"
    assert "since" in body and "expires_at" in body

    shown = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert shown["editing"]["holder"] == "editor"

    clock.advance(LEASE_SECONDS)  # the tab closed without a release
    assert _api_save(client, agent_token, draft_id, 1).status_code == 200
    shown = client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).json()
    assert shown["editing"] is None


def test_reads_and_previews_are_not_blocked(client: TestClient, agent_token: str) -> None:
    _clocked(client)
    draft_id = _draft(client, agent_token)
    client.get(f"/content/drafts/{draft_id}")
    assert client.get(f"/v1/drafts/{draft_id}", headers=auth(agent_token)).status_code == 200
    preview = client.post(f"/v1/drafts/{draft_id}/actions/preview", headers=auth(agent_token))
    assert preview.status_code == 200


def test_the_editor_saving_its_own_draft_is_not_blocked(
    client: TestClient, agent_token: str
) -> None:
    _clocked(client)
    draft_id = _draft(client, agent_token)
    client.get(f"/content/drafts/{draft_id}")
    response = client.post(
        f"/content/drafts/{draft_id}/save",
        data={"base_version": "1", "title": "Locked Post", "body": "editor edit"},
    )
    assert response.status_code == 200
    assert "saved" in response.text


def test_another_holder_makes_the_editor_read_only_with_a_banner(
    client: TestClient, agent_token: str
) -> None:
    _clocked(client)
    draft_id = _draft(client, agent_token)
    _leases(client).touch(draft_id, "ghostwriter")

    html = client.get(f"/content/drafts/{draft_id}").text
    assert 'id="lock-notice"' in html
    assert "Being edited by ghostwriter since" in html
    assert 'data-read-only="1"' in html
    assert "disabled data-locked=" in html

    refused = client.post(
        f"/content/drafts/{draft_id}/save",
        data={"base_version": "1", "title": "Locked Post", "body": "sneaky"},
    )
    assert refused.status_code == 423


def test_the_heartbeat_reports_the_holder_and_hands_over_once_it_lapses(
    client: TestClient, agent_token: str
) -> None:
    clock = _clocked(client)
    draft_id = _draft(client, agent_token)
    _leases(client).touch(draft_id, "ghostwriter")
    beat = client.post(f"/content/drafts/{draft_id}/lease").json()
    assert beat["mine"] is False and beat["holder"] == "ghostwriter"
    clock.advance(LEASE_SECONDS)
    beat = client.post(f"/content/drafts/{draft_id}/lease").json()
    assert beat["mine"] is True and beat["holder"] == "editor"


def test_closing_the_page_releases_the_lease(client: TestClient, agent_token: str) -> None:
    _clocked(client)
    draft_id = _draft(client, agent_token)
    client.get(f"/content/drafts/{draft_id}")
    assert client.post(f"/content/drafts/{draft_id}/lease/release").status_code == 200
    assert _api_save(client, agent_token, draft_id, 1).status_code == 200


def test_the_board_marks_a_draft_open_in_the_editor(client: TestClient, agent_token: str) -> None:
    _clocked(client)
    draft_id = _draft(client, agent_token)
    assert "editing: editor" not in client.get("/content/drafts").text
    client.get(f"/content/drafts/{draft_id}")
    assert "editing: editor" in client.get("/content/drafts").text


def test_the_claim_ui_is_gone(client: TestClient, agent_token: str) -> None:
    draft_id = _draft(client, agent_token)
    html = client.get(f"/content/drafts/{draft_id}").text
    assert "Unclaimed" not in html and ">Claim<" not in html
    assert "claim:" not in client.get("/content/drafts").text
