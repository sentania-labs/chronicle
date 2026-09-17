"""publisher.py: git data API sequence, PR body, idempotency, date stamping."""

from __future__ import annotations

import pytest

from chronicle.api import publisher, watcher
from chronicle.api.store import Store
from tests.conftest import png_bytes
from tests.fakes import FakeRepoOps


def _target(default_branch: str = "main") -> tuple[publisher.RepoTarget, FakeRepoOps]:
    ops = FakeRepoOps(default_branch=default_branch)
    target = publisher.RepoTarget(
        ops=ops, owner="o", repo="r", default_branch=default_branch, token_provider=lambda: "tok"
    )
    return target, ops


def _approved_draft(store: Store, *, summary: str = "") -> tuple:
    draft, _ = store.create_draft("scott")
    frontmatter = {"title": "My First Post"}
    if summary:
        frontmatter["summary"] = summary
    store.save_draft(draft.id, "scott", 0, frontmatter, "Hello, world.\n")
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None and run.kind == "publish"
    return draft, run


def test_publish_git_data_call_sequence(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)

    calls = ops.calls
    assert calls[0] == "get_ref"
    assert calls[1] == "get_commit"
    assert calls.index("create_blob") > 1
    assert calls.index("create_tree") > calls.index("create_blob")
    assert calls.index("create_commit") > calls.index("create_tree")
    ref_write = "create_ref" if "create_ref" in calls else "update_ref"
    assert calls.index(ref_write) > calls.index("create_commit")
    assert calls.index("list_open_pulls_by_head") > calls.index(ref_write)
    assert calls.index("create_pull") > calls.index("list_open_pulls_by_head")

    run = store.get_run(run.id)
    assert run.status == "succeeded"


def test_pr_body_has_marker_run_id_and_summary(store: Store) -> None:
    draft, run = _approved_draft(store, summary="A great post about testing.")
    target, ops = _target()
    publisher.run_one(store, target, run)

    watch = store.get_watch(draft.id)
    assert watch is not None
    pr = ops.pulls[watch.pr_number]
    assert "chronicle: publish" in pr["body"]
    assert run.id in pr["body"]
    assert "A great post about testing." in pr["body"]
    assert draft.title in pr["body"]


def test_idempotent_republish_updates_same_pr_not_a_second_one(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    first_watch = store.get_watch(draft.id)
    assert first_watch is not None
    assert ops.calls.count("create_pull") == 1

    # A second publish run for the same draft before the first PR merges
    # (e.g. a crash-recovered retry): must reset the branch and update the
    # same PR, never open a second one.
    second_run = store._queue_run(draft.id, "publish")
    publisher.run_one(store, target, second_run)

    assert ops.calls.count("create_pull") == 1
    assert ops.calls.count("update_pull_body") == 1
    second_watch = store.get_watch(draft.id)
    assert second_watch is not None
    assert second_watch.pr_number == first_watch.pr_number


def test_first_publish_stamps_date_in_america_chicago() -> None:
    date = publisher.stamp_publish_date()
    assert date.endswith(("-05:00", "-06:00"))  # CDT or CST


def test_first_publish_writes_date_republish_keeps_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    published = store.get_draft(draft.id).published
    assert published is not None
    first_date = published["date"]
    assert first_date

    # Observe the merge, then revise, resubmit, and re-approve: a republish.
    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)
    assert store.get_draft(draft.id).status == "published"

    store.save_draft(
        draft.id,
        "scott",
        store.get_draft(draft.id).version_no,
        {"title": "My First Post"},
        "Updated body.\n",
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft2, run2 = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run2 is not None
    publisher.run_one(store, target, run2)

    republished = store.get_draft(draft.id).published
    assert republished is not None
    assert republished["date"] == first_date
    assert republished["kind"] == "publish"


def test_unpublish_deletes_exactly_what_publish_wrote(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    published = store.get_draft(draft.id).published
    assert published is not None
    post_path = published["post_path"]

    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)
    assert store.get_draft(draft.id).status == "published"

    draft3, unpublish_run = store.act_on_draft(draft.id, "unpublish", "scott", True)
    assert unpublish_run is not None and unpublish_run.kind == "unpublish"
    publisher.run_one(store, target, unpublish_run)

    branch_ref = ops.refs[f"heads/post/{draft.slug}"]
    commit = ops.commits[branch_ref]
    tree = ops.trees[commit["tree"]["sha"]]
    deleted_paths = {entry["path"] for entry in tree if entry["sha"] is None}
    assert post_path in deleted_paths

    run_record = store.get_run(unpublish_run.id)
    assert run_record.status == "succeeded"


def test_failed_publish_returns_draft_to_in_review_with_chronicle_feedback(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    ops.fail_on = "create_blob"

    publisher.run_one(store, target, run)

    run_record = store.get_run(run.id)
    assert run_record.status == "failed"
    assert run_record.result is not None
    assert run_record.result["error_class"] == "simulated_failure"

    updated = store.get_draft(draft.id)
    assert updated.status == "in_review"
    feedback = store.list_feedback(draft.id)
    assert any(
        entry.author == "chronicle" and "simulated_failure" in entry.text for entry in feedback
    )


def test_reapprove_after_failed_publish_queues_a_new_run(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    ops.fail_on = "create_blob"
    publisher.run_one(store, target, run)
    assert store.get_draft(draft.id).status == "in_review"

    draft2, run2 = store.act_on_draft(draft.id, "approve", "scott", True)
    assert draft2.status == "approved"
    assert run2 is not None and run2.kind == "publish"

    ops.fail_on = None
    publisher.run_one(store, target, run2)
    assert store.get_run(run2.id).status == "succeeded"


def test_reapprove_is_rejected_while_a_publish_pr_is_still_open(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    assert store.get_watch(draft.id) is not None

    with pytest.raises(Exception) as excinfo:
        store.act_on_draft(draft.id, "approve", "scott", True)
    assert getattr(excinfo.value, "status_code", None) == 409


def test_save_is_rejected_with_409_while_a_publish_pr_is_open(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None

    with pytest.raises(Exception) as excinfo:
        store.save_draft(
            draft.id, "scott", store.get_draft(draft.id).version_no, {"title": "A Post"}, "x\n"
        )
    assert getattr(excinfo.value, "status_code", None) == 409
    assert watch.pr_url in str(getattr(excinfo.value, "extra", {}).get("pr_url", ""))


def test_republish_deletes_an_image_detached_since_the_last_publish(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)
    draft, _ = store.create_draft("scott")
    image, _ = store.put_image(png_bytes(), "cover.png")
    store.attach_image(draft.id, image.image_id, "feature", "scott")
    store.save_draft(
        draft.id,
        "scott",
        store.get_draft(draft.id).version_no,
        {"title": "Has An Image", "featureImage": "cover.png"},
        "Body with an image.\n",
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None

    target, ops = _target()
    publisher.run_one(store, target, run)
    published = store.get_draft(draft.id).published
    assert published is not None
    image_paths = [img["path"] for img in published["images"]]
    assert len(image_paths) == 1

    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)

    # Detach the image and republish.
    store.detach_image(draft.id, image.image_id, "scott")
    store.save_draft(
        draft.id,
        "scott",
        store.get_draft(draft.id).version_no,
        {"title": "Has An Image"},
        "Body without the image now.\n",
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run2 = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run2 is not None
    publisher.run_one(store, target, run2)

    branch_ref = ops.refs[f"heads/post/{draft.slug}"]
    commit = ops.commits[branch_ref]
    tree = ops.trees[commit["tree"]["sha"]]
    deleted_paths = {entry["path"] for entry in tree if entry["sha"] is None}
    assert image_paths[0] in deleted_paths
