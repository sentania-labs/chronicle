"""Merge watch: poll the PRs Chronicle opened, react to merge or close.

Spec section 9. Runs in the api process alongside the publisher (ADR 013),
polling `Store.list_watches()` (backed by `data/repo/watch/`, so a restart
resumes from exactly what was open before it). No webhooks: the service has
no public endpoint (spec section 9), so this is the only way it learns a PR
it opened has moved.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import announce
from . import digest as digest_mod
from .admin_deps import AdminServices
from .digest_runner import refresh_from_target
from .github_client import GitHubApiError
from .models import WatchEntry, now_stamp
from .publisher import RepoTarget, build_repo_target
from .store import Store

log = logging.getLogger("chronicle.api.watcher")

WATCHER_ACTOR = "chronicle-watcher"
HEARTBEAT_PATH = ("state", "watcher", "heartbeat.json")


def _handle_merged(store: Store, target: RepoTarget, watch: WatchEntry) -> None:
    target.ops.delete_ref(f"heads/{watch.branch}")
    # Never deletes a Post record itself (digest's own no-delete rule,
    # AGENTS.md); the unpublish case right below is not digest, and knows
    # with certainty this specific removal is real.
    refresh_from_target(store, target, WATCHER_ACTOR)
    draft = store.get_draft(watch.draft_id)
    if watch.kind == "unpublish" and draft.slug:
        store.remove_post(draft.slug, WATCHER_ACTOR)
    # Retryable end to end (issue 60): if a step below already ran on an
    # earlier tick and a later one then failed, the watch is still open and
    # this whole handler runs again. `already_observed` still guards
    # `record_publish_behind_draft` alone, preserving the invariant its own
    # docstring (P3) relies on: it never runs once the draft already
    # carries this merge's implied status. `observe_pr_outcome` no longer
    # needs this guard (issue 60 finding 1): it is idempotent end to end on
    # its own durable markers, so it is always called, and a retry that
    # reaches it after an earlier attempt already wrote `draft.status` but
    # failed before its event, commit, or index upsert landed completes
    # exactly those missing steps instead of being skipped.
    expected_status = "published" if watch.kind == "publish" else "unpublished"
    already_observed = draft.status == expected_status
    if (
        watch.kind == "publish"
        and not already_observed
        and watch.built_version is not None
        and draft.version_no > watch.built_version
    ):
        # The draft was revised again while this publish PR was open: the
        # merged content is only what the run actually converted at
        # `built_version`, not the draft's current, newer version (round C4
        # review, P1). Still flips to `published` below (the merge is real),
        # but flagged so reconciliation surfaces the mismatch rather than
        # silently reporting the newer content as live.
        store.record_publish_behind_draft(
            draft.id,
            WATCHER_ACTOR,
            built_version=watch.built_version,
            current_version=draft.version_no,
        )
    store.observe_pr_outcome(watch.draft_id, "merged", watch.pr_number, actor=WATCHER_ACTOR)
    if watch.kind == "publish":
        _fill_announcement_links(store, watch.draft_id)
    store.clear_watch(watch.draft_id, WATCHER_ACTOR, f"PR #{watch.pr_number} merged")


def _fill_announcement_links(store: Store, draft_id: str) -> None:
    """Put the post's public link into its announcements (issue #71).

    The base is the site's own production `baseURL`, as the digest that
    `refresh_from_target` just ran read it; the path is the url the publish
    run recorded. A site with no absolute `baseURL` leaves the announcements
    alone. A failure here is logged, never raised: the merge is real and the
    watch must still clear, and the fill is only a convenience.
    """
    try:
        draft = store.get_draft(draft_id)
        post_url = (draft.published or {}).get("url")
        base_url = digest_mod.read_base_url_from_state(store.data_dir)
        if draft.status != "published" or not isinstance(post_url, str) or not base_url:
            return
        store.fill_announcement_links(
            draft_id, announce.public_link(base_url, post_url), WATCHER_ACTOR
        )
    except Exception:
        log.exception("filling the published link into draft %s announcements failed", draft_id)


def _handle_closed(store: Store, watch: WatchEntry) -> None:
    store.observe_pr_outcome(watch.draft_id, "closed", watch.pr_number, actor=WATCHER_ACTOR)
    store.clear_watch(
        watch.draft_id, WATCHER_ACTOR, f"PR #{watch.pr_number} closed without merging"
    )


def check_one(store: Store, target: RepoTarget, watch: WatchEntry) -> str:
    """Poll one watched PR; returns "merged", "closed", or "open"."""
    pr = target.ops.get_pull(watch.pr_number)
    if pr.get("merged"):
        _handle_merged(store, target, watch)
        return "merged"
    if pr.get("state") == "closed":
        _handle_closed(store, watch)
        return "closed"
    return "open"


def tick(store: Store, admin: AdminServices, reconcile_after_merge: Any = None) -> int:
    """Poll every watched PR once. Returns how many merged or closed this tick."""
    watches = store.list_watches()
    if not watches:
        return 0
    target = build_repo_target(admin)
    if target is None:
        return 0
    settled = 0
    for watch in watches:
        try:
            outcome = check_one(store, target, watch)
        except GitHubApiError as exc:
            log.warning(
                "watch draft %s PR #%d: %s", watch.draft_id, watch.pr_number, exc.error_class
            )
            continue
        if outcome != "open":
            settled += 1
            if outcome == "merged" and reconcile_after_merge is not None:
                try:
                    reconcile_after_merge()
                except Exception:  # noqa: BLE001 - a failed post-merge reconcile must not break the watcher
                    log.exception("reconciliation after merge failed")
    return settled


def write_heartbeat(store: Store, interval_seconds: float) -> None:
    path = store.data_dir.joinpath(*HEARTBEAT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_loop_at": now_stamp(),
        "watching": len(store.list_watches()),
        "poll_interval_seconds": interval_seconds,
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def run_loop(
    store: Store,
    admin: AdminServices,
    base_interval: float,
    max_interval: float,
    stop_event: threading.Event,
    reconcile_after_merge: Any = None,
) -> None:
    """Poll at `base_interval` while something is open, backing off toward
    `max_interval` (doubling each empty tick) once nothing is (spec section
    9: "poll ... at a modest interval, default 60s while a PR is open")."""
    interval = base_interval
    while not stop_event.is_set():
        write_heartbeat(store, interval)
        try:
            watches = store.list_watches()
            if watches:
                tick(store, admin, reconcile_after_merge)
                interval = base_interval
            else:
                interval = min(interval * 2, max_interval) if interval else base_interval
        except Exception:  # noqa: BLE001 - a bad tick must not kill the watcher thread
            log.exception("watcher tick failed")
        stop_event.wait(interval)
