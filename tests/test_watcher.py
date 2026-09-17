"""watcher.py: merge, close without merge, restart resume, backoff."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import cast

import pytest

from chronicle.api import publisher, watcher
from chronicle.api.admin_deps import AdminServices
from chronicle.api.errors import ApiError
from chronicle.api.models import Post
from chronicle.api.store import Store
from tests.fakes import FakeRepoOps

_ADMIN = cast(AdminServices, None)


def _target() -> tuple[publisher.RepoTarget, FakeRepoOps]:
    ops = FakeRepoOps()
    target = publisher.RepoTarget(
        ops=ops, owner="o", repo="r", default_branch="main", token_provider=lambda: "tok"
    )
    return target, ops


def _approved_draft(store: Store):
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "A Post"}, "Body.\n")
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    return draft, run


@pytest.fixture(autouse=True)
def _no_real_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)


def test_merge_flips_draft_to_published_and_clears_watch(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    branch = watch.branch

    ops.merge(watch.pr_number)
    outcome = watcher.check_one(store, target, watch)

    assert outcome == "merged"
    assert store.get_draft(draft.id).status == "published"
    assert store.get_watch(draft.id) is None
    assert f"heads/{branch}" not in ops.refs, "merged branch must be deleted"


def test_unpublish_merge_removes_post_record(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)
    slug = store.get_draft(draft.id).slug
    assert slug is not None
    # Simulate the post existing on main (as digest would have found it).
    store.apply_digest(
        "test",
        [Post(slug=slug, path="content/posts/x.md", title="A Post", date="2026-01-01", sha="abc")],
    )
    assert store.get_post(slug) is not None

    draft2, unpub_run = store.act_on_draft(draft.id, "unpublish", "scott", True)
    assert unpub_run is not None
    publisher.run_one(store, target, unpub_run)
    watch2 = store.get_watch(draft.id)
    assert watch2 is not None
    ops.merge(watch2.pr_number)
    watcher.check_one(store, target, watch2)

    assert store.get_draft(draft.id).status == "unpublished"
    with pytest.raises(ApiError):
        store.get_post(slug)


def test_close_without_merge_returns_draft_to_in_review_with_github_feedback(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None

    ops.close_unmerged(watch.pr_number)
    outcome = watcher.check_one(store, target, watch)

    assert outcome == "closed"
    assert store.get_draft(draft.id).status == "in_review"
    assert store.get_watch(draft.id) is None
    feedback = store.list_feedback(draft.id)
    assert any(entry.author == "github" and "closed" in entry.text for entry in feedback)


def test_open_pr_is_left_alone(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None

    outcome = watcher.check_one(store, target, watch)

    assert outcome == "open"
    assert store.get_draft(draft.id).status == "approved"
    assert store.get_watch(draft.id) is not None


def test_watch_persists_and_resumes_after_restart(data_dir: Path) -> None:
    store = Store.open(data_dir)
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch_before = store.get_watch(draft.id)
    assert watch_before is not None
    store.close()

    # A fresh Store, as a restarted process would open, still finds the
    # watch file under data/repo/watch/ and can resume polling it.
    reopened = Store.open(data_dir)
    watches = reopened.list_watches()
    assert len(watches) == 1
    assert watches[0].draft_id == draft.id
    assert watches[0].pr_number == watch_before.pr_number
    reopened.close()


def test_merge_after_a_revise_while_pr_open_flags_published_behind_draft(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    assert watch.built_version == store.get_draft(draft.id).version_no

    # The draft's version moves on while the publish PR is still open (a
    # `save` cannot do this any more now that it 409s against an open
    # publish PR; `record_github_version` is the one write that still can,
    # the same content-drift path reconciliation uses).
    store.record_github_version(draft.id, {"title": "A Post"}, "Revised body.\n", "test drift")
    assert store.get_draft(draft.id).version_no == watch.built_version + 1

    ops.merge(watch.pr_number)
    outcome = watcher.check_one(store, target, watch)

    assert outcome == "merged"
    assert store.get_draft(draft.id).status == "published"
    flags = [f for f in store.list_flags() if f.type == "content_drift" and f.draft_id == draft.id]
    assert len(flags) == 1
    feedback = store.list_feedback(draft.id)
    assert any(
        entry.author == "chronicle" and entry.action == "published_behind_draft"
        for entry in feedback
    )


def test_merge_with_no_revision_does_not_flag_published_behind_draft(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None

    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)

    flags = [f for f in store.list_flags() if f.type == "content_drift"]
    assert flags == []


def test_backoff_grows_when_nothing_is_open_and_caps_at_the_maximum(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher, "build_repo_target", lambda admin: _target()[0])
    intervals: list[float] = []
    stop_event = threading.Event()
    seen = {"n": 0}

    def fake_wait(timeout: float) -> bool:
        intervals.append(timeout)
        seen["n"] += 1
        if seen["n"] >= 4:
            stop_event.set()
        return stop_event.is_set()

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    watcher.run_loop(
        store, admin=_ADMIN, base_interval=10.0, max_interval=80.0, stop_event=stop_event
    )

    assert intervals == [20.0, 40.0, 80.0, 80.0], "interval must double toward the cap, then hold"


def test_backoff_resets_once_something_is_open_again(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    monkeypatch.setattr(watcher, "build_repo_target", lambda admin: target)
    intervals: list[float] = []
    stop_event = threading.Event()
    seen = {"n": 0}

    def fake_wait(timeout: float) -> bool:
        intervals.append(timeout)
        seen["n"] += 1
        if seen["n"] >= 2:
            stop_event.set()
        return stop_event.is_set()

    monkeypatch.setattr(stop_event, "wait", fake_wait)
    watcher.run_loop(
        store, admin=_ADMIN, base_interval=10.0, max_interval=80.0, stop_event=stop_event
    )

    # A PR stays open (never merged or closed in this test), so every tick
    # sees something watched and the interval stays at the base, never backs off.
    assert intervals == [10.0, 10.0]
