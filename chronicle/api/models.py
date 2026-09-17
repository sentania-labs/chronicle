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

# Spec section 4 says "Hugo allowlist"; which keys are in it is ADR 007.
FRONTMATTER_ALLOWLIST = (
    "title",
    "date",
    "lastmod",
    "draft",
    "description",
    "tags",
    "categories",
    "series",
    "slug",
    "featureImage",
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


class DraftImage(BaseModel):
    image_id: str
    filename: str
    role: str


class Claim(BaseModel):
    author: str
    since: str


class Draft(BaseModel):
    id: str
    created_at: str
    updated_at: str
    slug: str | None = None
    title: str = ""
    frontmatter: dict[str, Any] = {}
    body: str = ""
    status: str = "drafting"
    version_no: int = 0
    source_submission: str | None = None
    source_post: dict[str, str] | None = None
    images: list[DraftImage] = []
    claim: Claim | None = None


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
    log_path: str | None = None
    result: dict[str, Any] | None = None


class Event(BaseModel):
    seq: int
    ts: str
    type: str
    actor: str
    draft_id: str | None = None
    submission_id: str | None = None
    from_status: str | None = None
    to_status: str | None = None


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
