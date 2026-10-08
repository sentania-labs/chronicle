"""What every UI route needs: the ui-token consumer, and a CSRF door.

ADR 014: the UI backend never holds a session and the browser never holds a
token. It authenticates its own calls into the domain layer the same way any
other consumer would, except the bearer value is read fresh from
`data/state/ui_token.txt` on every request instead of an Authorization
header, so a rotation (or a revoke on `/admin/tokens`) takes effect on the
next click, not on next restart. `Consumer(token_name=UI_TOKEN_NAME)` is the
same type `require_consumer` returns for a real bearer call, so
`consumer.name` resolves to `editor` and `consumer.is_ui` is `True` exactly as
spec section 11 requires.

ADR 027 adds a second door in front of the first: when OIDC sign-in is
configured, `require_ui_session` (on the whole UI router) demands a session
cookie, and the person it names becomes `Consumer.actor`, so the records
say who did what while the credential into the store stays the ui token.
When it is not configured, both are no-ops and nothing above changes.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from starlette.responses import RedirectResponse

from .deps import Consumer, Services, get_services
from .errors import ApiError
from .oidc import current_principal, get_oidc
from .oidc_session import Principal
from .settings import Settings
from .tokens import UI_COMMIT_AUTHOR, UI_TOKEN_NAME


class SignInRedirect(Exception):
    """Raised by `require_ui_session` for a browser navigation with no
    session: rendered as a 303 to the sign-in start (`main.py` registers
    `sign_in_redirect_handler`), the way `AdminAuthRedirect` is."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location


async def sign_in_redirect_handler(request: Request, exc: Exception) -> RedirectResponse:
    assert isinstance(exc, SignInRedirect)
    return RedirectResponse(exc.location, status_code=303)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def require_ui_session(request: Request) -> Principal | None:
    """The content UI's sign-in wall, a dependency on the whole UI router.

    No OIDC configured: returns None and changes nothing (ADR 014 as it
    stands). Configured: a valid session cookie is required. A browser
    navigation without one is sent to the sign-in start with the page it
    wanted as `next`; anything else (a fetch from `editor.js`, a script)
    gets a plain 401 in the usual error envelope.
    """
    auth = get_oidc(request)
    if auth is None:
        return None
    principal = auth.principal(request)
    if principal is not None:
        return principal
    if request.method in ("GET", "HEAD") and _wants_html(request):
        wanted = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise SignInRedirect("/auth/oidc/start?" + urlencode({"next": wanted}))
    raise ApiError(401, "session_required", "sign in to use the content UI")


def ui_actor(request: Request) -> str:
    """The name the records carry for this request: the signed-in person's,
    else `editor`, the ui token's own (ADR 014's amendment)."""
    principal = current_principal(request)
    return principal.name if principal is not None else UI_COMMIT_AUTHOR


def require_ui_consumer(request: Request) -> Consumer:
    services = get_services(request)
    path = services.tokens.ui_token_path
    if not path.exists():
        raise ApiError(503, "ui_token_unavailable", "the ui token has not been minted yet")
    plaintext = path.read_text(encoding="utf-8").strip()
    record = services.tokens.authenticate(plaintext)
    if record is None or record.name != UI_TOKEN_NAME:
        # Revoked on /admin/tokens, or the file is stale: the UI must stop
        # working the moment that happens, not keep acting as a name no
        # token backs any more.
        raise ApiError(503, "ui_token_revoked", "the ui token is revoked or unavailable")
    principal: Principal | None = None
    if get_oidc(request) is not None:
        # The router gate has already run, so this only ever fails for a
        # cookie that expired between the two checks; refuse rather than
        # fall back to writing as `editor` under a sign-in regime.
        principal = current_principal(request)
        if principal is None:
            raise ApiError(401, "session_required", "sign in to use the content UI")
    return Consumer(token_name=UI_TOKEN_NAME, actor=principal.name if principal else None)


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = Settings.from_env()
        request.app.state.settings = settings
    return settings


def banner_enabled(request: Request) -> bool:
    """The "internal-only and unauthenticated" banner: the operator's setting,
    and never shown once sign-in is configured, because then it is not true."""
    return get_settings(request).ui_banner and get_oidc(request) is None


def page_chrome(request: Request) -> dict[str, Any]:
    """The keyword arguments every UI page takes about its visitor: the banner
    flag and, under OIDC, the signed-in name for the header."""
    principal = current_principal(request)
    return {
        "banner": banner_enabled(request),
        "signed_in": principal.name if principal is not None else None,
    }


def check_same_origin(request: Request) -> None:
    """A cheap CSRF door for a surface with no session and no token in the browser.

    Content and preview carry no login (spec section 11, ADR 014): there is
    no cookie or bearer value for a third-party page to ride on, but a
    browser sitting on the same internal network can still be tricked into
    POSTing here. Same-origin on Origin (falling back to Referer) costs one
    header comparison and closes that off without adding any credential the
    banner would then have to explain away.
    """
    from urllib.parse import urlsplit

    # `request.headers.get(...)` returns None only when the header is truly
    # absent; an empty string ("Origin:" with nothing after it) is a present
    # header and must not collapse into the same "no header" branch as a
    # missing one. The original `origin or referer` fallback did exactly
    # that: an empty (or otherwise unparsable) Origin is falsy, so the `or`
    # silently substituted Referer, or the "no header at all" pass-through,
    # for a header that was actually sent and did not resolve to this host.
    # A round C5 review already fixed the literal "null" case the same way
    # ("null" is truthy, so it never took this shortcut); a follow-up review
    # found the empty/malformed case was still open. Presence and validity
    # are now checked separately: any present Origin that does not resolve
    # to this host is refused outright, and Referer is only ever consulted
    # when Origin is genuinely absent.
    origin_header = request.headers.get("origin")
    if origin_header is not None:
        if urlsplit(origin_header).netloc != request.url.netloc:
            raise ApiError(403, "cross_origin_request", "cross-origin form submissions are refused")
        return

    referer_header = request.headers.get("referer")
    if referer_header is None:
        return
    if urlsplit(referer_header).netloc != request.url.netloc:
        raise ApiError(403, "cross_origin_request", "cross-origin form submissions are refused")


__all__ = [
    "Services",
    "SignInRedirect",
    "banner_enabled",
    "check_same_origin",
    "get_services",
    "get_settings",
    "page_chrome",
    "require_ui_consumer",
    "require_ui_session",
    "sign_in_redirect_handler",
    "ui_actor",
]
