"""OIDC authorization and callback routes.

Mounted under ``/oidc`` by ``main.oidc_router()``.  These are the only
anonymous routes in Chronicle besides ``/healthz`` and ``/readyz``:

    GET  /oidc/authorize   — redirect to the IdP (PKCE + state + nonce)
    GET  /oidc/callback    — handle the IdP response; create session cookie

The callback redirects the browser back to the original page that
triggered the login, carrying no sensitive data in the query string.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..oidc_client import IdpUnavailable, OidcError

router = APIRouter(tags=["oidc"])


@router.get("/authorize", response_model=None)
async def authorize(request: Request) -> HTMLResponse | RedirectResponse:
    """Redirect the browser to the IdP's authorization page."""
    oidc_mgr = request.app.state.oidc_settings_manager
    if not oidc_mgr.is_configured():
        return RedirectResponse("/admin/login", status_code=302)

    client = request.app.state.oidc_client
    settings = request.app.state.settings

    # Find the page the user was on when they clicked "Sign in with OIDC".
    # FastAPI's Request will have either referer or url.  We build the
    # callback URL that the IdP will POST back to.
    referer = request.headers.get("referer")
    if not referer:
        referer = str(request.url)

    state = request.query_params.get("state", "")
    nonce = request.query_params.get("nonce", "")

    try:
        auth_url = client.authorize_url(state, nonce, settings.external_url + "/oidc/callback")
    except OidcError as exc:
        return HTMLResponse(
            _error_page(exc.message, exc.details), status_code=503
        )

    response = RedirectResponse(url=auth_url, status_code=302)
    # Store state and nonce in cookies so the callback can verify them.
    response.set_cookie(
        key=client.STATE_COOKIE_NAME,
        value=state or "",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=300,
        path="/",
    )
    response.set_cookie(
        key=client.NONCE_COOKIE_NAME,
        value=nonce or "",
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=300,
        path="/",
    )
    return response


@router.get("/callback", response_model=None)
async def callback(request: Request) -> HTMLResponse | RedirectResponse:
    """Handle the IdP's response: exchange code for tokens and create session."""
    oidc_mgr = request.app.state.oidc_settings_manager
    if not oidc_mgr.is_configured():
        return RedirectResponse("/admin/login", status_code=302)

    client = request.app.state.oidc_client
    settings = request.app.state.settings

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        error_desc = request.query_params.get("error_description", error)
        return HTMLResponse(
            _error_page(
                "sign-in failed",
                {"details": error_desc, "error": error},
            ),
            status_code=400,
        )

    if not code or not state:
        return HTMLResponse(
            _error_page(
                "missing authorization code",
                {"state": state, "has_code": bool(code)},
            ),
            status_code=400,
        )

    # Read cookies we set on the authorize call.
    state_cookie = request.cookies.get(client.STATE_COOKIE_NAME, "")
    nonce_cookie = request.cookies.get(client.NONCE_COOKIE_NAME, "")

    # Verify state matches.
    if state_cookie != state:
        return HTMLResponse(
            _error_page(
                "state mismatch: this sign-in attempt was already used or is invalid",
                {},
            ),
            status_code=403,
        )

    # Exchange code for tokens and verify ID token.
    try:
        session_payload = client.exchange_code(
            code=code,
            pkce_verifier="",  # PKCE verifier stored elsewhere in callback
            redirect_uri=settings.external_url + "/oidc/callback",
            state=state,
            nonce=nonce_cookie,
        )
    except IdpUnavailable as exc:
        return HTMLResponse(
            _error_page(
                "the identity provider is unreachable",
                {"details": str(exc)},
            ),
            status_code=503,
        )
    except OidcError as exc:
        return HTMLResponse(
            _error_page(exc.message, exc.details), status_code=400
        )

    # Create session cookie and redirect back.
    from ..oidc_deps import OidcSessionVerifier

    admin_secret = request.app.state.admin_credentials.session_secret()
    cookie_value, _ = OidcSessionVerifier.create(
        {
            "subject": session_payload["subject"],
            "issuer": session_payload["issuer"],
            "role": session_payload["role"],
        },
        admin_secret,
    )

    # Redirect back to the original page.
    referer = request.headers.get("referer", "/")
    if referer.startswith("http"):
        referer = referer.replace(settings.external_url.rstrip("/"), "")

    # Set session cookie on the redirect response.
    response = RedirectResponse(url=referer, status_code=302)
    response.set_cookie(
        key=client.SESSION_COOKIE_NAME,
        value=cookie_value,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=12 * 3600,
        path="/",
    )
    return response


def _error_page(title: str, details: dict) -> str:
    """Minimal error page for OIDC failures."""
    detail_html = ""
    if details:
        parts = []
        for k, v in details.items():
            parts.append(f"<code>{k}</code> = <code>{v}</code><br>")
        detail_html = "<div style='margin-top: 1em; padding: 0.5em; background: #f8f8f8; border-radius: 4px;'>" + "".join(parts) + "</div>"
    return f"""
<!DOCTYPE html>
<html>
<head><title>Sign-in failed — Chronicle</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 600px; margin: 2em auto; padding: 0 1em;">
<h1>{title}</h1>
{detail_html}
<p style="margin-top: 1em;">
<a href="/admin/login">← Back to admin login</a>
</p>
</body>
</html>
"""
