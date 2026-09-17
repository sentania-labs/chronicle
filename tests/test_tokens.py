"""Consumer tokens: the only door into /v1, and the CLI that issues them."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from chronicle.api import gitrepo
from chronicle.api.tokens import (
    UI_TOKEN_FILE_NAME,
    UI_TOKEN_NAME,
    TokenStore,
    commit_author,
    hash_token,
)
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


def test_interleaved_stores_both_land(data_dir: Path) -> None:
    """Two TokenStore instances stand in for the api and the CLI process.

    Both open the same tokens.json under the same interprocess lock, so an
    issue on one and a revoke on the other, run back to back, must not lose
    either effect to a stale load-mutate-save.
    """
    first = TokenStore(data_dir / "state")
    second = TokenStore(data_dir / "state")

    first_token = first.issue("dashboard")
    second.revoke("dashboard")

    records = TokenStore(data_dir / "state").load()
    dashboard = next(record for record in records if record.name == "dashboard")
    assert dashboard.revoked_at is not None
    assert hash_token(first_token) == dashboard.hash


def test_ui_token_rotates_when_plaintext_file_is_missing(
    data_dir: Path, client: TestClient
) -> None:
    tokens = TokenStore(data_dir / "state")
    original = next(r for r in tokens.load() if r.name == UI_TOKEN_NAME and r.revoked_at is None)
    (data_dir / "state" / UI_TOKEN_FILE_NAME).unlink()

    tokens.ensure_ui_token()

    records = tokens.load()
    assert any(
        r.name == UI_TOKEN_NAME and r.hash == original.hash and r.revoked_at for r in records
    )
    live = [r for r in records if r.name == UI_TOKEN_NAME and r.revoked_at is None]
    assert len(live) == 1
    new_plaintext = (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()
    assert hash_token(new_plaintext) == live[0].hash


def test_ui_token_rotates_when_plaintext_does_not_authenticate(
    data_dir: Path, client: TestClient
) -> None:
    tokens = TokenStore(data_dir / "state")
    original = next(r for r in tokens.load() if r.name == UI_TOKEN_NAME and r.revoked_at is None)
    (data_dir / "state" / UI_TOKEN_FILE_NAME).write_text("not-the-real-token\n", encoding="utf-8")

    tokens.ensure_ui_token()

    records = tokens.load()
    assert any(
        r.name == UI_TOKEN_NAME and r.hash == original.hash and r.revoked_at for r in records
    )
    live = [r for r in records if r.name == UI_TOKEN_NAME and r.revoked_at is None]
    assert len(live) == 1
    new_plaintext = (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()
    assert hash_token(new_plaintext) == live[0].hash


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


def test_cli_token_issue_ui_is_rejected(data_dir: Path, capsys) -> None:
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    assert cli_main(["--data-dir", str(data_dir), "token", "issue", "ui"]) == 1
    err = capsys.readouterr().err
    assert "reserved" in err
    assert TokenStore(data_dir / "state").load() == []


def test_admin_tokens_page_rejects_the_name_ui(admin_client: TestClient) -> None:
    response = admin_client.post("/admin/tokens", data={"name": "ui"})
    assert response.status_code == 422
    assert "reserved" in response.text


def test_admin_tokens_api_rejects_the_name_ui(admin_client: TestClient) -> None:
    response = admin_client.post("/admin/api/tokens", json={"name": "ui"})
    assert response.status_code == 422
    assert response.json()["error"] == "token_name_reserved"


def test_revoked_ui_token_stays_disabled_across_a_restart(
    data_dir: Path, client: TestClient
) -> None:
    """Adversarial review finding: `ensure_ui_token` runs on every process
    start and could not tell a deliberate `/admin/tokens` revoke from a
    bootstrap failure, so a restart silently re-minted the UI's token and
    undid the operator's kill switch."""
    tokens = TokenStore(data_dir / "state")
    original = next(r for r in tokens.load() if r.name == UI_TOKEN_NAME and r.revoked_at is None)
    original_plaintext = (
        (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()
    )

    tokens.revoke(UI_TOKEN_NAME)
    assert tokens.ui_disabled_path.exists()
    assert tokens.authenticate(original_plaintext) is None

    # A restart calls ensure_ui_token again; the marker must survive it.
    restarted = TokenStore(data_dir / "state")
    restarted.ensure_ui_token()
    assert restarted.ui_disabled_path.exists()
    live = [r for r in restarted.load() if r.name == UI_TOKEN_NAME and r.revoked_at is None]
    assert live == []
    # The plaintext file was never touched by the no-op restart: still the
    # pre-revoke value, and it still does not authenticate.
    assert (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip() == (
        original_plaintext
    )
    assert restarted.authenticate(original_plaintext) is None
    assert original.hash not in [r.hash for r in restarted.load() if r.revoked_at is None]


def test_reenable_ui_token_clears_the_marker_and_mints_a_working_token(
    data_dir: Path, client: TestClient
) -> None:
    tokens = TokenStore(data_dir / "state")
    tokens.revoke(UI_TOKEN_NAME)
    assert tokens.ui_disabled_path.exists()

    fresh = tokens.reenable_ui_token()

    assert not tokens.ui_disabled_path.exists()
    record = tokens.authenticate(fresh)
    assert record is not None and record.name == UI_TOKEN_NAME
    assert (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip() == fresh

    # A restart after re-enabling leaves the fresh token alone (it is valid,
    # so ensure_ui_token has nothing to fix).
    restarted = TokenStore(data_dir / "state")
    restarted.ensure_ui_token()
    assert restarted.authenticate(fresh) is not None


def test_ui_disabled_across_restart_via_admin_page_and_re_enable_via_admin_page(
    admin_client: TestClient, data_dir: Path
) -> None:
    """End to end through the routes an operator actually clicks: revoke
    `ui` on `/admin/tokens`, confirm the UI is refused even after a
    simulated restart, then re-enable and confirm it works again."""
    revoked = admin_client.post("/admin/tokens/ui/revoke")
    assert revoked.status_code == 200
    assert "Re-enable UI" in revoked.text

    TokenStore(data_dir / "state").ensure_ui_token()  # simulated restart
    claim = admin_client.post("/content/drafts/does-not-exist/claim")
    assert claim.status_code == 503

    reenabled = admin_client.post("/admin/tokens/ui/reenable")
    assert reenabled.status_code == 200
    assert "New token (shown once)" in reenabled.text
    assert "Re-enable UI" not in reenabled.text


def test_admin_tokens_api_issue_list_and_revoke(admin_client: TestClient) -> None:
    issued = admin_client.post("/admin/api/tokens", json={"name": "dashboard"})
    assert issued.status_code == 200
    token = issued.json()["token"]
    assert token

    listed = admin_client.get("/admin/api/tokens")
    names = [t["name"] for t in listed.json()["tokens"]]
    assert "dashboard" in names

    revoked = admin_client.post("/admin/api/tokens/dashboard/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked"] == 1
