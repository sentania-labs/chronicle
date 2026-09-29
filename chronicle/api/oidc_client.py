"""OIDC authorization code flow with PKCE.

Handles the complete login sequence:
    1. /oidc/authorize   — generate PKCE challenge, state, nonce; redirect to IdP
    2. /oidc/callback    — validate state/nonce, exchange code for tokens,
                           verify JWT, create signed session cookie

Discovery and JWKS are fetched lazily and cached in-memory (30-minute TTL).
If the IdP is unreachable the authorize endpoint returns a readable error
page instead of failing to start.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from typing import Any

import httpx
import jwt

from .roles import ROLES


class OidcError(Exception):
    """OIDC flow error; the caller should render a friendly page."""

    def __init__(
        self, message: str, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


class IdpUnavailable(OidcError):
    """The IdP could not be reached or did not return valid data."""


class JwkCache:
    """Cache JWKS from the IdP, fetched lazily on first use."""

    def __init__(self) -> None:
        self._keys: list[dict[str, Any]] | None = None
        self._fetched_at: float = 0
        self._ttl: float = 30 * 60  # 30 minutes
        self._lock = threading.Lock()
        self._issuer: str = ""

    def set_issuer(self, issuer: str) -> None:
        self._issuer = issuer

    def get_keys(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._keys and (now - self._fetched_at) < self._ttl:
            return self._keys

        with self._lock:
            if self._keys and (now - self._fetched_at) < self._ttl:
                return self._keys

            keys = self._fetch()
            self._keys = keys
            self._fetched_at = time.monotonic()
            return keys

    def _fetch(self) -> list[dict[str, Any]]:
        if not self._issuer:
            raise IdpUnavailable("OIDC issuer is not configured")

        # Fetch discovery document.
        disc_url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = httpx.get(disc_url, timeout=10.0)
            resp.raise_for_status()
            disc: dict = resp.json()
        except httpx.RequestError as exc:
            raise IdpUnavailable(
                f"could not reach IdP at {disc_url}: {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise IdpUnavailable(
                f"IdP discovery returned {exc.response.status_code}"
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise IdpUnavailable(
                f"IdP discovery returned invalid JSON: {exc}"
            ) from exc

        jwks_uri = disc.get("jwks_uri")
        if not jwks_uri:
            raise IdpUnavailable("discovery document has no jwks_uri")

        # Fetch JWKS.
        try:
            resp = httpx.get(jwks_uri, timeout=10.0)
            resp.raise_for_status()
            jwks: dict = resp.json()
        except httpx.RequestError as exc:
            raise IdpUnavailable(f"could not fetch JWKS: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise IdpUnavailable(
                f"JWKS endpoint returned {exc.response.status_code}"
            ) from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise IdpUnavailable(
                f"JWKS endpoint returned invalid JSON: {exc}"
            ) from exc

        keys = jwks.get("keys")
        if not isinstance(keys, list) or not keys:
            raise IdpUnavailable("JWKS document has no keys")
        return keys


class OidcClient:
    """OIDC authorization code flow with PKCE.

    Depends on OidcSettingsManager for the current configuration.
    JWK keys are cached in a JwkCache instance.
    """

    SESSION_COOKIE_NAME = "chronicle_oidc_session"
    STATE_COOKIE_NAME = "chronicle_oidc_state"
    NONCE_COOKIE_NAME = "chronicle_oidc_nonce"
    PKCE_COOKIE_NAME = "chronicle_oidc_pkce"
    SESSION_LIFETIME_HOURS = 12

    def __init__(self, settings_manager: OidcSettingsManager) -> None:
        self._settings = settings_manager
        self._jwks = JwkCache()

    def authorize_url(
        self, state: str, nonce: str, callback_url: str
    ) -> str:
        """Build the authorization URL and return it."""
        config = self._settings._ensure_config()
        if not config.issuer:
            raise OidcError("OIDC is not configured")

        self._jwks.set_issuer(config.issuer)

        issuer = config.issuer.rstrip("/")
        scope = " ".join(config.scopes)
        code_challenge = self._make_pkce_challenge()

        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": callback_url,
            "scope": scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        auth_url = f"{issuer}/authorize"
        return f"{auth_url}?{self._encode_params(params)}"

    @staticmethod
    def _make_pkce_challenge() -> str:
        code_verifier = secrets.token_urlsafe(96)
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(
            digest
        ).rstrip(b"=").decode("ascii")
        return code_challenge

    @staticmethod
    def _encode_params(params: dict[str, str]) -> str:
        from urllib.parse import urlencode

        return urlencode(params)

    def exchange_code(
        self,
        code: str,
        pkce_verifier: str,
        redirect_uri: str,
        state: str,
        nonce: str,
    ) -> dict[str, Any]:
        """Exchange an authorization code for tokens and verify the ID token.

        Returns the session payload dict.
        Raises OidcError on any failure.
        """
        config = self._settings._ensure_config()

        # Verify state and nonce match what we issued (caller must
        # validate cookies).  We store the expected values on the
        # settings manager during the authorize call so the callback
        # can check them here.
        if state != getattr(config, "_last_state", ""):
            raise OidcError(
                "state mismatch: the request was already submitted or expired",
                {"state": state},
            )
        if nonce != getattr(config, "_last_nonce", ""):
            raise OidcError(
                "nonce mismatch: the nonce did not match the issued value",
                {"nonce": nonce},
            )

        # Fetch and verify JWKS.
        try:
            keys = self._jwks.get_keys()
        except IdpUnavailable as exc:
            raise OidcError(
                "IdP is unavailable: could not verify the sign-in response",
                {"details": str(exc)},
            ) from exc

        # Exchange code for tokens.
        token_url = config.issuer.rstrip("/") + "/token"
        try:
            resp = httpx.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": config.client_id,
                    "code_verifier": pkce_verifier,
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            token_resp: dict = resp.json()
        except httpx.RequestError as exc:
            raise OidcError(
                "could not reach IdP token endpoint",
                {"details": str(exc)},
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise OidcError(
                f"token endpoint returned {exc.response.status_code}",
                {"status": exc.response.status_code},
            ) from exc

        id_token = token_resp.get("id_token")
        if not id_token:
            raise OidcError("token response has no id_token")

        # Verify the ID token.
        payload = self._verify_id_token(
            id_token,
            nonce=nonce,
            issuer=config.issuer,
            client_id=config.client_id,
            keys=keys,
        )

        # Resolve groups to role.
        group_claim_name = config.groups_claim_name
        groups: list[str] = payload.get(group_claim_name, [])
        if not isinstance(groups, list):
            groups = []

        role = self._resolve_role(groups, config.group_role_map)
        if role is None:
            raise OidcError(
                "you are not in any group with access to Chronicle",
                {"groups": groups},
            )

        subject = payload.get("sub", "")
        issuer_val = payload.get("iss", "")

        return {
            "subject": subject,
            "issuer": issuer_val,
            "role": role,
            "claims": payload,
        }

    def _verify_id_token(
        self,
        token: str,
        nonce: str,
        issuer: str,
        client_id: str,
        keys: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Verify the ID token's signature, issuer, audience, and expiry."""
        # Build the key set for PyJWT.
        jwks_dict = {"keys": keys}

        try:
            payload = jwt.decode(
                token,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_nbf": False,
                    "issuer": issuer,
                    "audience": client_id,
                    "algorithms": ["RS256"],
                },
                jwks=jwks_dict,
            )
        except jwt.ExpiredSignatureError:
            raise OidcError("the sign-in response has expired")
        except jwt.InvalidIssuerError:
            raise OidcError(
                "the sign-in response came from an unexpected issuer"
            )
        except jwt.InvalidAudienceError:
            raise OidcError(
                "the sign-in response is not for this application"
            )
        except jwt.InvalidTokenError as exc:
            raise OidcError(f"ID token verification failed: {exc}")

        # Verify nonce manually (PyJWT has no verify_nonce option).
        token_nonce = payload.get("nonce")
        if token_nonce != nonce:
            raise OidcError(
                "nonce mismatch in the ID token",
                {"expected": nonce, "got": token_nonce},
            )

        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise OidcError("ID token has no subject")
        return payload

    def _resolve_role(
        self, groups: list[str], group_role_map: dict[str, str]
    ) -> str | None:
        """Map a list of group names to the highest matching role."""
        best: str | None = None
        for group in groups:
            if group in group_role_map:
                candidate = group_role_map[group]
                if best is None or self._role_is_stronger(candidate, best):
                    best = candidate
        return best

    @staticmethod
    def _role_is_stronger(a: str, b: str) -> bool:
        return ROLES.index(a) > ROLES.index(b)
