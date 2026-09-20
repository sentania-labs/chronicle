"""What every `/v1` route needs: the services, and the consumer behind the call.

There is no anonymous path into `/v1`, reads included (spec sections 6 and
11, ADR 004). `require_consumer` is the only door, and it is a dependency on
the whole router rather than a per-route decision so a new route cannot
forget it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Header, Request

from .errors import ApiError
from .store import Store
from .tokens import UI_TOKEN_NAME, TokenStore, commit_author


@dataclass
class Consumer:
    """Who is acting: the token that authenticated, and the name it acts under.

    The `ui` token is the editor at the keyboard (ADR 004), so everything the
    domain records about it (version author, commit author, claim holder) says
    `editor`. `token_name` stays raw because authorization asks a different
    question: which token is this, not who is behind it.
    """

    token_name: str

    @property
    def name(self) -> str:
        return commit_author(self.token_name)

    @property
    def is_ui(self) -> bool:
        return self.token_name == UI_TOKEN_NAME


class Services:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store = Store.open(data_dir)
        self.tokens = TokenStore(self.store.state_dir)
        self.tokens.ensure_ui_token()

    def close(self) -> None:
        self.store.close()


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise ApiError(503, "data_dir_unavailable", "the data directory is not ready")
    return services


def require_consumer(request: Request, authorization: str = Header(default="")) -> Consumer:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "token_required", "an Authorization: Bearer token is required")
    record = get_services(request).tokens.authenticate(token.strip())
    if record is None:
        raise ApiError(401, "token_invalid", "the bearer token is unknown or revoked")
    return Consumer(token_name=record.name)
