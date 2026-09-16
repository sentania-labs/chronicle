import os
import stat

import pytest
from fastapi.testclient import TestClient

from chronicle.api.main import DATA_DIR_ENV, create_app


def test_healthz_reports_up() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "chronicle-api"
    assert body["status"] == "ok"


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


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_schema_routes_are_not_anonymous(path: str) -> None:
    client = TestClient(create_app())
    response = client.get(path)
    assert response.status_code == 404
