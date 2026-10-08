"""Browser sign-in through OpenID Connect (issue 81 piece 1, ADR 027).

The authorization code flow with PKCE, `state` and `nonce` against any
standards-compliant provider, built from Authlib's protocol pieces (the PKCE
challenge, the grant and token request encoders, the client authentication
header, and `CodeIDToken`'s claim validation) with joserfc, Authlib's own
JOSE engine, verifying the id token's signature; Authlib 1.8 itself verifies
id tokens exactly this way and marks its older `authlib.jose` deprecated.
The HTTP calls go through httpx, so a test hands `OidcClient` a
`MockTransport` the way the GitHub client takes one, and nothing here ever
reaches a network in a test.

What a sign-in must survive before a session exists: the discovery
document's `issuer` equals the configured one; the token response carries an
id token signed by a key the provider publishes, with an asymmetric
algorithm the provider lists; the token's `iss`, `aud`, `exp`, `iat` and
`nonce` check out against this attempt; and the person's groups map to at
least one role. The last is the route's decision (`routes/oidc.py`), on
`ProviderIdentity.groups`; everything before it is this module's.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from authlib.common.security import generate_token
from authlib.oauth2.auth import ClientAuth
from authlib.oauth2.rfc6749.parameters import prepare_grant_uri, prepare_token_request
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from authlib.oidc.core import CodeIDToken
from fastapi import Request
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet
from joserfc.jws import JWSRegistry

from .oidc_session import SESSION_COOKIE_NAME, LoginAttempt, OidcSessions, Principal
from .settings import OidcSettings, Settings
from .tokens import RESERVED_TOKEN_NAMES

log = logging.getLogger("chronicle.api.oidc")

DISCOVERY_PATH = "/.well-known/openid-configuration"
HTTP_TIMEOUT_SECONDS = 10.0
# Clock skew tolerated on `exp`, `iat` and `nbf`; Authlib's own default is
# twice this, which is more than two servers on NTP ever need.
CLAIM_LEEWAY_SECONDS = 60
# Discovery is re-read this often so a provider's rotated endpoints or
# algorithms are picked up without a restart; keys are re-read on demand
# whenever an id token names a `kid` the cached set lacks.
METADATA_TTL_SECONDS = 3600
# Only asymmetric signatures. An HMAC id token would be signed with the
# client secret, which makes anyone holding it able to mint a sign-in.
ALLOWED_ID_TOKEN_ALGS = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)


class OidcError(Exception):
    """A sign-in that cannot continue. `str()` is written for the person at
    the browser and is escaped on render; `detail` is for the log only and
    may carry the provider's own words."""

    status_code = 400

    def __init__(self, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail


class ProviderUnreachable(OidcError):
    status_code = 502


class ProviderRejected(OidcError):
    """The provider answered, but not with something this flow can use."""

    status_code = 502


class SignInInvalid(OidcError):
    """The response does not belong to the attempt, or the token did not verify."""

    status_code = 400


class ClientSecretUnavailable(OidcError):
    status_code = 500


@dataclass(frozen=True)
class ProviderIdentity:
    """What the provider asserted about the person, after verification."""

    issuer: str
    subject: str
    preferred_username: str | None
    email: str | None
    groups: tuple[str, ...]


def display_name(identity: ProviderIdentity) -> str:
    """The name the records carry for this person: `preferred_username`,
    else the email, else the subject itself. The two names the ui token's
    own writes reserve (`ui`, `editor`) fall back to the subject too, so a
    person's account can never masquerade as the unauthenticated editor."""
    for candidate in (identity.preferred_username, identity.email):
        if candidate and candidate not in RESERVED_TOKEN_NAMES:
            return candidate
    return identity.subject


def _as_groups(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list):
        return tuple(item for item in raw if isinstance(item, str) and item)
    return ()


class OidcClient:
    """The relying-party side of one provider. Lazy: nothing is fetched until
    the first sign-in starts, so a provider that is down never stops the api
    from serving, and the admin password keeps working throughout."""

    def __init__(
        self,
        settings: OidcSettings,
        data_dir: Path,
        transport: httpx.BaseTransport | None = None,
        now: Any = time.time,
    ) -> None:
        self.settings = settings
        self.secret_path = settings.client_secret_path(data_dir)
        # A test passes an httpx.MockTransport here so the whole flow runs
        # against a provider it controls; production never sets it.
        self._transport = transport
        self._now = now
        self._lock = threading.Lock()
        self._metadata: dict[str, Any] | None = None
        self._metadata_at = 0.0
        self._keys: KeySet | None = None

    def _http(self) -> httpx.Client:
        return httpx.Client(
            timeout=HTTP_TIMEOUT_SECONDS,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )

    def _get_json(self, url: str, what: str, headers: dict[str, str] | None = None) -> Any:
        try:
            with self._http() as http:
                response = http.get(url, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderRejected(
                f"the identity provider answered {exc.response.status_code} for its {what}",
                detail=f"GET {url}",
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(
                "the identity provider could not be reached", detail=f"GET {url}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ProviderRejected(
                f"the identity provider's {what} is not JSON", detail=f"GET {url}: {exc}"
            ) from exc

    # --- discovery ---------------------------------------------------------

    def metadata(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = self._now() - self._metadata_at < METADATA_TTL_SECONDS
            if self._metadata is not None and fresh and not force:
                return self._metadata
        issuer = self.settings.issuer
        url = issuer.rstrip("/") + DISCOVERY_PATH
        document = self._get_json(url, "discovery document")
        if not isinstance(document, dict):
            raise ProviderRejected("the identity provider's discovery document is not an object")
        if document.get("issuer") != issuer:
            raise ProviderRejected(
                "the identity provider's discovery document names a different issuer",
                detail=f"document says {document.get('issuer')!r}, configured {issuer!r};"
                " the two must be identical, trailing slash included",
            )
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(document.get(key), str) or not document[key]:
                raise ProviderRejected(
                    f"the identity provider's discovery document has no {key}", detail=url
                )
        with self._lock:
            self._metadata = document
            self._metadata_at = self._now()
        return document

    def _key_set(self, force: bool = False) -> KeySet:
        with self._lock:
            if self._keys is not None and not force:
                return self._keys
        document = self._get_json(self.metadata()["jwks_uri"], "key set")
        try:
            keys = KeySet.import_key_set(document)
        except (JoseError, ValueError, TypeError, KeyError) as exc:
            raise ProviderRejected(
                "the identity provider's key set could not be read", detail=str(exc)
            ) from exc
        with self._lock:
            self._keys = keys
        return keys

    # --- the flow -----------------------------------------------------------

    def begin(self, next_path: str) -> tuple[str, LoginAttempt]:
        """The provider URL to send the browser to, and the attempt the callback
        must match: fresh `state`, `nonce` and PKCE verifier every time."""
        metadata = self.metadata()
        attempt = LoginAttempt(
            state=generate_token(32),
            nonce=generate_token(32),
            code_verifier=generate_token(64),
            next_path=next_path,
        )
        url = prepare_grant_uri(
            metadata["authorization_endpoint"],
            client_id=self.settings.client_id,
            response_type="code",
            redirect_uri=self.settings.redirect_uri,
            scope=" ".join(self.settings.scopes),
            state=attempt.state,
            nonce=attempt.nonce,
            code_challenge=create_s256_code_challenge(attempt.code_verifier),
            code_challenge_method="S256",
        )
        return url, attempt

    def complete(self, code: str, attempt: LoginAttempt) -> ProviderIdentity:
        """Exchange the code, verify the id token against `attempt`, and read
        the person's groups (from the id token, else from userinfo)."""
        metadata = self.metadata()
        token = self._exchange(code, attempt, metadata)
        claims = self._verify_id_token(token, attempt, metadata)
        groups = self._groups(claims, token, metadata)
        email = claims.get("email")
        username = claims.get("preferred_username")
        return ProviderIdentity(
            issuer=claims["iss"],
            subject=claims["sub"],
            preferred_username=username if isinstance(username, str) else None,
            email=email if isinstance(email, str) else None,
            groups=groups,
        )

    def _client_secret(self) -> str:
        """Read fresh on every exchange, the way the ui token is, so a rotated
        secret file is live on the next sign-in without a restart."""
        try:
            secret = self.secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ClientSecretUnavailable(
                "this Chronicle's OIDC client secret file is missing or unreadable",
                detail=f"{self.secret_path}: {exc}",
            ) from exc
        if not secret:
            raise ClientSecretUnavailable(
                "this Chronicle's OIDC client secret file is empty", detail=str(self.secret_path)
            )
        return secret

    def _exchange(
        self, code: str, attempt: LoginAttempt, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        body = prepare_token_request(
            "authorization_code",
            code=code,
            redirect_uri=self.settings.redirect_uri,
            code_verifier=attempt.code_verifier,
        )
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        supported = metadata.get("token_endpoint_auth_methods_supported")
        method = "client_secret_basic"
        if (
            isinstance(supported, list)
            and method not in supported
            and "client_secret_post" in supported
        ):
            method = "client_secret_post"
        url, headers, body = ClientAuth(
            self.settings.client_id, self._client_secret(), method
        ).prepare("POST", metadata["token_endpoint"], headers, body)
        try:
            with self._http() as http:
                response = http.post(url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnreachable(
                "the identity provider could not be reached to exchange the code",
                detail=f"POST {url}: {exc}",
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderRejected(
                "the identity provider's token response is not JSON",
                detail=f"status {response.status_code}",
            ) from exc
        if not isinstance(payload, dict):
            raise ProviderRejected("the identity provider's token response is not an object")
        if response.status_code != 200 or "error" in payload:
            error = payload.get("error") or f"HTTP {response.status_code}"
            raise ProviderRejected(
                f"the identity provider refused the code exchange ({error})",
                detail=str(payload.get("error_description") or ""),
            )
        if not isinstance(payload.get("id_token"), str) or not payload["id_token"]:
            raise ProviderRejected("the identity provider's token response carries no id token")
        return payload

    def _verify_id_token(
        self, token: dict[str, Any], attempt: LoginAttempt, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        advertised = metadata.get("id_token_signing_alg_values_supported")
        candidates = advertised if isinstance(advertised, list) else list(ALLOWED_ID_TOKEN_ALGS)
        algorithms = [alg for alg in candidates if alg in ALLOWED_ID_TOKEN_ALGS]
        if not algorithms:
            raise ProviderRejected(
                "the identity provider signs id tokens with no algorithm this Chronicle accepts",
                detail=f"advertised {advertised!r}",
            )
        registry = JWSRegistry(algorithms=algorithms, strict_check_header=False)
        try:
            try:
                decoded = jwt.decode(token["id_token"], self._key_set(), registry=registry)
            except InvalidKeyIdError:
                # A rotated key: fetch the set once more before giving up.
                decoded = jwt.decode(
                    token["id_token"], self._key_set(force=True), registry=registry
                )
        except JoseError as exc:
            raise SignInInvalid(
                "the identity provider's id token did not verify",
                detail=f"{exc.error}: {exc.description}",
            ) from exc
        options = {
            "iss": {"essential": True, "value": self.settings.issuer},
            "aud": {"essential": True, "value": self.settings.client_id},
        }
        params: dict[str, Any] = {"nonce": attempt.nonce, "client_id": self.settings.client_id}
        if isinstance(token.get("access_token"), str):
            params["access_token"] = token["access_token"]
        claims = CodeIDToken(decoded.claims, decoded.header, options, params)
        try:
            claims.validate(now=int(self._now()), leeway=CLAIM_LEEWAY_SECONDS)
        except JoseError as exc:
            raise SignInInvalid(
                f"the identity provider's id token was refused ({exc.description or exc.error})",
                detail=f"{exc.error}: {exc.description}",
            ) from exc
        return dict(claims)

    def _groups(
        self, claims: dict[str, Any], token: dict[str, Any], metadata: dict[str, Any]
    ) -> tuple[str, ...]:
        name = self.settings.groups_claim
        if name in claims:
            return _as_groups(claims[name])
        userinfo_url = metadata.get("userinfo_endpoint")
        access_token = token.get("access_token")
        if not isinstance(userinfo_url, str) or not isinstance(access_token, str):
            return ()
        # The claim was not in the id token (a provider that only puts it in
        # userinfo); ask there, and only trust an answer about the same person.
        userinfo = self._get_json(
            userinfo_url, "userinfo", headers={"Authorization": f"Bearer {access_token}"}
        )
        if not isinstance(userinfo, dict) or userinfo.get("sub") != claims["sub"]:
            raise ProviderRejected(
                "the identity provider's userinfo answer was not about the signed-in person"
            )
        return _as_groups(userinfo.get(name))


@dataclass
class OidcAuth:
    """Everything the routes and the two session gates need, on `app.state.oidc`;
    None there means sign-in is not configured and nothing changes."""

    settings: OidcSettings
    client: OidcClient
    sessions: OidcSessions

    @classmethod
    def build(
        cls,
        data_dir: Path,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
    ) -> OidcAuth | None:
        if settings.oidc is None:
            return None
        client = OidcClient(settings.oidc, data_dir, transport=transport)
        if not client.secret_path.is_file():
            log.error(
                "oidc: client secret file %s does not exist; sign-in will fail until it does"
                " (the admin password still works)",
                client.secret_path,
            )
        state_dir = data_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        return cls(settings=settings.oidc, client=client, sessions=OidcSessions(state_dir))

    def principal(self, request: Request) -> Principal | None:
        return self.sessions.read(request.cookies.get(SESSION_COOKIE_NAME))


def get_oidc(request: Request) -> OidcAuth | None:
    found = getattr(request.app.state, "oidc", None)
    return found if isinstance(found, OidcAuth) else None


def current_principal(request: Request) -> Principal | None:
    """The signed-in person, or None: no OIDC, no cookie, or a stale one."""
    auth = get_oidc(request)
    return auth.principal(request) if auth is not None else None
