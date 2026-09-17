"""Wiring and the session dependency for the admin router.

Deliberately its own module, parallel to `deps.py`'s consumer-token wiring:
content routes under `/v1` never import this, and this module never touches
`require_consumer`, so a session cookie can never authenticate a `/v1` call
and a bearer token can never authenticate `/admin`.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from starlette.responses import RedirectResponse

from . import crypto
from .admin_auth import (
    GITHUB_STATE_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_LIFETIME_HOURS,
    AdminCredentials,
    AdminRecordUnreadable,
    SignedSessions,
)
from .errors import ApiError
from .github_app import GitHubAppStore
from .github_client import GitHubClient
from .settings import Settings

DIGEST_STATUS_FILE_NAME = "digest-status.json"
TOOLCHAIN_FILE_NAME = "toolchain.json"


class AdminAuthRedirect(Exception):
    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


async def admin_redirect_handler(request: Request, exc: Exception) -> RedirectResponse:
    assert isinstance(exc, AdminAuthRedirect)
    return RedirectResponse(exc.location, status_code=303)


@dataclass
class InstallationToken:
    token: str
    expires_at: str


@dataclass
class AdminServices:
    state_dir: Path
    settings: Settings
    credentials: AdminCredentials
    sessions: SignedSessions
    instance_key: bytes
    github_store: GitHubAppStore
    github_client: GitHubClient
    _installation_tokens: dict[str, InstallationToken] = field(default_factory=dict)
    _digest_lock: threading.Lock = field(default_factory=threading.Lock)
    digest_running: bool = False

    @classmethod
    def build(
        cls,
        data_dir: Path,
        settings: Settings | None = None,
        github_client: GitHubClient | None = None,
    ) -> AdminServices:
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        settings = settings or Settings.from_env()
        credentials = AdminCredentials(state_dir)
        credentials.ensure_claim_code()
        instance_key = crypto.load_or_create_instance_key(state_dir)
        github_store = GitHubAppStore(state_dir, instance_key)
        return cls(
            state_dir=state_dir,
            settings=settings,
            credentials=credentials,
            sessions=SignedSessions(credentials),
            instance_key=instance_key,
            github_store=github_store,
            github_client=github_client or GitHubClient(settings),
        )

    def cached_installation_token(self, installation_id: str) -> InstallationToken | None:
        return self._installation_tokens.get(installation_id)

    def cache_installation_token(self, installation_id: str, token: InstallationToken) -> None:
        self._installation_tokens[installation_id] = token

    def write_digest_status(self, summary: dict[str, Any]) -> None:
        path = self.state_dir / DIGEST_STATUS_FILE_NAME
        path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_digest_status(self) -> dict[str, Any] | None:
        path = self.state_dir / DIGEST_STATUS_FILE_NAME
        if not path.exists():
            return None
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def write_toolchain(self, toolchain: dict[str, Any]) -> None:
        path = self.state_dir / TOOLCHAIN_FILE_NAME
        path.write_text(json.dumps(toolchain, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def read_toolchain(self) -> dict[str, Any] | None:
        path = self.state_dir / TOOLCHAIN_FILE_NAME
        if not path.exists():
            return None
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded


def get_admin_services(request: Request) -> AdminServices:
    admin_services = getattr(request.app.state, "admin_services", None)
    if admin_services is None:
        raise ApiError(503, "admin_unavailable", "the data directory is not ready")
    return admin_services


def _cookie_session_valid(request: Request, admin: AdminServices) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not token:
        return False
    try:
        return admin.sessions.valid(token)
    except AdminRecordUnreadable:
        return False


def require_admin_session_html(request: Request) -> AdminServices:
    """HTML admin pages: unauthenticated visits redirect instead of erroring."""
    admin = get_admin_services(request)
    if not admin.credentials.is_claimed():
        raise AdminAuthRedirect("/admin/claim")
    if not _cookie_session_valid(request, admin):
        raise AdminAuthRedirect("/admin/login")
    return admin


def require_admin_session_json(request: Request) -> AdminServices:
    """/admin/api routes: unauthenticated calls get a plain 401, like /v1."""
    admin = get_admin_services(request)
    if not admin.credentials.is_claimed():
        raise ApiError(401, "admin_not_claimed", "this instance has not been claimed yet")
    if not _cookie_session_valid(request, admin):
        raise ApiError(401, "admin_session_required", "an admin session cookie is required")
    return admin


def set_session_cookie(response: Any, admin: AdminServices, request: Request) -> None:
    token = admin.sessions.create()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_LIFETIME_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=admin.settings.cookie_secure or request.url.scheme == "https",
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def set_github_state_cookie(
    response: Any, admin: AdminServices, request: Request, state: str
) -> None:
    response.set_cookie(
        GITHUB_STATE_COOKIE_NAME,
        state,
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=admin.settings.cookie_secure or request.url.scheme == "https",
        path="/admin/github",
    )
