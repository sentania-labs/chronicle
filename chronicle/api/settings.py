"""Environment-driven settings, collected once instead of read ad hoc.

C0 and C1 read a handful of `CHRONICLE_*` variables inline, next to their one
call site (`DATA_DIR_ENV` in `main.py`, `PREVIEW_DIR_ENV` in `preview/main.py`).
C2 adds enough settings that all feed the same admin bootstrap flow (the
external URL and the manifest redirect it builds, the cookie security flag,
the digest source, the builder's pinned Hugo version, and a GitHub API base
tests can override) that collecting them in one place is worth the small new
pattern. Nothing here is secret; secrets live in `credentials.py` behind the
instance key. The OIDC settings (ADR 027) follow the same rule: the client
secret is named by a file path here and read from that file under the data
directory, never carried as a value.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("chronicle.api.settings")

EXTERNAL_URL_ENV = "CHRONICLE_EXTERNAL_URL"
COOKIE_SECURE_ENV = "CHRONICLE_COOKIE_SECURE"
DIGEST_REPO_URL_ENV = "CHRONICLE_DIGEST_REPO_URL"
BUILDER_HUGO_VERSION_ENV = "CHRONICLE_BUILDER_HUGO_VERSION"
GITHUB_API_BASE_ENV = "CHRONICLE_GITHUB_API_BASE"
GITHUB_WEB_BASE_ENV = "CHRONICLE_GITHUB_WEB_BASE"
APP_NAME_PREFIX_ENV = "CHRONICLE_GITHUB_APP_NAME"
# ADR 012: test-only, never mentioned in examples/k8s/.
GITHUB_TEST_TOKEN_ENV = "CHRONICLE_GITHUB_TEST_TOKEN"
ALLOW_TEST_TOKEN_ENV = "CHRONICLE_ALLOW_TEST_TOKEN"
GITHUB_TEST_REPO_ENV = "CHRONICLE_GITHUB_TEST_REPO"
# ADR 013.
PUBLISH_POLL_SECONDS_ENV = "CHRONICLE_PUBLISH_POLL_SECONDS"
PUBLISH_QUEUE_TIMEOUT_SECONDS_ENV = "CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS"
WATCH_POLL_SECONDS_ENV = "CHRONICLE_WATCH_POLL_SECONDS"
WATCH_POLL_MAX_SECONDS_ENV = "CHRONICLE_WATCH_POLL_MAX_SECONDS"
RECONCILE_INTERVAL_SECONDS_ENV = "CHRONICLE_RECONCILE_INTERVAL_SECONDS"
# C5, ADR 014: on by default, content and preview carry no login of their own.
UI_BANNER_ENV = "CHRONICLE_UI_BANNER"
# Zone every UI-rendered timestamp is shown in; read at render time by
# `ui_time.py`, not carried on `Settings`, so templates need no extra argument.
UI_TIMEZONE_ENV = "CHRONICLE_UI_TIMEZONE"
DEFAULT_UI_TIMEZONE = "America/Chicago"
# Issue 81 piece 1, ADR 027: browser sign-in through OpenID Connect. All unset
# means the content UI stays exactly as ADR 014 left it. Setting any of them
# means all the required ones must be set, or the api refuses to start.
OIDC_ISSUER_ENV = "CHRONICLE_OIDC_ISSUER"
OIDC_CLIENT_ID_ENV = "CHRONICLE_OIDC_CLIENT_ID"
# A path to a file holding the client secret, under the data directory. The
# secret itself is never an environment value.
OIDC_CLIENT_SECRET_FILE_ENV = "CHRONICLE_OIDC_CLIENT_SECRET_FILE"
OIDC_REDIRECT_URI_ENV = "CHRONICLE_OIDC_REDIRECT_URI"
OIDC_SCOPES_ENV = "CHRONICLE_OIDC_SCOPES"
OIDC_GROUPS_CLAIM_ENV = "CHRONICLE_OIDC_GROUPS_CLAIM"
# `group=role` pairs separated by commas, or a JSON object of the same. No
# group name is ever a default here: a deployer names theirs.
OIDC_GROUP_ROLES_ENV = "CHRONICLE_OIDC_GROUP_ROLES"
OIDC_ENV_NAMES = (
    OIDC_ISSUER_ENV,
    OIDC_CLIENT_ID_ENV,
    OIDC_CLIENT_SECRET_FILE_ENV,
    OIDC_REDIRECT_URI_ENV,
    OIDC_SCOPES_ENV,
    OIDC_GROUPS_CLAIM_ENV,
    OIDC_GROUP_ROLES_ENV,
)
# Relative to the data directory; an absolute value must still resolve
# under it (`OidcSettings.client_secret_path`).
DEFAULT_OIDC_CLIENT_SECRET_FILE = "state/oidc-client-secret"
DEFAULT_OIDC_SCOPES = ("openid", "profile", "email")
DEFAULT_OIDC_GROUPS_CLAIM = "groups"
# The second of ADR 027's two anonymous routes; the default redirect URI is
# the external URL plus this path.
OIDC_CALLBACK_PATH = "/auth/oidc/callback"
# The roles a mapped group can grant. `admin` opens /admin; any role opens
# the content UI. Pieces 2 and 3 of issue 81 (personal and service tokens)
# will key off the same names.
ROLE_ADMIN = "admin"
ROLE_EDITOR = "editor"
OIDC_ROLES = frozenset({ROLE_ADMIN, ROLE_EDITOR})

DEFAULT_EXTERNAL_URL = "http://localhost:8080"
DEFAULT_GITHUB_API_BASE = "https://api.github.com"
DEFAULT_GITHUB_WEB_BASE = "https://github.com"
DEFAULT_APP_NAME_PREFIX = "chronicle"
UNKNOWN_HUGO_VERSION = "unknown"
DEFAULT_PUBLISH_POLL_SECONDS = 5.0
DEFAULT_PUBLISH_QUEUE_TIMEOUT_SECONDS = 900.0
DEFAULT_WATCH_POLL_SECONDS = 60.0
DEFAULT_WATCH_POLL_MAX_SECONDS = 900.0
DEFAULT_RECONCILE_INTERVAL_SECONDS = 3600.0
# A timeout shorter than this multiple of the poll interval cannot be
# claimed by a healthy publisher even in the best case, so it is
# indistinguishable from the failure it exists to report (finding, round C6
# adversarial review). The floor is enforced, not just documented.
PUBLISH_QUEUE_TIMEOUT_FLOOR_MULTIPLE = 3.0


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _effective_publish_queue_timeout_seconds(poll_seconds: float, configured: float) -> float:
    """Never let the timeout be shorter than a healthy publisher can respond in.

    A run cannot be claimed faster than one poll, so a timeout below
    `PUBLISH_QUEUE_TIMEOUT_FLOOR_MULTIPLE` polls fails a run before the
    publisher could ever reach it, which looks exactly like the "nothing is
    configured" case the timeout exists to report and is not one.
    """
    floor = poll_seconds * PUBLISH_QUEUE_TIMEOUT_FLOOR_MULTIPLE
    if configured >= floor:
        return configured
    log.warning(
        "%s=%.0f is shorter than %.0fx the publish poll interval (%s=%.0f); using %.0f instead",
        PUBLISH_QUEUE_TIMEOUT_SECONDS_ENV,
        configured,
        PUBLISH_QUEUE_TIMEOUT_FLOOR_MULTIPLE,
        PUBLISH_POLL_SECONDS_ENV,
        poll_seconds,
        floor,
    )
    return floor


class OidcMisconfigured(Exception):
    """A `CHRONICLE_OIDC_*` value is set but the set as a whole is unusable.

    A startup refusal, like `TestTokenNotAllowed`: an issuer with no client
    id, a group mapped to a role that does not exist, a secret file outside
    the data directory. Starting anyway would either leave the content UI
    wide open while the operator believes it is behind sign-in, or lock
    everyone out at the callback with an error nobody reads until then.
    """


@dataclass(frozen=True)
class OidcSettings:
    """What a deployer provides to turn browser sign-in on (ADR 027)."""

    issuer: str
    client_id: str
    client_secret_file: str
    redirect_uri: str
    scopes: tuple[str, ...]
    groups_claim: str
    group_roles: Mapping[str, str]

    def roles_for(self, groups: Iterable[str]) -> frozenset[str]:
        """The roles a person's groups grant: the union over every mapped
        group, matched exactly (case and all). An empty set is a refusal."""
        return frozenset(self.group_roles[group] for group in groups if group in self.group_roles)

    def client_secret_path(self, data_dir: Path) -> Path:
        """Where the client secret is read from, which must be under `data_dir`.

        Relative values are joined onto the data directory; an absolute one is
        allowed only when it still resolves inside it, so the secret can be a
        mounted file but never a stray path on the image's own filesystem.
        """
        configured = Path(self.client_secret_file)
        path = configured if configured.is_absolute() else data_dir / configured
        resolved = path.resolve()
        root = data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise OidcMisconfigured(
                f"{OIDC_CLIENT_SECRET_FILE_ENV}={self.client_secret_file} resolves to {resolved},"
                f" which is not under the data directory {root}"
            )
        return path


def _parse_group_roles(raw: str) -> dict[str, str]:
    text = raw.strip()
    pairs: list[tuple[str, str]] = []
    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OidcMisconfigured(f"{OIDC_GROUP_ROLES_ENV} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict) or not all(
            isinstance(group, str) and isinstance(role, str) for group, role in loaded.items()
        ):
            raise OidcMisconfigured(
                f"{OIDC_GROUP_ROLES_ENV} must be a JSON object of group name to role name"
            )
        pairs = [(group.strip(), role.strip()) for group, role in loaded.items()]
    else:
        for item in text.split(","):
            if not item.strip():
                continue
            # `rpartition`: a group name may itself contain `=`; the role
            # never does.
            group, separator, role = item.rpartition("=")
            if not separator or not group.strip() or not role.strip():
                raise OidcMisconfigured(
                    f"{OIDC_GROUP_ROLES_ENV} entries are group=role pairs; got {item.strip()!r}"
                )
            pairs.append((group.strip(), role.strip()))
    mapping: dict[str, str] = {}
    for group, role in pairs:
        if role not in OIDC_ROLES:
            raise OidcMisconfigured(
                f"{OIDC_GROUP_ROLES_ENV} maps group {group!r} to unknown role {role!r};"
                f" the roles are {', '.join(sorted(OIDC_ROLES))}"
            )
        mapping[group] = role
    if not mapping:
        raise OidcMisconfigured(
            f"{OIDC_GROUP_ROLES_ENV} maps no group to a role, so nobody could ever sign in"
        )
    return mapping


def _oidc_from_env(external_url: str) -> OidcSettings | None:
    values = {name: os.environ.get(name, "").strip() for name in OIDC_ENV_NAMES}
    if not any(values.values()):
        return None
    missing = [
        name
        for name in (OIDC_ISSUER_ENV, OIDC_CLIENT_ID_ENV, OIDC_GROUP_ROLES_ENV)
        if not values[name]
    ]
    if missing:
        raise OidcMisconfigured(
            "OIDC is partly configured: "
            + ", ".join(missing)
            + " must be set whenever any CHRONICLE_OIDC_* variable is"
        )
    issuer = values[OIDC_ISSUER_ENV]
    if not re.match(r"^https?://", issuer):
        raise OidcMisconfigured(
            f"{OIDC_ISSUER_ENV} must be an absolute http(s) URL, got {issuer!r}"
        )
    if issuer.startswith("http://"):
        log.warning(
            "%s uses plain http; every token the provider issues crosses it in the clear",
            OIDC_ISSUER_ENV,
        )
    scopes = tuple(dict.fromkeys(re.split(r"[,\s]+", values[OIDC_SCOPES_ENV].strip())))
    scopes = tuple(scope for scope in scopes if scope) or DEFAULT_OIDC_SCOPES
    if "openid" not in scopes:
        scopes = ("openid", *scopes)
    return OidcSettings(
        issuer=issuer,
        client_id=values[OIDC_CLIENT_ID_ENV],
        client_secret_file=values[OIDC_CLIENT_SECRET_FILE_ENV] or DEFAULT_OIDC_CLIENT_SECRET_FILE,
        redirect_uri=values[OIDC_REDIRECT_URI_ENV] or external_url + OIDC_CALLBACK_PATH,
        scopes=scopes,
        groups_claim=values[OIDC_GROUPS_CLAIM_ENV] or DEFAULT_OIDC_GROUPS_CLAIM,
        group_roles=_parse_group_roles(values[OIDC_GROUP_ROLES_ENV]),
    )


class TestTokenNotAllowed(Exception):
    """`CHRONICLE_GITHUB_TEST_TOKEN` is set without `CHRONICLE_ALLOW_TEST_TOKEN=1`.

    A token alone must never be enough to grant test-token mode (ADR 012):
    this is a startup refusal, not a silent downgrade, so a real deployment
    that picks up a leftover test token from its environment fails loudly
    instead of quietly running against a personal access token no one
    intended it to use.
    """


@dataclass(frozen=True)
class Settings:
    external_url: str
    cookie_secure: bool
    digest_repo_url: str | None
    builder_hugo_version: str
    github_api_base: str
    github_web_base: str
    app_name_prefix: str
    github_test_token: str | None
    github_test_repo: str | None
    publish_poll_seconds: float
    publish_queue_timeout_seconds: float
    watch_poll_seconds: float
    watch_poll_max_seconds: float
    reconcile_interval_seconds: float
    ui_banner: bool
    # None is the only value that leaves the content UI without a sign-in.
    oidc: OidcSettings | None

    @classmethod
    def from_env(cls) -> Settings:
        test_token = os.environ.get(GITHUB_TEST_TOKEN_ENV, "").strip() or None
        allow_test_token = _truthy(os.environ.get(ALLOW_TEST_TOKEN_ENV))
        if test_token and not allow_test_token:
            raise TestTokenNotAllowed(
                f"{GITHUB_TEST_TOKEN_ENV} is set but {ALLOW_TEST_TOKEN_ENV}=1 is not;"
                " refusing to start rather than silently run in test-token mode"
                " (see docs/decisions/012-test-token-github-mode.md)"
            )
        publish_poll_seconds = _float_env(PUBLISH_POLL_SECONDS_ENV, DEFAULT_PUBLISH_POLL_SECONDS)
        external_url = os.environ.get(EXTERNAL_URL_ENV, DEFAULT_EXTERNAL_URL).rstrip("/")
        return cls(
            external_url=external_url,
            cookie_secure=_truthy(os.environ.get(COOKIE_SECURE_ENV)),
            digest_repo_url=os.environ.get(DIGEST_REPO_URL_ENV, "").strip() or None,
            builder_hugo_version=os.environ.get(
                BUILDER_HUGO_VERSION_ENV, UNKNOWN_HUGO_VERSION
            ).strip()
            or UNKNOWN_HUGO_VERSION,
            github_api_base=os.environ.get(GITHUB_API_BASE_ENV, DEFAULT_GITHUB_API_BASE).rstrip(
                "/"
            ),
            github_web_base=os.environ.get(GITHUB_WEB_BASE_ENV, DEFAULT_GITHUB_WEB_BASE).rstrip(
                "/"
            ),
            app_name_prefix=os.environ.get(APP_NAME_PREFIX_ENV, DEFAULT_APP_NAME_PREFIX).strip()
            or DEFAULT_APP_NAME_PREFIX,
            github_test_token=test_token,
            github_test_repo=os.environ.get(GITHUB_TEST_REPO_ENV, "").strip() or None,
            publish_poll_seconds=publish_poll_seconds,
            publish_queue_timeout_seconds=_effective_publish_queue_timeout_seconds(
                publish_poll_seconds,
                _float_env(
                    PUBLISH_QUEUE_TIMEOUT_SECONDS_ENV, DEFAULT_PUBLISH_QUEUE_TIMEOUT_SECONDS
                ),
            ),
            watch_poll_seconds=_float_env(WATCH_POLL_SECONDS_ENV, DEFAULT_WATCH_POLL_SECONDS),
            watch_poll_max_seconds=_float_env(
                WATCH_POLL_MAX_SECONDS_ENV, DEFAULT_WATCH_POLL_MAX_SECONDS
            ),
            reconcile_interval_seconds=_float_env(
                RECONCILE_INTERVAL_SECONDS_ENV, DEFAULT_RECONCILE_INTERVAL_SECONDS
            ),
            ui_banner=_truthy(os.environ.get(UI_BANNER_ENV, "1")),
            oidc=_oidc_from_env(external_url),
        )

    @property
    def test_token_mode(self) -> bool:
        return bool(self.github_test_token)
