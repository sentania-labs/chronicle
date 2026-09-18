"""Publish and unpublish runs: the git data API and pull requests.

Spec section 9. Runs in the api process alongside the watcher (ADR 013): it
claims `publish` and `unpublish` queue entries, which the builder's own
`claim_next` never looks at (`kind="preview"` only), builds one commit
through the git data API, opens or updates a PR, and records the result on
the run and on the draft (`Store.record_publish_result`). Merge itself is
the watcher's job (`watcher.py`), not this module's.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import convert, lint
from . import digest as digest_mod
from .admin_deps import AdminServices, InstallationToken
from .github_client import (
    AppRepoOps,
    GitHubApiError,
    GitHubRepoOps,
    TestRepoOps,
    parse_owner_repo,
)
from .models import Draft, Run, WatchEntry, now_stamp
from .store import Store

log = logging.getLogger("chronicle.api.publisher")

PUBLISHER_ACTOR = "chronicle-publisher"
# Spec section 5: "First publish stamps today in America/Chicago", regardless
# of what timezone the container itself runs in.
PUBLISH_TZ = ZoneInfo("America/Chicago")
HEARTBEAT_PATH = ("state", "publisher", "heartbeat.json")


class PublishFailed(Exception):
    def __init__(self, error_class: str, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def stamp_publish_date() -> str:
    return datetime.now(tz=PUBLISH_TZ).isoformat(timespec="seconds")


def _installation_token(admin: AdminServices, installation_id: str) -> str:
    """Mint-and-cache an installation token; duplicated from routes/admin.py
    and digest_runner.py on purpose (existing pattern: each caller of the
    App JWT flow keeps its own copy rather than share a private route
    helper across modules)."""
    cached = admin.cached_installation_token(installation_id)
    if cached is not None:
        expires = datetime.fromisoformat(cached.expires_at)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires > datetime.now(tz=UTC):
            return cached.token
    record = admin.github_store.load()
    assert record is not None
    app_jwt = admin.github_client.mint_app_jwt(record.app_id, admin.github_store.pem(record))
    minted = admin.github_client.mint_installation_token(app_jwt, installation_id)
    admin.cache_installation_token(
        installation_id, InstallationToken(token=minted["token"], expires_at=minted["expires_at"])
    )
    return str(minted["token"])


@dataclass(frozen=True)
class RepoTarget:
    ops: GitHubRepoOps
    owner: str
    repo: str
    default_branch: str
    # A raw bearer token, for the one caller that is not a GitHubRepoOps
    # method: `digest.clone_or_update`'s git-over-https auth (watcher and
    # reconcile both fetch the default branch after a merge, spec section 9
    # and 12), which needs a token string, not a JSON API call.
    token_provider: Any = None

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}.git"


def build_repo_target(admin: AdminServices) -> RepoTarget | None:
    """The repo-scoped ops client and default branch, or None if nothing
    is configured yet (no GitHub App installed, no test-token mode)."""
    settings = admin.settings
    if settings.test_token_mode:
        if not settings.github_test_repo:
            raise PublishFailed(
                "test_token_misconfigured",
                "CHRONICLE_GITHUB_TEST_TOKEN is set but CHRONICLE_GITHUB_TEST_REPO is not",
            )
        owner, repo = parse_owner_repo(settings.github_test_repo)
        token = settings.github_test_token or ""
        test_ops: GitHubRepoOps = TestRepoOps(
            owner=owner, repo=repo, api_base=settings.github_api_base, static_token=token
        )
        return RepoTarget(
            ops=test_ops,
            owner=owner,
            repo=repo,
            default_branch="main",
            token_provider=lambda: token,
        )

    record = admin.github_store.load()
    if record is None or not record.owner_repo or not record.default_branch:
        return None
    installation_id = record.installation_id
    if not installation_id:
        return None
    owner, repo = parse_owner_repo(record.owner_repo)
    token_provider = lambda: _installation_token(admin, installation_id)  # noqa: E731
    app_ops: GitHubRepoOps = AppRepoOps(
        owner=owner, repo=repo, api_base=settings.github_api_base, token_provider=token_provider
    )
    return RepoTarget(
        ops=app_ops,
        owner=owner,
        repo=repo,
        default_branch=record.default_branch,
        token_provider=token_provider,
    )


def _pr_title(draft: Draft, kind: str) -> str:
    verb = "Publish" if kind == "publish" else "Unpublish"
    return f"{verb}: {draft.title}"


def _pr_body(
    draft: Draft, kind: str, run_id: str, preview_url: str | None, images: list[dict[str, str]]
) -> str:
    summary = str(draft.frontmatter.get("summary") or draft.frontmatter.get("description") or "")
    lines = [f"# {draft.title}", ""]
    if summary:
        lines += [summary, ""]
    if preview_url:
        lines += [f"Preview: {preview_url}", ""]
    lines.append(f"Run: {run_id}")
    if images:
        lines.append("")
        lines.append("Images:")
        lines.extend(f"- {image['url']}" for image in images)
    lines += ["", f"chronicle: {kind}"]
    return "\n".join(lines)


def _open_or_update_pr(
    store: Store,
    ops: GitHubRepoOps,
    default_branch: str,
    branch: str,
    draft: Draft,
    kind: str,
    run_id: str,
    preview_url: str | None,
    images: list[dict[str, str]],
    built_version: int | None,
) -> WatchEntry:
    """Idempotent: an already-open PR for this branch gets its body updated
    instead of a second PR being opened, whether or not this process still
    has the earlier watch record (a restart, or a first-ever publish after
    an abandoned attempt, both go through this same path)."""
    body = _pr_body(draft, kind, run_id, preview_url, images)
    open_prs = ops.list_open_pulls_by_head(branch)
    if open_prs:
        pr = open_prs[0]
        ops.update_pull_body(int(pr["number"]), body)
    else:
        pr = ops.create_pull(_pr_title(draft, kind), branch, default_branch, body)
    entry = WatchEntry(
        draft_id=draft.id,
        kind=kind,
        branch=branch,
        pr_number=int(pr["number"]),
        pr_url=str(pr["html_url"]),
        created_at=now_stamp(),
        built_version=built_version,
    )
    store.record_watch(entry, PUBLISHER_ACTOR)
    return entry


def _reset_branch(ops: GitHubRepoOps, default_branch: str, branch: str, commit_sha: str) -> None:
    existing = ops.get_ref(f"heads/{branch}")
    if existing is None:
        ops.create_ref(f"heads/{branch}", commit_sha)
    else:
        ops.update_ref(f"heads/{branch}", commit_sha, force=True)


def _base_tree_sha(ops: GitHubRepoOps, default_branch: str) -> tuple[str, str]:
    ref = ops.get_ref(f"heads/{default_branch}")
    if ref is None:
        raise PublishFailed("default_branch_missing", f"no ref heads/{default_branch} on the repo")
    base_sha = str(ref["object"]["sha"])
    commit = ops.get_commit(base_sha)
    return base_sha, str(commit["tree"]["sha"])


def _publish(
    store: Store, ops: GitHubRepoOps, default_branch: str, draft: Draft, run: Run
) -> dict[str, Any]:
    if not draft.slug:
        raise PublishFailed("no_slug", f"draft {draft.id} has no pinned slug")

    date = (
        (draft.published or {}).get("date") or draft.frontmatter.get("date") or stamp_publish_date()
    )
    stamped_frontmatter = dict(draft.frontmatter)
    stamped_frontmatter.setdefault("date", date)
    if draft.published:
        stamped_frontmatter["lastmod"] = stamp_publish_date()
    linted_body, lint_warnings = lint.lint_and_normalize_body(draft.body, slug=draft.slug)
    for warning in lint_warnings:
        log.info("run %s: lint: %s", run.id, warning)
    working_draft = draft.model_copy(
        update={"body": linted_body, "frontmatter": stamped_frontmatter}
    )
    static_dir = digest_mod.read_static_dir_from_state(store.data_dir)
    content_dir = digest_mod.read_content_dir_from_state(store.data_dir)
    converted = convert.convert(working_draft, static_dir, content_dir)

    base_sha, base_tree = _base_tree_sha(ops, default_branch)
    post_blob_sha = ops.create_blob(
        base64.b64encode(converted.text.encode("utf-8")).decode("ascii")
    )
    entries: list[dict[str, Any]] = [
        {"path": converted.post_path, "mode": "100644", "type": "blob", "sha": post_blob_sha}
    ]
    images: list[dict[str, str]] = []
    for placement in converted.images:
        data = store.image_blob(placement.image_id).read_bytes()
        blob_sha = ops.create_blob(base64.b64encode(data).decode("ascii"))
        entries.append(
            {"path": placement.site_path, "mode": "100644", "type": "blob", "sha": blob_sha}
        )
        images.append({"path": placement.site_path, "url": placement.url})

    # A republish that detached an image since the last publish must delete
    # its old blob from main, or `record_publish_result` below overwrites
    # the saved image list and a later unpublish never learns the orphan
    # needs removing (round C4 review, P2).
    prior_paths = {image["path"] for image in (draft.published or {}).get("images", [])}
    new_paths = {placement.site_path for placement in converted.images}
    for stale_path in sorted(prior_paths - new_paths):
        entries.append({"path": stale_path, "mode": "100644", "type": "blob", "sha": None})

    tree_sha = ops.create_tree(base_tree, entries)
    verb = "update" if draft.published else "publish"
    commit_sha = ops.create_commit(f"chronicle: {verb} {draft.slug}", tree_sha, [base_sha])

    branch = f"post/{draft.slug}"
    _reset_branch(ops, default_branch, branch, commit_sha)

    preview_run = store.last_run(draft.id, kind="preview")
    preview_url = None
    if preview_run is not None and preview_run.status == "succeeded":
        preview_url = (preview_run.result or {}).get("preview_url")

    watch = _open_or_update_pr(
        store,
        ops,
        default_branch,
        branch,
        draft,
        "publish",
        run.id,
        preview_url,
        images,
        run.built_version,
    )

    store.record_publish_result(
        draft.id,
        PUBLISHER_ACTOR,
        kind="publish",
        branch=branch,
        pr_number=watch.pr_number,
        pr_url=watch.pr_url,
        commit_sha=commit_sha,
        post_path=converted.post_path,
        url=converted.url,
        date=str(stamped_frontmatter["date"]),
        images=images,
        post_blob_sha=post_blob_sha,
    )
    return {
        "branch": branch,
        "pr_number": watch.pr_number,
        "pr_url": watch.pr_url,
        "commit_sha": commit_sha,
    }


def _unpublish(
    store: Store, ops: GitHubRepoOps, default_branch: str, draft: Draft, run: Run
) -> dict[str, Any]:
    published = draft.published
    if not published:
        raise PublishFailed(
            "not_published", f"draft {draft.id} has no recorded publish to unpublish"
        )
    slug = draft.slug or ""
    branch = f"post/{slug}"

    base_sha, base_tree = _base_tree_sha(ops, default_branch)
    entries: list[dict[str, Any]] = [
        {"path": published["post_path"], "mode": "100644", "type": "blob", "sha": None}
    ]
    for image in published.get("images", []):
        entries.append({"path": image["path"], "mode": "100644", "type": "blob", "sha": None})

    tree_sha = ops.create_tree(base_tree, entries)
    commit_sha = ops.create_commit(f"chronicle: unpublish {slug}", tree_sha, [base_sha])
    _reset_branch(ops, default_branch, branch, commit_sha)

    watch = _open_or_update_pr(
        store, ops, default_branch, branch, draft, "unpublish", run.id, None, [], None
    )

    store.record_publish_result(
        draft.id,
        PUBLISHER_ACTOR,
        kind="unpublish",
        branch=branch,
        pr_number=watch.pr_number,
        pr_url=watch.pr_url,
        commit_sha=commit_sha,
        post_path=published["post_path"],
        url=published["url"],
        date=str(published["date"]),
        images=[],
        post_blob_sha="",
    )
    return {
        "branch": branch,
        "pr_number": watch.pr_number,
        "pr_url": watch.pr_url,
        "commit_sha": commit_sha,
    }


def recover_stuck_runs(store: Store) -> int:
    """Put back on the queue any publish/unpublish run a crashed api left mid-flight.

    No lease directory the way the builder has one (ADR 013): the
    publisher is the only writer of `publish`/`unpublish` claim state,
    since this round assumes a single api replica, so "was anyone
    building this when the process died" reduces to "is it still marked
    `building`" with no second signal to cross-check.
    """
    recovered = 0
    for kind in ("publish", "unpublish"):
        for run in store.runs_in_flight(kind):
            store.requeue_run(run.id, PUBLISHER_ACTOR, "publisher restarted mid-run")
            recovered += 1
    return recovered


def claim_next(store: Store) -> Run | None:
    for entry in store.queued_entries():
        if entry.get("kind") not in ("publish", "unpublish"):
            continue
        run = store.get_run(str(entry["run_id"]))
        if run.status == "queued":
            return run
    return None


def run_one(store: Store, target: RepoTarget, run: Run) -> Run:
    draft = store.get_draft(run.draft_id)
    run = store.start_run(
        run.id,
        PUBLISHER_ACTOR,
        hugo_version="",
        toolchain_drift=False,
        built_version=draft.version_no,
    )
    try:
        if run.kind == "publish":
            result = _publish(store, target.ops, target.default_branch, draft, run)
        elif run.kind == "unpublish":
            result = _unpublish(store, target.ops, target.default_branch, draft, run)
        else:  # pragma: no cover - claim_next never hands back another kind
            raise PublishFailed("unknown_run_kind", run.kind)
    except GitHubApiError as exc:
        log.warning("run %s: GitHub call failed: %s", run.id, exc.error_class)
        return store.finish_run(
            run.id, PUBLISHER_ACTOR, succeeded=False, result={"error_class": exc.error_class}
        )[0]
    except (PublishFailed, convert.ConversionError) as exc:
        error_class = exc.error_class if isinstance(exc, PublishFailed) else "conversion_failed"
        log.warning("run %s: %s: %s", run.id, error_class, exc)
        return store.finish_run(
            run.id, PUBLISHER_ACTOR, succeeded=False, result={"error_class": error_class}
        )[0]
    return store.finish_run(run.id, PUBLISHER_ACTOR, succeeded=True, result=result)[0]


def tick(store: Store, admin: AdminServices) -> bool:
    """One claimed run, built and recorded; False when the queue is empty
    or nothing is configured to publish against yet."""
    run = claim_next(store)
    if run is None:
        return False
    target = build_repo_target(admin)
    if target is None:
        log.info("run %s: no GitHub App or test-token repo configured yet, leaving queued", run.id)
        return False
    run_one(store, target, run)
    return True


def write_heartbeat(store: Store) -> None:
    path = store.data_dir.joinpath(*HEARTBEAT_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_loop_at": now_stamp(),
        "queue_depth": sum(
            1 for e in store.queued_entries() if e.get("kind") in ("publish", "unpublish")
        ),
    }
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def run_loop(
    store: Store, admin: AdminServices, poll_seconds: float, stop_event: threading.Event
) -> None:
    while not stop_event.is_set():
        try:
            write_heartbeat(store)
            claimed = tick(store, admin)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the publisher thread
            log.exception("publisher tick failed")
            claimed = False
        if not claimed:
            stop_event.wait(poll_seconds)
