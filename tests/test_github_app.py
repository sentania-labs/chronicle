"""The GitHub App manifest flow, credential encryption, and live checks.

Every test here uses a generated RSA key and an httpx.MockTransport standing
in for GitHub; nothing makes a real network call (spec section 9, ADR 002,
ADR 009).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from chronicle.api import crypto
from chronicle.api.admin_deps import AdminServices
from chronicle.api.github_app import GitHubAppStore, readiness_state
from chronicle.api.github_client import GitHubClient, build_manifest
from chronicle.api.settings import Settings
from tests.conftest import claim_and_login


@pytest.fixture
def rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def test_manifest_has_no_webhook_and_the_right_permissions_and_redirect() -> None:
    settings = Settings.from_env()
    manifest = build_manifest(settings, "chronicle-test")
    assert manifest["public"] is False
    assert "hook_attributes" not in manifest
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "metadata": "read",
        "actions": "read",
    }
    assert manifest["redirect_url"].endswith("/admin/github/callback")
    assert "single_file" not in manifest


def test_connect_page_carries_a_state_and_the_manifest(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/github/connect")
    assert response.status_code == 200
    assert "chronicle_admin_github_state" in response.cookies
    assert "settings.yaml" not in response.text  # sanity: no stray secret path leaked
    assert "manifest" in response.text.lower()


def test_callback_rejects_a_mismatched_state(admin_client: TestClient) -> None:
    admin_client.get("/admin/github/connect")
    response = admin_client.get(
        "/admin/github/callback", params={"code": "abc", "state": "not-the-real-state"}
    )
    assert response.status_code == 400


def _failing_conversion_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "code has expired"})

    return httpx.MockTransport(handler)


def test_callback_renders_the_failure_page_when_no_app_record_exists_yet(
    admin_client: TestClient,
) -> None:
    """The very first exchange has no App record to record_error() against.

    Before this fix, GitHubAppStore.record_error() called _require() with no
    record on disk, which raised ValueError instead of returning the 502
    page, turning an ordinary expired code into an internal server error.
    """
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    admin_services.github_client = GitHubClient(
        admin_services.settings, transport=_failing_conversion_transport()
    )
    assert admin_services.github_store.load() is None

    connect = admin_client.get("/admin/github/connect")
    state = connect.cookies["chronicle_admin_github_state"]

    response = admin_client.get(
        "/admin/github/callback", params={"code": "expired-code", "state": state}
    )
    assert response.status_code == 502
    assert "Traceback" not in response.text
    assert admin_services.github_store.load() is None


def test_org_connect_route_renders_a_form_targeting_the_organization_manifest_url(
    admin_client: TestClient,
) -> None:
    connect = admin_client.get("/admin/github/connect")
    response = admin_client.post(
        "/admin/github/connect/org", data={"org": "sentania-labs", "manifest": "{}"}
    )
    assert connect.status_code == 200
    assert response.status_code == 200
    assert "https://github.com/organizations/sentania-labs/settings/apps/new" in response.text


def test_org_connect_route_requires_an_org(admin_client: TestClient) -> None:
    response = admin_client.post("/admin/github/connect/org", data={"manifest": "{}"})
    assert response.status_code == 422


def _manifest_transport(pem: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app-manifests/good-code/conversions":
            return httpx.Response(
                201,
                json={
                    "id": 4242,
                    "slug": "chronicle-test-app",
                    "client_id": "client-id-123",
                    "client_secret": "client-secret-xyz",
                    "webhook_secret": "webhook-secret-xyz",
                    "pem": pem,
                    "html_url": "https://github.com/apps/chronicle-test-app",
                },
            )
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def test_callback_exchanges_the_code_and_never_stores_the_pem_in_plaintext(
    admin_client: TestClient, data_dir: Path, rsa_pem: str
) -> None:
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    admin_services.github_client = GitHubClient(
        admin_services.settings, transport=_manifest_transport(rsa_pem)
    )

    connect = admin_client.get("/admin/github/connect")
    state = connect.cookies["chronicle_admin_github_state"]

    response = admin_client.get(
        "/admin/github/callback", params={"code": "good-code", "state": state}
    )
    assert response.status_code == 200
    # The success page is reached with a session, so it keeps the tabs and Log out.
    assert 'href="/admin/github/connect" aria-current="page"' in response.text
    assert "Log out" in response.text

    record = admin_services.github_store.load()
    assert record is not None
    assert record.app_id == "4242"
    assert record.slug == "chronicle-test-app"

    # The PEM text must not appear verbatim anywhere under data/state/.
    pem_marker = "-----BEGIN"
    for path in (data_dir / "state").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert pem_marker not in text, f"{path} contains PEM text"
            assert "client-secret-xyz" not in text
            assert "webhook-secret-xyz" not in text

    # But the store can still get back the exact PEM it was given.
    assert admin_services.github_store.pem(record) == rsa_pem
    assert admin_services.github_store.client_secret(record) == "client-secret-xyz"


def test_app_jwt_is_rs256_and_short_lived(rsa_pem: str) -> None:
    settings = Settings.from_env()
    client = GitHubClient(settings)
    token = client.mint_app_jwt("4242", rsa_pem, now=1_700_000_000)
    private_key = serialization.load_pem_private_key(rsa_pem.encode(), password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    public_key = private_key.public_key()
    decoded = jwt.decode(token, public_key, algorithms=["RS256"], options={"verify_exp": False})
    assert decoded["iss"] == "4242"
    assert decoded["exp"] - decoded["iat"] <= 630


def _installation_transport(pem: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/installations":
            return httpx.Response(200, json=[{"id": 555, "account": {"login": "sentania-labs"}}])
        if request.url.path == "/app/installations/555/access_tokens":
            return httpx.Response(
                201,
                json={
                    "token": "ghs_installation_token",
                    "expires_at": "2999-01-01T00:00:00Z",
                    "permissions": {"contents": "write", "pull_requests": "write"},
                },
            )
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "repositories": [
                        {"full_name": "sentania/sentania.github.io", "default_branch": "main"}
                    ]
                },
            )
        if request.url.path == "/repos/sentania/sentania.github.io/contents/README.md":
            return httpx.Response(200, json={"content": base64.b64encode(b"hi").decode()})
        if request.url.path == "/repos/sentania/sentania.github.io/pulls":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


def _configured_admin(admin_client: TestClient, rsa_pem: str) -> AdminServices:
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    admin_services.github_store.store_new_app(
        app_id="4242",
        slug="chronicle-test-app",
        client_id="client-id",
        client_secret="secret",
        webhook_secret="whsecret",
        pem=rsa_pem,
        html_url="https://github.com/apps/chronicle-test-app",
    )
    admin_services.github_client = GitHubClient(
        admin_services.settings, transport=_installation_transport(rsa_pem)
    )
    return admin_services


def test_install_and_repo_flow_verifies_contents_and_pulls_access(
    admin_client: TestClient, rsa_pem: str
) -> None:
    admin_services = _configured_admin(admin_client, rsa_pem)

    installs = admin_client.get("/admin/github/install")
    assert installs.status_code == 200
    assert "sentania-labs" in installs.text

    picked = admin_client.post("/admin/github/install", data={"installation_id": "555"})
    assert picked.status_code == 200
    assert admin_services.github_store.load().installation_id == "555"  # type: ignore[union-attr]

    repos = admin_client.get("/admin/github/repo")
    assert repos.status_code == 200
    assert "sentania/sentania.github.io" in repos.text

    chosen = admin_client.post("/admin/github/repo", data={"repo": "sentania/sentania.github.io"})
    assert chosen.status_code == 200

    record = admin_services.github_store.load()
    assert record is not None
    assert record.owner_repo == "sentania/sentania.github.io"
    assert record.default_branch == "main"
    assert record.last_verified_at is not None
    assert record.permissions["contents"] == "read"
    assert record.permissions["pull_requests"] == "read"


def test_installation_token_is_never_written_to_disk(
    admin_client: TestClient, data_dir: Path, rsa_pem: str
) -> None:
    admin_services = _configured_admin(admin_client, rsa_pem)
    admin_client.post("/admin/github/install", data={"installation_id": "555"})
    admin_client.post("/admin/github/repo", data={"repo": "sentania/sentania.github.io"})

    for path in (data_dir / "state").rglob("*"):
        if path.is_file():
            assert "ghs_installation_token" not in path.read_text(encoding="utf-8", errors="ignore")
    assert admin_services.cached_installation_token("555") is not None


def test_readyz_reflects_github_app_states(
    client: TestClient, data_dir: Path, rsa_pem: str
) -> None:
    assert client.get("/readyz").json()["checks"][3] == {
        "name": "github_app",
        "ok": True,
        "detail": "not configured",
    }

    claim_and_login(client, data_dir)
    admin_services: AdminServices = client.app.state.admin_services  # type: ignore[attr-defined]
    admin_services.github_store.store_new_app(
        app_id="1",
        slug="s",
        client_id="c",
        client_secret="cs",
        webhook_secret="ws",
        pem=rsa_pem,
        html_url="https://github.com/apps/s",
    )
    detail = client.get("/readyz").json()["checks"][3]["detail"]
    assert detail == "configured but unverified"

    admin_services.github_store.record_verification({"contents": "read"})
    detail = client.get("/readyz").json()["checks"][3]["detail"]
    assert detail.startswith("verified at")

    admin_services.github_store.record_error("list_repositories_failed")
    detail = client.get("/readyz").json()["checks"][3]
    assert detail["detail"] == "failing: list_repositories_failed"
    assert detail["ok"] is False


def test_readiness_state_helper_never_needs_the_record_decrypted() -> None:
    assert readiness_state(None) == "not configured"


def test_github_app_store_permissions_default_empty(tmp_path: Path) -> None:
    key = crypto.load_or_create_instance_key(tmp_path)
    store = GitHubAppStore(tmp_path, key)
    assert store.load() is None
    assert not store.is_configured()


def test_digest_json_endpoint_returns_409_when_a_digest_is_already_running(
    admin_client: TestClient,
) -> None:
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    assert admin_services._digest_lock.acquire(blocking=False)
    try:
        response = admin_client.post("/admin/api/digest")
        assert response.status_code == 409
    finally:
        admin_services._digest_lock.release()


def test_digest_html_endpoint_shows_an_already_running_notice(admin_client: TestClient) -> None:
    admin_services: AdminServices = admin_client.app.state.admin_services  # type: ignore[attr-defined]
    assert admin_services._digest_lock.acquire(blocking=False)
    try:
        response = admin_client.post("/admin/digest")
        assert response.status_code == 409
        assert "already running" in response.text
    finally:
        admin_services._digest_lock.release()


def test_manifest_json_round_trips_through_the_connect_page(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/github/connect")
    text = response.text
    marker = '<pre class="lat-code">'
    start = text.index(marker) + len(marker)
    end = text.index("</pre>")
    from html import unescape

    manifest = json.loads(unescape(text[start:end]))
    assert manifest["public"] is False
