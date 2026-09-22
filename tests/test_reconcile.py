"""reconcile.py: every flag type from fixtures, and every resolution action."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import cast

import pytest

from chronicle.api import digest as digest_mod
from chronicle.api import publisher, reconcile, watcher
from chronicle.api.admin_deps import AdminServices
from chronicle.api.errors import ApiError
from chronicle.api.models import Draft, Post, ReconcileFlag
from chronicle.api.store import Store
from tests.fakes import FakeRepoOps

# `reconcile.build_repo_target` is monkeypatched in every test here, so the
# real AdminServices this parameter would otherwise need is never touched.
_ADMIN = cast(AdminServices, None)

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.com",
    "GIT_COMMITTER_NAME": "fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.com",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], env=_GIT_ENV, check=True, capture_output=True, text=True
    )


def _target() -> tuple[publisher.RepoTarget, FakeRepoOps]:
    ops = FakeRepoOps()
    target = publisher.RepoTarget(
        ops=ops, owner="o", repo="r", default_branch="main", token_provider=lambda: "tok"
    )
    return target, ops


def _write_post(
    site_dir: Path, path: str, *, slug: str | None = None, title: str = "T", body: str = "hi\n"
) -> None:
    full = site_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    fm = f"title: {title}\ndate: '2026-01-01T00:00:00-06:00'\n"
    if slug:
        fm += f"slug: {slug}\n"
    full.write_text(f"---\n{fm}---\n{body}", encoding="utf-8")
    _git(site_dir, "add", "-A")
    _git(site_dir, "commit", "-m", f"post: {path}")


def _fake_refresh(
    store: Store, target: publisher.RepoTarget, actor: str, admin: AdminServices | None = None
) -> None:
    """`digest_runner.refresh_from_target` minus the clone/fetch: the test
    fixture already writes straight into `store.site_dir`'s own git repo,
    so there is no separate remote to pull from (`test_digest.py` is where
    the actual clone/fetch path is exercised)."""
    discovered = digest_mod.discover_posts(store.site_dir)
    posts = [
        Post(slug=item.slug, path=item.path, title=item.title, date=item.date, sha=item.sha)
        for item in discovered
    ]
    store.apply_digest(actor, posts)


@pytest.fixture(autouse=True)
def _fake_repo(monkeypatch: pytest.MonkeyPatch) -> publisher.RepoTarget:
    target, ops = _target()
    monkeypatch.setattr(reconcile, "build_repo_target", lambda admin: target)
    monkeypatch.setattr(reconcile, "refresh_from_target", _fake_refresh)
    monkeypatch.setattr(watcher, "refresh_from_target", _fake_refresh)
    return target


def _approved_and_published(store: Store, target: publisher.RepoTarget, ops: FakeRepoOps) -> Draft:
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "A Post"}, "Body.\n")
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)  # refresh_from_target is faked above
    return store.get_draft(draft.id)


def _init_site(store: Store) -> None:
    (store.site_dir / "content" / "posts").mkdir(parents=True, exist_ok=True)
    _git(store.site_dir, "init", "--initial-branch=main")
    (store.site_dir / ".gitkeep").write_text("", encoding="utf-8")
    _git(store.site_dir, "add", "-A")
    _git(store.site_dir, "commit", "-m", "init")


def test_post_on_main_without_published_draft_is_never_raised_for_a_digested_post(
    store: Store,
) -> None:
    """The regression ADR 017 fixes: digest itself lands a published working
    record for a post it finds on main, so `post_on_main_without_published_draft`'s
    own condition (a post with nothing tracking it) is never true, and stays
    that way on a later reconcile pass, without Scott clicking anything."""
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")

    summary = reconcile.run(store, _ADMIN)
    flags = store.list_flags()
    assert not any(
        f.type == "post_on_main_without_published_draft" and f.slug == "orphan" for f in flags
    )
    assert summary.flags_created == 0
    tracked = [d for d in store.list_drafts() if d.slug == "orphan"]
    assert len(tracked) == 1
    assert tracked[0].status == "published"
    assert tracked[0].published is not None
    assert tracked[0].published["post_blob_sha"]

    # A second reconcile pass an hour later must not re-raise it either.
    reconcile.run(store, _ADMIN)
    flags_again = store.list_flags()
    assert not any(
        f.type == "post_on_main_without_published_draft" and f.slug == "orphan" for f in flags_again
    )


def test_draft_published_missing_on_main(store: Store) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    # Nothing written to store.site_dir for this slug this time: main no longer has it.
    reconcile.run(store, _ADMIN)
    flags = store.list_flags()
    assert any(
        f.type == "draft_published_missing_on_main" and f.draft_id == draft.id for f in flags
    )


def test_post_removed_without_unpublish(store: Store) -> None:
    _init_site(store)
    store.apply_digest(
        "test",
        [
            Post(
                slug="gone",
                path="content/posts/gone.md",
                title="Gone",
                date="2026-01-01",
                sha="abc",
            )
        ],
    )
    # No draft ever tracked "gone", and it is not on main any more.
    reconcile.run(store, _ADMIN)
    flags = store.list_flags()
    assert any(f.type == "post_removed_without_unpublish" and f.slug == "gone" for f in flags)


def test_slug_drift(store: Store) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    assert draft.published is not None
    post_path = draft.published["post_path"]
    # Scott renamed the slug directly on GitHub: same file path, new slug.
    _write_post(store.site_dir, post_path, slug=f"{draft.slug}-renamed", title=draft.title)
    reconcile.run(store, _ADMIN)
    flags = store.list_flags()
    assert any(f.type == "slug_drift" and f.draft_id == draft.id for f in flags)


def test_content_drift_records_a_github_authored_version(store: Store) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    assert draft.published is not None
    post_path = draft.published["post_path"]
    version_before = store.get_draft(draft.id).version_no
    _write_post(
        store.site_dir, post_path, slug=draft.slug, title=draft.title, body="Edited on GitHub.\n"
    )

    reconcile.run(store, _ADMIN)

    flags = store.list_flags()
    assert any(f.type == "content_drift" and f.draft_id == draft.id for f in flags)
    updated = store.get_draft(draft.id)
    assert updated.version_no == version_before + 1
    assert updated.status == "published", "content_drift must never change status"
    version = store.get_version(draft.id, updated.version_no)
    assert version.author == "github"
    assert "Edited on GitHub." in version.body


def test_resolve_mark_unpublished(store: Store) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "draft_published_missing_on_main")

    resolved = store.resolve_flag(flag.id, "mark_unpublished", "admin")

    assert resolved.resolved is True
    assert resolved.resolution == "mark_unpublished"
    assert store.get_draft(draft.id).status == "unpublished"


def _fabricate_post_on_main_without_published_draft_flag(store: Store, slug: str) -> ReconcileFlag:
    """A `post_on_main_without_published_draft` flag, raised directly rather
    than through `reconcile.run`.

    Since ADR 017, digest itself lands a published working record for any
    post it can actually read from main, so `reconcile.run` only raises this
    flag type when digest's own attempt failed (an image_dir collision,
    most plausibly) or, for an instance already carrying flags from before
    this round, one that predates the fix entirely. Fabricating the flag
    directly, the way an already-existing flag on the live instance looks,
    is what lets these tests still exercise `resolve_flag`'s generic
    mechanics for this flag type without needing to reproduce a live
    digest failure.
    """
    return store.create_flag(
        "post_on_main_without_published_draft",
        slug=slug,
        draft_id=None,
        detail=f"post {slug!r} is on main with no published draft tracking it",
        actor="test",
    )


def test_resolve_import_as_draft(store: Store) -> None:
    """A pre-existing flag (from before ADR 017, or from a digest attempt
    that failed) resolved by hand once the post's file is actually there."""
    _init_site(store)
    store.apply_digest(
        "test",
        [
            Post(
                slug="orphan",
                path="content/posts/orphan.md",
                title="Orphan",
                date="2026-01-01",
                sha="abc",
            )
        ],
    )
    flag = _fabricate_post_on_main_without_published_draft_flag(store, "orphan")
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")

    store.resolve_flag(flag.id, "import_as_draft", "admin")

    imported = [d for d in store.list_drafts() if d.slug == "orphan"]
    assert len(imported) == 1
    assert imported[0].status == "drafting"


def test_resolve_ignore_emits_event_and_leaves_state_alone(store: Store) -> None:
    _init_site(store)
    flag = _fabricate_post_on_main_without_published_draft_flag(store, "orphan")

    events_before, cursor = store.events_since(0)
    resolved = store.resolve_flag(flag.id, "ignore", "admin")
    events_after, _ = store.events_since(cursor)

    assert resolved.resolution == "ignore"
    assert any(event.type == "reconcile.resolved" for event in events_after)
    assert store.list_drafts() == []


def test_resolving_an_already_resolved_flag_is_rejected(store: Store) -> None:
    _init_site(store)
    flag = _fabricate_post_on_main_without_published_draft_flag(store, "orphan")
    store.resolve_flag(flag.id, "ignore", "admin")

    with pytest.raises(ApiError):
        store.resolve_flag(flag.id, "ignore", "admin")


def test_resolve_flag_rejects_a_resolution_not_applicable_to_the_flag_type(store: Store) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    assert draft.published is not None
    _write_post(
        store.site_dir,
        draft.published["post_path"],
        slug=draft.slug,
        title=draft.title,
        body="Edited on GitHub.\n",
    )
    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "content_drift")

    with pytest.raises(ApiError) as excinfo:
        store.resolve_flag(flag.id, "mark_unpublished", "admin")
    assert excinfo.value.status_code == 422

    fresh = store.get_flag(flag.id)
    assert fresh.resolved is False
    assert store.get_draft(draft.id).status == "published", "a rejected resolution must not act"


def test_ignoring_content_drift_records_the_acknowledged_sha_and_stops_reflagging(
    store: Store,
) -> None:
    _init_site(store)
    target, ops = _target()
    draft = _approved_and_published(store, target, ops)
    assert draft.published is not None
    post_path = draft.published["post_path"]
    _write_post(
        store.site_dir, post_path, slug=draft.slug, title=draft.title, body="Edited on GitHub.\n"
    )

    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "content_drift")
    assert flag.main_sha is not None
    version_after_first_run = store.get_draft(draft.id).version_no

    store.resolve_flag(flag.id, "ignore", "admin")
    published = store.get_draft(draft.id).published
    assert published is not None
    assert published["acknowledged_blob_sha"] == flag.main_sha

    # Rerun: same content on main, must not re-flag or write another version.
    reconcile.run(store, _ADMIN)
    open_flags = [f for f in store.list_flags(resolved=False) if f.type == "content_drift"]
    assert open_flags == []
    assert store.get_draft(draft.id).version_no == version_after_first_run

    # Main changes again: a genuinely new sha must still flag.
    _write_post(
        store.site_dir, post_path, slug=draft.slug, title=draft.title, body="Edited again.\n"
    )
    reconcile.run(store, _ADMIN)
    open_flags_again = [f for f in store.list_flags(resolved=False) if f.type == "content_drift"]
    assert len(open_flags_again) == 1
    assert store.get_draft(draft.id).version_no == version_after_first_run + 1


def test_reconcile_does_not_duplicate_flags_across_runs(store: Store) -> None:
    """`_already_flagged`'s dedup is generic across flag types; exercised
    here through `post_removed_without_unpublish`, since ADR 017's own
    digest-creates-the-record fix makes `post_on_main_without_published_draft`
    the one flag type that no longer fires for a post digest can actually
    read (see the dedicated regression test above)."""
    _init_site(store)
    store.apply_digest(
        "test",
        [Post(slug="gone", path="content/posts/gone.md", title="Gone", date="2026-01-01", sha="a")],
    )
    reconcile.run(store, _ADMIN)
    reconcile.run(store, _ADMIN)
    flags = [f for f in store.list_flags() if f.type == "post_removed_without_unpublish"]
    assert len(flags) == 1


def test_content_drift_compares_against_a_digest_created_post_blob_sha(store: Store) -> None:
    """A working record digest landed directly at `published` (ADR 017)
    gives `content_drift` a real `post_blob_sha` to diff against, the same
    as a record a publish run created."""
    _init_site(store)
    _write_post(store.site_dir, "content/posts/hello.md", title="Hello")
    reconcile.run(store, _ADMIN)
    draft = next(d for d in store.list_drafts() if d.slug == "hello")
    assert draft.published is not None
    assert draft.published["post_blob_sha"]

    _write_post(store.site_dir, "content/posts/hello.md", title="Hello", body="Edited on GitHub.\n")
    version_before = store.get_draft(draft.id).version_no

    reconcile.run(store, _ADMIN)

    flags = store.list_flags()
    assert any(f.type == "content_drift" and f.draft_id == draft.id for f in flags)
    updated = store.get_draft(draft.id)
    assert updated.version_no == version_before + 1
    assert updated.status == "published"


def test_publisher_treats_a_digest_created_record_as_an_update(store: Store) -> None:
    """`draft.published` set directly by digest (ADR 017) is exactly what
    `publisher.py` already checks to pick `update` over `publish`; this
    confirms that check fires for a digest-created record too, not only one
    a publish run created."""
    _init_site(store)
    _write_post(store.site_dir, "content/posts/hello.md", title="Hello")
    reconcile.run(store, _ADMIN)
    draft = next(d for d in store.list_drafts() if d.slug == "hello")
    assert draft.published is not None

    store.save_draft(
        draft.id, "scott", draft.version_no, {**draft.frontmatter, "title": "Hello v2"}, "New.\n"
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    _, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None

    target, ops = _target()
    publisher.run_one(store, target, run)

    commit_messages = [commit["message"] for commit in ops.commits.values() if "message" in commit]
    assert any(msg.startswith("chronicle: update hello") for msg in commit_messages)
    assert not any(msg.startswith("chronicle: publish hello") for msg in commit_messages)


def test_run_once_logged_shows_gits_stderr_not_just_the_exit_code(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #57's second ask: the log only showed
    `returned non-zero exit status 128`, never git's own error. This drives
    a real git failure (cloning a repo url that does not exist) through
    `reconcile.run_once_logged` exactly as the hourly loop would hit one,
    and checks git's real stderr line reaches the log, not only the
    generic subprocess message.
    """

    def _refresh_against_a_bad_url(
        store_: Store, target: publisher.RepoTarget, actor: str, admin: AdminServices | None = None
    ) -> None:
        digest_mod.clone_or_update(store_.site_dir, "file:///no/such/repo/on/this/host", "main")

    monkeypatch.setattr(reconcile, "refresh_from_target", _refresh_against_a_bad_url)
    caplog.set_level(logging.WARNING)

    reconcile.run_once_logged(store, _ADMIN)

    assert "reconcile: run failed" in caplog.text
    assert "returned non-zero exit status" in caplog.text
    assert "fatal" in caplog.text.lower() or "does not exist" in caplog.text.lower()


def test_run_once_logged_treats_a_refused_stale_lock_as_an_expected_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`digest.DigestError` (raised when `clone_or_update` refuses a lock
    that is not yet old enough to call stale) must land in the same
    "will retry next trigger" path as a plain git failure, not the
    `_run_once_never_raises` catch-all for a genuinely unexpected bug.
    Adversarial review of this change found `DigestError` missing from
    `run_once_logged`'s except tuple, which would have logged a stale-lock
    refusal as "unexpected failure" instead.
    """

    def _refresh_that_hits_a_fresh_lock(
        store_: Store, target: publisher.RepoTarget, actor: str, admin: AdminServices | None = None
    ) -> None:
        raise digest_mod.DigestError("data/site/.git/index.lock exists and is 4s old")

    monkeypatch.setattr(reconcile, "refresh_from_target", _refresh_that_hits_a_fresh_lock)
    caplog.set_level(logging.WARNING)

    reconcile.run_once_logged(store, _ADMIN)

    assert "reconcile: run failed, will retry next trigger" in caplog.text
    assert "unexpected failure" not in caplog.text
