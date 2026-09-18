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
    # How many posts this run landed a working record for directly at
    # `published` (ADR 017), because nothing already tracked their slug.
    published_created: int
    hugo_version: str
    submodule_count: int
    started_at: str
    finished_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "published_created": self.published_created,
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
    settings_url: str | None = None
    fallback_token: str | None = None
    if admin is not None:
        record = admin.github_store.load()
        if record is not None and record.owner_repo and record.installation_id:
            token = _installation_token(admin, record.installation_id)
            return f"https://github.com/{record.owner_repo}.git", record.default_branch, token
        settings_url = admin.settings.digest_repo_url
        # Test-token mode (ADR 012) has no GitHub App record to mint an
        # installation token from, but CHRONICLE_DIGEST_REPO_URL can still
        # name the same private repo the test token acts against (the C4
        # live check's own shape: chronicle-target is private); passing the
        # test token here is what makes that clone succeed instead of
        # falling back to an anonymous fetch that a private repo refuses.
        fallback_token = (
            admin.settings.github_test_token if admin.settings.test_token_mode else None
        )
    if settings_url:
        return settings_url, None, fallback_token
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


def refresh_from_target(
    store: Store, target: Any, actor: str, admin: AdminServices | None = None
) -> None:
    """Fetch a repo target's default branch and apply the digest.

    Shared by the watcher (a merge just landed) and reconciliation (spec
    section 12): both already hold a `publisher.RepoTarget` from resolving
    the same GitHub App or test-token configuration `run()` above resolves
    on its own, so this skips `_clone_url` and takes the repo URL, default
    branch, and token straight from it instead of re-deriving them.

    `admin`, when given, also parses and writes toolchain state the same
    way the manual digest path does (round C4 review, P2): scheduled and
    post-merge reconciliation both call this with `admin` set, so a Hugo
    version or theme submodule change on main is reflected without waiting
    for someone to run a manual digest. The watcher's own call (right after
    observing a merge, before reconciliation runs again) omits `admin`,
    since the reconcile pass that follows the same merge covers it.
    """
    token = target.token_provider() if target.token_provider else None
    digest_mod.clone_or_update(store.site_dir, target.repo_url, target.default_branch, token=token)
    conventions = digest_mod.read_hugo_conventions(store.site_dir)
    discovered = digest_mod.discover_posts(store.site_dir, conventions)
    posts = [
        Post(slug=item.slug, path=item.path, title=item.title, date=item.date, sha=item.sha)
        for item in discovered
    ]
    store.apply_digest(actor, posts, conventions)
    if admin is not None:
        toolchain = digest_mod.parse_toolchain(store.site_dir)
        admin.write_toolchain(
            {
                "hugo_version": toolchain.hugo_version,
                "submodules": toolchain.submodules,
                "conventions": conventions.as_dict(),
            }
        )


def run(store: Store, actor: str, admin: AdminServices | None = None) -> DigestSummary:
    started_at = now_stamp()
    repo_url, branch, token = _clone_url(admin)
    digest_mod.clone_or_update(store.site_dir, repo_url, branch, token=token)
    conventions = digest_mod.read_hugo_conventions(store.site_dir)
    discovered = digest_mod.discover_posts(store.site_dir, conventions)
    posts = [
        Post(slug=item.slug, path=item.path, title=item.title, date=item.date, sha=item.sha)
        for item in discovered
    ]
    counts = store.apply_digest(actor, posts, conventions)
    toolchain = digest_mod.parse_toolchain(store.site_dir)

    finished_at = now_stamp()
    summary = DigestSummary(
        created=counts["created"],
        updated=counts["updated"],
        unchanged=counts["unchanged"],
        published_created=counts["published_created"],
        hugo_version=toolchain.hugo_version,
        submodule_count=len(toolchain.submodules),
        started_at=started_at,
        finished_at=finished_at,
    )
    if admin is not None:
        admin.write_toolchain(
            {
                "hugo_version": toolchain.hugo_version,
                "submodules": toolchain.submodules,
                "conventions": conventions.as_dict(),
            }
        )
        admin.write_digest_status(summary.as_dict())
    return summary
