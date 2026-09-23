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
from dataclasses import dataclass, field
from typing import Any, Protocol

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


def _ref_already_gone(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("message") == "Reference does not exist"


def _raise_for_status(response: httpx.Response, error_class: str) -> None:
    if response.status_code >= 400:
        raise GitHubApiError(
            error_class, f"GitHub returned {response.status_code} for {response.request.url}"
        )


# --- GitHubRepoOps: the narrow interface publish, watch, and reconcile need
# (ADR 012, ADR 013). `AppRepoOps` (production) and `TestRepoOps` (test-only)
# are the only two implementations; neither is used outside this round's
# publisher, watcher, and reconciler.


class GitHubRepoOps(Protocol):
    """Everything publish, watch, and reconcile need against one repo.

    Every method already knows which repo it acts against (bound at
    construction, `owner`/`repo`), so a caller never threads them through
    every call the way `GitHubClient` above does for the App bootstrap
    flow, which has no single repo yet when most of its calls happen.
    """

    def get_ref(self, ref: str) -> dict[str, Any] | None: ...
    def create_ref(self, ref: str, sha: str) -> None: ...
    def update_ref(self, ref: str, sha: str, force: bool = True) -> None: ...
    def delete_ref(self, ref: str) -> None: ...
    def get_commit(self, sha: str) -> dict[str, Any]: ...
    def get_contents(self, path: str, ref: str) -> dict[str, Any] | None: ...
    def create_blob(self, content_b64: str) -> str: ...
    def create_tree(self, base_tree: str, entries: list[dict[str, Any]]) -> str: ...
    def create_commit(self, message: str, tree_sha: str, parents: list[str]) -> str: ...
    def create_pull(self, title: str, head: str, base: str, body: str) -> dict[str, Any]: ...
    def get_pull(self, number: int) -> dict[str, Any]: ...
    def list_open_pulls_by_head(self, head: str) -> list[dict[str, Any]]: ...
    def update_pull_body(self, number: int, body: str) -> dict[str, Any]: ...


@dataclass
class _HttpRepoOps:
    """Shared httpx plumbing for both `GitHubRepoOps` implementations.

    Subclasses provide `_token()`; everything else is one repo's worth of
    the git data, contents, and pulls APIs, each call a single request (no
    retries: a failed call surfaces as `GitHubApiError` and the caller, a
    run, records it as `failed` the same way a builder run does).
    """

    owner: str
    repo: str
    api_base: str
    timeout: float = 15.0
    transport: httpx.BaseTransport | None = None

    def _token(self) -> str:
        raise NotImplementedError

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {self._token()}",
            },
            timeout=self.timeout,
            transport=self.transport,
        )

    def _path(self, suffix: str) -> str:
        return f"/repos/{self.owner}/{self.repo}{suffix}"

    def get_ref(self, ref: str) -> dict[str, Any] | None:
        with self._client() as client:
            response = client.get(self._path(f"/git/ref/{ref}"))
        if response.status_code == 404:
            return None
        _raise_for_status(response, "get_ref_failed")
        result: dict[str, Any] = response.json()
        return result

    def create_ref(self, ref: str, sha: str) -> None:
        with self._client() as client:
            response = client.post(self._path("/git/refs"), json={"ref": f"refs/{ref}", "sha": sha})
        _raise_for_status(response, "create_ref_failed")

    def update_ref(self, ref: str, sha: str, force: bool = True) -> None:
        with self._client() as client:
            response = client.patch(
                self._path(f"/git/refs/{ref}"), json={"sha": sha, "force": force}
            )
        _raise_for_status(response, "update_ref_failed")

    def delete_ref(self, ref: str) -> None:
        with self._client() as client:
            response = client.delete(self._path(f"/git/refs/{ref}"))
        if response.status_code == 404:
            # Already gone (a second unpublish, or Scott deleted it by hand):
            # the caller wanted it absent, and it is, so this is success.
            return
        if response.status_code == 422 and _ref_already_gone(response):
            # GitHub answers a delete of a ref that is already gone with 422
            # and message "Reference does not exist", not 404 (issue 60's
            # live case). The caller wanted it absent, and it is, so this is
            # success too. Any other 422 (a real conflict) still raises.
            return
        _raise_for_status(response, "delete_ref_failed")

    def get_commit(self, sha: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.get(self._path(f"/git/commits/{sha}"))
        _raise_for_status(response, "get_commit_failed")
        result: dict[str, Any] = response.json()
        return result

    def get_contents(self, path: str, ref: str) -> dict[str, Any] | None:
        with self._client() as client:
            response = client.get(self._path(f"/contents/{path}"), params={"ref": ref})
        if response.status_code == 404:
            return None
        _raise_for_status(response, "get_contents_failed")
        result: dict[str, Any] = response.json()
        return result

    def create_blob(self, content_b64: str) -> str:
        with self._client() as client:
            response = client.post(
                self._path("/git/blobs"), json={"content": content_b64, "encoding": "base64"}
            )
        _raise_for_status(response, "create_blob_failed")
        return str(response.json()["sha"])

    def create_tree(self, base_tree: str, entries: list[dict[str, Any]]) -> str:
        with self._client() as client:
            response = client.post(
                self._path("/git/trees"), json={"base_tree": base_tree, "tree": entries}
            )
        _raise_for_status(response, "create_tree_failed")
        return str(response.json()["sha"])

    def create_commit(self, message: str, tree_sha: str, parents: list[str]) -> str:
        with self._client() as client:
            response = client.post(
                self._path("/git/commits"),
                json={"message": message, "tree": tree_sha, "parents": parents},
            )
        _raise_for_status(response, "create_commit_failed")
        return str(response.json()["sha"])

    def create_pull(self, title: str, head: str, base: str, body: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.post(
                self._path("/pulls"),
                json={"title": title, "head": head, "base": base, "body": body},
            )
        _raise_for_status(response, "create_pull_failed")
        result: dict[str, Any] = response.json()
        return result

    def get_pull(self, number: int) -> dict[str, Any]:
        with self._client() as client:
            response = client.get(self._path(f"/pulls/{number}"))
        _raise_for_status(response, "get_pull_failed")
        result: dict[str, Any] = response.json()
        return result

    def list_open_pulls_by_head(self, head: str) -> list[dict[str, Any]]:
        with self._client() as client:
            response = client.get(
                self._path("/pulls"),
                params={"state": "open", "head": f"{self.owner}:{head}"},
            )
        _raise_for_status(response, "list_pull_requests_failed")
        result: list[dict[str, Any]] = response.json()
        return result

    def update_pull_body(self, number: int, body: str) -> dict[str, Any]:
        with self._client() as client:
            response = client.patch(self._path(f"/pulls/{number}"), json={"body": body})
        _raise_for_status(response, "update_pull_failed")
        result: dict[str, Any] = response.json()
        return result


@dataclass
class AppRepoOps(_HttpRepoOps):
    """Production `GitHubRepoOps`: an installation token, minted and cached.

    `token_provider` is a zero-argument callable that returns a live
    installation token, refreshing it if the cached one has expired; the
    publisher, watcher, and reconciler each get one bound to the
    configured repo from `AdminServices` (the same mint-and-cache recipe
    `routes/admin.py` and `digest_runner.py` already use).
    """

    token_provider: Any = None

    def _token(self) -> str:
        assert self.token_provider is not None
        return str(self.token_provider())


@dataclass
class TestRepoOps(_HttpRepoOps):
    """Test-only `GitHubRepoOps`: a bearer token handed to it directly.

    Only constructed when `CHRONICLE_GITHUB_TEST_TOKEN` and
    `CHRONICLE_ALLOW_TEST_TOKEN=1` are both set (ADR 012); the token never
    touches disk or a log line here or anywhere else in this module.
    """

    static_token: str = field(default="")

    def _token(self) -> str:
        return self.static_token


def parse_owner_repo(owner_repo: str) -> tuple[str, str]:
    owner, _, repo = owner_repo.partition("/")
    if not owner or not repo:
        raise ValueError(f"{owner_repo!r} is not an owner/repo pair")
    return owner, repo
