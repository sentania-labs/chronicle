"""The two anonymous sign-in routes, and sign-out (issue 81 piece 1, ADR 027).

`/auth/oidc/start` sends the browser to the identity provider with a fresh
state, nonce and PKCE challenge sealed into a short-lived cookie;
`/auth/oidc/callback` is where the provider sends it back. Both run before any
session exists, so they are the one amendment to "only `/healthz` and
`/readyz` are anonymous" (ADR 004), and both answer 404 unless OIDC is
configured. `/auth/oidc/logout` is not a third: it requires the session it
ends. Mounted in `main.py` beside the UI router, never inside `/v1`:
nothing here can authenticate a `/v1` call, and no bearer token gets in here.

Every refusal renders `ui_templates.sign_in_error_page`, whose text is this
module's own wording plus, at most, the provider's `error` code; the
provider's free text (`error_description`, token-endpoint messages) goes to
the log, never into the page.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ui_templates as tpl
from ..errors import ApiError
from ..oidc import OidcAuth, OidcError, display_name, get_oidc
from ..oidc_session import (
    LOGIN_COOKIE_NAME,
    Principal,
    clear_login_cookie,
    clear_session_cookie,
    set_login_cookie,
    set_session_cookie,
)
from ..ui_deps import check_same_origin, get_settings, require_ui_session

log = logging.getLogger("chronicle.api.oidc")

router = APIRouter(prefix="/auth/oidc", tags=["auth"])

DEFAULT_NEXT = "/content/drafts"
# Only a sanitised error code from the provider is ever echoed to the page.
_ERROR_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz_")


def require_oidc(request: Request) -> OidcAuth:
    auth = get_oidc(request)
    if auth is None:
        raise ApiError(404, "oidc_not_configured", "browser sign-in is not configured here")
    return auth


def _cookie_secure(request: Request) -> bool:
    return get_settings(request).cookie_secure or request.url.scheme == "https"


def _safe_next(value: str | None) -> str:
    """Where to land after sign-in: a path on this host, or the board. Never
    an absolute URL or a protocol-relative one, so the callback cannot be
    turned into an open redirect."""
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and value.isprintable()
    ):
        return value
    return DEFAULT_NEXT


def _refusal(
    message: str, status_code: int, *, retry: bool = True, clear_attempt: bool = True
) -> HTMLResponse:
    response = HTMLResponse(tpl.sign_in_error_page(message, retry=retry), status_code=status_code)
    if clear_attempt:
        clear_login_cookie(response)
    return response


@router.get("/start")
def start(request: Request, next: str | None = None, auth: OidcAuth = Depends(require_oidc)) -> Any:
    try:
        url, attempt = auth.client.begin(_safe_next(next))
    except OidcError as exc:
        log.warning("oidc: cannot start a sign-in: %s (%s)", exc, exc.detail or "no detail")
        return _refusal(f"{exc}; try again in a moment", exc.status_code)
    response = RedirectResponse(url, status_code=303)
    set_login_cookie(response, auth.sessions.seal_login(attempt), secure=_cookie_secure(request))
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    auth: OidcAuth = Depends(require_oidc),
) -> Any:
    attempt = auth.sessions.open_login(request.cookies.get(LOGIN_COOKIE_NAME))
    if attempt is None:
        return _refusal(
            "this sign-in attempt has expired or was not started in this browser; start again",
            400,
        )
    # State first, for an error callback too: a response that does not carry
    # this attempt's state is not the provider answering it, so it neither
    # ends the attempt (the login cookie stays for the real callback) nor is
    # logged as a provider refusal. A cross-site link to the callback cannot
    # cancel someone's sign-in this way.
    if not state or not secrets.compare_digest(state, attempt.state):
        log.warning("oidc: callback state did not match the attempt that started it")
        return _refusal(
            "the sign-in response did not match the attempt that started it; start again",
            400,
            clear_attempt=False,
        )
    if error:
        description = request.query_params.get("error_description", "")
        log.warning("oidc: the provider refused the sign-in: %s %s", error, description)
        code_text = error if set(error) <= _ERROR_CODE_CHARS else "an error"
        return _refusal(f"the identity provider refused the sign-in ({code_text})", 400)
    if not code:
        log.warning("oidc: callback carried the attempt's state but no code")
        return _refusal("the sign-in response carried no authorization code; start again", 400)
    try:
        identity = auth.client.complete(code, attempt)
    except OidcError as exc:
        log.warning("oidc: sign-in failed: %s (%s)", exc, exc.detail or "no detail")
        return _refusal(str(exc), exc.status_code)
    roles = auth.settings.roles_for(identity.groups)
    if not roles:
        # Authenticated, not authorised: refused, and no session of any kind.
        log.warning(
            "oidc: sign-in refused for issuer=%s subject=%s: none of its groups %s maps to a role",
            identity.issuer,
            identity.subject,
            sorted(identity.groups),
        )
        return _refusal(
            "your account is not in any group this Chronicle maps to a role;"
            " an administrator can add you and you can sign in again",
            403,
            retry=False,
        )
    principal = Principal(
        issuer=identity.issuer,
        subject=identity.subject,
        name=display_name(identity),
        roles=roles,
    )
    # The one line that ties the name the records carry to the identity.
    log.info(
        "oidc: sign-in allowed: %s as %r with roles %s",
        principal.describe(),
        principal.name,
        sorted(roles),
    )
    response = RedirectResponse(attempt.next_path, status_code=303)
    set_session_cookie(response, auth.sessions.issue(principal), secure=_cookie_secure(request))
    clear_login_cookie(response)
    return response


@router.post("/logout")
def logout(
    auth: OidcAuth = Depends(require_oidc),
    _origin: None = Depends(check_same_origin),
    principal: Principal | None = Depends(require_ui_session),
) -> Any:
    """Ends the Chronicle session in this browser. Not a third anonymous
    route: it takes the session it ends, and the same-origin check every UI
    form carries. The session at the provider is the person's own and is
    left alone."""
    assert principal is not None  # require_oidc ran first, so the gate was live
    log.info("oidc: sign-out: %s (%r)", principal.describe(), principal.name)
    response = HTMLResponse(tpl.signed_out_page())
    clear_session_cookie(response)
    return response
