"""Admin surface for reconciliation and test-token mode (ADR 012, spec section 12)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chronicle.api.main import create_app
from chronicle.api.store import Store
from tests.conftest import claim_and_login


def _app_with_env(data_dir: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> TestClient:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    client = TestClient(app)
    client.__enter__()
    return client


def test_status_page_shows_test_token_mode(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app_with_env(
        data_dir,
        monkeypatch,
        CHRONICLE_ALLOW_TEST_TOKEN="1",
        CHRONICLE_GITHUB_TEST_TOKEN="fake-token-value",
        CHRONICLE_GITHUB_TEST_REPO="o/r",
    )
    try:
        claim_and_login(client, data_dir)
        response = client.get("/admin")
        assert response.status_code == 200
        assert "test token mode" in response.text
        assert "fake-token-value" not in response.text, "the token itself must never render"
        assert "o/r" in response.text
    finally:
        client.__exit__(None, None, None)


def test_readyz_reports_test_token_mode(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _app_with_env(
        data_dir,
        monkeypatch,
        CHRONICLE_ALLOW_TEST_TOKEN="1",
        CHRONICLE_GITHUB_TEST_TOKEN="fake-token-value",
        CHRONICLE_GITHUB_TEST_REPO="o/r",
    )
    try:
        response = client.get("/readyz")
        body = response.json()
        github_check = next(c for c in body["checks"] if c["name"] == "github_app")
        assert github_check["detail"] == "test token mode"
        assert "fake-token-value" not in response.text
    finally:
        client.__exit__(None, None, None)


def test_resolve_flag_via_admin_html_route(store: Store, admin_client: TestClient) -> None:
    flag = store.create_flag(
        "post_on_main_without_published_draft",
        slug="orphan",
        draft_id=None,
        detail="test",
        actor="test",
    )
    response = admin_client.post(
        f"/admin/reconcile/{flag.id}/resolve", data={"resolution": "ignore"}, follow_redirects=False
    )
    assert response.status_code == 303
    resolved = store.get_flag(flag.id)
    assert resolved.resolved is True
    assert resolved.resolution == "ignore"


def test_resolve_flag_via_admin_json_route(store: Store, admin_client: TestClient) -> None:
    flag = store.create_flag(
        "post_on_main_without_published_draft",
        slug="orphan",
        draft_id=None,
        detail="test",
        actor="test",
    )
    response = admin_client.post(
        f"/admin/api/reconcile/{flag.id}/resolve", json={"resolution": "ignore"}
    )
    assert response.status_code == 200
    assert response.json()["resolution"] == "ignore"


def test_api_refuses_to_start_with_test_token_but_no_allow_flag(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chronicle.api.settings import TestTokenNotAllowed

    monkeypatch.setenv("CHRONICLE_GITHUB_TEST_TOKEN", "fake-token-value")
    monkeypatch.delenv("CHRONICLE_ALLOW_TEST_TOKEN", raising=False)
    with pytest.raises(TestTokenNotAllowed) as excinfo:
        create_app()
    assert "CHRONICLE_GITHUB_TEST_TOKEN" in str(excinfo.value)
    assert "CHRONICLE_ALLOW_TEST_TOKEN" in str(excinfo.value)
