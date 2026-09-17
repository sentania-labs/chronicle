import os
import sqlite3
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronicle.api.index import index_path
from chronicle.api.main import DATA_DIR_ENV, create_app
from chronicle.api.store import Store
from chronicle.api.tokens import UI_TOKEN_FILE_NAME

from .conftest import auth


def test_csp_header_present_and_scripts_stay_self_only() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src" not in csp  # falls back to default-src 'self': no inline allowance anywhere


def test_healthz_reports_up() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "chronicle-api"
    assert body["status"] == "ok"
    assert body["version"] == "dev"


def test_healthz_reports_the_build_version_env(monkeypatch) -> None:
    monkeypatch.setenv("CHRONICLE_BUILD_VERSION", "v1.2.3")
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.json()["version"] == "v1.2.3"


def test_readyz_ok_when_data_dir_writable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    names = {check["name"] for check in body["checks"]}
    assert {"data_dir", "git", "github_app"} <= names


def test_readyz_fails_when_data_dir_missing(tmp_path, monkeypatch) -> None:
    missing = tmp_path / "does-not-exist"
    monkeypatch.setenv(DATA_DIR_ENV, str(missing))
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["ready"] is False
    data_dir_check = next(check for check in body["checks"] if check["name"] == "data_dir")
    assert data_dir_check["ok"] is False


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permission bits")
def test_readyz_fails_when_data_dir_not_writable(tmp_path, monkeypatch) -> None:
    read_only = tmp_path / "read-only"
    read_only.mkdir()
    read_only.chmod(0o500)
    try:
        monkeypatch.setenv(DATA_DIR_ENV, str(read_only))
        client = TestClient(create_app())
        response = client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["ready"] is False
        data_dir_check = next(check for check in body["checks"] if check["name"] == "data_dir")
        assert data_dir_check["ok"] is False
    finally:
        read_only.chmod(stat.S_IRWXU)


def test_startup_rebuilds_a_missing_index(data_dir: Path) -> None:
    store = Store.open(data_dir)
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, {"title": "Restored"}, "body")
    store.close()
    index_path(store.repo_dir).unlink()

    app = create_app()
    with TestClient(app) as client:
        token = (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()
        listed = client.get("/v1/drafts", headers=auth(token))
        assert [item["id"] for item in listed.json()["drafts"]] == [draft.id]
    app.state.services.close()


def test_bootstrap_survives_an_index_that_cannot_open(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    client = TestClient(create_app())
    response = client.get("/readyz")
    assert response.status_code == 503
    index_check = next(check for check in response.json()["checks"] if check["name"] == "index")
    assert index_check["ok"] is False


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_schema_routes_are_not_anonymous(path: str) -> None:
    client = TestClient(create_app())
    response = client.get(path)
    assert response.status_code == 404
