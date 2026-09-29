"""High-level OIDC settings management.

Wraps SettingsDB with validation, client-secret file management, discovery
testing, and lazy configuration loading.  Settings change at runtime without
a restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from .atomic import write_atomic
from .errors import ApiError
from .roles import validate_role
from .settings_db import SettingsDB


@dataclass(frozen=True)
class OidcConfiguration:
    """Immutable snapshot of the OIDC configuration."""

    issuer: str
    client_id: str
    client_secret_file: str
    redirect_uri: str
    scopes: list[str]
    groups_claim_name: str
    group_role_map: dict[str, str]

    @property
    def is_configured(self) -> bool:
        return bool(self.issuer)


class SettingsValidationError(Exception):
    """One or more form fields failed validation."""

    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__(json.dumps(errors))


class OidcSettingsManager:
    """Manage OIDC settings in the database and client-secret file.

    The client secret is written to a separate file (never stored in the
    database row).  The file path is ``<data_dir>/state/oidc_client_secret``
    by default, but the deployer-provided path is respected.
    """

    _DEFAULT_SECRET_FILE = "oidc_client_secret"

    def __init__(self, settings_db: SettingsDB) -> None:
        self._db = settings_db
        self._config: OidcConfiguration | None = None
        self._client_secret: str | None = None  # cached secret value for validation

    def _ensure_config(self) -> OidcConfiguration:
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _load_config(self) -> OidcConfiguration:
        """Read OIDC settings from the database."""
        rows = self._db.raw_query(
            "SELECT issuer, client_id, client_secret_file, redirect_uri, "
            "scopes, groups_claim_name FROM oidc_settings WHERE id=1"
        )
        if not rows:
            return OidcConfiguration(
                issuer="",
                client_id="",
                client_secret_file=self._DEFAULT_SECRET_FILE,
                redirect_uri="",
                scopes=[],
                groups_claim_name="groups",
                group_role_map={},
            )

        row = rows[0]
        scopes_str: str = row["scopes"]
        try:
            scopes: list[str] = json.loads(scopes_str)
        except (json.JSONDecodeError, TypeError):
            scopes = []

        group_rows = self._db.raw_query(
            "SELECT group_name, min_role FROM oidc_group_role_map"
        )
        group_role_map: dict[str, str] = {}
        for gr in group_rows:
            group_role_map[gr["group_name"]] = gr["min_role"]

        return OidcConfiguration(
            issuer=row["issuer"],
            client_id=row["client_id"],
            client_secret_file=row["client_secret_file"],
            redirect_uri=row["redirect_uri"],
            scopes=scopes,
            groups_claim_name=row["groups_claim_name"],
            group_role_map=group_role_map,
        )

    def is_configured(self) -> bool:
        return self._ensure_config().is_configured

    @property
    def issuer(self) -> str:
        return self._ensure_config().issuer

    @property
    def client_id(self) -> str:
        return self._ensure_config().client_id

    def get_client_secret(self) -> str:
        """Return the current client secret value.

        Read from the file on every call to stay fresh across processes.
        """
        return self._client_secret_file_read()

    def _client_secret_file_read(self) -> str:
        path = Path(self._ensure_config().client_secret_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        return ""

    @property
    def redirect_uri(self) -> str:
        return self._ensure_config().redirect_uri

    @property
    def scopes(self) -> list[str]:
        return self._ensure_config().scopes

    @property
    def groups_claim_name(self) -> str:
        return self._ensure_config().groups_claim_name

    @property
    def group_role_map(self) -> dict[str, str]:
        return dict(self._ensure_config().group_role_map)

    # -- mutation --

    def save(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        groups_claim_name: str,
        group_role_map: list[tuple[str, str]],
    ) -> None:
        """Validate and persist the full OIDC configuration.

        Raises ``SettingsValidationError`` on any issue.
        """
        # ── validate ──────────────────────────────────────────────
        errors: dict[str, str] = {}

        if not issuer or not issuer.strip().startswith(("http://", "https://")):
            errors["issuer"] = "must be a URL starting with http:// or https://"
        if not client_id or len(client_id) < 4:
            errors["client_id"] = "must be at least 4 characters"
        if not client_secret:
            errors["client_secret"] = "required when configuring OIDC"
        if not redirect_uri or not redirect_uri.strip().startswith(("http://", "https://")):
            errors["redirect_uri"] = "must be a URL starting with http:// or https://"
        if not scopes or not all(s.strip() for s in scopes):
            errors["scopes"] = "must list at least one scope"
        if not groups_claim_name or not groups_claim_name.strip():
            errors["groups_claim_name"] = "required"
        if not group_role_map:
            errors["group_role_map"] = "must list at least one group-to-role mapping"
        else:
            seen_groups: set[str] = set()
            for group_name, role in group_role_map:
                if not group_name or not group_name.strip():
                    errors["group_role_map"] = "every row must have a group name"
                    break
                if group_name in seen_groups:
                    errors["group_role_map"] = f"duplicate group name: {group_name}"
                    break
                seen_groups.add(group_name)
                try:
                    validate_role(role)
                except ValueError:
                    errors["group_role_map"] = f"invalid role: {role}"
                    break

        if errors:
            raise SettingsValidationError(errors)

        # ── write client-secret file ──────────────────────────────
        secret_path = Path(self._ensure_config().client_secret_file)
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(secret_path, client_secret + "\n", mode=0o600)

        # ── upsert database rows ──────────────────────────────────
        scopes_json = json.dumps(scopes)
        self._db.raw_execute(
            """INSERT OR REPLACE INTO oidc_settings
               (id, issuer, client_id, client_secret_file, redirect_uri,
                scopes, groups_claim_name)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (
                issuer.strip(),
                client_id.strip(),
                self._DEFAULT_SECRET_FILE,
                redirect_uri.strip(),
                scopes_json,
                groups_claim_name.strip(),
            ),
        )

        # Replace all group-role mappings.
        self._db.raw_execute("DELETE FROM oidc_group_role_map")
        for group_name, role in group_role_map:
            self._db.raw_execute(
                "INSERT INTO oidc_group_role_map (group_name, min_role) VALUES (?, ?)",
                (group_name.strip(), role),
            )

        # Invalidate cached config so next read re-fetches.
        self._config = None
        self._client_secret = None

    def test_discovery(self) -> dict:
        """Probe the issuer's discovery endpoint and return the result.

        Raises ``ApiError`` if the probe fails.
        """
        issuer = self.issuer
        if not issuer:
            raise ApiError(400, "oidc_not_configured", "OIDC issuer is not set")
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            return {"status": "ok", "data": data}
        except httpx.HTTPStatusError as exc:
            raise ApiError(
                502,
                "oidc_discovery_failed",
                f"discovery endpoint returned {exc.response.status_code}",
            )
        except httpx.RequestError as exc:
            raise ApiError(
                502,
                "oidc_discovery_failed",
                f"could not reach {url}: {exc}",
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise ApiError(
                502,
                "oidc_discovery_failed",
                f"discovery endpoint returned invalid JSON: {exc}",
            )

    def reload(self) -> OidcConfiguration:
        """Force a re-read from the database and return the new config."""
        self._config = self._load_config()
        return self._config
