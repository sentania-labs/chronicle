"""Ties `digest.py`'s pure clone/parse to the store, admin state, and CLI.

Two ways in: `chronicle digest` (cli.py) and the admin "Run digest now"
button, both call `run()`. When a GitHub App and repo are configured, the
clone URL carries a short-lived installation token; when
`CHRONICLE_DIGEST_REPO_URL` names a public https URL instead, digest runs
fully anonymously, which is how CI and this round's real-world check both
exercise it without credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import digest as digest_mod
from .admin_deps import AdminServices
from .models import Post, now_stamp
from .store import Store


class DigestNotConfigured(Exception):
    pass


@dataclass
class DigestSummary:
    created: int
    updated: int
    unchanged: int
    hugo_version: str
    submodule_count: int
    started_at: str
    finished_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "hugo_version": self.hugo_version,
            "submodule_count": self.submodule_count,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _clone_url(admin: AdminServices | None) -> tuple[str, str | None, str | None]:
    """The origin URL, branch, and token to use, kept apart so the URL stays credential-free.

    `clone_or_update` injects the token per invocation (`digest.py`'s
    `_auth_env`) rather than embedding it in the URL, which is what git
    would otherwise persist into `remote.origin.url`.
    """
    if admin is not None:
        record = admin.github_store.load()
        if record is not None and record.owner_repo and record.installation_id:
            token = _installation_token(admin, record.installation_id)
            return f"https://github.com/{record.owner_repo}.git", record.default_branch, token
        settings_url = admin.settings.digest_repo_url
    else:
        settings_url = None
    if settings_url:
        return settings_url, None, None
    raise DigestNotConfigured(
        "no GitHub App repository is configured and CHRONICLE_DIGEST_REPO_URL is not set"
    )


def _installation_token(admin: AdminServices, installation_id: str) -> str:
    record = admin.github_store.load()
    assert record is not None
    app_jwt = admin.github_client.mint_app_jwt(record.app_id, admin.github_store.pem(record))
    minted = admin.github_client.mint_installation_token(app_jwt, installation_id)
    token: str = minted["token"]
    return token


def run(store: Store, actor: str, admin: AdminServices | None = None) -> DigestSummary:
    started_at = now_stamp()
    repo_url, branch, token = _clone_url(admin)
    digest_mod.clone_or_update(store.site_dir, repo_url, branch, token=token)
    discovered = digest_mod.discover_posts(store.site_dir)
    posts = [
        Post(slug=item.slug, path=item.path, title=item.title, date=item.date, sha=item.sha)
        for item in discovered
    ]
    counts = store.apply_digest(actor, posts)
    toolchain = digest_mod.parse_toolchain(store.site_dir)

    finished_at = now_stamp()
    summary = DigestSummary(
        created=counts["created"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        hugo_version=toolchain.hugo_version,
        submodule_count=len(toolchain.submodules),
        started_at=started_at,
        finished_at=finished_at,
    )
    if admin is not None:
        admin.write_toolchain(
            {"hugo_version": toolchain.hugo_version, "submodules": toolchain.submodules}
        )
        admin.write_digest_status(summary.as_dict())
    return summary
