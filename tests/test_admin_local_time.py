"""Admin pages render stamps as local clock time, keeping their fallback words.

Every stamp is far from today and distinct, so a call site reverted to the raw
ISO value fails on both the rendered string and the absent raw stamp. January
and July stamps cover CST and CDT.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from chronicle.api import admin_templates as tpl

CREATED = "2020-01-02T03:04:05+00:00"  # 2020-01-01 21:04 CST
USED = "2020-07-08T09:10:11+00:00"  # 2020-07-08 04:10 CDT
REVOKED = "2020-08-09T10:11:12+00:00"  # 2020-08-09 05:11 CDT
BACKUP = "2020-09-10T11:12:13+00:00"  # 2020-09-10 06:12 CDT
DIGEST = "2020-10-11T12:13:14+00:00"  # 2020-10-11 07:13 CDT
LOOP = "2020-11-12T13:14:15+00:00"  # 2020-11-12 07:14 CST


def _absent(html: str, raw: str) -> None:
    assert raw not in html
    assert raw.split("+")[0] not in html


def _token(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": "ghostwriter",
        "hash": "x",
        "created_at": CREATED,
        "last_used_at": None,
        "revoked_at": None,
    }
    row.update(overrides)
    return row


def test_tokens_table_renders_created_used_and_revoked_as_local_time() -> None:
    html = tpl.tokens_page([_token(last_used_at=USED, revoked_at=REVOKED)])
    assert "<td>2020-01-01 21:04 CST</td>" in html
    assert "<td>2020-07-08 04:10 CDT</td>" in html
    assert "<td>2020-08-09 05:11 CDT</td>" in html
    for raw in (CREATED, USED, REVOKED):
        _absent(html, raw)


def test_tokens_table_keeps_never_and_no_for_an_unused_unrevoked_token() -> None:
    html = tpl.tokens_page([_token()])
    assert "<td>never</td><td>no</td>" in html
    assert "<td>-</td>" not in html


def test_tokens_table_date_only_stamp_stays_its_own_date() -> None:
    html = tpl.tokens_page([_token(created_at="2020-01-02", last_used_at="2020-07-08")])
    assert "<td>2020-01-02</td>" in html
    assert "<td>2020-07-08</td>" in html
    assert "2020-01-01" not in html


def test_tokens_page_over_http_shows_never_for_a_freshly_issued_token(
    admin_client: TestClient,
) -> None:
    admin_client.post("/admin/tokens", data={"name": "fresh"})
    html = admin_client.get("/admin/tokens").text
    fresh_row = html.split("<td>fresh</td>")[1].split("</tr>")[0]
    assert "<td>never</td><td>no</td>" in fresh_row
    assert "+00:00" not in fresh_row
    assert " CDT</td>" in fresh_row or " CST</td>" in fresh_row


def test_status_page_renders_last_backup_and_digest_as_local_time(
    admin_client: TestClient, data_dir: Path
) -> None:
    state = data_dir / "state"
    (state / "last_backup.json").write_text(json.dumps({"created_at": BACKUP}), encoding="utf-8")
    (state / "digest-status.json").write_text(json.dumps({"finished_at": DIGEST}), encoding="utf-8")
    html = admin_client.get("/admin").text
    assert "<td>last backup</td><td>2020-09-10 06:12 CDT</td>" in html
    assert "<td>last digest</td><td>2020-10-11 07:13 CDT</td>" in html
    _absent(html, BACKUP)
    _absent(html, DIGEST)


def test_status_page_keeps_never_when_no_backup_or_digest_has_run(
    admin_client: TestClient,
) -> None:
    html = admin_client.get("/admin").text
    assert "<td>last backup</td><td>never</td>" in html
    assert "<td>last digest</td><td>never</td>" in html


def test_status_page_date_only_digest_stamp_stays_its_own_date(
    admin_client: TestClient, data_dir: Path
) -> None:
    (data_dir / "state" / "digest-status.json").write_text(
        json.dumps({"finished_at": "2020-10-11"}), encoding="utf-8"
    )
    html = admin_client.get("/admin").text
    assert "<td>last digest</td><td>2020-10-11</td>" in html


def test_status_page_renders_heartbeat_at_keys_as_local_time(
    admin_client: TestClient, data_dir: Path
) -> None:
    path = data_dir / "state" / "builder" / "heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"builder_id": "b-1", "last_loop_at": LOOP, "queue_depth": 0}),
        encoding="utf-8",
    )
    html = admin_client.get("/admin").text
    assert "<td>last_loop_at</td><td>2020-11-12 07:14 CST</td>" in html
    _absent(html, LOOP)
    assert "<td>b-1</td>" in html


def test_backup_page_renders_last_backup_as_local_time_and_keeps_never(
    admin_client: TestClient, data_dir: Path
) -> None:
    assert "Last backup created: never" in admin_client.get("/admin/backup").text
    (data_dir / "state" / "last_backup.json").write_text(
        json.dumps({"created_at": BACKUP}), encoding="utf-8"
    )
    html = admin_client.get("/admin/backup").text
    assert "Last backup created: 2020-09-10 06:12 CDT" in html
    _absent(html, BACKUP)


def test_restore_confirm_page_renders_bundle_created_as_local_time() -> None:
    html = tpl.backup_confirm_page(
        token="t", manifest={"created_at": BACKUP, "chronicle_version": "1", "counts": {}}
    )
    assert "Bundle created 2020-09-10 06:12 CDT by Chronicle" in html
    _absent(html, BACKUP)


def test_restore_confirm_page_keeps_question_mark_when_manifest_has_no_stamp() -> None:
    html = tpl.backup_confirm_page(token="t", manifest={"counts": {}})
    assert "Bundle created ? by Chronicle" in html


def test_restore_confirm_page_date_only_stamp_stays_its_own_date() -> None:
    html = tpl.backup_confirm_page(token="t", manifest={"created_at": "2020-09-10", "counts": {}})
    assert "Bundle created 2020-09-10 by Chronicle" in html


def test_heartbeat_at_key_with_no_value_renders_as_before() -> None:
    html = tpl._heartbeat_rows(
        lambda k, v: f"<tr><td>{k}</td><td>{v}</td></tr>", {"last_loop_at": None}, "builder"
    )
    assert "<td>last_loop_at</td><td>None</td>" in html


VERIFIED = "2020-12-13T14:15:16+00:00"  # 2020-12-13 08:15 CST


def _github_store(admin_client: TestClient) -> Any:
    services: Any = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    services.github_store.store_new_app(
        app_id="1",
        slug="s",
        client_id="c",
        client_secret="cs",
        webhook_secret="ws",
        pem="not-a-real-key",
        html_url="https://github.com/apps/s",
    )
    return services.github_store


def test_status_page_renders_github_verified_at_as_local_time(
    admin_client: TestClient,
) -> None:
    store = _github_store(admin_client)
    store.record_verification({"contents": "read"})
    record = store.load()
    record.last_verified_at = VERIFIED
    store._save(record)
    html = admin_client.get("/admin").text
    assert "<td>state</td><td>verified at 2020-12-13 08:15 CST</td>" in html
    _absent(html, VERIFIED)
    assert admin_client.get("/admin/api/status").json()["github_app_verified_at"] == VERIFIED


def test_readyz_still_reports_the_raw_iso_verified_at_stamp(admin_client: TestClient) -> None:
    """Machine readable: do not "simplify" by localizing `readiness_state`."""
    store = _github_store(admin_client)
    store.record_verification({"contents": "read"})
    record = store.load()
    record.last_verified_at = VERIFIED
    store._save(record)
    check = admin_client.get("/readyz").json()["checks"][3]
    assert check["name"] == "github_app"
    assert check["detail"] == f"verified at {VERIFIED}"


def test_status_page_other_github_readiness_states_render_unchanged(
    admin_client: TestClient,
) -> None:
    assert "<td>state</td><td>not configured</td>" in admin_client.get("/admin").text
    store = _github_store(admin_client)
    assert "<td>state</td><td>configured but unverified</td>" in admin_client.get("/admin").text
    store.record_verification({"contents": "read"})
    store.record_error("list_repositories_failed")
    html = admin_client.get("/admin").text
    assert "<td>state</td><td>failing: list_repositories_failed</td>" in html
    assert admin_client.get("/admin/api/status").json()["github_app_verified_at"] is None
