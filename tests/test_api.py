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
