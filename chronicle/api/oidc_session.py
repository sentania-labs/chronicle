"""Who is signed in through OIDC, and the cookies that carry it (ADR 027).

Two cookies, both sealed with one key, `data/state/oidc-session.key`
(32 random bytes, 0600, made the way `instance.key` is, never backed up):

- the session cookie, carrying the `Principal` a successful sign-in produced
  (issuer, subject, the name the records show, the roles the groups mapped
  to at that sign-in) for `SESSION_LIFETIME_HOURS`;
- the login cookie, carrying one sign-in attempt's state, nonce and PKCE
  verifier for the few minutes between the redirect to the provider and
  the callback, scoped to `/auth/oidc` so no other route ever sees it.

Fernet (authenticated encryption with a timestamp) rather than the admin
session's bare HMAC: the login cookie holds the PKCE verifier, which should
not be readable by anything that happens to see the cookie, and the two
cookie kinds are tagged so one can never be replayed as the other. Deleting
the key file ends every session at once, which is the whole revocation
story this piece needs; nothing is stored per session server-side.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from . import crypto
from .settings import OIDC_ROLES, ROLE_ADMIN

SESSION_COOKIE_NAME = "chronicle_session"
LOGIN_COOKIE_NAME = "chronicle_oidc_login"
LOGIN_COOKIE_PATH = "/auth/oidc"
SESSION_KEY_FILE_NAME = "oidc-session.key"
SESSION_LIFETIME_HOURS = 12
LOGIN_LIFETIME_SECONDS = 600
# A sealed cookie is a few hundred bytes; anything far past that is not ours.
MAX_COOKIE_LENGTH = 4096
_KIND_SESSION = "session"
_KIND_LOGIN = "login"


@dataclass(frozen=True)
class Principal:
    """A signed-in person. `issuer` plus `subject` is the identity (the spec's
    rule); `name` is what the records show and must stay readable to Scott in
    `git log`, so it is the provider's `preferred_username` where there is one
    (`oidc.display_name`), with the identity always beside it in the log line
    that admitted the session."""

    issuer: str
    subject: str
    name: str
    roles: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return ROLE_ADMIN in self.roles

    def describe(self) -> str:
        """The identity for a log line: never the display name alone."""
        return f"issuer={self.issuer} subject={self.subject}"


@dataclass(frozen=True)
class LoginAttempt:
    """One sign-in in flight: what the callback must match the response to."""

    state: str
    nonce: str
    code_verifier: str
    next_path: str


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


class OidcSessions:
    def __init__(
        self,
        state_dir: Path,
        now: Callable[[], float] = time.time,
        lifetime_seconds: int = SESSION_LIFETIME_HOURS * 3600,
    ) -> None:
        self.key_path = state_dir / SESSION_KEY_FILE_NAME
        self._fernet = Fernet(base64.urlsafe_b64encode(crypto.load_or_create_key(self.key_path)))
        self._now = now
        self.lifetime_seconds = lifetime_seconds

    # --- the session cookie ---------------------------------------------

    def issue(self, principal: Principal) -> str:
        payload = {
            "v": 1,
            "kind": _KIND_SESSION,
            "iss": principal.issuer,
            "sub": principal.subject,
            "name": principal.name,
            "roles": sorted(principal.roles),
        }
        return self._seal(payload)

    def read(self, token: str | None) -> Principal | None:
        """The principal a cookie carries, or None for anything else: absent,
        expired, tampered with, sealed as a login attempt, or malformed."""
        payload = self._open(token, _KIND_SESSION, self.lifetime_seconds)
        if payload is None:
            return None
        issuer, subject, name = payload.get("iss"), payload.get("sub"), payload.get("name")
        roles = payload.get("roles")
        if not (_text(issuer) and _text(subject) and _text(name)):
            return None
        if not isinstance(roles, list) or not roles or not set(roles) <= OIDC_ROLES:
            return None
        return Principal(
            issuer=str(issuer), subject=str(subject), name=str(name), roles=frozenset(roles)
        )

    # --- the login cookie -------------------------------------------------

    def seal_login(self, attempt: LoginAttempt) -> str:
        payload = {
            "v": 1,
            "kind": _KIND_LOGIN,
            "state": attempt.state,
            "nonce": attempt.nonce,
            "code_verifier": attempt.code_verifier,
            "next": attempt.next_path,
        }
        return self._seal(payload)

    def open_login(self, token: str | None) -> LoginAttempt | None:
        payload = self._open(token, _KIND_LOGIN, LOGIN_LIFETIME_SECONDS)
        if payload is None:
            return None
        fields = [payload.get(key) for key in ("state", "nonce", "code_verifier", "next")]
        if not all(_text(value) for value in fields):
            return None
        state, nonce, code_verifier, next_path = (str(value) for value in fields)
        return LoginAttempt(
            state=state, nonce=nonce, code_verifier=code_verifier, next_path=next_path
        )

    # --- sealing ------------------------------------------------------------

    def _seal(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt_at_time(raw, int(self._now())).decode("ascii")

    def _open(self, token: str | None, kind: str, ttl: int) -> dict[str, Any] | None:
        if not token or len(token) > MAX_COOKIE_LENGTH or not token.isascii():
            return None
        try:
            raw = self._fernet.decrypt_at_time(token.encode("ascii"), ttl, int(self._now()))
            payload = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("v") != 1 or payload.get("kind") != kind:
            return None
        return payload


def set_session_cookie(response: Any, token: str, *, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_LIFETIME_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def set_login_cookie(response: Any, token: str, *, secure: bool) -> None:
    response.set_cookie(
        LOGIN_COOKIE_NAME,
        token,
        max_age=LOGIN_LIFETIME_SECONDS,
        httponly=True,
        samesite="lax",
        secure=secure,
        path=LOGIN_COOKIE_PATH,
    )


def clear_login_cookie(response: Any) -> None:
    response.delete_cookie(LOGIN_COOKIE_NAME, path=LOGIN_COOKIE_PATH)
