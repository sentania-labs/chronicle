"""reconcile.py: every flag type from fixtures, and every resolution action."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from chronicle.api import digest as digest_mod
from chronicle.api import publisher, reconcile, watcher
from chronicle.api.admin_deps import AdminServices
from chronicle.api.errors import ApiError
from chronicle.api.models import Draft, Post
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


def _fake_refresh(store: Store, target: publisher.RepoTarget, actor: str) -> None:
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


def test_post_on_main_without_published_draft(store: Store) -> None:
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")
    summary = reconcile.run(store, _ADMIN)
    flags = store.list_flags()
    assert any(
        f.type == "post_on_main_without_published_draft" and f.slug == "orphan" for f in flags
    )
    assert summary.flags_created >= 1


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


def test_resolve_import_as_draft(store: Store) -> None:
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")
    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "post_on_main_without_published_draft")

    store.resolve_flag(flag.id, "import_as_draft", "admin")

    imported = [d for d in store.list_drafts() if d.slug == "orphan"]
    assert len(imported) == 1


def test_resolve_ignore_emits_event_and_leaves_state_alone(store: Store) -> None:
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")
    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "post_on_main_without_published_draft")

    events_before, cursor = store.events_since(0)
    resolved = store.resolve_flag(flag.id, "ignore", "admin")
    events_after, _ = store.events_since(cursor)

    assert resolved.resolution == "ignore"
    assert any(event.type == "reconcile.resolved" for event in events_after)
    assert store.list_drafts() == []


def test_resolving_an_already_resolved_flag_is_rejected(store: Store) -> None:
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")
    reconcile.run(store, _ADMIN)
    flag = next(f for f in store.list_flags() if f.type == "post_on_main_without_published_draft")
    store.resolve_flag(flag.id, "ignore", "admin")

    with pytest.raises(ApiError):
        store.resolve_flag(flag.id, "ignore", "admin")


def test_reconcile_does_not_duplicate_flags_across_runs(store: Store) -> None:
    _init_site(store)
    _write_post(store.site_dir, "content/posts/orphan.md", title="Orphan")
    reconcile.run(store, _ADMIN)
    reconcile.run(store, _ADMIN)
    flags = [f for f in store.list_flags() if f.type == "post_on_main_without_published_draft"]
    assert len(flags) == 1
