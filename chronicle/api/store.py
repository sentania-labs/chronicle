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
import fcntl
import functools
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import yaml

from . import convert, gitrepo
from . import digest as digest_mod
from .atomic import write_atomic
from .errors import ApiError
from .images import is_plain_filename, normalise
from .index import Index, index_path
from .models import (
    ANNOUNCEMENT_CHANNELS,
    FRONTMATTER_ALLOWLIST,
    Claim,
    Draft,
    DraftImage,
    Event,
    FeedbackEntry,
    Image,
    Material,
    Post,
    ReconcileFlag,
    Run,
    Submission,
    SubmissionVersion,
    Version,
    WatchEntry,
    is_valid_slug,
    now_stamp,
    render_content,
    render_submission_content,
    slugify,
)
from .transitions import (
    PREVIEW_SUCCEEDED,
    PUBLISH_RUN_FAILED,
    RECONCILE_STATUS,
    plan_action,
    resolve_draft,
    resolve_run_outcome,
    resolve_save,
    resolve_submission,
    resolve_submission_revise,
    resolve_watch,
)

IMAGE_ROLES = ("inline", "feature")
LOCK_FILE_NAME = ".store.lock"

# from_post image discovery: markdown `![alt](path)` and bare HTML `<img
# src="...">`, the two ways a real post's body points at an image.
_MARKDOWN_IMAGE_REF = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)")
_HTML_IMG_REF = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.IGNORECASE)

# Keys whose value type the store depends on: a non-string slug reaches the
# slug index and a non-list tags reaches Hugo, so both are rejected at the door
# rather than persisted and discovered on the next read.
FRONTMATTER_STRING_KEYS = (
    "title",
    "author",
    "type",
    "date",
    "lastmod",
    "url",
    "slug",
    "description",
    "summary",
    "series",
    "featureImage",
    "shareImage",
)
FRONTMATTER_STRING_LIST_KEYS = ("tags", "categories")

# Seeding a draft from a submission (`_seed_draft_from_submission`): a material
# "looks like a post" when it opens with a frontmatter block (preferred) or a
# markdown heading. The frontmatter pattern mirrors what `digest.parse_frontmatter`
# will actually split, so detection and parsing cannot disagree.
_FRONTMATTER_BLOCK = re.compile(r"\A(---|\+\+\+)\n.*?\n\1\n", re.DOTALL)
# The `action` of the feedback entry `_seed_draft_from_submission` writes for
# each non-primary material; `changes_since` includes these at any cutoff.
MATERIAL_ACTION = "material"

_IMAGE_ID = re.compile(r"[0-9a-f]{64}")
_LEADING_HEADING = re.compile(r"\s*#{1,6}[ \t]+(\S[^\n]*)")

log = logging.getLogger("chronicle.api.store")


def new_id() -> str:
    return uuid.uuid4().hex


class StoreLock:
    """One lock across threads and across processes.

    C1 and C2 had exactly one writer, the api process, so a `threading.Lock`
    was the whole story. C3 adds a second: the builder container opens the
    same data directory to move a run from queued to building to succeeded,
    and it commits those writes to the same internal git repository. Two
    processes running `git add`/`git commit` at once collide on git's own
    `index.lock`, and two processes doing read-check-write on the same JSON
    record race the same way two threads would.

    So the thread lock is kept (it is what serialises the api's own
    threadpool, and it is cheap) and an advisory `flock` on a single file at
    the root of the data directory is taken underneath it. The lock file
    lives outside `repo/` deliberately: nothing that is pure runtime
    coordination belongs in the history git tracks.

    Not reentrant, on purpose: a mutating method that needs another one
    splits out an `_unlocked` half instead (see `_put_image_unlocked`).
    """

    def __init__(self, lock_path: Path) -> None:
        self._thread_lock = threading.Lock()
        self._lock_path = lock_path
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+")

    def acquire(self) -> None:
        self._thread_lock.acquire()
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._thread_lock.release()

    def __enter__(self) -> StoreLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def close(self) -> None:
        self._handle.close()


def locked[Method: Callable[..., Any]](method: Method) -> Method:
    """Run a mutating store method start to finish under the store lock."""

    @functools.wraps(method)
    def wrapper(self: Store, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return cast(Method, wrapper)


class Store:
    def __init__(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = data_dir
        self.repo_dir = data_dir / "repo"
        self.submissions_dir = self.repo_dir / "submissions"
        self.drafts_dir = self.repo_dir / "drafts"
        self.feedback_dir = self.repo_dir / "feedback"
        self.posts_dir = self.repo_dir / "posts"
        self.runs_dir = self.repo_dir / "runs"
        self.queue_dir = self.runs_dir / "queue"
        self.watch_dir = self.repo_dir / "watch"
        self.reconcile_dir = self.repo_dir / "reconcile"
        self.events_file = self.repo_dir / "events" / "log.jsonl"
        self.images_dir = data_dir / "images"
        self.state_dir = data_dir / "state"
        self.preview_dir = data_dir / "preview"
        self.site_dir = data_dir / "site"
        self.index = Index(index_path(self.repo_dir))
        # One lock held across each mutating method is what makes
        # read-check-write-commit-index atomic; without it two saves at the
        # same base_version both pass the conflict check. It spans processes
        # as well as threads because the builder is a second writer (see
        # StoreLock).
        self._lock = StoreLock(data_dir / LOCK_FILE_NAME)

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
            store.watch_dir,
            store.reconcile_dir,
            store.events_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return store

    def close(self) -> None:
        self.index.close()
        self._lock.close()

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

    def _submission_version_path(self, submission_id: str, version_no: int) -> Path:
        return self.submissions_dir / submission_id / "versions" / f"{version_no}.json"

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
        self._require_images(image_ids)
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
        self._write_submission_version(record, actor, base_version=0)
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

    def _write_submission_version(
        self, record: Submission, author: str, base_version: int, message: str = ""
    ) -> SubmissionVersion:
        version = SubmissionVersion(
            submission_id=record.id,
            version_no=record.version_no,
            author=author,
            created_at=now_stamp(),
            base_version=base_version,
            message=message,
            brief=record.brief,
            materials=record.materials,
            image_ids=record.image_ids,
        )
        self._write_json(
            self._submission_version_path(record.id, record.version_no),
            version.model_dump(mode="json"),
        )
        return version

    def get_submission_version(self, submission_id: str, version_no: int) -> SubmissionVersion:
        path = self._submission_version_path(submission_id, version_no)
        if not path.exists():
            raise ApiError(
                404,
                "version_not_found",
                f"no version {version_no} of submission {submission_id}",
            )
        return SubmissionVersion.model_validate(self._read_json(path))

    def list_submission_versions(self, submission_id: str) -> list[SubmissionVersion]:
        record = self.get_submission(submission_id)
        return [
            self.get_submission_version(submission_id, n)
            for n in range(1, record.version_no + 1)
            if self._submission_version_path(submission_id, n).exists()
        ]

    def _submission_content_at(self, record: Submission, version_no: int) -> str | None:
        """Rendered content of one version, None when it was never recorded.

        The record itself is authoritative for its current version; older
        ones come from their version files. A submission written before
        versioning has no file for its version 1 until its first revision
        writes one, which is why this can legitimately be None.
        """
        if version_no == record.version_no:
            return render_submission_content(record.brief, record.materials, record.image_ids)
        path = self._submission_version_path(record.id, version_no)
        if version_no < 1 or not path.exists():
            return None
        version = self.get_submission_version(record.id, version_no)
        return render_submission_content(version.brief, version.materials, version.image_ids)

    def submission_diff(self, record: Submission, from_version: int) -> str:
        """A unified diff from `from_version` to `record`'s own version.

        Takes the record rather than an id so a caller that also renders that
        record diffs the very snapshot it shows, never a second read.
        """
        before = self._submission_content_at(record, from_version)
        if before is None:
            return f"base_version {from_version} does not exist, no diff available"
        after = self._submission_content_at(record, record.version_no) or ""
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"v{from_version}",
                tofile=f"v{record.version_no}",
            )
        )

    def _image_exists(self, image_id: str) -> bool:
        # The shape guard comes first: an id becomes part of a path, so a
        # caller-chosen string must never reach `_image_sidecar_path`.
        return bool(_IMAGE_ID.fullmatch(image_id)) and self._image_sidecar_path(image_id).exists()

    def _require_images(self, image_ids: list[str]) -> None:
        """422 `image_not_found` naming the first id that is not in the image
        store. Create and revise both call this: the detail page loads every
        listed image, so an id that names nothing would leave a submission
        whose page (and its actions) cannot render."""
        for image_id in image_ids:
            if not self._image_exists(image_id):
                raise ApiError(
                    422,
                    "image_not_found",
                    f"image {image_id} is not in the image store; upload it first",
                    image_id=image_id,
                )

    def submission_images(self, image_ids: list[str]) -> tuple[list[Image], list[str]]:
        """The stored images among `image_ids`, in order, and the ids that are
        not in the store. A record written before create validated can carry
        an id that names nothing; readers use this instead of `get_image` so
        one such id does not take the whole page down."""
        found: list[Image] = []
        missing: list[str] = []
        for image_id in image_ids:
            if self._image_exists(image_id):
                found.append(self.get_image(image_id))
            else:
                missing.append(image_id)
        return found, missing

    @locked
    def revise_submission(
        self,
        submission_id: str,
        actor: str,
        base_version: int,
        brief: str,
        materials: list[Material],
        image_ids: list[str],
        message: str = "",
    ) -> Submission:
        """Replace a submission's brief, materials and images, guarded by
        `base_version` the way `save_draft` guards a draft save.

        Only a `new` or `claimed` submission can be revised (transitions.py);
        the history is one version file per revision plus the ordinary
        internal git commit authored by `actor`.
        """
        record = self.get_submission(submission_id)
        resolve_submission_revise(record.status, submission_id)
        self._require_images(image_ids)
        if base_version != record.version_no:
            raise ApiError(
                409,
                "stale_base_version",
                f"submission {submission_id} is at version {record.version_no}, not {base_version}",
                current_version=record.version_no,
                base_version=base_version,
                diff_summary=self.submission_diff(record, base_version),
            )
        if not self._submission_version_path(record.id, record.version_no).exists():
            # Written before submissions were versioned: record what version
            # 1 was, in this same commit, so the history has a starting point.
            self._write_submission_version(record, record.from_, base_version=0)

        record.brief = brief
        record.materials = materials
        record.image_ids = image_ids
        record.version_no += 1
        self._write_json(
            self._submission_path(record.id), record.model_dump(mode="json", by_alias=True)
        )
        self._write_submission_version(record, actor, base_version, message)
        self._append_event(
            type="submission.revise",
            actor=actor,
            submission_id=record.id,
            from_status=record.status,
            to_status=record.status,
        )
        self._commit(f"submission {record.id}: version {record.version_no} by {actor}", actor)
        self.index.upsert_submission(record)
        return record

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
    def create_draft(
        self,
        actor: str,
        from_submission: str | None = None,
        from_post: str | None = None,
    ) -> tuple[Draft, list[str]]:
        return self._create_draft_unlocked(actor, from_submission, from_post)

    def _create_draft_unlocked(
        self,
        actor: str,
        from_submission: str | None = None,
        from_post: str | None = None,
    ) -> tuple[Draft, list[str]]:
        """Split out so `resolve_flag`'s `import_as_draft` (already `@locked`)
        can create a draft without deadlocking on the store's non-reentrant
        lock, the same reason `_put_image_unlocked` exists."""
        if from_submission is not None and from_post is not None:
            raise ApiError(
                422,
                "import_source_conflict",
                "a draft can be created from_submission or from_post, not both",
            )

        submission = None
        if from_submission is not None:
            submission = self.get_submission(from_submission)
            resolve_submission(submission.status, "draft")

        warnings: list[str] = []
        stamp = now_stamp()
        draft = Draft(
            id=new_id(),
            created_at=stamp,
            updated_at=stamp,
            source_submission=from_submission,
        )

        if from_post is not None:
            draft, warnings = self._fill_from_post(draft, from_post)
        if submission is not None:
            warnings = self._seed_draft_from_submission(draft, submission, actor, stamp)

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

        message = f"draft {draft.id}: created"
        if from_post is not None:
            message += f" from post {from_post}"
        self._commit(message, actor)
        self.index.upsert_draft(draft)
        if submission is not None:
            self.index.upsert_submission(submission)
        return draft, warnings

    def _seed_draft_from_submission(
        self, draft: Draft, submission: Submission, actor: str, stamp: str
    ) -> list[str]:
        """Give a new draft the writing a submission already carries.

        The primary material (the first with frontmatter, else the first
        opening with a heading, else the first with any text) becomes the
        body; its frontmatter, if any, is parsed the way an import parses a
        post, then allowlisted and type-checked with a drop-and-warn rule (a
        key the save path would 422 is dropped, never a 422 here). Every
        other material is reference for the writer, so it goes to the
        feedback log and never into the body. Runs inside
        `_create_draft_unlocked`'s lock: only `_unlocked` helpers here.
        """
        warnings: list[str] = []
        primary = _primary_material(submission.materials)
        if primary is not None and primary.text is not None:
            frontmatter, body, parse_error = _split_material(_clean_material_text(primary.text))
            if parse_error is not None:
                warnings.append(f"material {primary.name!r}: {parse_error}")
            allowed: dict[str, Any] = {}
            for key, value in (frontmatter or {}).items():
                if key not in FRONTMATTER_ALLOWLIST:
                    warnings.append(f"dropped unknown frontmatter key {key!r}")
                    continue
                expected = _frontmatter_type_problem(key, value)
                if expected is not None:
                    # The save path would 422 this value; the seed itself
                    # succeeded, so drop the key and say why instead.
                    warnings.append(
                        f"dropped frontmatter key {key!r}: it must be {expected}"
                        f" (got {type(value).__name__})"
                    )
                    continue
                if key == "url" and (problem := convert.url_problem(value)) is not None:
                    warnings.append(f"dropped frontmatter key 'url': {problem}")
                    continue
                allowed[key] = value
            title = allowed.get("title")
            if not title:
                heading = _LEADING_HEADING.match(body)
                title = heading.group(1).strip() if heading else ""
                if title:
                    allowed["title"] = title
            draft.title = str(title)
            draft.frontmatter = allowed
            draft.body = body

        for material in submission.materials:
            if material is primary:
                continue
            parts = [f"Material: {material.name}"]
            if material.url:
                parts.append(f"URL: {material.url}")
            if material.text:
                parts.extend(["", material.text])
            self._append_feedback(
                FeedbackEntry(
                    draft_id=draft.id,
                    author=actor,
                    created_at=stamp,
                    action=MATERIAL_ACTION,
                    version_no=0,
                    text="\n".join(parts),
                )
            )

        for image_id in submission.image_ids:
            try:
                image = self.get_image(image_id)
            except ApiError:
                warnings.append(f"could not attach image {image_id}: it is not in the image store")
                continue
            if any(item.image_id == image_id for item in draft.images):
                continue
            if any(item.filename == image.filename for item in draft.images):
                warnings.append(
                    f"could not attach image {image_id}: another attached image"
                    f" is already named {image.filename!r}"
                )
                continue
            draft.images.append(
                DraftImage(image_id=image_id, filename=image.filename, role="inline")
            )
        return warnings

    def _fill_from_post(self, draft: Draft, slug: str) -> tuple[Draft, list[str]]:
        """Populate a new draft from a published post record and its file on main.

        The frontmatter allowlist is not enforced with a 422 here the way a
        save is: an existing post on main may carry a field the allowlist
        does not have (the site predates Chronicle), so unknown keys are
        dropped and reported as warnings instead of rejecting the import.
        """
        post = self.get_post(slug)
        source_path = self.site_dir / post.path
        if not source_path.exists():
            raise ApiError(
                404,
                "post_file_missing",
                f"post {slug!r} has no file at {post.path} in the last digest;"
                " run a digest before importing it",
            )
        frontmatter, body = digest_mod.parse_frontmatter(source_path.read_text(encoding="utf-8"))
        warnings = []
        allowed = {}
        for key, value in frontmatter.items():
            if key in FRONTMATTER_ALLOWLIST:
                allowed[key] = value
            else:
                warnings.append(f"dropped unknown frontmatter key {key!r}")
        allowed.setdefault("title", post.title)
        allowed["slug"] = slug

        draft.slug = slug
        draft.title = str(allowed["title"])
        draft.frontmatter = allowed
        draft.body = body
        draft.source_post = {"slug": post.slug, "path": post.path, "sha": post.sha}
        # ADR 015: the image directory is the post's own url slug, never
        # `post.slug` (digest's slug can carry a dated filename stem), so an
        # import reproduces the real blog's static/images/<dir>/ byte for
        # byte even when the two differ.
        draft.image_dir = convert.image_dir_name(allowed.get("url"), slug)
        if (problem := convert.url_problem(allowed.get("url"))) is not None:
            # The post is already on main, so the import cannot be refused for
            # it; the url is kept as found and the directory falls back.
            warnings.append(
                f"frontmatter url is not usable as an image directory ({problem});"
                f" images are pinned to static/images/{draft.image_dir}/ instead"
            )
        # `_pin_slug`'s own image_dir_collision check never runs for an
        # import (it only fires when `draft.slug is None`, and this method
        # sets it directly), so a second import of the same post, or of a
        # different post whose url happens to collide, would silently pin
        # a second draft onto the same static/images/<dir>/ another draft
        # already owns. The "already exists on main" half of that check
        # would always fire for a legitimate import (the directory being
        # imported from is exactly what's on main), so only the
        # other-draft check applies here.
        if draft.image_dir in self.index.pinned_image_dirs(exclude_draft_id=draft.id):
            raise ApiError(
                409,
                "image_dir_collision",
                f"static/images/{draft.image_dir}/ is already pinned by another draft",
                image_dir=draft.image_dir,
            )

        images, image_warnings = self._import_post_images(
            source_path,
            draft.image_dir,
            body,
            allowed.get("featureImage"),
            allowed.get("shareImage"),
        )
        draft.images = images
        warnings.extend(image_warnings)
        return draft, warnings

    def _referenced_images(
        self, body: str, feature_image: Any, share_image: Any
    ) -> list[tuple[str, str]]:
        """(reference, role) pairs in the order the post itself names them.

        `featureImage` and `shareImage` are frontmatter values, not body
        text, so they are listed first and separately from the body scan.
        `shareImage` gets the feature role too when it names the same file
        as `featureImage`; a real post's share image is otherwise just
        another inline attachment.
        """
        refs: list[tuple[str, str]] = []
        if feature_image:
            refs.append((str(feature_image), "feature"))
        if share_image and str(share_image) != str(feature_image):
            refs.append((str(share_image), "inline"))
        for match in _MARKDOWN_IMAGE_REF.finditer(body):
            refs.append((match.group(1), "inline"))
        for match in _HTML_IMG_REF.finditer(body):
            refs.append((match.group(1), "inline"))
        return refs

    def _resolve_image_ref(self, ref: str, source_path: Path) -> Path | None:
        """A root-relative ref resolves against `data/site/static/`; anything
        else resolves against the post's own bundle directory, matching how
        Hugo itself resolves the same two reference shapes."""
        if ref.startswith(("http://", "https://", "//")):
            return None
        clean = ref.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            return None
        candidate = (
            self.site_dir / "static" / clean.lstrip("/")
            if clean.startswith("/")
            else source_path.parent / clean
        )
        return candidate if candidate.is_file() else None

    def _import_post_images(
        self,
        source_path: Path,
        image_dir: str,
        body: str,
        feature_image: Any,
        share_image: Any,
    ) -> tuple[list[DraftImage], list[str]]:
        images: list[DraftImage] = []
        warnings: list[str] = []
        seen_refs: set[str] = set()
        imported_names: set[str] = set()

        for ref, role in self._referenced_images(body, feature_image, share_image):
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            path = self._resolve_image_ref(ref, source_path)
            if path is None:
                warnings.append(f"could not find referenced image {ref!r}")
                continue
            try:
                raw = path.read_bytes()
                record, _created = self._put_image_unlocked(raw, path.name)
            except ApiError as exc:
                warnings.append(f"could not import image {path.name}: {exc.message}")
                continue
            images.append(
                DraftImage(
                    image_id=record.image_id, filename=record.filename, role=role, source_ref=ref
                )
            )
            imported_names.add(path.name)

        # Fallback sweep for the page-bundle and static/images/<image_dir>/
        # layouts (ADR 007 predates the real-post case; ADR 015 fixed this
        # sweep to look in the post's actual, url-derived image directory
        # rather than a dated digest slug, which found nothing on a real
        # dated-filename post and silently dropped every unreferenced
        # image): anything not already picked up by an explicit reference
        # still gets attached, with no reference path to record.
        candidates: list[Path] = []
        if source_path.name == "index.md":
            candidates.extend(
                p for p in source_path.parent.iterdir() if p.is_file() and p != source_path
            )
        bundle_dir = self.site_dir / "static" / "images" / image_dir
        if bundle_dir.is_dir():
            candidates.extend(p for p in bundle_dir.iterdir() if p.is_file())

        feature_name = Path(str(feature_image)).name if feature_image else None
        for path in sorted(set(candidates)):
            if path.name in imported_names:
                continue
            try:
                raw = path.read_bytes()
                record, _created = self._put_image_unlocked(raw, path.name)
            except ApiError as exc:
                warnings.append(f"could not import image {path.name}: {exc.message}")
                continue
            role = "feature" if feature_name and path.name == feature_name else "inline"
            images.append(DraftImage(image_id=record.image_id, filename=record.filename, role=role))
            imported_names.add(path.name)
        return images, warnings

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

    def same_text_as_version(self, draft: Draft, version_no: int) -> bool:
        """Whether `draft`'s current frontmatter and body equal those of
        `version_no`, announcements aside. An announcement-only save bumps the
        version without changing anything a build converts, so a preview of
        the earlier version is still a preview of the current text. Not
        `@locked`: the editor's action guard calls it under the store lock."""
        if version_no == draft.version_no:
            return True
        try:
            version = self.get_version(draft.id, version_no)
        except ApiError:
            return False
        return version.frontmatter == draft.frontmatter and version.body == draft.body

    def list_versions(self, draft_id: str) -> list[Version]:
        draft = self.get_draft(draft_id)
        return [self.get_version(draft_id, n) for n in range(1, draft.version_no + 1)]

    def resolve_run_post_url(self, run: Run | None, draft: Draft | None) -> str | None:
        """`convert.resolve_run_post_url`, reading the frontmatter of the
        version `run.built_version` actually built (issue #61's Codex round).

        `convert.py` stays store-free on purpose, so this is the one place
        every caller (the UI routes, the API status/preview routes, the
        publisher) goes through instead of reading `run.built_version` itself.
        A run whose result already carries a string `post_url` needs no
        version at all, since `convert.resolve_run_post_url` returns it
        outright; checking that here first is what keeps a board full of
        current runs from taking one version read per card. A run with no
        `built_version`, or whose version file is gone, falls back to the
        draft's current frontmatter the same way `convert.py` always did.
        """
        built_frontmatter = None
        if run is not None and run.built_version is not None:
            result = run.result or {}
            post_url_value = result.get("post_url")
            if not (isinstance(post_url_value, str) and post_url_value):
                try:
                    version = self.get_version(run.draft_id, run.built_version)
                except ApiError:
                    version = None
                built_frontmatter = version.frontmatter if version is not None else None
        return convert.resolve_run_post_url(run, draft, built_frontmatter)

    def _content_at(self, draft_id: str, version_no: int) -> str:
        if version_no < 1:
            return ""
        version = self.get_version(draft_id, version_no)
        return render_content(version.frontmatter, version.body, version.announcements)

    def _diff(self, draft_id: str, from_version: int, to_version: int) -> str:
        return "".join(
            difflib.unified_diff(
                self._content_at(draft_id, from_version).splitlines(keepends=True),
                self._content_at(draft_id, to_version).splitlines(keepends=True),
                fromfile=f"v{from_version}",
                tofile=f"v{to_version}",
            )
        )

    def diff_between(self, draft_id: str, from_version: int, to_version: int) -> str:
        """A unified diff between any two versions, for the UI's diff view."""
        return self._diff(draft_id, from_version, to_version)

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
        announcements: dict[str, Any] | None = None,
    ) -> Draft:
        draft = self.get_draft(draft_id)
        watch = self.get_watch(draft_id)
        if watch is not None and watch.kind == "publish":
            raise ApiError(
                409,
                "publish_pr_open",
                f"draft {draft_id} has an open publish PR ({watch.pr_url}); "
                "wait for it to merge or close before saving",
                pr_url=watch.pr_url,
                pr_number=watch.pr_number,
            )
        # The same refusal for the stretch before the PR exists: approve has
        # queued a publish run (or the publisher is building it) but no watch
        # yet. Without it a save lands in that window, the draft stays
        # `approved`, and the run converts text nobody previewed or approved
        # (issue 41). A run that finishes releases the draft either way: a PR
        # is a watch, a failure returns the draft to `in_review`.
        self._refuse_while_publishing(draft, "saved")
        check_frontmatter(frontmatter, current_url=draft.frontmatter.get("url"))

        # ADR 022: once `_pin_slug` has stamped a date, a save that omits it
        # (the API) or clears it (the editor's Date field) must not lose the
        # stamp; the filename and url are derived from it, and `_pin_slug`
        # never runs a second time once `draft.slug` is set. A hand-set
        # different date still wins over the stored one.
        if draft.slug is not None and not frontmatter.get("date"):
            carried = draft.frontmatter.get("date") or convert.stamp_publish_date()
            frontmatter = {**frontmatter, "date": carried}

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

        # Omitted means keep: only a caller that sends a mapping (`{}`
        # included) replaces what the draft carries (ADR 021). Checked after
        # the conflict check above so a stale save is always the 409 it was
        # before this field existed, and never a 422 about announcements.
        if announcements is None:
            new_announcements = dict(draft.announcements)
        else:
            check_announcements(announcements)
            # Every value is a string by the line above; the rebuild is what
            # tells the type checker so without a cast.
            new_announcements = {key: str(value) for key, value in announcements.items()}

        version = Version(
            draft_id=draft_id,
            version_no=draft.version_no + 1,
            author=actor,
            created_at=now_stamp(),
            base_version=base_version,
            message=message,
            frontmatter=frontmatter,
            body=body,
            announcements=new_announcements,
        )
        self._write_json(
            self._version_path(draft_id, version.version_no), version.model_dump(mode="json")
        )

        from_status = draft.status
        # A save that changes only announcements leaves the status alone
        # (issue #69): they never reach the post, so there is nothing to
        # revise. The version still bumps so history keeps the change.
        text_unchanged = frontmatter == draft.frontmatter and body == draft.body
        transition = None if text_unchanged else resolve_save(draft.status)
        if transition is not None:
            draft.status = transition.to_status
        draft.frontmatter = frontmatter
        draft.title = str(frontmatter["title"])
        draft.body = body
        draft.announcements = new_announcements
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
        self._append_event(
            type="draft.claim" if held else "draft.release", actor=actor, draft_id=draft_id
        )
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
        # Two rules, and only two.
        #
        # Review feedback (an action's feedback, a closed PR, a failed
        # publish) is cut off by version: written against version n, it
        # arrived after the caller saved version n, so `since=n` includes it
        # or a ghostwriter resuming at its own last version would never see
        # the review that followed it. `since=n+1` does not.
        #
        # Seeded reference material (`MATERIAL_ACTION`) is not review of any
        # version. It arrived with the submission the draft was made from, at
        # version 0, and the ghostwriter has no memory between sessions, so a
        # cutoff would hide it from every session after its first save. It is
        # always included, at any `since`, in the same `feedback` list and log
        # order it already appears in at `since=0`, so a client that reads it
        # there needs no change. It is written once, when the draft is
        # seeded, and never grows afterwards, so repeating it is bounded by the
        # size of the submission; a client that only wants review comments skips entries whose
        # `action` is `material`.
        feedback = [
            entry.model_dump(mode="json")
            for entry in self.list_feedback(draft_id)
            if entry.action == MATERIAL_ACTION or entry.version_no >= since
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
        if not is_valid_slug(candidate):
            raise ApiError(
                422,
                "slug_invalid",
                f"slug {candidate!r} must be lowercase letters, digits, hyphens, or underscores",
                slug=candidate,
            )
        if candidate in self.index.pinned_slugs(exclude_draft_id=draft.id):
            raise ApiError(
                409,
                "slug_collision",
                f"slug {candidate!r} is already taken; set a unique slug in frontmatter first",
                slug=candidate,
            )
        image_dir = convert.image_dir_name(draft.frontmatter.get("url"), candidate)
        if image_dir in self.index.pinned_image_dirs(exclude_draft_id=draft.id):
            raise ApiError(
                409,
                "image_dir_collision",
                f"static/images/{image_dir}/ is already pinned by another draft",
                image_dir=image_dir,
            )
        if (self.site_dir / "static" / "images" / image_dir).is_dir():
            raise ApiError(
                409,
                "image_dir_collision",
                f"static/images/{image_dir}/ already exists on main for a different post",
                image_dir=image_dir,
            )
        draft.slug = candidate
        draft.image_dir = image_dir
        # ADR 022: stamped here, not only at first publish (record_publish_result
        # does the same thing for a draft written before this existed). The
        # pin is the moment the filename and url are derived from the date
        # (post_filename, post_url), so a date that drifted between preview
        # and publish would move the post's own url out from under it.
        if not draft.frontmatter.get("date"):
            draft.frontmatter = {**draft.frontmatter, "date": convert.stamp_publish_date()}

    def _queue_run(self, draft_id: str, kind: str, approved_version: int | None = None) -> Run:
        run = Run(
            id=new_id(),
            draft_id=draft_id,
            kind=kind,
            created_at=now_stamp(),
            approved_version=approved_version,
        )
        self._write_json(self._run_path(run.id), run.model_dump(mode="json"))
        self._write_json(
            self._queue_entry_path(run.id),
            {
                "run_id": run.id,
                "draft_id": draft_id,
                "kind": kind,
                "enqueued_at": run.created_at,
            },
        )
        return run

    def _refuse_while_publishing(self, draft: Draft, verb: str) -> None:
        """409 `publish_run_in_progress` while a publish or unpublish run is
        queued or building for `draft`. Everything the run converts or removes
        (the text, and the attached image set) must stay what it was queued
        against until it has opened its PR or failed, so `save_draft`,
        `attach_image` and `detach_image` all ask this first."""
        running = self.active_publish_run(draft.id)
        if running is not None:
            if running.kind == "unpublish":
                reason = f"draft {draft.id} has an unpublish run {running.status}"
            else:
                reason = (
                    f"draft {draft.id} was approved at version"
                    f" {running.approved_version or draft.version_no} and its"
                    f" publish run is {running.status}"
                )
            raise ApiError(
                409,
                "publish_run_in_progress",
                f"{reason}; it cannot be {verb} until the run has opened its pull request or"
                " failed",
                run_id=running.id,
                run_kind=running.kind,
                approved_version=running.approved_version,
            )

    def active_publish_run(self, draft_id: str) -> Run | None:
        """The draft's publish or unpublish run while it is queued or building,
        else None.

        This is the window between `approve` (or `unpublish`) and the run's PR
        existing, where `get_watch` is still None: neither an open PR nor a
        finished run says the draft is being published, but it is. An
        unpublish run counts for the same reason: it removes the post as the
        draft stands when it is claimed (issue 43).

        Answered from the durable files, never the index (ADR 006): a run whose
        approval wrote its files but died before the index update is still in
        flight, and this is a safety gate. A queue entry lives from `_queue_run`
        until `finish_run`, so the queue (which holds only unfinished runs) is
        the cheap way to find candidates; the run record then decides, since a
        crash between `finish_run`'s record write and its unlink leaves a
        finished run with an entry."""
        for entry in self.queued_entries():
            if entry.get("kind") not in ("publish", "unpublish"):
                continue
            if entry.get("draft_id") != draft_id:
                continue
            try:
                run = self.get_run(str(entry.get("run_id")))
            except ApiError:
                continue
            if run.status in ("queued", "building"):
                return run
        return None

    @locked
    def act_on_draft(
        self,
        draft_id: str,
        action: str,
        actor: str,
        actor_is_ui: bool,
        feedback: str | None = None,
    ) -> tuple[Draft, Run | None]:
        return self._act_on_draft_unlocked(draft_id, action, actor, actor_is_ui, feedback)

    @locked
    def act_on_draft_staged(
        self,
        draft_id: str,
        action: str,
        actor: str,
        actor_is_ui: bool,
        feedback: str | None = None,
        guard: Callable[[Draft], str | None] | None = None,
    ) -> tuple[Draft, Run | None]:
        """`act_on_draft`, first running whatever staging steps
        `transitions.plan_action` says stand between the draft's status and
        `action` (a `revise` before a preview of a published post, a `submit`
        before approving a previewed one). Each step is an ordinary
        transition with its own event and commit, and all of them run under
        one lock so no other writer lands between them. With no plan it is
        exactly `act_on_draft`, refusal included; the `/v1` routes never call
        this, only the editor's own buttons do.

        `guard` is asked about the draft as it stands under this lock, before
        anything is written, and returns why the click must be refused (a 409
        `offer_unavailable`) or None. The editor's offer check runs here rather
        than before the call, so a save landing between the check and the
        action cannot leave the click running against a draft the check never
        saw."""
        draft = self.get_draft(draft_id)
        if guard is not None:
            refusal = guard(draft)
            if refusal is not None:
                raise ApiError(409, "offer_unavailable", refusal)
        steps = plan_action(draft.status, action, actor_is_ui) or (action,)
        if len(steps) > 1 and action in ("preview", "approve") and draft.slug is None:
            # The one refusal the final step can raise that a staging step
            # cannot: find it out before anything is written, on a copy that
            # is never saved, so a refused click leaves the status alone.
            self._pin_slug(draft)
        for step in steps[:-1]:
            self._act_on_draft_unlocked(draft_id, step, actor, actor_is_ui)
        return self._act_on_draft_unlocked(draft_id, action, actor, actor_is_ui, feedback)

    def _act_on_draft_unlocked(
        self,
        draft_id: str,
        action: str,
        actor: str,
        actor_is_ui: bool,
        feedback: str | None = None,
    ) -> tuple[Draft, Run | None]:
        draft = self.get_draft(draft_id)
        transition = resolve_draft(draft.status, action, actor_is_ui)
        if action == "approve" and draft.status == "approved":
            # A re-approve after a failed publish run, or simply approving
            # again: only safe when there is nothing already in flight for
            # this draft, or a second PR/run would race the first.
            watch = self.get_watch(draft_id)
            if watch is not None:
                raise ApiError(
                    409,
                    "publish_pr_open",
                    f"draft {draft_id} already has an open publish PR ({watch.pr_url})",
                    pr_url=watch.pr_url,
                    pr_number=watch.pr_number,
                )
            running = self.active_publish_run(draft_id)
            if running is not None:
                raise ApiError(
                    409,
                    "publish_run_in_progress",
                    f"draft {draft_id} already has a {running.kind} run {running.status}",
                    run_id=running.id,
                )
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

        run = None
        if transition.run_kind:
            # A publish run is queued for exactly the version being approved:
            # the publisher checks it, and `save_draft` refuses new versions
            # while the run is in flight (issue 41).
            pinned = draft.version_no if transition.run_kind == "publish" else None
            run = self._queue_run(draft_id, transition.run_kind, pinned)
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
        return self._put_image_unlocked(raw, filename)

    def _put_image_unlocked(self, raw: bytes, filename: str) -> tuple[Image, bool]:
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

    def image_blob(self, image_id: str) -> Path:
        """The stored bytes of an image, found by id rather than by extension.

        The blob's extension is decided by what Pillow decoded on upload
        (`images.normalise`), not by the filename a draft records, so the
        only reliable way back to the file is the sidecar's neighbours.
        """
        self.get_image(image_id)
        directory = self.images_dir / image_id[:2]
        for path in sorted(directory.glob(f"{image_id}.*")):
            if path.suffix != ".json":
                return path
        raise ApiError(404, "image_blob_missing", f"image {image_id} has a record but no bytes")

    def get_image(self, image_id: str) -> Image:
        # An id becomes part of a path (`images/<id[:2]>/<id>.json`), and callers
        # hand this a URL segment. A malformed id is "no such image", the same
        # answer as an unknown one, so nothing here depends on the web
        # framework's own path handling (a bare `..` is one segment and got
        # through it, issue 47).
        if not self._image_exists(image_id):
            raise ApiError(404, "image_not_found", f"no image {image_id}")
        return Image.model_validate(self._read_json(self._image_sidecar_path(image_id)))

    @locked
    def put_and_attach_image(
        self, draft_id: str, raw: bytes, filename: str, role: str, actor: str
    ) -> tuple[Image, bool]:
        """`put_image` then `attach_image`, as one step that leaves nothing behind.

        The editor's upload. If the attach is refused (a name already on the
        draft, an unknown draft or role) and this call is what created the
        image, its blob, sidecar and index row are removed again, so a refused
        upload does not leave an orphan image record. An image that already
        existed is never touched. An inline reference to an already-stored
        image is refused when its stored name is not one a markdown reference
        can carry (`images.is_plain_filename`): an earlier `/v1` upload or an
        import can have named it anything, and the editor would otherwise
        insert text that does not parse.
        """
        record, created = self._put_image_unlocked(raw, filename)
        try:
            if role == "inline" and not is_plain_filename(record.filename):
                raise ApiError(
                    409,
                    "image_filename_unreferenceable",
                    f"this image is already stored as {record.filename!r}, a name a markdown "
                    "reference cannot carry (spaces, parentheses or other punctuation); "
                    "attach it as the feature image, or upload a different copy",
                )
            self._attach_image_unlocked(draft_id, record.image_id, role, actor)
        except ApiError:
            if created:
                self._discard_image_unlocked(record)
            raise
        return record, created

    def _discard_image_unlocked(self, record: Image) -> None:
        directory = self.images_dir / record.image_id[:2]
        for path in directory.glob(f"{record.image_id}.*"):
            path.unlink()
        self.index.remove_image(record.image_id)

    @locked
    def attach_image(self, draft_id: str, image_id: str, role: str, actor: str) -> Draft:
        return self._attach_image_unlocked(draft_id, image_id, role, actor)

    def _attach_image_unlocked(self, draft_id: str, image_id: str, role: str, actor: str) -> Draft:
        if role not in IMAGE_ROLES:
            raise ApiError(
                422, "image_role_unknown", f"role must be one of {', '.join(IMAGE_ROLES)}"
            )
        draft = self.get_draft(draft_id)
        self._refuse_while_publishing(draft, "given a new image")
        image = self.get_image(image_id)
        conflict = next(
            (
                item
                for item in draft.images
                if item.filename == image.filename and item.image_id != image_id
            ),
            None,
        )
        if conflict is not None:
            # Two attached images with the same output filename but
            # different content (different image_id) would collide once
            # convert.py places them both at static/images/<slug>/<filename>,
            # silently overwriting one with the other (round C3 review).
            raise ApiError(
                409,
                "image_filename_conflict",
                f"draft {draft_id} already has an attached image named {image.filename!r}",
            )
        draft.images = [item for item in draft.images if item.image_id != image_id]
        draft.images.append(DraftImage(image_id=image_id, filename=image.filename, role=role))
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._append_event(type="draft.image_attach", actor=actor, draft_id=draft_id)
        self._commit(f"draft {draft_id}: attach image {image_id} as {role}", actor)
        self.index.upsert_draft(draft)
        return draft

    @locked
    def detach_image(self, draft_id: str, image_id: str, actor: str) -> Draft:
        draft = self.get_draft(draft_id)
        self._refuse_while_publishing(draft, "have an image detached")
        remaining = [item for item in draft.images if item.image_id != image_id]
        if len(remaining) == len(draft.images):
            raise ApiError(
                404, "image_not_attached", f"image {image_id} is not attached to draft {draft_id}"
            )
        draft.images = remaining
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._append_event(type="draft.image_detach", actor=actor, draft_id=draft_id)
        self._commit(f"draft {draft_id}: detach image {image_id}", actor)
        self.index.upsert_draft(draft)
        return draft

    # Posts and runs

    def get_post(self, slug: str) -> Post:
        if not is_valid_slug(slug):
            raise ApiError(404, "post_not_found", f"no post {slug}")
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

    def last_run(self, draft_id: str, kind: str | None = None) -> Run | None:
        run_id = self.index.last_run_id(draft_id, kind)
        return self.get_run(run_id) if run_id else None

    def run_log(self, run_id: str) -> str:
        """A run's captured build output, or empty text until it has one.

        `log_path` is stored relative to the data directory so the record
        stays valid whatever the volume is mounted at in a given container.
        """
        run = self.get_run(run_id)
        if not run.log_path:
            return ""
        path = self.data_dir / run.log_path
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def log_path_for(self, run_id: str) -> Path:
        return self.runs_dir / "logs" / f"{run_id}.log"

    # The run queue: written by the api (`_queue_run`), drained by the builder

    def _queue_entry_path(self, run_id: str) -> Path:
        return self.queue_dir / f"{run_id}.json"

    def queued_entries(self, kind: str | None = None) -> list[dict[str, Any]]:
        """Queue entries oldest first, so a builder drains in enqueue order."""
        entries: list[dict[str, Any]] = []
        if not self.queue_dir.exists():
            return entries
        for path in sorted(self.queue_dir.glob("*.json")):
            try:
                entry = self._read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            if kind is not None and entry.get("kind") != kind:
                continue
            entries.append(entry)
        entries.sort(
            key=lambda entry: (str(entry.get("enqueued_at", "")), str(entry.get("run_id")))
        )
        return entries

    def queue_depth(self, kind: str | None = None) -> int:
        return len(self.queued_entries(kind))

    @locked
    def start_run(
        self,
        run_id: str,
        builder_id: str,
        hugo_version: str,
        toolchain_drift: bool,
        built_version: int | None = None,
    ) -> Run:
        """Move a claimed run to `building` and stamp who is building it.

        `built_version` records the draft's `version_no` at the moment the
        builder read it for this build, so `finish_run` can tell a build
        that is still current from one the draft has since moved past.
        """
        run = self.get_run(run_id)
        run.status = "building"
        run.started_at = now_stamp()
        run.finished_at = None
        run.builder_id = builder_id
        run.hugo_version = hugo_version
        run.toolchain_drift = toolchain_drift
        run.built_version = built_version
        run.log_path = str(self.log_path_for(run_id).relative_to(self.data_dir))
        self._write_json(self._run_path(run_id), run.model_dump(mode="json"))
        self._append_event(
            type="run.started", actor=builder_id, draft_id=run.draft_id, to_status=run.status
        )
        self._commit(f"run {run_id}: building on {builder_id}", builder_id)
        self.index.upsert_run(run)
        return run

    @locked
    def finish_run(
        self,
        run_id: str,
        actor: str,
        succeeded: bool,
        result: dict[str, Any],
    ) -> tuple[Run, Draft | None]:
        """Record the outcome, drop the queue entry, and move the draft if the
        lifecycle says so.

        The draft's status is decided by the transition table and nowhere
        else (AGENTS.md): a succeeded preview asks for `preview_succeeded`,
        and a draft whose status has no entry for that outcome (it was
        rejected, or saved back to `drafting`, while the build ran) is left
        exactly as it is.

        A build only transitions the draft when the version it actually
        built (`run.built_version`, stamped by `start_run`) still matches the
        draft's current version. A draft saved again while the build ran is
        left alone, the run is recorded `succeeded` with `result["stale"]`
        set, and a `draft.preview_stale` event says the draft moved on,
        because the generated preview reflects a version that is no longer
        current (round C3 review).
        """
        run = self.get_run(run_id)
        run.status = "succeeded" if succeeded else "failed"
        run.finished_at = now_stamp()

        draft: Draft | None = None
        stale = False
        if not succeeded and run.kind == "publish":
            current = self.get_draft(run.draft_id)
            transition = resolve_run_outcome(current.status, PUBLISH_RUN_FAILED)
            if transition is not None:
                from_status = current.status
                current.status = transition.to_status
                current.updated_at = run.finished_at
                self._write_json(self._draft_path(current.id), current.model_dump(mode="json"))
                self._append_event(
                    type="draft.publish_failed",
                    actor=actor,
                    draft_id=current.id,
                    from_status=from_status,
                    to_status=current.status,
                )
                self.index.upsert_draft(current)
                draft = current
                self._append_feedback(
                    FeedbackEntry(
                        draft_id=current.id,
                        author="chronicle",
                        created_at=run.finished_at,
                        action="publish_failed",
                        version_no=current.version_no,
                        text=_run_failure_text("Publish", "re-approve to retry", result),
                    )
                )
        if not succeeded and run.kind == "unpublish":
            # No status change: the draft is still `published`. This entry is
            # how its author learns the unpublish never happened.
            current = self.get_draft(run.draft_id)
            self._append_feedback(
                FeedbackEntry(
                    draft_id=current.id,
                    author="chronicle",
                    created_at=run.finished_at,
                    action="unpublish_failed",
                    version_no=current.version_no,
                    text=_run_failure_text("Unpublish", "unpublish again to retry", result),
                )
            )
        if succeeded and run.kind == "preview":
            current = self.get_draft(run.draft_id)
            if run.built_version is not None and current.version_no != run.built_version:
                stale = True
            else:
                transition = resolve_run_outcome(current.status, PREVIEW_SUCCEEDED)
                if transition is not None and transition.to_status != current.status:
                    from_status = current.status
                    current.status = transition.to_status
                    current.updated_at = run.finished_at
                    self._write_json(self._draft_path(current.id), current.model_dump(mode="json"))
                    self._append_event(
                        type=f"draft.{PREVIEW_SUCCEEDED}",
                        actor=actor,
                        draft_id=current.id,
                        from_status=from_status,
                        to_status=current.status,
                    )
                    self.index.upsert_draft(current)
                    draft = current

        run.result = {**result, "stale": True} if stale else result
        self._write_json(self._run_path(run_id), run.model_dump(mode="json"))
        self._queue_entry_path(run_id).unlink(missing_ok=True)

        if stale:
            self._append_event(
                type="draft.preview_stale",
                actor=actor,
                draft_id=run.draft_id,
            )
        self._append_event(
            type=f"run.{run.status}", actor=actor, draft_id=run.draft_id, to_status=run.status
        )
        self._commit(f"run {run_id}: {run.status}", actor)
        self.index.upsert_run(run)
        return run, draft

    @locked
    def requeue_run(self, run_id: str, actor: str, reason: str) -> Run:
        """Put a run a crashed builder left mid-flight back on the queue."""
        run = self.get_run(run_id)
        run.status = "queued"
        run.started_at = None
        run.finished_at = None
        run.builder_id = None
        run.requeued_at = now_stamp()
        self._write_json(self._run_path(run_id), run.model_dump(mode="json"))
        self._write_json(
            self._queue_entry_path(run_id),
            {
                "run_id": run.id,
                "draft_id": run.draft_id,
                "kind": run.kind,
                "enqueued_at": run.created_at,
            },
        )
        self._append_event(
            type="run.requeued", actor=actor, draft_id=run.draft_id, to_status=run.status
        )
        self._commit(f"run {run_id}: requeued ({reason})", actor)
        self.index.upsert_run(run)
        return run

    @locked
    def lease_lost(self, run_id: str, actor: str, reason: str) -> None:
        """A builder finished a build after its lease on this run was taken over.

        The run keeps whatever state its new owner leaves it in; this only
        records that the old builder noticed and discarded its own result
        rather than swapping output or calling `finish_run` (round C3
        review: two builders must never both finish the same run).
        """
        run = self.get_run(run_id)
        self._append_event(
            type="run.lease_lost", actor=actor, draft_id=run.draft_id, to_status=run.status
        )
        self._commit(f"run {run_id}: lease lost, build discarded ({reason})", actor)

    def runs_in_flight(self, kind: str | None = None) -> list[Run]:
        """Runs the index says are `building`, read back from their files."""
        return [self.get_run(run_id) for run_id in self.index.run_ids_with_status("building", kind)]

    def run_counts(self, kind: str) -> dict[str, int]:
        return self.index.run_counts(kind)

    @locked
    def apply_digest(
        self,
        actor: str,
        discovered: list[Post],
        conventions: digest_mod.HugoConventions | None = None,
    ) -> dict[str, int]:
        """Write post records for a digest of main, one commit for the whole run.

        Idempotent by construction: a post record is only written when its
        dump differs from what is already on disk, so a second digest of an
        unchanged main writes nothing, appends no event, and makes no commit
        (`_commit` itself is also a no-op when `git status` is clean, but
        skipping the write and the event too is what makes "zero changes"
        mean zero changes, not zero-byte changes).

        A post that no longer exists on main is left alone: deciding that a
        post was removed is reconciliation's job (ADR 005), not digest's;
        digest only ever adds or updates what main currently has.
        """
        created = 0
        updated = 0
        unchanged = 0
        valid_posts: list[Post] = []
        for post in discovered:
            if not is_valid_slug(post.slug):
                # A slug this unsafe would land outside posts_dir/*.json if
                # written; refuse the one record rather than the whole digest.
                continue
            valid_posts.append(post)
            path = self.posts_dir / f"{post.slug}.json"
            payload = post.model_dump(mode="json")
            if path.exists():
                if self._read_json(path) == payload:
                    unchanged += 1
                    continue
                updated += 1
            else:
                created += 1
            self._write_json(path, payload)
            self.index.upsert_post(post)

        changed = created + updated
        if changed:
            self._append_event(
                type="posts.digested",
                actor=actor,
                to_status=f"{created} created, {updated} updated",
            )
            self._commit(
                f"digest: {created} created, {updated} updated, {unchanged} unchanged", actor
            )

        published_created = self._create_published_drafts_from_digest(
            actor, valid_posts, conventions
        )
        return {
            "created": created,
            "updated": updated,
            "unchanged": unchanged,
            "published_created": published_created,
        }

    def _create_published_drafts_from_digest(
        self,
        actor: str,
        discovered: list[Post],
        conventions: digest_mod.HugoConventions | None = None,
    ) -> int:
        """Land a working record at status `published` for a post digest just
        saw, when nothing already tracks that slug (ADR 017).

        This is what kills the `post_on_main_without_published_draft` reconcile
        loop: digest itself puts a PUBLISHED record in place, so the flag's own
        condition (a post with no published record tracking it) is false from
        the moment digest has seen the post, with nobody clicking "Import as
        draft". Reuses `_fill_from_post` (frontmatter allowlist with warnings,
        body, images, `source_post`) rather than a parallel importer, then
        lands the result at `published` with a `published` dict populated from
        what digest itself observed instead of from a publish run.

        A slug already tracked by a Draft, at ANY status, is left alone: a
        record still `drafting` (or anywhere else) is Scott mid-edit and must
        never be dragged back to `published` by a later digest run; a record
        already `published` keeps whatever `published` dict it already has,
        since comparing it against main for drift is `reconcile.py`'s job
        (`content_drift`), not digest's, unchanged by this method. This is
        what makes a second digest of the same post idempotent: nothing here
        writes a second time once a working record exists at all.

        A post whose import fails (an image_dir collision with another
        draft, most plausibly) is skipped with a warning logged rather than
        aborting the whole digest run over one post.

        Existing unresolved `post_on_main_without_published_draft` flags from
        before this method existed are not touched here: ADR 005 forbids
        reconciliation (or digest) from resolving a flag on its own
        conclusion, so a flag raised for a post that now has a
        digest-created published record simply stops recurring on the next
        reconcile pass; the already-created flag itself stays unresolved
        until Scott resolves it by hand (`ignore` fits every one of these,
        since the condition it flagged is already satisfied; `import_as_draft`
        on the same flag would now try to create a second working record and
        hit the same `image_dir_collision` a duplicate import always would).
        """
        # Not `self.list_drafts()`: it takes `self._lock` itself, and this
        # method only ever runs from inside an already-`@locked` caller
        # (`apply_digest`), so re-acquiring here would deadlock the same
        # non-reentrant lock `_put_image_unlocked` exists to avoid.
        all_drafts = [self.get_draft(did) for did in self.index.draft_ids()]
        tracked_slugs = {draft.slug for draft in all_drafts if draft.slug}
        # A post whose file path some existing draft already tracks (as the
        # draft it was imported/published from, or as what it last actually
        # published) is also left alone, even under a slug that has changed
        # since: `slug_drift` is reconciliation's flag for exactly that
        # mismatch, and digest creating a second working record for the
        # same file under its new slug would just be a duplicate for Scott
        # to clean up by hand.
        tracked_paths = {
            (draft.source_post or {}).get("path") for draft in all_drafts if draft.source_post
        } | {(draft.published or {}).get("post_path") for draft in all_drafts if draft.published}
        static_dir = conventions.staticdir if conventions else None
        static_images_dir = f"{static_dir}/images" if static_dir else convert.STATIC_IMAGES_DIR
        created = 0
        for post in discovered:
            if post.slug in tracked_slugs or post.path in tracked_paths:
                continue
            draft = Draft(id=new_id(), created_at=now_stamp(), updated_at=now_stamp())
            try:
                draft, _warnings = self._fill_from_post(draft, post.slug)
            except ApiError as exc:
                log.warning(
                    "digest: skipping published-record import for post %r: %s",
                    post.slug,
                    exc,
                )
                continue
            draft.status = "published"
            placed = convert.placements(draft, draft.image_dir or post.slug, static_images_dir)
            draft.published = {
                "kind": "digest",
                "branch": None,
                "pr_number": None,
                "pr_url": None,
                "commit_sha": None,
                "post_path": post.path,
                "url": convert.post_url(draft, post.slug),
                "date": post.date,
                "images": [{"path": p.site_path, "url": p.url} for p in placed],
                "post_blob_sha": post.sha,
            }
            self._write_json(self._draft_path(draft.id), draft.model_dump(mode="json"))
            self._append_event(
                type="draft.created",
                actor=actor,
                draft_id=draft.id,
                to_status=draft.status,
            )
            self.index.upsert_draft(draft)
            tracked_slugs.add(post.slug)
            created += 1

        if created:
            self._commit(
                f"digest: {created} post(s) recorded as published working record(s)", actor
            )
        return created

    # Publish and unpublish (spec section 9)

    @locked
    def record_publish_result(
        self,
        draft_id: str,
        actor: str,
        *,
        kind: str,
        branch: str,
        pr_number: int,
        pr_url: str,
        commit_sha: str,
        post_path: str,
        url: str,
        date: str,
        images: list[dict[str, str]],
        post_blob_sha: str,
    ) -> Draft:
        """What a successful publish or unpublish run actually wrote, on the draft.

        Not a save: this never bumps `version_no` or creates a `Version`
        (nothing here is authored content), and it never changes
        `draft.status` (the transition table decides that on the merge
        the watcher later observes, not on the run that opened the PR).
        First publish also stamps `frontmatter["date"]` when the draft had
        none, so a republish and reconciliation's content_drift check both
        have a stable date to work from without recomputing it.
        """
        draft = self.get_draft(draft_id)
        if not draft.frontmatter.get("date"):
            draft.frontmatter = {**draft.frontmatter, "date": date}
        draft.published = {
            "kind": kind,
            "branch": branch,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "commit_sha": commit_sha,
            "post_path": post_path,
            "url": url,
            "date": date,
            "images": images,
            "post_blob_sha": post_blob_sha,
        }
        draft.updated_at = now_stamp()
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._append_event(
            type=f"draft.{kind}_recorded",
            actor=actor,
            draft_id=draft_id,
            to_status=draft.status,
        )
        self._commit(f"draft {draft_id}: recorded {kind} PR #{pr_number}", actor)
        self.index.upsert_draft(draft)
        return draft

    @locked
    def record_github_version(
        self, draft_id: str, frontmatter: dict[str, Any], body: str, message: str
    ) -> Draft:
        """A new version authored `github`: main moved under a published draft.

        Reconciliation's content_drift check (spec section 12), never a
        route a consumer token can reach. Deliberately not `save_draft`:
        this never implies the `revise` transition (a published draft whose
        content Scott edited directly on GitHub is still published; nothing
        here decides otherwise), it is not gated by `base_version` (there is
        no conflict to detect against an edit that already landed on main),
        and it does not run `check_frontmatter`'s allowlist rejection,
        because the frontmatter came from `digest.parse_frontmatter` filtered
        to the allowlist already, the same way `_fill_from_post` handles an
        import.
        """
        draft = self.get_draft(draft_id)
        version = Version(
            draft_id=draft_id,
            version_no=draft.version_no + 1,
            author="github",
            created_at=now_stamp(),
            base_version=draft.version_no,
            message=message,
            frontmatter=frontmatter,
            body=body,
            announcements=dict(draft.announcements),
        )
        self._write_json(
            self._version_path(draft_id, version.version_no), version.model_dump(mode="json")
        )
        draft.frontmatter = frontmatter
        draft.title = str(frontmatter.get("title") or draft.title)
        draft.body = body
        draft.version_no = version.version_no
        draft.updated_at = version.created_at
        self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))
        self._append_event(
            type="draft.github_version",
            actor="github",
            draft_id=draft_id,
            from_status=draft.status,
            to_status=draft.status,
        )
        self._commit(
            f"draft {draft_id}: version {version.version_no} from main (content drift)", "github"
        )
        self.index.upsert_draft(draft)
        self.index.upsert_version(version)
        return draft

    def _find_pr_event(self, draft_id: str, event: str, pr_number: int) -> Event | None:
        """Scan the event log for an already-recorded `draft.pr_{event}`.

        The durable marker `observe_pr_outcome` checks before writing a
        second one on a retry (issue 60 finding 1): the log, not the index,
        is the source of truth here for the same reason `_next_seq` reads
        it directly (a comment just above explains why).
        """
        if not self.events_file.exists():
            return None
        wanted_type = f"draft.pr_{event}"
        with self.events_file.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if (
                    payload.get("type") == wanted_type
                    and payload.get("draft_id") == draft_id
                    and payload.get("pr_number") == pr_number
                ):
                    return Event.model_validate(payload)
        return None

    def _find_pr_closed_feedback(self, draft_id: str, pr_number: int) -> FeedbackEntry | None:
        for entry in self.list_feedback(draft_id):
            if entry.action == "pr_closed" and entry.pr_number == pr_number:
                return entry
        return None

    @locked
    def observe_pr_outcome(
        self, draft_id: str, event: str, pr_number: int, actor: str = "github"
    ) -> Draft:
        """A Chronicle-opened PR merged or closed (spec section 9's merge watch).

        `event` is `"merged"` or `"closed"`; `resolve_watch` decides the
        status change from the draft's current status, and a status this
        table has no entry for (a draft revised again while its PR was
        still open) is left exactly as it is. A close without a merge also
        gets a `github`-authored feedback entry, the same record shape a
        `request_revision` or `reject` writes, so it shows up in the
        draft's history the same way.

        Idempotent end to end (issue 60 finding 1): a retry that reaches
        this method again, whether an earlier attempt got as far as
        writing `draft.status` or not, must still complete whichever of
        the feedback entry, the event, the commit, and the index upsert
        did not land the first time. `draft.status` alone cannot be that
        check: it is this method's own first mutating step, so a bare
        status comparison cannot tell a fully completed observation from
        one that crashed right after it, and re-deriving the transition
        through `resolve_watch` off an already-moved status is exactly the
        ambiguity `_handle_merged`'s `already_observed` guard exists to
        avoid (`WATCH_TRANSITIONS` has a second, unrelated entry keyed on
        the post-transition status; round C4/issue 60 P1's finding). So
        `to_status` is decided once, from the durable event record when
        one already exists and from `resolve_watch` only when it does not,
        and `draft.status` is written last, gated on whether it already
        matches rather than on whether this call has run before.
        """
        draft = self.get_draft(draft_id)
        existing_event = self._find_pr_event(draft_id, event, pr_number)
        if existing_event is not None:
            to_status = existing_event.to_status or draft.status
        else:
            transition = resolve_watch(draft.status, event)
            to_status = transition.to_status if transition is not None else draft.status

        if event == "closed" and self._find_pr_closed_feedback(draft_id, pr_number) is None:
            # Spec section 9: the feedback entry is authored `github`, not
            # whatever process actor observed it (the watcher), the same
            # way a `github`-authored version (`record_github_version`) is
            # attributed to where the change actually came from.
            self._append_feedback(
                FeedbackEntry(
                    draft_id=draft_id,
                    author="github",
                    created_at=now_stamp(),
                    action="pr_closed",
                    version_no=draft.version_no,
                    text=f"PR #{pr_number} was closed on GitHub without merging.",
                    pr_number=pr_number,
                )
            )

        if existing_event is None:
            self._append_event(
                type=f"draft.pr_{event}",
                actor=actor,
                draft_id=draft_id,
                from_status=draft.status,
                to_status=to_status,
                pr_number=pr_number,
            )

        if draft.status != to_status:
            draft.status = to_status
            draft.updated_at = now_stamp()
            self._write_json(self._draft_path(draft_id), draft.model_dump(mode="json"))

        self._commit(f"draft {draft_id}: PR #{pr_number} {event}", actor)
        self.index.upsert_draft(draft)
        return draft

    @locked
    def remove_post(self, slug: str, actor: str) -> None:
        """Drop a post record once the watcher has directly confirmed it is gone.

        Digest never deletes a post record (AGENTS.md): a post missing from
        a digest might just be a transient clone problem, and deciding it
        was really removed is reconciliation's job. This method exists for
        the one case that is not a heuristic: the watcher observing its own
        `unpublish` PR merge, where Chronicle itself caused the file's
        removal and knows so with certainty. Reconciliation's own
        `post_removed_without_unpublish` flag is exactly the heuristic case
        this bypasses; the two never call each other.
        """
        path = self.posts_dir / f"{slug}.json"
        if not path.exists():
            return
        path.unlink()
        self._append_event(type="posts.removed", actor=actor, to_status=slug)
        self._commit(f"posts: removed {slug} (unpublish merged)", actor)
        self.index.remove_post(slug)

    # Watch (spec section 9's merge watch, ADR 013)

    def _watch_path(self, draft_id: str) -> Path:
        return self.watch_dir / f"{draft_id}.json"

    @locked
    def record_watch(self, entry: WatchEntry, actor: str) -> None:
        self._write_json(self._watch_path(entry.draft_id), entry.model_dump(mode="json"))
        self._commit(f"watch: draft {entry.draft_id} PR #{entry.pr_number} ({entry.kind})", actor)

    def get_watch(self, draft_id: str) -> WatchEntry | None:
        path = self._watch_path(draft_id)
        if not path.exists():
            return None
        return WatchEntry.model_validate(self._read_json(path))

    def list_watches(self) -> list[WatchEntry]:
        if not self.watch_dir.exists():
            return []
        return [
            WatchEntry.model_validate(self._read_json(path))
            for path in sorted(self.watch_dir.glob("*.json"))
        ]

    @locked
    def clear_watch(self, draft_id: str, actor: str, reason: str) -> None:
        path = self._watch_path(draft_id)
        if not path.exists():
            return
        path.unlink()
        self._commit(f"watch: draft {draft_id} cleared ({reason})", actor)

    # Reconciliation (spec section 12, ADR 005: flags only, never a correction)

    def _flag_path(self, flag_id: str) -> Path:
        return self.reconcile_dir / f"{flag_id}.json"

    def _create_flag_unlocked(
        self,
        flag_type: str,
        *,
        slug: str | None,
        draft_id: str | None,
        detail: str,
        actor: str,
        main_sha: str | None = None,
        built_version: int | None = None,
        current_version: int | None = None,
    ) -> ReconcileFlag:
        """Split out so `record_publish_behind_draft` (already `@locked`) can
        reuse it, the same reason `_put_image_unlocked` exists."""
        flag = ReconcileFlag(
            id=new_id(),
            type=flag_type,
            created_at=now_stamp(),
            slug=slug,
            draft_id=draft_id,
            detail=detail,
            main_sha=main_sha,
            built_version=built_version,
            current_version=current_version,
        )
        self._write_json(self._flag_path(flag.id), flag.model_dump(mode="json"))
        self._append_event(
            type="reconcile.flagged", actor=actor, draft_id=draft_id, to_status=flag_type
        )
        self._commit(f"reconcile: flagged {flag_type} ({slug or draft_id})", actor)
        return flag

    @locked
    def create_flag(
        self,
        flag_type: str,
        *,
        slug: str | None,
        draft_id: str | None,
        detail: str,
        actor: str,
        main_sha: str | None = None,
    ) -> ReconcileFlag:
        return self._create_flag_unlocked(
            flag_type, slug=slug, draft_id=draft_id, detail=detail, actor=actor, main_sha=main_sha
        )

    @locked
    def record_publish_behind_draft(
        self, draft_id: str, actor: str, *, built_version: int, current_version: int
    ) -> None:
        """A publish PR merged after the draft was revised again while it was
        open (round C4 review, P1): the merged content is what the run
        actually converted at `built_version`, not the draft's current,
        newer version, so this is surfaced as a `content_drift`-shaped flag
        (reused rather than a new flag type, per the review's own
        suggestion) plus a `chronicle`-authored feedback entry, without
        touching `draft.status` (the watcher's own WATCH_TRANSITIONS call
        still decides that).

        A retry of `_handle_merged` after this call already landed but a
        later step (`observe_pr_outcome`, `clear_watch`) failed reaches this
        method again before `draft.status` has moved, so the same guard
        `reconcile.py`'s `_already_flagged` uses for the other flag types
        applies here too, keyed to the individual publish rather than any
        open flag for the draft (issue 60 finding 2): an unresolved
        `content_drift` flag already open for this draft, with the same
        `built_version`/`current_version` pair this call carries, means
        this exact merge was already recorded, and a second one would
        double the flag and the feedback entry for one event. A *different*
        pair (a later, distinct publish-behind merge for the same draft
        while an earlier one's flag is still unresolved) is not the same
        event and must still get its own flag, which matching on
        `draft_id` alone used to suppress. Matched on `slug=None` as well,
        because `reconcile.py`'s own `content_drift` flag for the same
        draft always carries its `slug` (it only runs once the draft is
        `published`, which this method's caller never is yet); without that
        an old, still-unresolved reconcile flag from a previous publish
        cycle would silently suppress a genuinely new one here. A flag
        written before this field existed carries `built_version=None`,
        which never equals this call's own (always-set) integer, so it is
        left alone rather than mistaken for a match.
        """
        already_flagged = any(
            flag.type == "content_drift"
            and flag.draft_id == draft_id
            and flag.slug is None
            and flag.built_version == built_version
            and flag.current_version == current_version
            for flag in self.list_flags(resolved=False)
        )
        if already_flagged:
            return
        detail = (
            f"draft {draft_id} was revised to version {current_version} while its publish PR"
            f" was open; the PR that merged only carried version {built_version}"
        )
        self._create_flag_unlocked(
            "content_drift",
            slug=None,
            draft_id=draft_id,
            detail=detail,
            actor=actor,
            built_version=built_version,
            current_version=current_version,
        )
        self._append_feedback(
            FeedbackEntry(
                draft_id=draft_id,
                author="chronicle",
                created_at=now_stamp(),
                action="published_behind_draft",
                version_no=current_version,
                text=(
                    f"Published PR merged with version {built_version}, but the draft had"
                    f" already moved on to version {current_version} before it merged."
                ),
            )
        )

    def get_flag(self, flag_id: str) -> ReconcileFlag:
        path = self._flag_path(flag_id)
        if not path.exists():
            raise ApiError(404, "flag_not_found", f"no reconciliation flag {flag_id}")
        return ReconcileFlag.model_validate(self._read_json(path))

    def list_flags(self, resolved: bool | None = None) -> list[ReconcileFlag]:
        if not self.reconcile_dir.exists():
            return []
        flags = [
            ReconcileFlag.model_validate(self._read_json(path))
            for path in sorted(self.reconcile_dir.glob("*.json"))
        ]
        if resolved is not None:
            flags = [flag for flag in flags if flag.resolved == resolved]
        return flags

    @locked
    def resolve_flag(self, flag_id: str, resolution: str, actor: str) -> ReconcileFlag:
        flag = self.get_flag(flag_id)
        if flag.resolved:
            raise ApiError(409, "flag_already_resolved", f"flag {flag_id} is already resolved")

        # Deferred import: `reconcile.py` imports `Store` at module level, so
        # a module-level import here would be circular; `APPLICABLE_RESOLUTIONS`
        # itself never changes at runtime, so resolving it lazily costs nothing.
        from .reconcile import APPLICABLE_RESOLUTIONS

        applicable = APPLICABLE_RESOLUTIONS.get(flag.type, ("ignore",))
        if resolution not in applicable:
            raise ApiError(
                422,
                "resolution_not_applicable",
                f"resolution {resolution!r} is not applicable to flag type {flag.type!r}"
                f" (applicable: {', '.join(applicable)})",
                flag_type=flag.type,
                resolution=resolution,
            )

        if flag.type == "content_drift" and resolution == "ignore" and flag.draft_id:
            # The next reconcile run must not re-flag or re-version the exact
            # content just acknowledged; a genuinely new change on main still
            # differs from this sha and flags again (round C4 review, P2).
            draft = self.get_draft(flag.draft_id)
            published = dict(draft.published or {})
            published["acknowledged_blob_sha"] = flag.main_sha
            draft.published = published
            draft.updated_at = now_stamp()
            self._write_json(self._draft_path(draft.id), draft.model_dump(mode="json"))
            self.index.upsert_draft(draft)

        new_status = RECONCILE_STATUS.get(resolution)
        if new_status is not None:
            if flag.draft_id is None:
                raise ApiError(
                    422,
                    "resolution_not_applicable",
                    f"flag {flag_id} has no draft to set to {new_status!r}",
                )
            draft = self.get_draft(flag.draft_id)
            from_status = draft.status
            draft.status = new_status
            draft.updated_at = now_stamp()
            self._write_json(self._draft_path(draft.id), draft.model_dump(mode="json"))
            self._append_event(
                type="draft.reconcile_resolved",
                actor=actor,
                draft_id=draft.id,
                from_status=from_status,
                to_status=new_status,
            )
            self.index.upsert_draft(draft)
        elif resolution == "import_as_draft":
            if not flag.slug:
                raise ApiError(
                    422, "resolution_not_applicable", f"flag {flag_id} has no post slug to import"
                )
            self._create_draft_unlocked(actor, from_post=flag.slug)
        elif resolution == "ignore":
            pass
        else:
            raise ApiError(422, "resolution_unknown", f"{resolution!r} is not a known resolution")

        flag.resolved = True
        flag.resolution = resolution
        flag.resolved_at = now_stamp()
        flag.resolved_by = actor
        self._write_json(self._flag_path(flag.id), flag.model_dump(mode="json"))
        self._append_event(
            type="reconcile.resolved", actor=actor, draft_id=flag.draft_id, to_status=resolution
        )
        self._commit(f"reconcile: resolved flag {flag.id} ({resolution})", actor)
        return flag

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


def _run_failure_text(what: str, retry: str, result: dict[str, Any]) -> str:
    """The feedback line for a failed publish or unpublish run: the machine
    class, then the run's own plain-language `message` when it gave one."""
    error_class = str(result.get("error_class", "unknown"))
    message = str(result.get("message") or "").strip()
    detail = f": {message}" if message else ""
    return f"{what} failed ({error_class}){detail}; {retry}."


def _clean_material_text(text: str) -> str:
    """A submitter's text as the seeding heuristics should see it: no byte
    order mark and LF line endings, or a CRLF post's frontmatter fence would
    never match and the whole block would land in the body unannounced."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n")


def _primary_material(materials: list[Material]) -> Material | None:
    """The material a draft body is seeded from: the first with a frontmatter
    block, else the first that opens with a heading, else the first with any
    text at all, else None. A frontmatter block outranks a heading because a
    run log or script that merely starts with `# ` is common, and a real post
    with frontmatter must not be demoted behind it."""
    with_text = [
        (material, _clean_material_text(material.text)) for material in materials if material.text
    ]
    for pattern in (_FRONTMATTER_BLOCK, _LEADING_HEADING):
        for material, text in with_text:
            if pattern.match(text):
                return material
    return with_text[0][0] if with_text else None


def _split_material(text: str) -> tuple[dict[str, Any] | None, str, str | None]:
    """(frontmatter, body, warning). Frontmatter is None when the text has no
    block, or a block that does not parse; then the whole text stays the body
    and the warning says why, rather than losing the writing to a 500."""
    try:
        frontmatter, body = digest_mod.parse_frontmatter(text)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        return None, text, f"frontmatter did not parse ({exc}); kept the whole text as the body"
    if body == text and not frontmatter:
        return None, text, None
    return frontmatter, body, None


def _frontmatter_type_problem(key: str, value: Any) -> str | None:
    """What `key` must be when `value` is the wrong type, else None. The one
    definition of the type rules, shared by the save path (which 422s) and
    the submission seed (which drops the key and warns)."""
    if key in FRONTMATTER_STRING_KEYS and not isinstance(value, str):
        return "a string"
    if key in FRONTMATTER_STRING_LIST_KEYS and (
        not isinstance(value, list) or not all(isinstance(item, str) for item in value)
    ):
        return "a list of strings"
    if key == "draft" and not isinstance(value, bool):
        return "true or false"
    return None


def check_announcements(announcements: dict[str, Any]) -> None:
    """Exactly the fixed channel keys, each holding plain text (ADR 021).

    A channel may be absent, but no other key is accepted and no value type
    other than a string is, so what reaches the record is always a mapping the
    edit page can render into a textarea without guessing.
    """
    unknown = sorted(key for key in announcements if key not in ANNOUNCEMENT_CHANNELS)
    if unknown:
        raise ApiError(
            422,
            "announcement_channel_not_allowed",
            f"announcement channels not allowed: {', '.join(unknown)}",
            unknown_channels=unknown,
            allowed_channels=list(ANNOUNCEMENT_CHANNELS),
        )
    wrong = sorted(key for key, value in announcements.items() if not isinstance(value, str))
    if wrong:
        raise ApiError(
            422,
            "announcement_wrong_type",
            f"announcement text must be a string: {', '.join(wrong)}",
            channels=wrong,
        )


def check_frontmatter(frontmatter: dict[str, Any], current_url: Any = None) -> None:
    unknown = sorted(key for key in frontmatter if key not in FRONTMATTER_ALLOWLIST)
    if unknown:
        raise ApiError(
            422,
            "frontmatter_key_not_allowed",
            f"frontmatter keys not in the allowlist: {', '.join(unknown)}",
            unknown_keys=unknown,
            allowed_keys=list(FRONTMATTER_ALLOWLIST),
        )
    for key, value in frontmatter.items():
        expected = _frontmatter_type_problem(key, value)
        if expected is not None:
            raise _wrong_type(key, expected)

    # A url the draft already carries is never refused again: an import keeps
    # what main has, and an older save may hold one, and the editor re-sends the
    # stored value with every save. It is harmless because the image directory
    # falls back to the slug; only a url being newly set or changed is judged.
    url = frontmatter.get("url")
    url_problem = None if url == current_url else convert.url_problem(url)
    if url_problem is not None:
        # ADR 015: the url's last segment names the image directory, so a
        # segment that cannot be a directory name is refused here, not later
        # at preview or publish (issue 28).
        raise ApiError(
            422,
            "frontmatter_url_invalid",
            f"frontmatter.url cannot be used: {url_problem}",
            url=frontmatter.get("url"),
        )

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
