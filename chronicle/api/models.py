"""The record shapes of spec section 4, as they are stored on disk.

Every model here round-trips through JSON in `data/repo/`: what
`model_dump(mode="json", by_alias=True)` writes is exactly the file, and the
file is the durable record. The index and the API responses are both derived
from these.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Spec section 4 says "Hugo allowlist"; which keys are in it is ADR 007,
# amended 2026-09-16 against the real blog's 347 posts and the dashboard
# port's field set.
FRONTMATTER_ALLOWLIST = (
    "title",
    "author",
    "type",
    "date",
    "lastmod",
    "draft",
    "url",
    "slug",
    "description",
    "summary",
    "categories",
    "tags",
    "series",
    "featureImage",
    "shareImage",
)

SUBMISSION_STATUSES = ("new", "claimed", "drafted", "discarded")
DRAFT_STATUSES = (
    "drafting",
    "in_review",
    "revision_requested",
    "previewed",
    "approved",
    "published",
    "unpublished",
    "rejected",
)
RUN_KINDS = ("preview", "publish", "unpublish")
# A run is queued by the api, claimed and moved to building by a builder, and
# ends succeeded or failed. `requeued` is not a status: a run a crashed
# builder left behind goes back to `queued`, which is the same state it was
# in before anyone claimed it.
RUN_STATUSES = ("queued", "building", "succeeded", "failed")


def now_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slugify(title: str) -> str:
    kebab = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return kebab


# A slug becomes a filename component (`posts/{slug}.json`) in more than one
# place (digest, draft slug pinning, from_post import): this is the one
# pattern all of them check against, so `/` and `..` can never reach a path.
SLUG_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def is_valid_slug(slug: str) -> bool:
    return bool(slug) and bool(SLUG_PATTERN.fullmatch(slug))


class Material(BaseModel):
    name: str
    text: str | None = None
    url: str | None = None


class Submission(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    created_at: str
    from_: str = Field(alias="from")
    brief: str
    materials: list[Material] = []
    image_ids: list[str] = []
    status: str = "new"
    claimed_by: str | None = None
    draft_id: str | None = None
    # The submission as first posted is version 1, so a record written before
    # submissions were mutable (no field on disk) reads back as version 1 and
    # the first revision is version 2. Deliberately not 0: unlike a draft, a
    # submission never exists without content.
    version_no: int = 1


class SubmissionVersion(BaseModel):
    """One revision of a submission's editable content (brief, materials,
    image ids), stored beside the record so a stale write can be shown what
    moved underneath it. The internal git history carries the same diffs."""

    submission_id: str
    version_no: int
    author: str
    created_at: str
    base_version: int
    message: str = ""
    brief: str = ""
    materials: list[Material] = []
    image_ids: list[str] = []


class DraftImage(BaseModel):
    image_id: str
    filename: str
    role: str
    # The path the post itself used to reach this image (a frontmatter value
    # or a body reference), kept so publish (C4) can rewrite references
    # against wherever the image ends up. None for images picked up only by
    # the static/images/<slug>/ directory sweep, which has no reference to
    # record.
    source_ref: str | None = None


class Claim(BaseModel):
    author: str
    since: str


class Draft(BaseModel):
    id: str
    created_at: str
    updated_at: str
    slug: str | None = None
    # The directory name under static/images/ this draft's images live in,
    # pinned once and never recomputed (ADR 015): the last non-empty path
    # segment of frontmatter["url"] when one is set (an import, or a new
    # draft that already has a url), the pinned slug otherwise. Set at the
    # same moment the slug is pinned (import, or first preview/approve).
    image_dir: str | None = None
    title: str = ""
    frontmatter: dict[str, Any] = {}
    body: str = ""
    status: str = "drafting"
    version_no: int = 0
    source_submission: str | None = None
    source_post: dict[str, str] | None = None
    images: list[DraftImage] = []
    claim: Claim | None = None
    # What the last successful publish or unpublish run actually wrote
    # (branch, PR number and URL, the commit sha, the post's path and url,
    # the stamped date, the exact image site-paths placed, and the post
    # file's own blob sha). Unpublish needs this to know precisely what to
    # delete without recomputing it from the draft's current state,
    # reconciliation's content_drift check compares against post_blob_sha,
    # and republish reads date from here to keep it stable (spec section 9).
    published: dict[str, Any] | None = None


class Version(BaseModel):
    draft_id: str
    version_no: int
    author: str
    created_at: str
    base_version: int
    message: str = ""
    frontmatter: dict[str, Any] = {}
    body: str = ""


class FeedbackEntry(BaseModel):
    draft_id: str
    author: str
    created_at: str
    action: str
    version_no: int
    text: str


class Post(BaseModel):
    slug: str
    path: str
    title: str
    date: str
    sha: str


class Image(BaseModel):
    image_id: str
    sha256: str
    filename: str
    bytes: int
    mime: str


class Run(BaseModel):
    id: str
    draft_id: str
    kind: str
    status: str = "queued"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    # Relative to the data directory, so the record survives the volume being
    # mounted somewhere else in another container.
    log_path: str | None = None
    result: dict[str, Any] | None = None
    # Set when a builder claims the run: which builder took it, the Hugo it
    # actually used, and whether that Hugo differs from the version the site's
    # own Pages workflow names (spec section 8; drift never blocks a build).
    builder_id: str | None = None
    hugo_version: str | None = None
    toolchain_drift: bool = False
    # The draft's version_no at the moment this run's build actually read it,
    # so a `preview_succeeded` transition can be skipped when the draft has
    # since moved on to a newer version the build never saw (round C3 review).
    built_version: int | None = None
    # A publish run only: the draft's `version_no` when `approve` queued it,
    # which is the version the reviewer approved. The publisher refuses to
    # convert a draft that has moved past it (issue 41). None on a run queued
    # before this field existed, and on every other run kind.
    approved_version: int | None = None
    # Set by `Store.requeue_run` when a crashed builder or publisher put this
    # run back on the queue. `created_at` stays the record of when the run
    # was first created; the queue-timeout sweep measures its waiting window
    # from this instead so a run requeued after already sitting past the
    # timeout gets a fresh window rather than failing on the next sweep
    # (ADR 020 amendment). None on a run that has never been requeued.
    requeued_at: str | None = None


class WatchEntry(BaseModel):
    """One open Chronicle-opened PR the watcher is polling (ADR 013).

    Persisted at `data/repo/watch/<draft_id>.json`, one per draft: an
    idempotent re-run of `approve` or `unpublish` resets the same branch and
    updates the same PR rather than opening a second, so a draft never has
    more than one Chronicle PR open at a time.
    """

    draft_id: str
    kind: str  # "publish" | "unpublish"
    branch: str
    pr_number: int
    pr_url: str
    created_at: str
    poll_interval_seconds: float | None = None
    # The draft's `version_no` this run actually converted (`Run.built_version`,
    # stamped by `Store.start_run`). Checked against the draft's current
    # version when the PR merges, so a save made while the PR was still open
    # is not silently reported as published (round C4 review, P1). None for
    # `unpublish`, which never needs it.
    built_version: int | None = None


RECONCILE_FLAG_TYPES = (
    "draft_published_missing_on_main",
    "post_on_main_without_published_draft",
    "post_removed_without_unpublish",
    "slug_drift",
    "content_drift",
)
RECONCILE_RESOLUTIONS = ("mark_published", "mark_unpublished", "import_as_draft", "ignore")


class ReconcileFlag(BaseModel):
    """A mismatch between main and Chronicle's records (spec section 12, ADR 005).

    Flags only, never a correction: nothing here changes a draft or a post
    record on its own; `Store.resolve_flag` only acts once an admin picks a
    resolution.
    """

    id: str
    type: str
    created_at: str
    slug: str | None = None
    draft_id: str | None = None
    detail: str = ""
    # The main blob sha a `content_drift` flag was raised against, so
    # resolving it `ignore` can record exactly what was acknowledged
    # (`Store.resolve_flag`) rather than re-deriving it from `detail` text.
    # None for every other flag type.
    main_sha: str | None = None
    resolved: bool = False
    resolution: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None


class Event(BaseModel):
    seq: int
    ts: str
    type: str
    actor: str
    draft_id: str | None = None
    submission_id: str | None = None
    from_status: str | None = None
    to_status: str | None = None


def render_submission_content(brief: str, materials: list[Material], image_ids: list[str]) -> str:
    """Render a submission version as text for diffing."""
    lines = ["brief:", brief, ""]
    for number, material in enumerate(materials, start=1):
        lines.append(f"material {number}: {material.name}")
        if material.url:
            lines.append(f"url: {material.url}")
        if material.text:
            lines.extend(["text:", material.text])
        lines.append("")
    lines.extend(f"image: {image_id}" for image_id in image_ids)
    return "\n".join(lines) + "\n"


def render_content(frontmatter: dict[str, Any], body: str) -> str:
    """Render a version as text for diffing, in allowlist field order."""
    lines = ["---"]
    for key in FRONTMATTER_ALLOWLIST:
        if key in frontmatter:
            value = frontmatter[key]
            rendered = value if isinstance(value, str) else repr(value)
            lines.append(f"{key}: {rendered}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + body
