"""Claim, login, session expiry, and secret rotation (spec sections 10, 11)."""

from __future__ import annotations

import stat
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from chronicle.api.admin_deps import AdminServices
from tests.conftest import ADMIN_PASSWORD, claim_and_login, claim_code


def test_unclaimed_instance_redirects_everything_to_claim(client: TestClient) -> None:
    for path in ("/admin", "/admin/tokens", "/admin/github/connect"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/claim"


def test_claim_requires_a_valid_code(client: TestClient, data_dir: Path) -> None:
    response = client.post("/admin/claim", data={"code": "wrong", "password": ADMIN_PASSWORD})
    assert response.status_code == 422
    # A wrong code never deletes the real one.
    assert claim_code(data_dir)


def test_claim_rejects_a_short_password(client: TestClient, data_dir: Path) -> None:
    code = claim_code(data_dir)
    response = client.post("/admin/claim", data={"code": code, "password": "short"})
    assert response.status_code == 422


def test_claim_deletes_the_code_and_writes_a_restricted_record(
    client: TestClient, data_dir: Path
) -> None:
    claim_and_login(client, data_dir)
    assert not (data_dir / "state" / "claim-code").exists()
    admin_json = data_dir / "state" / "admin.json"
    assert admin_json.exists()
    assert stat.S_IMODE(admin_json.stat().st_mode) == 0o600
    body = admin_json.read_text(encoding="utf-8")
    assert '"password_hash": "$argon2' in body
    assert ADMIN_PASSWORD not in body


def test_double_claim_is_refused(client: TestClient, data_dir: Path) -> None:
    claim_and_login(client, data_dir)
    response = client.post("/admin/claim", data={"code": "anything", "password": ADMIN_PASSWORD})
    assert response.status_code == 409


def test_login_sets_a_session_cookie_and_wrong_password_does_not(
    client: TestClient, data_dir: Path
) -> None:
    code = claim_code(data_dir)
    client.post("/admin/claim", data={"code": code, "password": ADMIN_PASSWORD})
    response = client.post("/admin/login", data={"password": "not it"}, follow_redirects=False)
    assert response.status_code == 401
    assert "chronicle_admin_session" not in response.cookies

    response = client.post(
        "/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "chronicle_admin_session" in response.cookies


def test_admin_api_route_is_401_json_without_a_session(client: TestClient, data_dir: Path) -> None:
    claim_and_login(client, data_dir)
    client.cookies.clear()
    response = client.get("/admin/api/status")
    assert response.status_code == 401
    assert response.json()["error"] == "admin_session_required"


def test_logout_clears_the_cookie(admin_client: TestClient) -> None:
    response = admin_client.post("/admin/logout", follow_redirects=False)
    assert response.status_code == 303
    response = admin_client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_session_expires_after_its_lifetime(admin_client: TestClient) -> None:
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    real_now = admin_services.sessions._now
    admin_services.sessions._now = lambda: real_now() + timedelta(hours=13)
    response = admin_client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_reclaiming_after_deleting_the_admin_record_ends_every_session(
    admin_client: TestClient, data_dir: Path
) -> None:
    old_cookie = admin_client.cookies.get("chronicle_admin_session")
    assert old_cookie

    (data_dir / "state" / "admin.json").unlink()
    (data_dir / "state" / "claim-code").write_text("new-code\n", encoding="utf-8")

    fresh = TestClient(admin_client.app, base_url="http://testserver")
    response = fresh.post(
        "/admin/claim", data={"code": "new-code", "password": "a different password"}
    )
    assert response.status_code == 200

    admin_client.cookies.set("chronicle_admin_session", old_cookie)
    response = admin_client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_changing_the_password_ends_every_session(admin_client: TestClient) -> None:
    old_cookie = admin_client.cookies.get("chronicle_admin_session")
    response = admin_client.post(
        "/admin/password",
        data={"current_password": ADMIN_PASSWORD, "new_password": "a brand new password"},
    )
    assert response.status_code == 200

    other = TestClient(admin_client.app, base_url="http://testserver")
    other.cookies.set("chronicle_admin_session", old_cookie)
    response = other.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"

    other.cookies.clear()
    login = other.post("/admin/login", data={"password": "a brand new password"})
    assert login.status_code == 200


def test_wrong_current_password_does_not_change_anything(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/password",
        data={"current_password": "nope", "new_password": "a brand new password"},
    )
    assert response.status_code == 401
    response = admin_client.get("/admin")
    assert response.status_code == 200


def test_a_tampered_cookie_is_rejected(admin_client: TestClient) -> None:
    good = admin_client.cookies.get("chronicle_admin_session")
    assert good
    tampered = good[:-1] + ("a" if good[-1] != "a" else "b")
    admin_client.cookies.set("chronicle_admin_session", tampered)
    response = admin_client.get("/admin", follow_redirects=False)
    assert response.status_code == 303


def test_oversized_claim_form_is_rejected(client: TestClient) -> None:
    huge_code = "x" * 20000
    response = client.post("/admin/claim", data={"code": huge_code, "password": ADMIN_PASSWORD})
    assert response.status_code == 413


def test_status_page_shows_builder_section_with_no_heartbeat_yet(
    admin_client: TestClient,
) -> None:
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "Builder" in response.text
    assert "no heartbeat yet" in response.text


def test_status_page_shows_builder_heartbeat_once_written(
    admin_client: TestClient, data_dir: Path
) -> None:
    heartbeat_path = data_dir / "state" / "builder" / "heartbeat.json"
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.write_text(
        '{"builder_id": "chronicle-builder-1", "last_loop_at": "2026-09-16T09:00:00-05:00", '
        '"hugo_version": "0.164.0", "queue_depth": 0}',
        encoding="utf-8",
    )
    response = admin_client.get("/admin")
    assert response.status_code == 200
    assert "chronicle-builder-1" in response.text
    assert "0.164.0" in response.text
