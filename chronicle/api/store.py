"""The filesystem store: files are the record, git is the history.

Every mutating method here writes files under `data/repo/` and then makes
exactly one commit authored by the acting consumer, so `git log` in the data
directory is a real, readable history of who changed what (ADR 001). The
SQLite index is updated alongside as a query cache only (ADR 006); nothing
here reads a record back out of it.

Images are the one exception to git: they are content-addressed under
`data/images/` with a JSON sidecar, outside git because binary blobs do not
diff and the backup bundle is what protects them (spec section 13).
"""

from __future__ import annotations

import difflib
import functools
import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from . import gitrepo
from .atomic import write_atomic
from .errors import ApiError
from .images import normalise
from .index import Index, index_path
from .models import (
    FRONTMATTER_ALLOWLIST,
    Claim,
    Draft,
    DraftImage,
    Event,
    FeedbackEntry,
    Image,
    Material,
    Post,
    Run,
    Submission,
    Version,
    now_stamp,
    render_content,
    slugify,
)
from .transitions import resolve_draft, resolve_save, resolve_submission

IMAGE_ROLES = ("inline", "feature")

# Keys whose value type the store depends on: a non-string slug reaches the
# slug index and a non-list tags reaches Hugo, so both are rejected at the door
# rather than persisted and discovered on the next read.
FRONTMATTER_STRING_KEYS = (
    "title",
    "date",
    "lastmod",
    "description",
    "series",
    "slug",
    "featureImage",
)
FRONTMATTER_STRING_LIST_KEYS = ("tags", "categories")


def new_id() -> str:
    return uuid.uuid4().hex


def locked[Method: Callable[..., Any]](method: Method) -> Method:
    """Run a mutating store method start to finish under the store lock."""

    @functools.wraps(method)
    def wrapper(self: Store, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(Method, wrapper)


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.repo_dir = data_dir / "repo"
        self.submissions_dir = self.repo_dir / "submissions"
        self.drafts_dir = self.repo_dir / "drafts"
        self.feedback_dir = self.repo_dir / "feedback"
        self.posts_dir = self.repo_dir / "posts"
        self.runs_dir = self.repo_dir / "runs"
        self.queue_dir = self.runs_dir / "queue"
        self.events_file = self.repo_dir / "events" / "log.jsonl"
        self.images_dir = data_dir / "images"
        self.state_dir = data_dir / "state"
        self.preview_dir = data_dir / "preview"
        self.site_dir = data_dir / "site"
        self.index = Index(index_path(self.repo_dir))
        # The api is one process serving sync handlers on a threadpool, so one
        # process-wide lock held across each mutating method is what makes
        # read-check-write-commit-index atomic; without it two saves at the
        # same base_version both pass the conflict check.
        self._lock = threading.Lock()

    @classmethod
    def open(cls, data_dir: Path) -> Store:
        for path in (
            data_dir / "repo",
            data_dir / "images",
            data_dir / "preview",
            data_dir / "site",
        ):
            path.mkdir(parents=True, exist_ok=True)
        gitrepo.init_repo(data_dir / "repo")
        store = cls(data_dir)
        for path in (
            store.submissions_dir,
            store.drafts_dir,
            store.feedback_dir,
            store.posts_dir,
            store.queue_dir,
            store.events_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return store

    def close(self) -> None:
        self.index.close()

    # Files

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _read_json(self, path: Path) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def _commit(self, message: str, actor: str) -> None:
        gitrepo.commit_all(self.repo_dir, message, actor)

    def _submission_path(self, submission_id: str) -> Path:
        return self.submissions_dir / f"{submission_id}.json"

    def _draft_path(self, draft_id: str) -> Path:
        return self.drafts_dir / draft_id / "draft.json"

    def _version_path(self, draft_id: str, version_no: int) -> Path:
        return self.drafts_dir / draft_id / "versions" / f"{version_no}.json"

    def _feedback_path(self, draft_id: str) -> Path:
        return self.feedback_dir / f"{draft_id}.md"

    def _feedback_log_path(self, draft_id: str) -> Path:
        return self.feedback_dir / f"{draft_id}.jsonl"

    def _run_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    # Events

    def _next_seq(self) -> int:
        # The index is a rebuildable cache (ADR 006): if a process died between
        # the log append and the index upsert, or a reindex is mid-flight, the
        # index can lag the log. The log itself is the durable record, so the
        # next sequence is derived from its own last line, never from the
        # index. Every append happens under the store lock, so this is never
        # read while another append is writing it.
        if not self.events_file.exists():
            return 1
        last_seq = 0
        with self.events_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_seq = json.loads(line)["seq"]
        return last_seq + 1

    def _append_event(self, **fields: Any) -> Event:
        event = Event(seq=self._next_seq(), ts=now_stamp(), **fields)
        self.events_file.parent.mkdir(parents=True, exist_ok=True)
        with self.events_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")
        self.index.add_event(event)
        return event

    def events_since(self, cursor: int) -> tuple[list[Event], int]:
        events = self.index.events_since(cursor)
        next_cursor = events[-1].seq if events else cursor
        return events, next_cursor

    # Submissions

    @locked
    def create_submission(
        self,
        actor: str,
        brief: str,
        materials: list[Material],
        image_ids: list[str],
    ) -> Submission:
        record = Submission.model_validate(
            {
                "id": new_id(),
                "created_at": now_stamp(),
                "from": actor,
                "brief": brief,
                "materials": [material.model_dump(mode="json") for material in materials],
                "image_ids": image_ids,
            }
        )
        self._write_json(
            self._submission_path(record.id), record.model_dump(mode="json", by_alias=True)
        )
        self._append_event(
            type="submission.created", actor=actor, submission_id=record.id, to_status=record.status
        )
        self._commit(f"submission {record.id}: created", actor)
        self.index.upsert_submission(record)
        return record

    def get_submission(self, submission_id: str) -> Submission:
        path = self._submission_path(submission_id)
        if not path.exists():
            raise ApiError(404, "submission_not_found", f"no submission {submission_id}")
        return Submission.model_validate(self._read_json(path))

    def list_submissions(self, status: str | None = None) -> list[Submission]:
        # A mutation writes its file before its index row (ADR 006's cache is
        # always one step behind the record), so without the store lock a list
        # can read the new index and old file, or the old index and new file,
        # of a submission that changed mid-read. Holding the lock across the
        # lookup and every file read makes the list one snapshot.
        with self._lock:
            return [self.get_submission(sid) for sid in self.index.submission_ids(status)]

    @locked
    def act_on_submission(self, submission_id: str, action: str, actor: str) -> Submission:
        record = self.get_submission(submission_id)
        transition = resolve_submission(record.status, action)
        from_status = record.status
        record.status = transition.to_status
        if action == "claim":
            record.claimed_by = actor
        self._write_json(
            self._submission_path(record.id), record.model_dump(mode="json", by_alias=True)
        )
        self._append_event(
            type=f"submission.{action}",
            actor=actor,
            submission_id=record.id,
            from_status=from_status,
            to_status=record.status,
        )
        self._commit(f"submission {record.id}: {action}", actor)
        self.index.upsert_submission(record)
        return record

    # Drafts

    @locked
    def create_draft(self, actor: str, from_submission: str | None = None) -> Draft:
        submission = None
        if from_submission is not None:
            submission = self.get_submission(from_submission)
            resolve_submission(submission.status, "draft")

        stamp = now_stamp()
        draft = Draft(
            id=new_id(),
            created_at=stamp,
            updated_at=stamp,
            source_submission=from_submission,
        )
        self._write_json(self._draft_path(draft.id), draft.model_dump(mode="json"))
        self._append_event(
            type="draft.created", actor=actor, draft_id=draft.id, to_status=draft.status
        )

        if submission is not None:
            from_status = submission.status
            submission.status = "drafted"
            submission.draft_id = draft.id
            submission.claimed_by = submission.claimed_by or actor
            self._write_json(
                self._submission_path(submission.id),
                submission.model_dump(mode="json", by_alias=True),
            )
            self._append_event(
                type="submission.draft",
                actor=actor,
                submission_id=submission.id,
                draft_id=draft.id,
                from_status=from_status,
                to_status=submission.status,
            )

        self._commit(f"draft {draft.id}: created", actor)
        self.index.upsert_draft(draft)
        if submission is not None:
            self.index.upsert_submission(submission)
        return draft

    def get_draft(self, draft_id: str) -> Draft:
        path = self._draft_path(draft_id)
        if not path.exists():
            raise ApiError(404, "draft_not_found", f"no draft {draft_id}")
        return Draft.model_validate(self._read_json(path))

    def list_drafts(self, status: str | None = None) -> list[Draft]:
        # Same race as list_submissions: hold the lock across the index
        # lookup and the file reads so the list is one snapshot.
        with self._lock:
            return [self.get_draft(did) for did in self.index.draft_ids(status)]

    def get_version(self, draft_id: str, version_no: int) -> Version:
        path = self._version_path(draft_id, version_no)
        if not path.exists():
            raise ApiError(404, "version_not_found", f"no version {version_no} of draft {draft_id}")
        return Version.model_validate(self._read_json(path))

    def list_versions(self, draft_id: str) -> list[Version]:
        draft = self.get_draft(draft_id)
        return [self.get_version(draft_id, n) for n in range(1, draft.version_no + 1)]

    def _content_at(self, draft_id: str, version_no: int) -> str:
        if version_no < 1:
            return ""
        version = self.get_version(draft_id, version_no)
        return render_content(version.frontmatter, version.body)

    def _diff(self, draft_id: str, from_version: int, to_version: int) -> str:
        return "".join(
            difflib.unified_diff(
                self._content_at(draft_id, from_version).splitlines(keepends=True),
                self._content_at(draft_id, to_version).splitlines(keepends=True),
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
            )
        )

    def _diff_summary(self, draft_id: str, from_version: int, to_version: int) -> str:
        if from_version != 0 and not self._version_path(draft_id, from_version).exists():
            return f"base_version {from_version} does not exist, no diff available"
        return self._diff(draft_id, from_version, to_version)

    @locked
    def save_draft(
        self,
        draft_id: str,
        actor: str,
        base_version: int,
        frontmatter: dict[str, Any],
        body: str,
        message: str = "",
    ) -> Draft:
        draft = self.get_draft(draft_id)
        check_frontmatter(frontmatter)

        if base_version != draft.version_no:
            # A base_version that names no real version (0 aside, or ahead of
            # current) is still a conflict, not a lookup failure, so the diff
            # is best effort and its absence never changes the status code.
            raise ApiError(
                409,
                "stale_base_version",
                f"draft {draft_id} is at version {draft.version_no}, not {base_version}",
                current_version=draft.version_no,
                base_version=base_version,
                diff_summary=self._diff_summary(draft_id, base_version, draft.version_no),
            )

        submitted_slug = frontmatter.get("slug")
        if draft.slug is not None and submitted_slug not in (None, draft.slug):
            raise ApiError(
                422,
                "slug_immutable",
                f"draft {draft_id} pinned slug {draft.slug!r} and cannot be renamed",
                slug=draft.slug,
            )

        version = Version(
            draft_id=draft_id,
            version_no=draft.version_no + 1,
            author=actor,
            created_at=now_stamp(),
            base_version=base_version,
            message=message,
            frontmatter=frontmatter,
            body=body,
        )
        self._write_json(
            self._version_path(draft_id, version.version_no), version.model_dump(mode="json")
        )

        from_status = draft.status
        transition = resolve_save(draft.status)
        if transition is not None:
            draft.status = transition.to_status
        draft.frontmatter = frontmatter
        draft.title = str(frontmatter["title"])
        draft.body = body
        draft.version_no = version.version_no
        draft.updated_at = version.created_at
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))

        self._append_event(
            type="draft.revise" if transition is not None else "draft.saved",
            actor=actor,
            draft_id=draft_id,
            from_status=from_status,
            to_status=draft.status,
        )
        self._commit(f"draft {draft_id}: version {version.version_no} by {actor}", actor)
        self.index.upsert_draft(draft)
        self.index.upsert_version(version)
        return draft

    @locked
    def set_claim(self, draft_id: str, actor: str, held: bool) -> Draft:
        draft = self.get_draft(draft_id)
        draft.claim = Claim(author=actor, since=now_stamp()) if held else None
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._commit(f"draft {draft_id}: {'claim' if held else 'release'} by {actor}", actor)
        self.index.upsert_draft(draft)
        return draft

    # Feedback

    def _append_feedback(self, entry: FeedbackEntry) -> None:
        """Append feedback to its JSONL record, and render it to the markdown log.

        The JSONL file is the record `list_feedback` reads. The markdown file
        stays for a human reading `git log`, and is never parsed back: a
        feedback text containing a line that looks like a heading would
        otherwise read back as a second entry attributed to whatever author and
        action the text claimed.
        """
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        with self._feedback_log_path(entry.draft_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n")
        header = f"## v{entry.version_no} {entry.action} by {entry.author} at {entry.created_at}"
        with self._feedback_path(entry.draft_id).open("a", encoding="utf-8") as handle:
            handle.write(f"{header}\n\n{entry.text.strip()}\n\n")

    def list_feedback(self, draft_id: str) -> list[FeedbackEntry]:
        path = self._feedback_log_path(draft_id)
        if not path.exists():
            return []
        return [
            FeedbackEntry.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def changes_since(self, draft_id: str, since: int) -> dict[str, Any]:
        draft = self.get_draft(draft_id)
        versions = []
        for version_no in range(since + 1, draft.version_no + 1):
            version = self.get_version(draft_id, version_no)
            versions.append(
                {
                    "version_no": version_no,
                    "author": version.author,
                    "created_at": version.created_at,
                    "message": version.message,
                    "diff": self._diff(draft_id, version_no - 1, version_no),
                }
            )
        # Feedback written against version n arrived after the caller saved
        # version n, so `since=n` has to include it or a ghostwriter resuming
        # at its own last version would never see the review that followed it.
        feedback = [
            entry.model_dump(mode="json")
            for entry in self.list_feedback(draft_id)
            if entry.version_no >= since
        ]
        return {
            "draft_id": draft_id,
            "since": since,
            "current_version": draft.version_no,
            "versions": versions,
            "feedback": feedback,
        }

    # Actions

    def _pin_slug(self, draft: Draft) -> None:
        candidate = draft.frontmatter.get("slug") or slugify(draft.title)
        if not candidate:
            raise ApiError(
                422,
                "slug_underivable",
                f"draft {draft.id} has no title or slug to pin a slug from",
            )
        if candidate in self.index.pinned_slugs(exclude_draft_id=draft.id):
            raise ApiError(
                409,
                "slug_collision",
                f"slug {candidate!r} is already taken; set a unique slug in frontmatter first",
                slug=candidate,
            )
        draft.slug = candidate

    def _queue_run(self, draft_id: str, kind: str) -> Run:
        run = Run(id=new_id(), draft_id=draft_id, kind=kind, created_at=now_stamp())
        self._write_json(self._run_path(run.id), run.model_dump(mode="json"))
        self._write_json(
            self.queue_dir / f"{run.id}.json",
            {
                "run_id": run.id,
                "draft_id": draft_id,
                "kind": kind,
                "enqueued_at": run.created_at,
            },
        )
        return run

    @locked
    def act_on_draft(
        self,
        draft_id: str,
        action: str,
        actor: str,
        actor_is_ui: bool,
        feedback: str | None = None,
    ) -> tuple[Draft, Run | None]:
        draft = self.get_draft(draft_id)
        transition = resolve_draft(draft.status, action, actor_is_ui)
        # A whitespace-only string is truthy, so the required check tests the
        # stripped text; the feedback entry itself still stores what was sent.
        if transition.feedback_required and not (feedback and feedback.strip()):
            raise ApiError(
                422, "feedback_required", f"action {action!r} requires feedback", action=action
            )

        if action in ("preview", "approve") and draft.slug is None:
            self._pin_slug(draft)

        from_status = draft.status
        draft.status = transition.to_status
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))

        run = self._queue_run(draft_id, transition.run_kind) if transition.run_kind else None
        if feedback:
            self._append_feedback(
                FeedbackEntry(
                    draft_id=draft_id,
                    author=actor,
                    created_at=draft.updated_at,
                    action=action,
                    version_no=draft.version_no,
                    text=feedback,
                )
            )

        self._append_event(
            type=f"draft.{action}",
            actor=actor,
            draft_id=draft_id,
            from_status=from_status,
            to_status=draft.status,
        )
        self._commit(f"draft {draft_id}: {action} by {actor}", actor)
        self.index.upsert_draft(draft)
        if run is not None:
            self.index.upsert_run(run)
        return draft, run

    # Images

    def _image_blob_path(self, image_id: str, extension: str) -> Path:
        return self.images_dir / image_id[:2] / f"{image_id}.{extension}"

    def _image_sidecar_path(self, image_id: str) -> Path:
        return self.images_dir / image_id[:2] / f"{image_id}.json"

    @locked
    def put_image(self, raw: bytes, filename: str) -> tuple[Image, bool]:
        normalised = normalise(raw)
        sidecar = self._image_sidecar_path(normalised.sha256)
        if sidecar.exists():
            return Image.model_validate(self._read_json(sidecar)), False
        record = Image(
            image_id=normalised.sha256,
            sha256=normalised.sha256,
            filename=filename,
            bytes=len(normalised.data),
            mime=normalised.mime,
        )
        blob = self._image_blob_path(record.image_id, normalised.extension)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(normalised.data)
        self._write_json(sidecar, record.model_dump(mode="json"))
        self.index.upsert_image(record)
        return record, True

    def get_image(self, image_id: str) -> Image:
        sidecar = self._image_sidecar_path(image_id)
        if not sidecar.exists():
            raise ApiError(404, "image_not_found", f"no image {image_id}")
        return Image.model_validate(self._read_json(sidecar))

    @locked
    def attach_image(self, draft_id: str, image_id: str, role: str, actor: str) -> Draft:
        if role not in IMAGE_ROLES:
            raise ApiError(
                422, "image_role_unknown", f"role must be one of {', '.join(IMAGE_ROLES)}"
            )
        draft = self.get_draft(draft_id)
        image = self.get_image(image_id)
        draft.images = [item for item in draft.images if item.image_id != image_id]
        draft.images.append(DraftImage(image_id=image_id, filename=image.filename, role=role))
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._commit(f"draft {draft_id}: attach image {image_id} as {role}", actor)
        self.index.upsert_draft(draft)
        return draft

    @locked
    def detach_image(self, draft_id: str, image_id: str, actor: str) -> Draft:
        draft = self.get_draft(draft_id)
        remaining = [item for item in draft.images if item.image_id != image_id]
        if len(remaining) == len(draft.images):
            raise ApiError(
                404, "image_not_attached", f"image {image_id} is not attached to draft {draft_id}"
            )
        draft.images = remaining
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._commit(f"draft {draft_id}: detach image {image_id}", actor)
        self.index.upsert_draft(draft)
        return draft

    # Posts and runs

    def get_post(self, slug: str) -> Post:
        path = self.posts_dir / f"{slug}.json"
        if not path.exists():
            raise ApiError(404, "post_not_found", f"no post {slug}")
        return Post.model_validate(self._read_json(path))

    def list_posts(self) -> list[Post]:
        return [self.get_post(slug) for slug in self.index.post_slugs()]

    def get_run(self, run_id: str) -> Run:
        path = self._run_path(run_id)
        if not path.exists():
            raise ApiError(404, "run_not_found", f"no run {run_id}")
        return Run.model_validate(self._read_json(path))

    def last_run(self, draft_id: str) -> Run | None:
        run_id = self.index.last_run_id(draft_id)
        return self.get_run(run_id) if run_id else None

    # Index rebuild

    @locked
    def reindex(self) -> dict[str, int]:
        """Rebuild every index row from the files, the only durable record."""
        self.index.clear()
        counts = dict.fromkeys(
            ("submissions", "drafts", "versions", "runs", "posts", "images", "events"), 0
        )

        for path in sorted(self.submissions_dir.glob("*.json")):
            self.index.upsert_submission(Submission.model_validate(self._read_json(path)))
            counts["submissions"] += 1

        for path in sorted(self.drafts_dir.glob("*/draft.json")):
            draft = Draft.model_validate(self._read_json(path))
            self.index.upsert_draft(draft)
            counts["drafts"] += 1
            for version_path in sorted(path.parent.glob("versions/*.json")):
                self.index.upsert_version(Version.model_validate(self._read_json(version_path)))
                counts["versions"] += 1

        for path in sorted(self.runs_dir.glob("*.json")):
            self.index.upsert_run(Run.model_validate(self._read_json(path)))
            counts["runs"] += 1

        for path in sorted(self.posts_dir.glob("*.json")):
            self.index.upsert_post(Post.model_validate(self._read_json(path)))
            counts["posts"] += 1

        for path in sorted(self.images_dir.glob("*/*.json")):
            self.index.upsert_image(Image.model_validate(self._read_json(path)))
            counts["images"] += 1

        if self.events_file.exists():
            for line in self.events_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.index.add_event(Event.model_validate_json(line))
                    counts["events"] += 1

        return counts


def check_frontmatter(frontmatter: dict[str, Any]) -> None:
    unknown = sorted(key for key in frontmatter if key not in FRONTMATTER_ALLOWLIST)
    if unknown:
        raise ApiError(
            422,
            "frontmatter_key_not_allowed",
            f"frontmatter keys not in the allowlist: {', '.join(unknown)}",
            unknown_keys=unknown,
            allowed_keys=list(FRONTMATTER_ALLOWLIST),
        )
    for key in FRONTMATTER_STRING_KEYS:
        if key in frontmatter and not isinstance(frontmatter[key], str):
            raise _wrong_type(key, "a string")
    for key in FRONTMATTER_STRING_LIST_KEYS:
        value = frontmatter.get(key)
        if key in frontmatter and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            raise _wrong_type(key, "a list of strings")
    if "draft" in frontmatter and not isinstance(frontmatter["draft"], bool):
        raise _wrong_type("draft", "true or false")

    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ApiError(422, "title_required", "frontmatter.title is required and must be non-empty")


def _wrong_type(key: str, expected: str) -> ApiError:
    return ApiError(
        422,
        "frontmatter_invalid_type",
        f"frontmatter.{key} must be {expected}",
        key=key,
        expected=expected,
    )
