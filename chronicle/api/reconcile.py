"""Reconciliation: main is the source of truth for published posts and drafts.

Spec section 12. Runs at startup, hourly, and after every observed merge
(the last of those is a direct call from `watcher.tick`). Flags only, never
a correction (ADR 005, AGENTS.md): every mismatch found here is persisted
under `data/repo/reconcile/` for an admin to resolve one at a time
(`Store.resolve_flag`); nothing in this module writes a `Post` or `Draft`
status on its own conclusion. The one write it does make on its own is
narrower than a correction: recording a `github`-authored version when a
published draft's content on main has moved (spec section 12's own
instruction), which changes no status and corrects nothing, it only stops
Chronicle's copy of the content from silently going stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import digest as digest_mod
from .admin_deps import AdminServices
from .digest_runner import refresh_from_target
from .models import FRONTMATTER_ALLOWLIST, Draft
from .publisher import build_repo_target
from .store import Store

log = logging.getLogger("chronicle.api.reconcile")
RECONCILE_ACTOR = "chronicle-reconcile"


class ReconcileNotConfigured(Exception):
    pass


@dataclass
class ReconcileSummary:
    flags_created: int
    checked_posts: int
    checked_drafts: int


def _already_flagged(store: Store, flag_type: str, slug: str | None, draft_id: str | None) -> bool:
    return any(
        flag.type == flag_type and flag.slug == slug and flag.draft_id == draft_id
        for flag in store.list_flags(resolved=False)
    )


def _create_if_new(
    store: Store, flag_type: str, *, slug: str | None, draft_id: str | None, detail: str, actor: str
) -> int:
    if _already_flagged(store, flag_type, slug, draft_id):
        return 0
    store.create_flag(flag_type, slug=slug, draft_id=draft_id, detail=detail, actor=actor)
    return 1


def _allowed_frontmatter(raw: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in raw.items() if key in FRONTMATTER_ALLOWLIST}


def run(store: Store, admin: AdminServices, actor: str = RECONCILE_ACTOR) -> ReconcileSummary:
    target = build_repo_target(admin)
    if target is None:
        raise ReconcileNotConfigured("no GitHub App or test-token repo is configured yet")

    refresh_from_target(store, target, actor)
    discovered = digest_mod.discover_posts(store.site_dir)
    discovered_by_slug = {item.slug: item for item in discovered}

    drafts = store.list_drafts()
    published_by_slug = {d.slug: d for d in drafts if d.status == "published" and d.slug}
    posts = store.list_posts()

    flags_created = 0

    for draft in drafts:
        if draft.status != "published" or not draft.slug:
            continue
        if draft.slug not in discovered_by_slug:
            flags_created += _create_if_new(
                store,
                "draft_published_missing_on_main",
                slug=draft.slug,
                draft_id=draft.id,
                detail=f"draft {draft.id} is published but slug {draft.slug!r} is not on main",
                actor=actor,
            )

    for post in posts:
        if post.slug in discovered_by_slug or post.slug in published_by_slug:
            continue
        flags_created += _create_if_new(
            store,
            "post_removed_without_unpublish",
            slug=post.slug,
            draft_id=None,
            detail=f"post {post.slug!r} was on main (last seen {post.date}) and is gone,"
            " with no published draft and no unpublish run",
            actor=actor,
        )

    for item in discovered:
        if item.slug in published_by_slug:
            continue
        flags_created += _create_if_new(
            store,
            "post_on_main_without_published_draft",
            slug=item.slug,
            draft_id=None,
            detail=f"post {item.slug!r} is on main at {item.path} with no published draft"
            " tracking it",
            actor=actor,
        )

    for draft in drafts:
        if draft.status != "published" or not draft.published:
            continue
        post_path = draft.published.get("post_path")
        if not post_path:
            continue
        landed = next((item for item in discovered if item.path == post_path), None)
        if landed is None or landed.slug == draft.slug:
            continue
        flags_created += _create_if_new(
            store,
            "slug_drift",
            slug=landed.slug,
            draft_id=draft.id,
            detail=f"draft {draft.id} is pinned to slug {draft.slug!r} but main now names"
            f" {post_path} with slug {landed.slug!r}",
            actor=actor,
        )

    for draft in drafts:
        flags_created += _content_drift(store, draft, discovered_by_slug, actor)

    return ReconcileSummary(
        flags_created=flags_created, checked_posts=len(posts), checked_drafts=len(drafts)
    )


def _content_drift(
    store: Store, draft: Draft, discovered_by_slug: dict[str, digest_mod.DiscoveredPost], actor: str
) -> int:
    if draft.status != "published" or not draft.published or not draft.slug:
        return 0
    known_sha = draft.published.get("post_blob_sha")
    landed = discovered_by_slug.get(draft.slug)
    if not known_sha or landed is None or landed.sha == known_sha:
        return 0
    if _already_flagged(store, "content_drift", draft.slug, draft.id):
        return 0

    source_path = store.site_dir / landed.path
    if source_path.exists():
        raw_frontmatter, body = digest_mod.parse_frontmatter(
            source_path.read_text(encoding="utf-8")
        )
        frontmatter = _allowed_frontmatter(raw_frontmatter)
        frontmatter.setdefault("title", draft.title)
        store.record_github_version(
            draft.id, frontmatter, body, "content drift: main moved since the last publish"
        )

    store.create_flag(
        "content_drift",
        slug=draft.slug,
        draft_id=draft.id,
        detail=f"draft {draft.id}'s published content differs from main"
        f" (main blob {landed.sha}, last published {known_sha})",
        actor=actor,
    )
    return 1
