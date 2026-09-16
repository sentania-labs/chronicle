"""The store: real files, real commits, and an index that is only a cache."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from chronicle.api import gitrepo
from chronicle.api.errors import ApiError
from chronicle.api.index import SCHEMA_VERSION, Index, index_path
from chronicle.api.models import Material
from chronicle.api.store import Store
from chronicle.cli import main as cli_main

from .conftest import png_bytes

FRONTMATTER = {"title": "A Post About Drift", "tags": ["lab"]}


def seed(store: Store) -> dict[str, str]:
    submission = store.create_submission(
        "ghostwriter", "a brief", [Material(name="notes", text="raw")], []
    )
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    draft = store.create_draft("ghostwriter", from_submission=submission.id)
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
    draft = store.create_draft("ghostwriter")
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
    draft = store.create_draft("ghostwriter")
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


def test_slug_is_pinned_at_first_preview_and_stays(store: Store) -> None:
    draft = store.create_draft("ghostwriter")
    store.save_draft(draft.id, "ghostwriter", 0, FRONTMATTER, "body")
    previewed, _ = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert previewed.slug == "a-post-about-drift"

    store.save_draft(draft.id, "ghostwriter", 1, FRONTMATTER, "more body")
    assert store.get_draft(draft.id).slug == "a-post-about-drift"
