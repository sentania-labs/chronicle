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
"""

from __future__ import annotations

from fastapi import Request

from .deps import Consumer, Services, get_services
from .errors import ApiError
from .settings import Settings
from .tokens import UI_TOKEN_NAME


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
    return Consumer(token_name=UI_TOKEN_NAME)


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        settings = Settings.from_env()
        request.app.state.settings = settings
    return settings


def banner_enabled(request: Request) -> bool:
    return get_settings(request).ui_banner


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
    "banner_enabled",
    "check_same_origin",
    "get_services",
    "get_settings",
    "require_ui_consumer",
]
