"""Browser sign-in through OIDC (issue 81 piece 1, ADR 027), against a mocked
identity provider: local signing keys, test tokens, an httpx.MockTransport in
place of the network. Nothing here reaches a real provider.

What the provider stand-in enforces is what a compliant one would: the
authorization request must carry PKCE (S256), `state` and `nonce`; the code
exchange must present the matching verifier, the registered redirect URI and
the client secret read from the file under the data directory; and the id
token it mints is signed with its own key and carries the nonce the attempt
asked for, unless a test tells it to lie.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from chronicle.api import gitrepo
from chronicle.api import settings as settings_mod
from chronicle.api.main import create_app
from chronicle.api.oidc import OidcAuth
from chronicle.api.oidc_session import (
    LOGIN_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    LoginAttempt,
    OidcSessions,
    Principal,
)
from chronicle.api.settings import OidcMisconfigured, Settings
from tests.conftest import ADMIN_PASSWORD, auth, claim_and_login

ISSUER = "https://idp.test/application/o/chronicle/"
CLIENT_ID = "chronicle-client-id"
CLIENT_SECRET = "s3cret-from-the-file"
ADMINS = "chronicle-admins"
EDITORS = "chronicle-editors"
AUTHORIZE_URL = "https://idp.test/application/o/authorize/"
TOKEN_URL = "https://idp.test/application/o/token/"
JWKS_URL = "https://idp.test/application/o/chronicle/jwks/"
USERINFO_URL = "https://idp.test/application/o/userinfo/"


class FakeIdp:
    """One provider: discovery, keys, token endpoint, userinfo."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
        self.codes: dict[str, dict[str, str]] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.userinfo_requests = 0
        self.discovery_requests = 0
        self.unreachable = False
        self.groups: list[str] = [EDITORS]
        self.groups_in_userinfo = False
        self.claims_override: dict[str, Any] = {}
        self.token_error: str | None = None
        self.subject = "subject-0001"
        self.username: str | None = "scott"

    # --- what the browser does -------------------------------------------

    def authorize(self, location: str) -> str:
        """Play the browser at the provider: check the authorization request
        and return the callback URL with a fresh code bound to it."""
        parts = urlsplit(location)
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == AUTHORIZE_URL
        query = dict(parse_qsl(parts.query))
        assert query["client_id"] == CLIENT_ID
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        for required in ("state", "nonce", "code_challenge", "redirect_uri"):
            assert query[required]
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "nonce": query["nonce"],
            "code_challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
        }
        return query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"]})

    # --- what the provider serves ----------------------------------------

    def metadata(self) -> dict[str, Any]:
        return {
            "issuer": ISSUER,
            "authorization_endpoint": AUTHORIZE_URL,
            "token_endpoint": TOKEN_URL,
            "jwks_uri": JWKS_URL,
            "userinfo_endpoint": USERINFO_URL,
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.unreachable:
            raise httpx.ConnectError("connection refused", request=request)
        url = str(request.url)
        if url == ISSUER + ".well-known/openid-configuration":
            self.discovery_requests += 1
            return httpx.Response(200, json=self.metadata())
        if url == JWKS_URL:
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url == TOKEN_URL:
            return self._token(request)
        if url == USERINFO_URL:
            return self._userinfo(request)
        return httpx.Response(404, json={"error": "not_found", "url": url})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = dict(parse_qsl(request.content.decode("utf-8")))
        authorization = request.headers.get("authorization", "")
        self.token_requests.append({"form": form, "authorization": authorization})
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if authorization != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        if self.token_error:
            return httpx.Response(400, json={"error": self.token_error})
        record = self.codes.pop(form.get("code", ""), None)
        if form.get("grant_type") != "authorization_code" or record is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != record["redirect_uri"]:
            return httpx.Response(400, json={"error": "invalid_grant", "reason": "redirect"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode("ascii")).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != record["code_challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant", "reason": "pkce"})
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": self.subject,
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "auth_time": now,
            "nonce": record["nonce"],
            "email": "scott@example.test",
            "groups": list(self.groups),
        }
        if self.username is not None:
            claims["preferred_username"] = self.username
        if self.groups_in_userinfo:
            del claims["groups"]
        claims.update(self.claims_override)
        id_token = jwt.encode({"alg": "RS256", "kid": "k1"}, claims, self.key)
        return httpx.Response(
            200,
            json={
                "access_token": "access-" + secrets.token_hex(4),
                "token_type": "Bearer",
                "id_token": id_token,
            },
        )

    def _userinfo(self, request: httpx.Request) -> httpx.Response:
        self.userinfo_requests += 1
        if not request.headers.get("authorization", "").startswith("Bearer access-"):
            return httpx.Response(401, json={"error": "invalid_token"})
        return httpx.Response(200, json={"sub": self.subject, "groups": list(self.groups)})


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


def configure_oidc(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, *, group_roles: str | None = None
) -> Path:
    """The deployer's side: the settings and the secret file under the data dir."""
    monkeypatch.setenv("CHRONICLE_EXTERNAL_URL", "http://testserver")
    monkeypatch.setenv("CHRONICLE_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("CHRONICLE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv(
        "CHRONICLE_OIDC_GROUP_ROLES", group_roles or f"{ADMINS}=admin, {EDITORS}=editor"
    )
    secret_path = data_dir / "state" / "oidc-client-secret"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    return secret_path


@pytest.fixture
def oidc_client(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, idp: FakeIdp
) -> Iterator[TestClient]:
    configure_oidc(monkeypatch, data_dir)
    app = create_app()
    # The same swap the GitHub client's tests make: the real build, with the
    # provider stand-in's transport in place of the network.
    app.state.oidc = OidcAuth.build(
        data_dir, app.state.admin_services.settings, transport=httpx.MockTransport(idp.handler)
    )
    with TestClient(app) as client:
        yield client
    app.state.services.close()


def start_sign_in(client: TestClient, next_path: str = "/content/drafts") -> httpx.Response:
    return client.get("/auth/oidc/start", params={"next": next_path}, follow_redirects=False)


def sign_in(client: TestClient, idp: FakeIdp, next_path: str = "/content/drafts") -> httpx.Response:
    started = start_sign_in(client, next_path)
    assert started.status_code == 303, started.text
    return client.get(idp.authorize(started.headers["location"]), follow_redirects=False)


def session_cookie(client: TestClient) -> str | None:
    return client.cookies.get(SESSION_COOKIE_NAME)


# --- the happy path -----------------------------------------------------------


def test_sign_in_allowed_sets_a_session_and_returns_to_the_page_asked_for(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    started = start_sign_in(oidc_client, "/content/previews")
    assert started.status_code == 303
    location = started.headers["location"]
    assert location.startswith(AUTHORIZE_URL)
    assert LOGIN_COOKIE_NAME in started.cookies
    login_cookie = started.headers["set-cookie"]
    assert "HttpOnly" in login_cookie and "Path=/auth/oidc" in login_cookie

    finished = oidc_client.get(idp.authorize(location), follow_redirects=False)
    assert finished.status_code == 303, finished.text
    assert finished.headers["location"] == "/content/previews"
    assert session_cookie(oidc_client)
    set_cookie = finished.headers.get_list("set-cookie")
    session_header = next(h for h in set_cookie if h.startswith(SESSION_COOKIE_NAME))
    assert "HttpOnly" in session_header and "SameSite=lax" in session_header
    assert "Path=/" in session_header

    # The code exchange carried the PKCE verifier and the registered redirect
    # URI, and authenticated with the secret read from the file, never from
    # an environment value (none is set).
    assert len(idp.token_requests) == 1
    form = idp.token_requests[0]["form"]
    assert form["grant_type"] == "authorization_code"
    assert form["code_verifier"]
    assert form["redirect_uri"] == "http://testserver/auth/oidc/callback"
    assert not any(name.startswith("CHRONICLE_OIDC_CLIENT_SECRET") for name in os.environ)

    page = oidc_client.get("/content/drafts")
    assert page.status_code == 200
    assert "Signed in as <strong>scott</strong>" in page.text
    assert 'action="/auth/oidc/logout"' in page.text
    # The "unauthenticated" banner is no longer true, so it is gone.
    assert "internal-only and unauthenticated" not in page.text


def test_ui_requires_a_session_when_oidc_is_configured(oidc_client: TestClient) -> None:
    response = oidc_client.get(
        "/content/drafts?status=published", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/auth/oidc/start?next=%2Fcontent%2Fdrafts%3Fstatus%3Dpublished"
    )
    # A script's fetch, not a navigation: a plain 401 in the usual envelope.
    fetched = oidc_client.get("/content/drafts", headers={"accept": "application/json"})
    assert fetched.status_code == 401
    assert fetched.json()["error"] == "session_required"
    posted = oidc_client.post("/content/drafts/new", follow_redirects=False)
    assert posted.status_code == 401
    # Health and readiness stay anonymous, as do the static assets a sign-in
    # error page needs.
    assert oidc_client.get("/healthz").status_code == 200
    assert oidc_client.get("/static/style.css").status_code == 200


def test_actions_are_recorded_against_the_signed_in_person(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    assert sign_in(oidc_client, idp).status_code == 303
    created = oidc_client.post("/content/drafts/new", follow_redirects=False)
    assert created.status_code == 303
    draft_id = created.headers["location"].split("/")[3].split("?")[0]
    store = oidc_client.app.state.services.store  # type: ignore[attr-defined]
    events, _cursor = store.events_since(0)
    assert events[-1].type == "draft.created" and events[-1].actor == "scott"
    assert gitrepo.log_authors(store.repo_dir, 1) == ["scott"]
    # The ui token is still the credential, so the editor-only actions still
    # work, now authored by the person at the keyboard.
    assert oidc_client.post(f"/content/drafts/{draft_id}/actions/submit").status_code == 200
    revision = oidc_client.post(
        f"/content/drafts/{draft_id}/actions/request_revision", data={"feedback": "more detail"}
    )
    assert revision.status_code == 200
    assert store.list_feedback(draft_id)[-1].author == "scott"
    events, _cursor = store.events_since(0)
    assert [e.actor for e in events if e.type == "draft.request_revision"] == ["scott"]
    page = oidc_client.get(f"/content/drafts/{draft_id}")
    assert "Signed in as <strong>scott</strong>" in page.text


def test_sign_out_ends_the_session_in_this_browser(oidc_client: TestClient, idp: FakeIdp) -> None:
    assert sign_in(oidc_client, idp).status_code == 303
    response = oidc_client.post("/auth/oidc/logout")
    assert response.status_code == 200
    assert "Signed out" in response.text
    assert not session_cookie(oidc_client)
    bounced = oidc_client.get(
        "/content/drafts", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert bounced.status_code == 303
    # Without a session there is nothing to end: not an anonymous route.
    assert oidc_client.post("/auth/oidc/logout").status_code == 401
    # Cross-origin sign-out requests are refused like every other UI form.
    assert sign_in(oidc_client, idp).status_code == 303
    refused = oidc_client.post("/auth/oidc/logout", headers={"origin": "https://evil.test"})
    assert refused.status_code == 403
    assert session_cookie(oidc_client)


def test_groups_are_read_from_userinfo_when_the_id_token_lacks_the_claim(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    idp.groups_in_userinfo = True
    assert sign_in(oidc_client, idp).status_code == 303
    assert idp.userinfo_requests == 1
    assert session_cookie(oidc_client)


def test_display_name_falls_back_and_never_takes_a_reserved_name(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    idp.username = "editor"  # the ui token's own author name
    assert sign_in(oidc_client, idp).status_code == 303
    page = oidc_client.get("/content/drafts")
    assert "Signed in as <strong>scott@example.test</strong>" in page.text


# --- refusals --------------------------------------------------------------------


def test_unmapped_group_is_refused_and_gets_no_session(
    oidc_client: TestClient, idp: FakeIdp, caplog: pytest.LogCaptureFixture
) -> None:
    idp.groups = ["some-other-team"]
    with caplog.at_level("WARNING", logger="chronicle.api.oidc"):
        response = sign_in(oidc_client, idp)
    assert response.status_code == 403
    assert "not in any group this Chronicle maps to a role" in response.text
    assert session_cookie(oidc_client) is None
    assert not oidc_client.cookies.get(LOGIN_COOKIE_NAME)
    # Logs say who was refused, by identity, not just that someone was.
    assert any("subject=subject-0001" in record.message for record in caplog.records)
    bounced = oidc_client.get(
        "/content/drafts", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert bounced.status_code == 303


def test_bad_state_is_refused(oidc_client: TestClient, idp: FakeIdp) -> None:
    started = start_sign_in(oidc_client)
    callback = idp.authorize(started.headers["location"])
    tampered = re.sub(r"state=[^&]+", "state=not-the-state", callback)
    response = oidc_client.get(tampered, follow_redirects=False)
    assert response.status_code == 400
    assert "did not match the attempt" in response.text
    assert session_cookie(oidc_client) is None
    assert idp.token_requests == []  # refused before any code exchange


def test_bad_nonce_is_refused(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.claims_override = {"nonce": "a-replayed-nonce"}
    response = sign_in(oidc_client, idp)
    assert response.status_code == 400
    assert "id token was refused" in response.text
    assert session_cookie(oidc_client) is None


def test_bad_audience_is_refused(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.claims_override = {"aud": "some-other-client"}
    response = sign_in(oidc_client, idp)
    assert response.status_code == 400
    assert "id token was refused" in response.text
    assert session_cookie(oidc_client) is None


def test_wrong_issuer_and_bad_signature_are_refused(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.claims_override = {"iss": "https://someone-else.test/"}
    assert sign_in(oidc_client, idp).status_code == 400
    assert session_cookie(oidc_client) is None
    idp.claims_override = {}
    # A token signed by a key the provider does not publish.
    idp.key = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig", "alg": "RS256"})
    oidc_client.app.state.oidc.client._keys = None  # type: ignore[attr-defined]
    original_handler = idp.handler

    def stale_keys(request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            other = RSAKey.generate_key(2048, parameters={"kid": "k1", "use": "sig"})
            return httpx.Response(200, json={"keys": [other.as_dict(private=False)]})
        return original_handler(request)

    oidc_client.app.state.oidc.client._transport = httpx.MockTransport(stale_keys)  # type: ignore[attr-defined]
    response = sign_in(oidc_client, idp)
    assert response.status_code == 400
    assert "did not verify" in response.text
    assert session_cookie(oidc_client) is None


def test_callback_without_a_started_attempt_is_refused(oidc_client: TestClient) -> None:
    response = oidc_client.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False)
    assert response.status_code == 400
    assert "expired or was not started in this browser" in response.text


def test_provider_error_text_is_never_written_into_the_page(
    oidc_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    start_sign_in(oidc_client)
    payload = "<script>alert(1)</script>"
    with caplog.at_level("WARNING", logger="chronicle.api.oidc"):
        response = oidc_client.get(
            "/auth/oidc/callback",
            params={"error": "access_denied<img>", "error_description": payload},
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert payload not in response.text
    assert "<img>" not in response.text
    assert "refused the sign-in (an error)" in response.text
    # The provider's words are in the log for the operator, not on the page.
    assert any(payload in record.message for record in caplog.records)


def test_code_exchange_refusal_is_readable(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.token_error = "invalid_grant"
    response = sign_in(oidc_client, idp)
    assert response.status_code == 502
    assert "refused the code exchange (invalid_grant)" in response.text
    assert session_cookie(oidc_client) is None


def test_idp_unreachable_gives_a_readable_error_and_break_glass_still_works(
    oidc_client: TestClient, idp: FakeIdp, data_dir: Path
) -> None:
    idp.unreachable = True
    response = start_sign_in(oidc_client)
    assert response.status_code == 502
    assert "could not be reached" in response.text
    assert 'href="/admin/login"' in response.text
    assert "<script>" not in response.text.split("</head>")[1].split("<script src=")[0]

    # Admin's claim and password flows are untouched by the provider being down.
    claim_and_login(oidc_client, data_dir)
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 200
    # ... and the login page offers both doors.
    oidc_client.cookies.clear()
    login_page = oidc_client.get("/admin/login")
    assert 'href="/auth/oidc/start?next=/admin"' in login_page.text
    assert 'action="/admin/login"' in login_page.text
    assert "break-glass" in login_page.text


def test_missing_client_secret_file_fails_the_exchange_readably(
    oidc_client: TestClient, idp: FakeIdp, data_dir: Path
) -> None:
    (data_dir / "state" / "oidc-client-secret").unlink()
    response = sign_in(oidc_client, idp)
    assert response.status_code == 500
    assert "client secret file is missing" in response.text
    assert session_cookie(oidc_client) is None


# --- admin and /v1 ------------------------------------------------------------------


def test_admin_accepts_an_admin_role_session_and_refuses_an_editor_one(
    oidc_client: TestClient, idp: FakeIdp, data_dir: Path
) -> None:
    # A claimed instance, so a refusal lands on the login page rather than
    # the claim page; the OIDC admin path below needs neither.
    claim_and_login(oidc_client, data_dir)
    oidc_client.cookies.clear()
    idp.groups = [EDITORS]
    assert sign_in(oidc_client, idp).status_code == 303
    bounced = oidc_client.get("/admin", follow_redirects=False)
    assert bounced.status_code == 303
    assert bounced.headers["location"] == "/admin/login"
    login_page = oidc_client.get("/admin/login")
    assert "whose groups grant no admin role" in login_page.text
    assert oidc_client.get("/admin/api/status").status_code == 401

    idp.groups = [ADMINS, EDITORS]
    finished = sign_in(oidc_client, idp, next_path="/admin")
    assert finished.status_code == 303 and finished.headers["location"] == "/admin"
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 200
    assert oidc_client.get("/admin/api/status").status_code == 200
    assert oidc_client.get("/admin/tokens").status_code == 200
    # An admin also uses the content UI.
    assert oidc_client.get("/content/drafts").status_code == 200


def test_roles_are_rechecked_at_every_sign_in(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.groups = [ADMINS]
    assert sign_in(oidc_client, idp).status_code == 303
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 200
    # The person is moved out of the admins group at the provider and signs
    # in again: the new session carries only what the groups grant now.
    idp.groups = [EDITORS]
    assert sign_in(oidc_client, idp).status_code == 303
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 303
    assert oidc_client.get("/content/drafts").status_code == 200


def test_admin_actions_by_an_oidc_admin_are_recorded_under_their_name(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    idp.groups = [ADMINS]
    assert sign_in(oidc_client, idp).status_code == 303
    store = oidc_client.app.state.services.store  # type: ignore[attr-defined]
    flag = store.create_flag(
        "post_removed_without_unpublish",
        slug="gone",
        draft_id=None,
        detail="a post main no longer has",
        actor="chronicle",
    )
    response = oidc_client.post(
        f"/admin/api/reconcile/{flag.id}/resolve", json={"resolution": "ignore"}
    )
    assert response.status_code == 200, response.text
    assert store.get_flag(flag.id).resolved_by == "scott"


def test_admin_logout_also_ends_an_oidc_admin_session(
    oidc_client: TestClient, idp: FakeIdp
) -> None:
    idp.groups = [ADMINS]
    assert sign_in(oidc_client, idp).status_code == 303
    assert oidc_client.post("/admin/logout", follow_redirects=False).status_code == 303
    assert not session_cookie(oidc_client)
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 303


def test_session_cookie_never_authenticates_v1(oidc_client: TestClient, idp: FakeIdp) -> None:
    idp.groups = [ADMINS, EDITORS]
    assert sign_in(oidc_client, idp).status_code == 303
    assert session_cookie(oidc_client)
    for path in ("/v1/drafts", "/v1/submissions", "/v1/events"):
        response = oidc_client.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"] == "token_required"
    # A real bearer token still works exactly as before, with the cookie present.
    token = oidc_client.app.state.services.tokens.issue("ghostwriter")  # type: ignore[attr-defined]
    assert oidc_client.get("/v1/drafts", headers=auth(token)).status_code == 200


def test_password_login_still_works_alongside_oidc(oidc_client: TestClient, data_dir: Path) -> None:
    claim_and_login(oidc_client, data_dir, ADMIN_PASSWORD)
    assert oidc_client.get("/admin", follow_redirects=False).status_code == 200
    assert session_cookie(oidc_client) is None  # the password session is its own cookie


# --- OIDC unset: exactly today's behaviour ---------------------------------------


def test_oidc_unset_leaves_the_ui_open_and_the_routes_absent(client: TestClient) -> None:
    assert client.get("/content/drafts").status_code == 200
    assert "internal-only and unauthenticated" in client.get("/content/drafts").text
    assert "Signed in as" not in client.get("/content/drafts").text
    for path in ("/auth/oidc/start", "/auth/oidc/callback?code=a&state=b"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404
        assert response.json()["error"] == "oidc_not_configured"
    assert client.post("/auth/oidc/logout").status_code == 404
    assert client.app.state.oidc is None  # type: ignore[attr-defined]


def test_oidc_unset_login_page_offers_only_the_password(client: TestClient, data_dir: Path) -> None:
    claim_and_login(client, data_dir)
    client.cookies.clear()
    page = client.get("/admin/login")
    assert "/auth/oidc/start" not in page.text
    assert 'action="/admin/login"' in page.text


# --- configuration ------------------------------------------------------------------


def test_group_role_mapping_parses_pairs_and_json_and_refuses_the_rest() -> None:
    parse = settings_mod._parse_group_roles
    assert parse("team-a=admin, team-b=editor") == {"team-a": "admin", "team-b": "editor"}
    assert parse("odd=name=editor") == {"odd=name": "editor"}
    assert parse('{"Team With Spaces, Inc": "admin"}') == {"Team With Spaces, Inc": "admin"}
    with pytest.raises(OidcMisconfigured, match="unknown role"):
        parse("team-a=root")
    with pytest.raises(OidcMisconfigured, match="group=role"):
        parse("team-a")
    with pytest.raises(OidcMisconfigured, match="maps no group"):
        parse("  ,  ")
    with pytest.raises(OidcMisconfigured, match="not valid JSON"):
        parse("{not json")
    # No group name lives in the code or its defaults.
    assert settings_mod.DEFAULT_OIDC_GROUPS_CLAIM == "groups"
    assert not any(
        isinstance(value, dict) and value
        for name, value in vars(settings_mod).items()
        if name.startswith("DEFAULT_OIDC")
    )


def test_settings_default_and_derive_what_the_deployer_left_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_oidc(monkeypatch, tmp_path)
    monkeypatch.setenv("CHRONICLE_OIDC_SCOPES", "profile,email groups")
    oidc = Settings.from_env().oidc
    assert oidc is not None
    assert oidc.scopes == ("openid", "profile", "email", "groups")
    assert oidc.redirect_uri == "http://testserver/auth/oidc/callback"
    assert oidc.groups_claim == "groups"
    assert oidc.client_secret_file == "state/oidc-client-secret"
    assert oidc.client_secret_path(tmp_path) == tmp_path / "state" / "oidc-client-secret"
    assert oidc.roles_for([ADMINS, "unrelated"]) == frozenset({"admin"})
    assert oidc.roles_for(["unrelated"]) == frozenset()
    assert oidc.roles_for([EDITORS.upper()]) == frozenset()  # matched exactly


def test_partial_configuration_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRONICLE_OIDC_ISSUER", ISSUER)
    with pytest.raises(OidcMisconfigured, match="CHRONICLE_OIDC_CLIENT_ID"):
        Settings.from_env()
    monkeypatch.setenv("CHRONICLE_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("CHRONICLE_OIDC_GROUP_ROLES", f"{ADMINS}=admin")
    monkeypatch.setenv("CHRONICLE_OIDC_ISSUER", "idp.test/no-scheme")
    with pytest.raises(OidcMisconfigured, match="absolute http"):
        Settings.from_env()


def test_client_secret_outside_the_data_dir_refuses_to_start(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    configure_oidc(monkeypatch, data_dir)
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "secret"
    elsewhere.write_text("nope\n", encoding="utf-8")
    monkeypatch.setenv("CHRONICLE_OIDC_CLIENT_SECRET_FILE", str(elsewhere))
    with pytest.raises(OidcMisconfigured, match="not under the data directory"):
        create_app()
    # A relative path that climbs out is refused the same way.
    monkeypatch.setenv("CHRONICLE_OIDC_CLIENT_SECRET_FILE", "../secret")
    with pytest.raises(OidcMisconfigured, match="not under the data directory"):
        create_app()


def test_discovery_issuer_must_match_exactly(oidc_client: TestClient, idp: FakeIdp) -> None:
    original = idp.metadata

    def without_slash() -> dict[str, Any]:
        document = original()
        document["issuer"] = ISSUER.rstrip("/")
        return document

    idp.metadata = without_slash  # type: ignore[method-assign]
    response = start_sign_in(oidc_client)
    assert response.status_code == 502
    assert "names a different issuer" in response.text


# --- the session itself -----------------------------------------------------------------


def test_sessions_expire_and_cannot_be_swapped_for_login_attempts(tmp_path: Path) -> None:
    clock = {"now": 1_700_000_000.0}
    sessions = OidcSessions(tmp_path, now=lambda: clock["now"])
    principal = Principal(issuer=ISSUER, subject="s", name="scott", roles=frozenset({"editor"}))
    token = sessions.issue(principal)
    assert sessions.read(token) == principal
    assert sessions.open_login(token) is None  # a session is not a login attempt
    attempt = LoginAttempt(state="st", nonce="no", code_verifier="cv", next_path="/x")
    login = sessions.seal_login(attempt)
    assert sessions.open_login(login) == attempt
    assert sessions.read(login) is None  # and a login attempt is not a session
    clock["now"] += 11 * 60
    assert sessions.open_login(login) is None  # ten minutes for an attempt
    assert sessions.read(token) == principal
    clock["now"] += 12 * 3600
    assert sessions.read(token) is None  # twelve hours for a session
    assert sessions.read("garbage") is None
    assert sessions.read(None) is None
    # A different key (another instance, or a deleted key file) reads nothing,
    # even inside the token's own lifetime.
    (tmp_path / "other").mkdir()
    other = OidcSessions(tmp_path / "other", now=lambda: clock["now"] - 13 * 3600)
    assert other.read(token) is None
    assert (tmp_path / "oidc-session.key").stat().st_mode & 0o777 == 0o600
    assert json.loads(json.dumps(sorted(principal.roles))) == ["editor"]
