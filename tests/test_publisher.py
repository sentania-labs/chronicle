"""publisher.py: git data API sequence, PR body, idempotency, date stamping."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chronicle.api import publisher, watcher
from chronicle.api.admin_deps import AdminServices
from chronicle.api.errors import ApiError
from chronicle.api.settings import Settings
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


def test_pr_body_links_the_post_first_and_the_preview_site_second(store: Store) -> None:
    """ADR 022: the PR body's Preview line is the post's own page, with the
    preview site's root on a second line, when the last preview run recorded
    both. A publish approved before this field existed (below) falls back to
    the site root alone."""
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "My First Post"}, "Hello, world.\n")
    _, preview_run = store.act_on_draft(draft.id, "preview", "scott", True)
    assert preview_run is not None
    store.finish_run(
        preview_run.id,
        "test-builder",
        succeeded=True,
        result={
            "preview_url": "https://x/preview/my-first-post/",
            "post_url": "https://x/preview/my-first-post/2026/08/my-first-post/",
            "slug": "my-first-post",
        },
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    target, ops = _target()
    publisher.run_one(store, target, run)

    watch = store.get_watch(draft.id)
    assert watch is not None
    body = ops.pulls[watch.pr_number]["body"]
    lines = body.splitlines()
    preview_line = next(line for line in lines if line.startswith("Preview:"))
    assert preview_line == "Preview: https://x/preview/my-first-post/2026/08/my-first-post/"
    assert "https://x/preview/my-first-post/" in lines


def test_pr_body_derives_the_post_link_when_the_run_predates_post_url(store: Store) -> None:
    """Issue #61: a preview run recorded before ADR 022 has no `post_url`,
    but the approved draft is pinned with a slug and a date, so the PR body
    still links the post itself rather than only the preview site's root."""
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "My First Post"}, "Hello, world.\n")
    _, preview_run = store.act_on_draft(draft.id, "preview", "scott", True)
    assert preview_run is not None
    store.finish_run(
        preview_run.id,
        "test-builder",
        succeeded=True,
        result={"preview_url": "https://x/preview/my-first-post/", "slug": "my-first-post"},
    )
    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    stamp = draft.frontmatter["date"][:7].replace("-", "/")
    derived = f"https://x/preview/my-first-post/{stamp}/my-first-post/"
    target, ops = _target()
    publisher.run_one(store, target, run)

    watch = store.get_watch(draft.id)
    assert watch is not None
    body = ops.pulls[watch.pr_number]["body"]
    assert f"Preview: {derived}" in body
    assert "https://x/preview/my-first-post/" in body


def test_pr_body_falls_back_to_the_site_root_without_a_derivable_date(store: Store) -> None:
    draft, _ = store.create_draft("scott")
    store.save_draft(draft.id, "scott", 0, {"title": "My First Post"}, "Hello, world.\n")
    _, preview_run = store.act_on_draft(draft.id, "preview", "scott", True)
    assert preview_run is not None
    store.finish_run(
        preview_run.id,
        "test-builder",
        succeeded=True,
        result={"preview_url": "https://x/preview/my-first-post/", "slug": "my-first-post"},
    )
    draft = store.get_draft(draft.id)
    draft.frontmatter.pop("date", None)
    store._write_json(store._draft_path(draft.id), draft.model_dump(mode="json"))
    run = store.get_run(preview_run.id)
    run.started_at = "not-a-timestamp"
    run.created_at = "also-not-a-timestamp"
    store._write_json(store._run_path(run.id), run.model_dump(mode="json"))

    store.act_on_draft(draft.id, "submit", "scott", True)
    draft, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    target, ops = _target()
    publisher.run_one(store, target, run)

    watch = store.get_watch(draft.id)
    assert watch is not None
    body = ops.pulls[watch.pr_number]["body"]
    assert "Preview: https://x/preview/my-first-post/" in body


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


def _pr_text(ops: FakeRepoOps, pr_number: int) -> str:
    """The post text the pull request's branch actually carries, decoded from
    the blob the publish commit's tree points at."""
    import base64

    branch = ops.pulls[pr_number]["head"]["ref"]
    commit = ops.commits[ops.refs[f"heads/{branch}"]]
    entries = ops.trees[commit["tree"]["sha"]]
    post = next(entry for entry in entries if entry["path"].endswith(".md"))
    return base64.b64decode(ops.blobs[post["sha"]]).decode("utf-8")


def test_save_between_approve_and_publish_run_never_reaches_the_pr(store: Store) -> None:
    """Issue 41: approve queues the run, a save lands before the run opens the
    PR. The save is refused, so the PR carries exactly the approved text."""
    draft, run = _approved_draft(store)
    approved_version = store.get_draft(draft.id).version_no

    with pytest.raises(ApiError) as excinfo:
        store.save_draft(
            draft.id, "ghostwriter", approved_version, {"title": "My First Post"}, "UNAPPROVED\n"
        )
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "publish_run_in_progress"
    assert excinfo.value.extra["run_id"] == run.id
    assert store.get_draft(draft.id).version_no == approved_version

    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    text = _pr_text(ops, watch.pr_number)
    assert "Hello, world." in text
    assert "UNAPPROVED" not in text


def test_save_while_publish_run_is_building_is_refused(store: Store) -> None:
    draft, run = _approved_draft(store)
    store.start_run(run.id, publisher.PUBLISHER_ACTOR, hugo_version="", toolchain_drift=False)
    with pytest.raises(ApiError) as excinfo:
        store.save_draft(
            draft.id, "ghostwriter", store.get_draft(draft.id).version_no, {"title": "T"}, "x\n"
        )
    assert excinfo.value.code == "publish_run_in_progress"


def test_save_is_allowed_again_once_the_publish_run_has_failed(store: Store) -> None:
    draft, run = _approved_draft(store)
    target, ops = _target()
    ops.fail_on = "create_blob"
    publisher.run_one(store, target, run)
    assert store.get_draft(draft.id).status == "in_review"
    saved = store.save_draft(
        draft.id, "scott", store.get_draft(draft.id).version_no, {"title": "T"}, "edited\n"
    )
    assert saved.version_no == 2


def test_publish_run_refuses_a_draft_that_moved_past_the_approved_version(store: Store) -> None:
    """The backstop under the save gate: a run pins the version approve
    queued it for, so a draft that moved on by any other route is never
    converted. The run fails, the draft returns to in_review, and no PR opens."""
    draft, run = _approved_draft(store)
    assert run.approved_version == 1
    # Bypass the save gate the way any future writer of a new version would.
    moved = store.get_draft(draft.id)
    moved.version_no = 2
    store._write_json(store._draft_path(draft.id), moved.model_dump(mode="json"))

    target, ops = _target()
    finished = publisher.run_one(store, target, run)
    assert finished.status == "failed"
    assert (finished.result or {})["error_class"] == "draft_moved_since_approval"
    assert "create_pull" not in ops.calls
    assert store.get_watch(draft.id) is None
    assert store.get_draft(draft.id).status == "in_review"


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


def test_attach_and_detach_are_refused_while_a_publish_run_is_in_flight(store: Store) -> None:
    """Review finding: image attach and detach change what the run converts
    without bumping the version, so the save gate alone left that window open."""
    draft, run = _approved_draft(store)
    image, _ = store.put_image(png_bytes(), "sneaky.png")

    with pytest.raises(ApiError) as attach_refused:
        store.attach_image(draft.id, image.image_id, "inline", "ghostwriter")
    assert attach_refused.value.status_code == 409
    assert attach_refused.value.code == "publish_run_in_progress"
    assert store.get_draft(draft.id).images == []

    with pytest.raises(ApiError) as upload_refused:
        store.put_and_attach_image(draft.id, png_bytes((9, 9, 9)), "other.png", "inline", "scott")
    assert upload_refused.value.code == "publish_run_in_progress"
    # A refused upload leaves no orphan image behind.
    assert store.get_draft(draft.id).images == []

    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    assert not any(
        entry["path"].startswith("static/")
        for entry in ops.trees[ops.commits[ops.refs[f"heads/{watch.branch}"]]["tree"]["sha"]]
    )


def test_detach_is_refused_while_a_publish_run_is_in_flight(store: Store) -> None:
    draft, _ = store.create_draft("scott")
    image, _ = store.put_image(png_bytes(), "kept.png")
    store.attach_image(draft.id, image.image_id, "inline", "scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T"}, "![kept](kept.png)\n")
    store.act_on_draft(draft.id, "submit", "scott", True)
    store.act_on_draft(draft.id, "approve", "scott", True)

    with pytest.raises(ApiError) as caught:
        store.detach_image(draft.id, image.image_id, "ghostwriter")
    assert caught.value.code == "publish_run_in_progress"
    assert [item.image_id for item in store.get_draft(draft.id).images] == [image.image_id]


def test_publish_gate_reads_durable_files_when_the_index_lacks_the_run(store: Store) -> None:
    """Codex review, P1: approval wrote the run and queue files but died before
    the index update. The index is a cache (ADR 006), so save, attach, detach
    and a re-approve must all still be refused from the files."""
    draft, _ = store.create_draft("scott")
    kept, _ = store.put_image(png_bytes(), "kept.png")
    store.attach_image(draft.id, kept.image_id, "inline", "scott")
    store.save_draft(draft.id, "scott", 0, {"title": "T"}, "![kept](kept.png)\n")
    store.act_on_draft(draft.id, "submit", "scott", True)
    _, run = store.act_on_draft(draft.id, "approve", "scott", True)
    assert run is not None
    other, _ = store.put_image(png_bytes((9, 9, 9)), "other.png")

    store.index.conn.execute("DELETE FROM runs WHERE id = ?", (run.id,))
    store.index.conn.commit()
    assert store.last_run(draft.id, kind="publish") is None  # the stale cache

    version = store.get_draft(draft.id).version_no
    with pytest.raises(ApiError) as saved:
        store.save_draft(draft.id, "ghostwriter", version, {"title": "T"}, "UNAPPROVED\n")
    assert saved.value.code == "publish_run_in_progress"
    with pytest.raises(ApiError) as attached:
        store.attach_image(draft.id, other.image_id, "inline", "ghostwriter")
    assert attached.value.code == "publish_run_in_progress"
    with pytest.raises(ApiError) as detached:
        store.detach_image(draft.id, kept.image_id, "ghostwriter")
    assert detached.value.code == "publish_run_in_progress"
    with pytest.raises(ApiError) as reapproved:
        store.act_on_draft(draft.id, "approve", "scott", True)
    assert reapproved.value.code == "publish_run_in_progress"

    fresh = store.get_draft(draft.id)
    assert fresh.version_no == version
    assert [item.image_id for item in fresh.images] == [kept.image_id]

    # A finished run whose queue entry survived a crash is not in flight.
    store.finish_run(run.id, publisher.PUBLISHER_ACTOR, succeeded=False, result={})
    store._queue_entry_path(run.id).write_text(
        json.dumps({"run_id": run.id, "draft_id": draft.id, "kind": "publish"})
    )
    assert store.active_publish_run(draft.id) is None


def _published_draft(store: Store, monkeypatch: pytest.MonkeyPatch) -> tuple:
    """A draft that has been published and merged, so `unpublish` is offered."""
    monkeypatch.setattr(watcher, "refresh_from_target", lambda *a, **k: None)
    draft, run = _approved_draft(store)
    target, ops = _target()
    publisher.run_one(store, target, run)
    watch = store.get_watch(draft.id)
    assert watch is not None
    ops.merge(watch.pr_number)
    watcher.check_one(store, target, watch)
    assert store.get_draft(draft.id).status == "published"
    return draft, target, ops


def test_save_between_unpublish_and_its_run_is_refused(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue 43: `unpublish` queues a run from `published`. Before the fix a save
    in that window was accepted, moved the draft to `drafting`, and the unpublish
    run then found a draft that was no longer published."""
    draft, target, ops = _published_draft(store, monkeypatch)
    _, unpublish_run = store.act_on_draft(draft.id, "unpublish", "scott", True)
    assert unpublish_run is not None and unpublish_run.kind == "unpublish"
    version = store.get_draft(draft.id).version_no

    with pytest.raises(ApiError) as excinfo:
        store.save_draft(draft.id, "ghostwriter", version, {"title": "My First Post"}, "NEW\n")
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "publish_run_in_progress"
    assert excinfo.value.extra["run_id"] == unpublish_run.id
    assert "unpublish" in str(excinfo.value)
    assert "approved" not in str(excinfo.value)
    after = store.get_draft(draft.id)
    assert after.version_no == version
    assert after.status == "published"

    # Attach and detach are refused for the same reason.
    image, _ = store.put_image(png_bytes(), "late.png")
    with pytest.raises(ApiError) as attached:
        store.attach_image(draft.id, image.image_id, "inline", "ghostwriter")
    assert attached.value.code == "publish_run_in_progress"

    publisher.run_one(store, target, unpublish_run)
    assert store.get_run(unpublish_run.id).status == "succeeded"


def _later(seconds: float) -> datetime:
    return datetime.now().astimezone() + timedelta(seconds=seconds)


def test_unclaimed_publish_run_times_out_and_the_draft_is_usable_again(store: Store) -> None:
    """Issue 44: nothing is configured to publish against, so the run stays
    queued and the draft is frozen. The sweep fails the run, returns the draft
    to `in_review`, and tells the author why."""
    draft, run = _approved_draft(store)
    version = store.get_draft(draft.id).version_no

    assert publisher.expire_unclaimed_runs(store, 900, now=_later(60)) == []
    assert store.get_run(run.id).status == "queued"
    with pytest.raises(ApiError) as frozen:
        store.save_draft(draft.id, "scott", version, {"title": "My First Post"}, "edit\n")
    assert frozen.value.code == "publish_run_in_progress"

    expired = publisher.expire_unclaimed_runs(store, 900, now=_later(901))
    assert [item.id for item in expired] == [run.id]

    record = store.get_run(run.id)
    assert record.status == "failed"
    assert record.result is not None
    assert record.result["error_class"] == "publish_queue_timeout"
    assert "900 seconds" in record.result["message"]
    assert store.active_publish_run(draft.id) is None
    assert store.queue_depth("publish") == 0

    assert store.get_draft(draft.id).status == "in_review"
    feedback = [e for e in store.list_feedback(draft.id) if e.action == "publish_failed"]
    assert len(feedback) == 1
    assert feedback[0].author == "chronicle"
    assert "publish_queue_timeout" in feedback[0].text
    assert "not picked up within 900 seconds" in feedback[0].text
    assert "re-approve to retry" in feedback[0].text

    saved = store.save_draft(draft.id, "scott", version, {"title": "My First Post"}, "edit\n")
    assert saved.version_no == version + 1
    _, again = store.act_on_draft(draft.id, "approve", "scott", True)
    assert again is not None and again.id != run.id


def test_unclaimed_unpublish_run_times_out_and_the_post_stays_published(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    draft, _, _ = _published_draft(store, monkeypatch)
    _, run = store.act_on_draft(draft.id, "unpublish", "scott", True)
    assert run is not None
    version = store.get_draft(draft.id).version_no

    expired = publisher.expire_unclaimed_runs(store, 900, now=_later(901))
    assert [item.id for item in expired] == [run.id]
    record = store.get_run(run.id)
    assert record.status == "failed"
    assert record.result is not None
    assert record.result["error_class"] == "publish_queue_timeout"
    assert "unpublish run was not picked up" in record.result["message"]

    assert store.get_draft(draft.id).status == "published"
    feedback = [e for e in store.list_feedback(draft.id) if e.action == "unpublish_failed"]
    assert len(feedback) == 1
    assert "unpublish again to retry" in feedback[0].text
    saved = store.save_draft(draft.id, "scott", version, {"title": "My First Post"}, "edit\n")
    assert saved.version_no == version + 1
    # An unpublish can be asked for again once the draft is `published` again.
    assert store.active_publish_run(draft.id) is None


def test_queue_timeout_leaves_building_and_preview_runs_alone(store: Store) -> None:
    draft, run = _approved_draft(store)
    store.start_run(run.id, publisher.PUBLISHER_ACTOR, hugo_version="", toolchain_drift=False)
    other, _ = store.create_draft("scott")
    store.save_draft(other.id, "scott", 0, {"title": "Other"}, "x\n")
    _, preview = store.act_on_draft(other.id, "preview", "scott", True)
    assert preview is not None and preview.kind == "preview"

    assert publisher.expire_unclaimed_runs(store, 1, now=_later(10_000)) == []
    assert store.get_run(run.id).status == "building"
    assert store.get_run(preview.id).status == "queued"


def test_requeued_run_gets_a_fresh_timeout_window(store: Store) -> None:
    """A run a crashed publisher left `building` is requeued by
    `recover_stuck_runs`. If its `created_at` was already older than the
    timeout when the crash happened, the sweep must measure the waiting
    window from the requeue moment, not from `created_at`, or a run that
    was never actually stuck in the queue fails on the very first sweep
    after the restart (ADR 020 amendment)."""
    draft, run = _approved_draft(store)
    store.start_run(run.id, publisher.PUBLISHER_ACTOR, hugo_version="", toolchain_drift=False)

    old_created_at = (datetime.now().astimezone() - timedelta(seconds=1000)).isoformat(
        timespec="seconds"
    )
    backdated = store.get_run(run.id).model_dump(mode="json")
    backdated["created_at"] = old_created_at
    store._write_json(store._run_path(run.id), backdated)

    assert publisher.recover_stuck_runs(store) == 1
    record = store.get_run(run.id)
    assert record.status == "queued"
    assert record.created_at == old_created_at
    assert record.requeued_at is not None

    # A fresh 900s window measured from the requeue moment, not from the
    # already-stale `created_at`.
    assert publisher.expire_unclaimed_runs(store, 900, now=_later(60)) == []
    assert store.get_run(run.id).status == "queued"

    expired = publisher.expire_unclaimed_runs(store, 900, now=_later(901))
    assert [item.id for item in expired] == [run.id]


def test_publisher_loop_expires_a_run_when_no_target_is_configured(
    data_dir: Path, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode itself: `tick` returns before it looks at anything when
    no GitHub App or test-token repo exists, and the loop must still sweep."""
    for name in (
        "CHRONICLE_GITHUB_TEST_TOKEN",
        "CHRONICLE_ALLOW_TEST_TOKEN",
        "CHRONICLE_GITHUB_TEST_REPO",
    ):
        monkeypatch.delenv(name, raising=False)
    admin = AdminServices.build(data_dir)
    assert publisher.build_repo_target(admin) is None
    draft, run = _approved_draft(store)

    stop = threading.Event()
    loop = threading.Thread(target=publisher.run_loop, args=(store, admin, 0.02, stop, 0.2))
    loop.start()
    try:
        deadline = time.monotonic() + 10
        while store.get_run(run.id).status == "queued" and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        loop.join(timeout=5)

    assert store.get_run(run.id).status == "failed"
    assert store.get_draft(draft.id).status == "in_review"


def test_queue_timeout_setting_follows_the_env_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS", raising=False)
    assert Settings.from_env().publish_queue_timeout_seconds == 900.0
    monkeypatch.setenv("CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS", "42")
    assert Settings.from_env().publish_queue_timeout_seconds == 42.0
    monkeypatch.setenv("CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS", "nonsense")
    assert Settings.from_env().publish_queue_timeout_seconds == 900.0


def test_queue_timeout_is_floored_to_the_poll_interval(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A timeout shorter than a healthy publisher can respond in is raised to
    a floor, not left to fail every run before the publisher ever reaches
    it (round C6 adversarial review)."""
    monkeypatch.setenv("CHRONICLE_PUBLISH_POLL_SECONDS", "10")
    monkeypatch.setenv("CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS", "5")
    with caplog.at_level("WARNING", logger="chronicle.api.settings"):
        settings = Settings.from_env()
    assert settings.publish_poll_seconds == 10.0
    assert settings.publish_queue_timeout_seconds == 30.0
    assert any(
        "CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS=5" in record.message for record in caplog.records
    )

    monkeypatch.setenv("CHRONICLE_PUBLISH_QUEUE_TIMEOUT_SECONDS", "900")
    assert Settings.from_env().publish_queue_timeout_seconds == 900.0
