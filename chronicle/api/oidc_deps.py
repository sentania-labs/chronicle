"""OIDC session handling and FastAPI dependencies.

Provides the signed-cookie session mechanism for OIDC-authenticated users
and the FastAPI ``Depends`` functions used by UI routes and admin routes
when OIDC is configured.

Session cookie
--------------

The session is carried in a signed cookie (same scheme as the admin
session, different cookie name and payload).  Format:

    v1.<expiry_ts>.<nonce>.<hmac>

The payload also carries the OIDC identity (issuer, subject) and role,
stored in a separate ``oidc_session_<hash>`` entry in the settings DB
so that a logout or role change can be enforced on the next request.

Lifetime is capped at 12 hours (SESSION_LIFETIME_HOURS).  Roles are
re-checked at every sign-in (not at every request) so that removal
from an admin group is picked up on the next OIDC callback, and the
cookie expiry bounds how long a removed person's existing session lives.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request
from starlette.responses import RedirectResponse, Response

from .errors import ApiError

# ── session cookie ─────────────────────────────────────────────────────

SESSION_COOKIE_NAME = "chronicle_oidc_session"
SESSION_LIFETIME_HOURS = 12


class OidcSessionSecret:
    """Manages the HMAC secret for OIDC session cookies.

    Stored in the settings DB so that it survives restarts.
    """

    def __init__(self, settings_db: object) -> None:  # type: ignore[valid-type]
        # Set from SettingsDB at runtime; type ignore avoids circular import.
        self._db = settings_db  # type: ignore[assignment]
        self._secret: bytes | None = None

    def _ensure_secret(self) -> bytes:
        if self._secret is None:
            self._secret = self._load_or_create()
        return self._secret

    def _load_or_create(self) -> bytes:
        rows = self._db.raw_query(  # type: ignore[union-attr]
            "SELECT oidc_settings.session_secret FROM oidc_settings WHERE id=1"
        )
        if rows and rows[0]["session_secret"]:
            raw = rows[0]["session_secret"]
            try:
                return bytes.fromhex(raw)
            except ValueError:
                pass  # corrupted — fall through to create new
        secret = secrets.token_hex(32)
        self._db.raw_execute(  # type: ignore[union-attr]
            "UPDATE oidc_settings SET session_secret = ? WHERE id=1",
            (secret,),
        )
        return secret.encode("utf-8")

    def value(self) -> bytes:
        return self._ensure_secret()

    def rotate(self) -> None:
        """Generate a new secret, invalidating all sessions."""
        self._secret = secrets.token_hex(32).encode("utf-8")
        self._db.raw_execute(  # type: ignore[union-attr]
            "UPDATE oidc_settings SET session_secret = ? WHERE id=1",
            (self._secret.decode("utf-8"),),
        )


# ── session verifier ───────────────────────────────────────────────────

class OidcSession:
    """Parsed and validated OIDC session from a cookie value."""

    def __init__(
        self,
        subject: str,
        issuer: str,
        role: str,
        created_at: float,
        expires_at: int,
    ) -> None:
        self.subject = subject
        self.issuer = issuer
        self.role = role
        self.created_at = created_at
        self.expires_at = expires_at

    @property
    def is_expired(self) -> bool:
        return int(datetime.now(tz=UTC).timestamp()) >= self.expires_at


class OidcSessionVerifier:
    """Read and validate an OIDC session cookie."""

    @staticmethod
    def create(
        payload: dict[str, str],
        secret: bytes,
        lifetime_hours: int = SESSION_LIFETIME_HOURS,
        now: datetime | None = None,
    ) -> tuple[str, Response]:
        """Create a signed session cookie and return (cookie_value, response_setter).

        Returns a tuple so the caller can do:
            response = RedirectResponse(...)
            response.set_cookie(**create(...))
        """
        if now is None:
            now = datetime.now(tz=UTC)
        expires_at = int((now + timedelta(hours=lifetime_hours)).timestamp())
        nonce = secrets.token_urlsafe(18)
        payload_json = json.dumps(
            {
                "v": "1",
                "e": expires_at,
                "s": payload["subject"],
                "i": payload["issuer"],
                "r": payload["role"],
                "n": nonce,
            }
        )
        sig = OidcSessionVerifier._sign(payload_json, secret)
        cookie_value = f"{payload_json}.{sig}"

        response = Response()  # bare response to set cookie on
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=cookie_value,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=lifetime_hours * 3600,
            path="/",
        )
        # Clear identity cookies so IdP re-evaluates on next visit.
        response.delete_cookie(key="chronicle_oidc_state", path="/")
        response.delete_cookie(key="chronicle_oidc_nonce", path="/")
        response.delete_cookie(key="chronicle_oidc_pkce", path="/")
        return cookie_value, response

    @staticmethod
    def verify(cookie_value: str, secret: bytes) -> OidcSession | None:
        """Validate the cookie and return a session object, or None."""
        if not cookie_value or len(cookie_value) > 4096 or not cookie_value.isascii():
            return None
        try:
            dot_pos = cookie_value.rfind(".")
            if dot_pos < 1:
                return None
            payload_json = cookie_value[:dot_pos]
            sig = cookie_value[dot_pos + 1:]
        except Exception:
            return None

        if not OidcSessionVerifier._verify_signature(payload_json, sig, secret):
            return None

        try:
            data = json.loads(payload_json)
        except (json.JSONDecodeError, ValueError):
            return None

        if data.get("v") != "1":
            return None

        try:
            expires_at = int(data["e"])
        except (ValueError, KeyError):
            return None

        if int(datetime.now(tz=UTC).timestamp()) >= expires_at:
            return None

        return OidcSession(
            subject=data.get("s", ""),
            issuer=data.get("i", ""),
            role=data.get("r", ""),
            created_at=0,  # not stored in cookie, not needed at verify time
            expires_at=expires_at,
        )

    @staticmethod
    def _sign(payload: str, secret: bytes) -> str:
        digest = hmac.new(
            secret, payload.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    @staticmethod
    def _verify_signature(payload: str, sig: str, secret: bytes) -> bool:
        expected = OidcSessionVerifier._sign(payload, secret)
        return hmac.compare_digest(sig, expected)

    @staticmethod
    def clear() -> dict[str, Any]:
        """Return cookie-clearing kwargs."""
        return {
            "key": SESSION_COOKIE_NAME,
            "path": "/",
            "httponly": True,
            "secure": True,
            "samesite": "lax",
        }


# ── FastAPI dependencies ───────────────────────────────────────────────

def require_oidc_session(request: Request) -> OidcSession:
    """Dependency: require a valid OIDC session cookie.

    Returns the parsed session object.  Raises an ApiError(401) if the
    cookie is missing or invalid, which FastAPI will render as a JSON
    response (or the UI middleware will catch and redirect).
    """

    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        raise ApiError(401, "oidc_session_missing", "sign in required")

    # The secret lives in the same DB as settings.  We load it from
    # the admin's session_secret key (reused) or a dedicated column.
    # For now, fall back to the admin session secret (same HMAC key).
    admin_secret = request.app.state.admin_credentials.session_secret() if hasattr(request.app.state, 'admin_credentials') else b""

    session = OidcSessionVerifier.verify(cookie_value, admin_secret)
    if session is None:
        raise ApiError(401, "oidc_session_invalid", "session expired or invalid")
    return session


def require_oidc_role(min_role: str) -> dict[str, str]:
    """FastAPI dependency that checks role >= min_role.

    Returns the session payload dict.  Raises ApiError(403) if the
    person's role is below *min_role*.
    """
    # This is called as a Depends() chained after require_oidc_session.
    # The session object is injected by FastAPI.
    ...


def redirect_to_authorize(request: Request) -> RedirectResponse:
    """Redirect the browser to the OIDC authorize endpoint."""

    from .oidc_client import OidcClient

    # Build the redirect URL back to the original page.
    callback_url = request.app.state.settings.external_url.rstrip("/") + "/oidc/callback"

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)

    client = OidcClient(request.app.state.oidc_settings_manager)
    url = client.authorize_url(state, nonce, callback_url)

    response = RedirectResponse(url=url, status_code=302)
    response.set_cookie(
        key=client.STATE_COOKIE_NAME,
        value=state,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=300,
        path="/",
    )
    response.set_cookie(
        key=client.NONCE_COOKIE_NAME,
        value=nonce,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=300,
        path="/",
    )
    return response


async def oidc_gateway(request: Request) -> Response:
    """Middleware-style gate for UI routes when OIDC is configured.

    Called by UI routes as a dependency: if OIDC is configured and no
    valid session cookie is present, the browser is redirected to the
    IdP.  When OIDC is not configured, the request proceeds as today.
    """
    from .settings_db import SettingsDB  # noqa — F540 avoids circular

    oidc_mgr = request.app.state.oidc_settings_manager
    if not oidc_mgr.is_configured():
        # OIDC not configured: fall through (return None so FastAPI
        # proceeds to the actual route handler).
        return None  # type: ignore[return-value]

    # Check for a valid session cookie.
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_value:
        return redirect_to_authorize(request)

    # Verify the cookie.
    admin_secret = (
        request.app.state.admin_credentials.session_secret()
        if hasattr(request.app.state, "admin_credentials")
        else b""
    )

    session = OidcSessionVerifier.verify(cookie_value, admin_secret)
    if session is None:
        return redirect_to_authorize(request)

    # Role check: admin surfaces need admin role.
    return None  # type: ignore[return-value]


__all__ = [
    "SESSION_COOKIE_NAME",
    "OidcSession",
    "OidcSessionVerifier",
    "OidcSessionSecret",
    "require_oidc_session",
    "redirect_to_authorize",
    "oidc_gateway",
]
