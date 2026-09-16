"""Submission intake and triage over HTTP."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import auth

BRIEF = {"brief": "screenshot of the failed run", "materials": [{"name": "note", "text": "hi"}]}


def test_create_list_and_claim(client: TestClient, agent_token: str) -> None:
    created = client.post("/v1/submissions", json=BRIEF, headers=auth(agent_token))
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "new"
    assert body["from"] == "ghostwriter"

    listed = client.get("/v1/submissions", params={"status": "new"}, headers=auth(agent_token))
    assert [item["id"] for item in listed.json()["submissions"]] == [body["id"]]

    claimed = client.post(f"/v1/submissions/{body['id']}/claim", headers=auth(agent_token))
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "claimed"
    assert claimed.json()["claimed_by"] == "ghostwriter"

    assert (
        client.get("/v1/submissions", params={"status": "new"}, headers=auth(agent_token)).json()[
            "submissions"
        ]
        == []
    )


def test_discard_then_claim_is_refused(client: TestClient, agent_token: str) -> None:
    submission = client.post("/v1/submissions", json=BRIEF, headers=auth(agent_token)).json()
    discarded = client.post(
        f"/v1/submissions/{submission['id']}/discard", headers=auth(agent_token)
    )
    assert discarded.json()["status"] == "discarded"

    refused = client.post(f"/v1/submissions/{submission['id']}/claim", headers=auth(agent_token))
    assert refused.status_code == 409
    assert refused.json()["error"] == "transition_not_allowed"


def test_unknown_submission_is_404(client: TestClient, agent_token: str) -> None:
    response = client.post("/v1/submissions/nope/claim", headers=auth(agent_token))
    assert response.status_code == 404
    assert response.json()["error"] == "submission_not_found"


def test_draft_from_submission_requires_a_claim(client: TestClient, agent_token: str) -> None:
    submission = client.post("/v1/submissions", json=BRIEF, headers=auth(agent_token)).json()
    refused = client.post(
        "/v1/drafts", json={"from_submission": submission["id"]}, headers=auth(agent_token)
    )
    assert refused.status_code == 409

    client.post(f"/v1/submissions/{submission['id']}/claim", headers=auth(agent_token))
    created = client.post(
        "/v1/drafts", json={"from_submission": submission["id"]}, headers=auth(agent_token)
    )
    assert created.status_code == 201
    assert created.json()["source_submission"] == submission["id"]

    after = client.get(f"/v1/submissions/{submission['id']}", headers=auth(agent_token)).json()
    assert after["status"] == "drafted"
    assert after["draft_id"] == created.json()["id"]
