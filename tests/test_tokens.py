"""Consumer tokens: the only door into /v1, and the CLI that issues them."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from chronicle.api import gitrepo
from chronicle.api.tokens import UI_TOKEN_FILE_NAME, TokenStore, commit_author, hash_token
from chronicle.cli import main as cli_main

from .conftest import auth


def test_ui_token_is_minted_once_and_written_0600(data_dir: Path, client: TestClient) -> None:
    token_file = data_dir / "state" / UI_TOKEN_FILE_NAME
    assert token_file.exists()
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    first = token_file.read_text(encoding="utf-8")

    TokenStore(data_dir / "state").ensure_ui_token()
    assert token_file.read_text(encoding="utf-8") == first


def test_tokens_file_never_holds_the_plaintext(data_dir: Path, ui_token: str) -> None:
    raw = (data_dir / "state" / "tokens.json").read_text(encoding="utf-8")
    assert ui_token not in raw
    assert hash_token(ui_token) in raw


def test_anonymous_request_to_v1_is_401(client: TestClient) -> None:
    response = client.get("/v1/drafts")
    assert response.status_code == 401
    assert response.json()["error"] == "token_required"


def test_unknown_token_is_401(client: TestClient) -> None:
    response = client.get("/v1/drafts", headers=auth("not-a-real-token"))
    assert response.status_code == 401
    assert response.json()["error"] == "token_invalid"


def test_revoked_token_is_401(client: TestClient, data_dir: Path, agent_token: str) -> None:
    assert client.get("/v1/drafts", headers=auth(agent_token)).status_code == 200
    TokenStore(data_dir / "state").revoke("ghostwriter")
    response = client.get("/v1/drafts", headers=auth(agent_token))
    assert response.status_code == 401


def test_successful_auth_stamps_last_used(
    client: TestClient, data_dir: Path, agent_token: str
) -> None:
    tokens = TokenStore(data_dir / "state")
    before = next(record for record in tokens.load() if record.name == "ghostwriter")
    assert before.last_used_at is None
    client.get("/v1/drafts", headers=auth(agent_token))
    after = next(record for record in tokens.load() if record.name == "ghostwriter")
    assert after.last_used_at is not None


def test_ui_token_commits_as_scott() -> None:
    assert commit_author("ui") == "scott"
    assert commit_author("ghostwriter") == "ghostwriter"


def test_git_author_is_the_acting_token_end_to_end(
    client: TestClient, agent_token: str, ui_token: str
) -> None:
    store = client.app.state.services.store  # type: ignore[attr-defined]
    draft_id = client.post("/v1/drafts", json={}, headers=auth(agent_token)).json()["id"]
    assert gitrepo.log_authors(store.repo_dir, limit=1) == ["ghostwriter"]

    saved = client.put(
        f"/v1/drafts/{draft_id}",
        json={"base_version": 0, "frontmatter": {"title": "Scott edits"}, "body": "b"},
        headers=auth(ui_token),
    )
    assert saved.status_code == 200
    assert gitrepo.log_authors(store.repo_dir, limit=1) == ["scott"]
    assert saved.json()["version_no"] == 1

    version = client.get(f"/v1/drafts/{draft_id}/versions/1", headers=auth(agent_token)).json()
    assert version["author"] == "scott"


def test_cli_token_issue_list_and_revoke(data_dir: Path, capsys) -> None:
    assert cli_main(["--data-dir", str(data_dir), "token", "issue", "dashboard"]) == 0
    token = capsys.readouterr().out.strip()
    assert token

    assert cli_main(["--data-dir", str(data_dir), "token", "list"]) == 0
    listing = capsys.readouterr().out
    assert "dashboard" in listing
    assert token not in listing
    assert hash_token(token) not in listing

    assert cli_main(["--data-dir", str(data_dir), "token", "revoke", "dashboard"]) == 0
    capsys.readouterr()
    assert TokenStore(data_dir / "state").authenticate(token) is None
