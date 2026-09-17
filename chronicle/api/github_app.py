"""The GitHub App credential record, encrypted at rest (ADR 008, ADR 009).

`data/state/github-app.json` holds everything the manifest flow and the
installation/repo steps produce. The three genuine secrets (private key PEM,
OAuth client secret, webhook secret) are Fernet-encrypted under the instance
key before they ever reach disk; nothing else here is secret, and the whole
file is still written 0600 as defence in depth.

Only this module and `github_client.py` decrypt anything. Content routes
under `/v1` never import this module, and the admin router only ever calls
into it, never reads the file directly (spec section 11's "narrow internal
interface").
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import crypto
from .atomic import write_atomic
from .models import now_stamp

RECORD_FILE_NAME = "github-app.json"
SCHEMA_VERSION = 1


@dataclass
class GitHubAppRecord:
    app_id: str
    slug: str
    client_id: str
    html_url: str
    client_secret_enc: str
    webhook_secret_enc: str
    pem_enc: str
    created_at: str
    installation_id: str | None = None
    owner_repo: str | None = None
    default_branch: str | None = None
    last_verified_at: str | None = None
    last_error: str | None = None
    permissions: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "app_id": self.app_id,
            "slug": self.slug,
            "client_id": self.client_id,
            "html_url": self.html_url,
            "client_secret_enc": self.client_secret_enc,
            "webhook_secret_enc": self.webhook_secret_enc,
            "pem_enc": self.pem_enc,
            "created_at": self.created_at,
            "installation_id": self.installation_id,
            "owner_repo": self.owner_repo,
            "default_branch": self.default_branch,
            "last_verified_at": self.last_verified_at,
            "last_error": self.last_error,
            "permissions": self.permissions,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> GitHubAppRecord:
        return cls(
            app_id=payload["app_id"],
            slug=payload["slug"],
            client_id=payload["client_id"],
            html_url=payload["html_url"],
            client_secret_enc=payload["client_secret_enc"],
            webhook_secret_enc=payload["webhook_secret_enc"],
            pem_enc=payload["pem_enc"],
            created_at=payload["created_at"],
            installation_id=payload.get("installation_id"),
            owner_repo=payload.get("owner_repo"),
            default_branch=payload.get("default_branch"),
            last_verified_at=payload.get("last_verified_at"),
            last_error=payload.get("last_error"),
            permissions=payload.get("permissions") or {},
        )


class GitHubAppStore:
    def __init__(self, state_dir: Path, instance_key: bytes) -> None:
        self.state_dir = state_dir
        self.instance_key = instance_key
        self.path = state_dir / RECORD_FILE_NAME
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        return self.path.exists()

    def load(self) -> GitHubAppRecord | None:
        if not self.path.exists():
            return None
        return GitHubAppRecord.from_json(json.loads(self.path.read_text(encoding="utf-8")))

    def _save(self, record: GitHubAppRecord) -> None:
        write_atomic(
            self.path, json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n", mode=0o600
        )
        os.chmod(self.path, 0o600)

    def store_new_app(
        self,
        *,
        app_id: str,
        slug: str,
        client_id: str,
        client_secret: str,
        webhook_secret: str,
        pem: str,
        html_url: str,
    ) -> GitHubAppRecord:
        with self._lock:
            record = GitHubAppRecord(
                app_id=app_id,
                slug=slug,
                client_id=client_id,
                html_url=html_url,
                client_secret_enc=crypto.encrypt(self.instance_key, client_secret),
                webhook_secret_enc=crypto.encrypt(self.instance_key, webhook_secret),
                pem_enc=crypto.encrypt(self.instance_key, pem),
                created_at=now_stamp(),
            )
            self._save(record)
            return record

    def set_installation(self, installation_id: str) -> GitHubAppRecord:
        with self._lock:
            record = self._require()
            record.installation_id = installation_id
            self._save(record)
            return record

    def set_repo(self, owner_repo: str, default_branch: str) -> GitHubAppRecord:
        with self._lock:
            record = self._require()
            record.owner_repo = owner_repo
            record.default_branch = default_branch
            self._save(record)
            return record

    def record_verification(self, permissions: dict[str, str]) -> GitHubAppRecord:
        with self._lock:
            record = self._require()
            record.permissions = permissions
            record.last_verified_at = now_stamp()
            record.last_error = None
            self._save(record)
            return record

    def record_error(self, error_class: str) -> GitHubAppRecord:
        with self._lock:
            record = self._require()
            record.last_error = error_class
            self._save(record)
            return record

    def _require(self) -> GitHubAppRecord:
        record = self.load()
        if record is None:
            raise ValueError("no GitHub App is configured yet")
        return record

    # Decrypted accessors: for github_client's use only.

    def pem(self, record: GitHubAppRecord) -> str:
        return crypto.decrypt(self.instance_key, record.pem_enc)

    def client_secret(self, record: GitHubAppRecord) -> str:
        return crypto.decrypt(self.instance_key, record.client_secret_enc)

    def webhook_secret(self, record: GitHubAppRecord) -> str:
        return crypto.decrypt(self.instance_key, record.webhook_secret_enc)


def readiness_state(record: GitHubAppRecord | None) -> str:
    """The four states README and /readyz both describe."""
    if record is None:
        return "not configured"
    if record.last_error:
        return f"failing: {record.last_error}"
    if record.last_verified_at:
        return f"verified at {record.last_verified_at}"
    return "configured but unverified"
