"""The store: real files, real commits, and an index that is only a cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from chronicle.api import gitrepo
from chronicle.api.errors import ApiError
from chronicle.api.index import SCHEMA_VERSION, Index, index_path
from chronicle.api.models import Material, Post, is_valid_slug
from chronicle.api.store import Store
from chronicle.cli import main as cli_main

from .conftest import png_bytes

FRONTMATTER = {"title": "A Post About Drift", "tags": ["lab"]}


def seed(store: Store) -> dict[str, str]:
    submission = store.create_submission(
        "ghostwriter", "a brief", [Material(name="notes", text="raw")], []
    )
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    draft, _ = store.create_draft("ghostwriter", from_submission=submission.id)
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "first body")
    store.save_draft(draft.id, "scott", 1, FRONTMATTER, "second body", message="tightened")
    image, _ = store.put_image(png_bytes(), "feature.png")
    store.attach_image(draft.id, image.image_id, "feature", "ghostwriter")
    _, run = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert run is not None
    return {"submission": submission.id, "draft": draft.id, "image": image.image_id, "run": run.id}


def snapshot(store: Store) -> dict[str, object]:
    return {
        "submissions": [item.model_dump(mode="json") for item in store.list_submissions()],
        "claimed": [item.id for item in store.list_submissions("drafted")],
        "drafts": [item.model_dump(mode="json") for item in store.list_drafts()],
        "previewed": [item.id for item in store.list_drafts("previewed")],
        "posts": [item.model_dump(mode="json") for item in store.list_posts()],
        "events": [item.model_dump(mode="json") for item in store.events_since(0)[0]],
    }


def test_git_author_is_the_acting_token_name(store: Store) -> None:
    store.create_submission("ghostwriter", "from an agent", [], [])
    assert gitrepo.log_authors(store.repo_dir, limit=1) == ["ghostwriter"]


def test_every_write_is_one_commit(store: Store) -> None:
    before = len(gitrepo.log_authors(store.repo_dir, limit=100))
    store.create_submission("ghostwriter", "one", [], [])
    store.create_submission("ghostwriter", "two", [], [])
    after = len(gitrepo.log_authors(store.repo_dir, limit=100))
    assert after - before == 2


def test_feedback_lands_in_the_same_commit_as_the_status_change(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    store.act_on_draft(draft.id, "submit", "ghostwriter", actor_is_ui=False)
    store.act_on_draft(
        draft.id, "request_revision", "ui", actor_is_ui=True, feedback="tighten the opening"
    )

    feedback_file = store.feedback_dir / f"{draft.id}.md"
    assert "tighten the opening" in feedback_file.read_text(encoding="utf-8")
    changed = set(gitrepo.files_in_head(store.repo_dir))
    assert f"feedback/{draft.id}.md" in changed
    assert f"drafts/{draft.id}/draft.json" in changed


def test_index_is_not_tracked_by_git(store: Store) -> None:
    store.create_submission("ghostwriter", "one", [], [])
    tracked = gitrepo.tracked_files(store.repo_dir)
    assert not any(path.startswith("index/") for path in tracked)
    assert index_path(store.repo_dir).exists()


def test_run_queue_entry_is_written_for_a_preview(store: Store) -> None:
    ids = seed(store)
    entry = json.loads((store.queue_dir / f"{ids['run']}.json").read_text(encoding="utf-8"))
    assert entry["run_id"] == ids["run"]
    assert entry["draft_id"] == ids["draft"]
    assert entry["kind"] == "preview"
    assert entry["enqueued_at"]


def test_reindex_rebuilds_every_row_from_the_files(store: Store, data_dir: Path) -> None:
    seed(store)
    before = snapshot(store)
    store.close()

    index_path(store.repo_dir).unlink()
    assert cli_main(["--data-dir", str(data_dir), "reindex"]) == 0

    rebuilt = Store.open(data_dir)
    assert snapshot(rebuilt) == before
    assert rebuilt.index.image_id_for_sha(seed_sha(rebuilt)) is not None
    rebuilt.close()


def seed_sha(store: Store) -> str:
    draft = store.list_drafts("previewed")[0]
    return draft.images[0].image_id


def test_concurrent_saves_at_one_base_version_leave_a_single_winner(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    writers = 8
    start = threading.Barrier(writers)
    outcomes: list[str] = []

    def save(number: int) -> None:
        start.wait()
        try:
            store.save_draft(draft.id, f"writer{number}", 0, FRONTMATTER, f"body {number}")
            outcomes.append("saved")
        except ApiError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=save, args=(number,)) for number in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("saved") == 1
    assert set(outcomes) == {"saved", "stale_base_version"}
    saved = store.get_draft(draft.id)
    assert saved.version_no == 1
    assert store.get_version(draft.id, 1).body == saved.body
    assert len({event.seq for event in store.events_since(0)[0]}) == len(store.events_since(0)[0])


def test_an_existing_schema_version_is_never_restamped(tmp_path: Path) -> None:
    path = tmp_path / "chronicle.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        f"INSERT INTO schema_meta VALUES ('schema_version', '{SCHEMA_VERSION + 41}');"
    )
    conn.commit()
    conn.close()

    index = Index(path)
    assert index.schema_version() == SCHEMA_VERSION + 41
    index.close()


def test_event_sequence_survives_a_dropped_index(store: Store, data_dir: Path) -> None:
    store.create_submission("ghostwriter", "one", [], [])
    first_seq = store.events_since(0)[0][-1].seq
    store.close()

    index_path(store.repo_dir).unlink()
    reopened = Store.open(data_dir)
    reopened.create_submission("ghostwriter", "two", [], [])
    second_seq = reopened.events_since(0)[0][-1].seq
    reopened.close()

    assert second_seq == first_seq + 1
    assert cli_main(["--data-dir", str(data_dir), "reindex"]) == 0
    rebuilt = Store.open(data_dir)
    seqs = [event.seq for event in rebuilt.events_since(0)[0]]
    assert seqs == sorted(set(seqs))
    rebuilt.close()


def test_list_submissions_is_a_snapshot_under_the_store_lock(store: Store) -> None:
    submission = store.create_submission("ghostwriter", "one", [], [])

    entered = threading.Event()
    release = threading.Event()
    original_ids = store.index.submission_ids

    def blocking_ids(status: str | None = None) -> list[str]:
        entered.set()
        release.wait(timeout=5)
        return original_ids(status)

    store.index.submission_ids = blocking_ids  # type: ignore[method-assign]

    results: list[list[str]] = []
    lister = threading.Thread(
        target=lambda: results.append([item.status for item in store.list_submissions()])
    )
    lister.start()
    assert entered.wait(timeout=5)

    claimed = threading.Event()

    def do_claim() -> None:
        store.act_on_submission(submission.id, "claim", "ghostwriter")
        claimed.set()

    claimer = threading.Thread(target=do_claim)
    claimer.start()
    # The store lock held by the in-progress list must block a concurrent
    # mutation; without the fix, act_on_submission would not need the lock
    # the list holds and this claim would finish immediately.
    claimer_finished_early = claimed.wait(timeout=0.2)

    release.set()
    lister.join()
    claimer.join(timeout=5)

    assert not claimer_finished_early
    assert results[0] == ["new"]
    assert store.get_submission(submission.id).status == "claimed"


def test_slug_is_pinned_at_first_preview_and_stays(store: Store) -> None:
    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert previewed.slug == "a-post-about-drift"

    store.save_draft(draft.id, "ghostwriter", 1, FRONTMATTER, "more body")
    assert store.get_draft(draft.id).slug == "a-post-about-drift"


@pytest.mark.parametrize("bad_slug", ["../x", "a/b", ""])
def test_is_valid_slug_rejects_path_components(bad_slug: str) -> None:
    assert not is_valid_slug(bad_slug)


def test_is_valid_slug_accepts_a_plain_filename_component() -> None:
    assert is_valid_slug("a-post-about-drift")
    assert is_valid_slug("post_2024")


def test_get_post_refuses_a_traversing_slug_instead_of_reading_outside_posts_dir(
    store: Store,
) -> None:
    for bad_slug in ("../x", "a/b", ""):
        try:
            store.get_post(bad_slug)
        except ApiError as exc:
            assert exc.status_code == 404
        else:
            raise AssertionError(f"expected get_post({bad_slug!r}) to fail")


def test_apply_digest_refuses_to_write_a_post_record_with_a_bad_slug(store: Store) -> None:
    posts = [
        Post(slug="../x", path="content/posts/x.md", title="Escape", date="2024-01-01", sha="a"),
        Post(slug="a/b", path="content/posts/b.md", title="Nested", date="2024-01-01", sha="b"),
        Post(slug="", path="content/posts/c.md", title="Empty", date="2024-01-01", sha="c"),
        Post(slug="fine", path="content/posts/fine.md", title="Fine", date="2024-01-01", sha="d"),
    ]
    counts = store.apply_digest("chronicle", posts)
    assert counts["created"] == 1
    assert store.list_posts() == [store.get_post("fine")]
    assert not (store.repo_dir / "x.json").exists()
    assert not (store.posts_dir / "a").exists()
    assert not (store.repo_dir / ".json").exists()
