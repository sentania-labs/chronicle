"""Posts, runs, and the event cursor."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import auth

FRONTMATTER = {"title": "Cursor Test"}


def test_posts_are_empty_until_the_digest_of_main(client: TestClient, agent_token: str) -> None:
    assert client.get("/v1/posts", headers=auth(agent_token)).json() == {"posts": []}
    missing = client.get("/v1/posts/anything", headers=auth(agent_token))
    assert missing.status_code == 404
    assert missing.json()["error"] == "post_not_found"


def test_unknown_run_is_404(client: TestClient, agent_token: str) -> None:
    assert client.get("/v1/runs/nope", headers=auth(agent_token)).status_code == 404


def test_events_are_cursor_based(client: TestClient, agent_token: str) -> None:
    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]
    client.put(
        f"/v1/drafts/{draft_id}",
        json={"base_version": 0, "frontmatter": FRONTMATTER, "body": "b"},
        headers=auth(agent_token),
    )

    first = client.get("/v1/events", headers=auth(agent_token)).json()
    assert [event["seq"] for event in first["events"]] == [1, 2]
    assert first["events"][0]["type"] == "draft.created"
    assert first["events"][0]["actor"] == "ghostwriter"
    assert first["next_cursor"] == 2

    client.post(f"/v1/drafts/{draft_id}/actions/submit", headers=auth(agent_token))
    later = client.get(
        "/v1/events", params={"since": first["next_cursor"]}, headers=auth(agent_token)
    ).json()
    assert [event["type"] for event in later["events"]] == ["draft.submit"]
    assert later["events"][0]["from_status"] == "drafting"
    assert later["events"][0]["to_status"] == "in_review"
    assert later["next_cursor"] == 3

    assert client.get(
        "/v1/events", params={"since": later["next_cursor"]}, headers=auth(agent_token)
    ).json() == {"events": [], "next_cursor": 3}


def test_claim_release_and_image_attach_detach_all_write_events(
    client: TestClient, agent_token: str
) -> None:
    from .conftest import png_bytes

    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]
    client.post(f"/v1/drafts/{draft_id}/claim", headers=auth(agent_token))
    client.post(f"/v1/drafts/{draft_id}/release", headers=auth(agent_token))

    image_id = client.post(
        "/v1/images",
        files={"file": ("f.png", png_bytes(), "image/png")},
        headers=auth(agent_token),
    ).json()["image_id"]
    client.put(
        f"/v1/drafts/{draft_id}/images/{image_id}",
        json={"role": "inline"},
        headers=auth(agent_token),
    )
    client.delete(f"/v1/drafts/{draft_id}/images/{image_id}", headers=auth(agent_token))

    events = client.get("/v1/events", headers=auth(agent_token)).json()["events"]
    types = [event["type"] for event in events]
    assert types == [
        "draft.created",
        "draft.claim",
        "draft.release",
        "draft.image_attach",
        "draft.image_detach",
    ]
