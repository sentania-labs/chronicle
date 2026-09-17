"""The one module that talks to GitHub (spec section 9, ADR 002, ADR 009).

Every outbound call goes through `httpx.Client(base_url=settings.github_api_base)`
so a test can point it at a mock transport instead of the network; nothing
here ever prints or logs a secret. The manifest flow itself is a browser
round trip (GitHub redirects the admin's own browser back to
`/admin/github/callback`), so this module's job starts at the callback: it
exchanges the manifest code, mints App and installation tokens, and makes the
small number of verification calls the admin page needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import jwt

from .settings import Settings

APP_JWT_LIFETIME_SECONDS = 600
DEFAULT_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "metadata": "read",
    "actions": "read",
}


class GitHubApiError(Exception):
    """A GitHub call failed; `error_class` is what /readyz is allowed to show.

    The message may carry response detail useful in a log, but /readyz and
    the admin page only ever surface `error_class`, never `str(exc)`, so a
    future response body containing something sensitive is never echoed back.
    """

    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def build_manifest(settings: Settings, app_name: str) -> dict[str, Any]:
    return {
        "name": app_name,
        "url": settings.external_url,
        "public": False,
        "default_permissions": DEFAULT_PERMISSIONS,
        "default_events": [],
        "redirect_url": f"{settings.external_url}/admin/github/callback",
    }


def manifest_target_url(settings: Settings, state: str, org: str | None = None) -> str:
    if org:
        return f"{settings.github_web_base}/organizations/{org}/settings/apps/new?state={state}"
    return f"{settings.github_web_base}/settings/apps/new?state={state}"


@dataclass
class AppConversion:
    app_id: str
    slug: str
    client_id: str
    client_secret: str
    webhook_secret: str
    pem: str
    html_url: str


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        # A test passes an httpx.MockTransport here so every call in this
        # module goes through a fixture instead of the network; production
        # code never sets it.
        self._transport = transport

    def _client(self, headers: dict[str, str] | None = None) -> httpx.Client:
        base_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if headers:
            base_headers.update(headers)
        return httpx.Client(
            base_url=self._settings.github_api_base,
            headers=base_headers,
            timeout=self._timeout,
            transport=self._transport,
        )

    def exchange_manifest_code(self, code: str) -> AppConversion:
        with self._client() as client:
            response = client.post(f"/app-manifests/{code}/conversions")
        _raise_for_status(response, "manifest_exchange_failed")
        body = response.json()
        return AppConversion(
            app_id=str(body["id"]),
            slug=body["slug"],
            client_id=body["client_id"],
            client_secret=body["client_secret"],
            webhook_secret=body.get("webhook_secret") or "",
            pem=body["pem"],
            html_url=body["html_url"],
        )

    def mint_app_jwt(self, app_id: str, pem: str, now: float | None = None) -> str:
        issued_at = int(now if now is not None else time.time())
        payload = {
            "iat": issued_at - 30,
            "exp": issued_at + APP_JWT_LIFETIME_SECONDS,
            "iss": app_id,
        }
        return jwt.encode(payload, pem, algorithm="RS256")

    def list_installations(self, app_jwt: str) -> list[dict[str, Any]]:
        with self._client({"Authorization": f"Bearer {app_jwt}"}) as client:
            response = client.get("/app/installations")
        _raise_for_status(response, "list_installations_failed")
        result: list[dict[str, Any]] = response.json()
        return result

    def mint_installation_token(self, app_jwt: str, installation_id: str) -> dict[str, Any]:
        with self._client({"Authorization": f"Bearer {app_jwt}"}) as client:
            response = client.post(f"/app/installations/{installation_id}/access_tokens")
        _raise_for_status(response, "installation_token_failed")
        result: dict[str, Any] = response.json()
        return result

    def list_installation_repositories(self, installation_token: str) -> list[dict[str, Any]]:
        with self._client({"Authorization": f"Bearer {installation_token}"}) as client:
            response = client.get("/installation/repositories")
        _raise_for_status(response, "list_repositories_failed")
        repos: list[dict[str, Any]] = response.json().get("repositories", [])
        return repos

    def get_file(
        self, installation_token: str, owner: str, repo: str, path: str, ref: str
    ) -> dict[str, Any] | None:
        with self._client({"Authorization": f"Bearer {installation_token}"}) as client:
            response = client.get(f"/repos/{owner}/{repo}/contents/{path}", params={"ref": ref})
        if response.status_code == 404:
            return None
        _raise_for_status(response, "get_file_failed")
        result: dict[str, Any] = response.json()
        return result

    def list_pull_requests(
        self, installation_token: str, owner: str, repo: str
    ) -> list[dict[str, Any]]:
        with self._client({"Authorization": f"Bearer {installation_token}"}) as client:
            response = client.get(f"/repos/{owner}/{repo}/pulls", params={"state": "open"})
        _raise_for_status(response, "list_pull_requests_failed")
        result: list[dict[str, Any]] = response.json()
        return result


def _raise_for_status(response: httpx.Response, error_class: str) -> None:
    if response.status_code >= 400:
        raise GitHubApiError(
            error_class, f"GitHub returned {response.status_code} for {response.request.url}"
        )
