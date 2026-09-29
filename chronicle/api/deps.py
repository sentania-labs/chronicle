"""What every `/v1` route needs: the services, and the consumer behind the call.

There is no anonymous path into `/v1`, reads included (spec sections 6 and
11, ADR 004).  `require_consumer` is the only door.  Token role resolution
is done here: legacy tokens map to `contributor`; the `ui` token maps to
`editor`.  Personal and service account tokens (steps 2/3) will add more
lookups to the same function.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import Header, Request

from .editor_lease import EditorLeases
from .errors import ApiError
from .store import Store
from .tokens import UI_TOKEN_NAME, TokenStore, commit_author

# Default role for legacy consumer tokens that aren't the `ui` token.
# Step 2/3 migrate these into service accounts explicitly.
LEGACY_DEFAULT_ROLE = "contributor"


@dataclass
class Consumer:
    """Who is acting: the token that authenticated, and the name it acts under.

    The `ui` token is the editor at the keyboard (ADR 004), so everything the
    domain records about it (version author, commit author, editor lease) says
    `editor`. `token_name` stays raw because authorization asks a different
    question: which token is this, not who is behind it.

    `role` is the minimum privilege this token holds (reader < contributor <
    editor < admin).  Legacy tokens are mapped here: `ui` → `editor`, everything
    else → `contributor`.  Personal and service account tokens (steps 2/3) carry
    their own role from the database.
    """

    token_name: str
    role: str = ""

    def __post_init__(self) -> None:
        if not self.role:
            self.role = LEGACY_DEFAULT_ROLE

    @property
    def name(self) -> str:
        return commit_author(self.token_name)

    @property
    def is_ui(self) -> bool:
        """Return True when this consumer has editor privileges (legacy compat).

        Kept for the domain layer's existing boolean gate.  Step 3 replaces it
        with a role check in `transitions.py`.
        """
        return self.token_name == UI_TOKEN_NAME


class Services:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store = Store.open(data_dir)
        self.tokens = TokenStore(self.store.state_dir)
        self.tokens.ensure_ui_token()
        # The editor lock (issue #64, ADR 025): runtime state, in memory.
        self.leases = EditorLeases()

    def close(self) -> None:
        self.store.close()


def get_services(request: Request) -> Services:
    services = getattr(request.app.state, "services", None)
    if services is None:
        raise ApiError(503, "data_dir_unavailable", "the data directory is not ready")
    return services


def _resolve_consumer_role(token_name: str) -> str:
    """Map a legacy token name to its role."""
    if token_name == UI_TOKEN_NAME:
        return "editor"
    return LEGACY_DEFAULT_ROLE


def require_consumer(request: Request, authorization: str = Header(default="")) -> Consumer:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "token_required", "an Authorization: Bearer *** is required")
    record = get_services(request).tokens.authenticate(token.strip())
    if record is None:
        raise ApiError(401, "token_invalid", "the bearer token is unknown or revoked")
    role = _resolve_consumer_role(record.name)
    return Consumer(token_name=record.name, role=role)
