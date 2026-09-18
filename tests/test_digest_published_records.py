"""ADR 017: digest lands a published post on main directly as a working
record, instead of leaving every post to be imported by hand.

Runs against a real git repository the same way `test_digest.py` and
`test_reconcile.py` do: `discover_posts` reads blob shas via `git ls-tree`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from chronicle.api import digest as digest_mod
from chronicle.api.models import Post
from chronicle.api.store import Store

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


def _init_site(store: Store) -> None:
    (store.site_dir / "content" / "posts").mkdir(parents=True, exist_ok=True)
    _git(store.site_dir, "init", "--initial-branch=main")
    (store.site_dir / ".gitkeep").write_text("", encoding="utf-8")
    _git(store.site_dir, "add", "-A")
    _git(store.site_dir, "commit", "-m", "init")


def _write_content(
    site_dir: Path, path: str, *, title: str = "T", type_: str = "post", body: str = "Body.\n"
) -> None:
    full = site_dir / path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f"---\ntitle: {title}\ntype: {type_}\ndate: '2026-01-01T00:00:00-06:00'\n---\n{body}",
        encoding="utf-8",
    )
    _git(site_dir, "add", "-A")
    _git(site_dir, "commit", "-m", f"content: {path}")


def _digest_once(
    store: Store, conventions: digest_mod.HugoConventions | None = None
) -> dict[str, int]:
    discovered = digest_mod.discover_posts(store.site_dir, conventions)
    posts = [
        Post(slug=item.slug, path=item.path, title=item.title, date=item.date, sha=item.sha)
        for item in discovered
    ]
    return store.apply_digest("chronicle-digest", posts, conventions)


def test_digest_lands_a_published_working_record_for_a_new_post(store: Store) -> None:
    _init_site(store)
    _write_content(store.site_dir, "content/posts/hello.md", title="Hello")

    counts = _digest_once(store)

    assert counts["published_created"] == 1
    drafts = [d for d in store.list_drafts() if d.slug == "hello"]
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft.status == "published"
    assert draft.published is not None
    assert draft.published["post_path"] == "content/posts/hello.md"
    assert draft.published["post_blob_sha"]
    assert draft.published["images"] == []
    # Reuses `_fill_from_post`: the frontmatter allowlist round-trips,
    # `type` included, exactly as an import would carry it.
    assert draft.frontmatter.get("type") == "post"
    assert draft.source_post == {
        "slug": "hello",
        "path": "content/posts/hello.md",
        "sha": draft.published["post_blob_sha"],
    }


def test_digest_run_twice_creates_exactly_one_working_record(store: Store) -> None:
    _init_site(store)
    _write_content(store.site_dir, "content/posts/hello.md", title="Hello")

    first = _digest_once(store)
    second = _digest_once(store)

    assert first["published_created"] == 1
    assert second["published_created"] == 0
    assert len([d for d in store.list_drafts() if d.slug == "hello"]) == 1


def test_digest_does_not_drag_a_mid_edit_record_back_to_published(store: Store) -> None:
    """A record digest already created that Scott has since started editing
    (`drafting`, via the `revise` transition a save triggers) must never be
    dragged back to `published` by a later digest run (ADR 017)."""
    _init_site(store)
    _write_content(store.site_dir, "content/posts/hello.md", title="Hello")
    _digest_once(store)
    draft = next(d for d in store.list_drafts() if d.slug == "hello")

    store.save_draft(draft.id, "scott", draft.version_no, draft.frontmatter, "Edited body.\n")
    edited = store.get_draft(draft.id)
    assert edited.status == "drafting"

    # Main is unchanged; digest runs again (the hourly reconcile loop, say).
    _digest_once(store)

    untouched = store.get_draft(draft.id)
    assert untouched.status == "drafting"
    assert untouched.body == "Edited body.\n"
    assert len([d for d in store.list_drafts() if d.slug == "hello"]) == 1


def test_digest_skips_a_page_bundle_and_only_lands_a_record_for_the_post(store: Store) -> None:
    """`discover_posts` already keeps a `type: page` bundle out of what it
    returns as a post (ADR 017, `mainsections`); this is what stops that
    filtering from also meaning "no working record", not a new filter."""
    (store.site_dir / "content" / "about").mkdir(parents=True, exist_ok=True)
    _git(store.site_dir, "init", "--initial-branch=main")
    (store.site_dir / ".gitkeep").write_text("", encoding="utf-8")
    _write_content(store.site_dir, "content/posts/hello.md", title="Hello", type_="post")
    _write_content(store.site_dir, "content/about/index.md", title="About", type_="page")

    conventions = digest_mod.HugoConventions(
        contentdir="content",
        staticdir="static",
        mainsections=("post",),
        taxonomies={},
        environment="production",
        source="hugo_config",
    )
    counts = _digest_once(store, conventions)

    assert counts["published_created"] == 1
    slugs = {d.slug for d in store.list_drafts()}
    assert slugs == {"hello"}
    assert store.list_posts() == [store.get_post("hello")]


def test_digest_import_failure_for_one_post_does_not_abort_the_run(store: Store) -> None:
    """A post whose file digest cannot actually read (missing on disk, the
    shape a Post record written by hand without a backing file takes) is
    skipped, logged, and never crashes the rest of the digest."""
    _init_site(store)
    _write_content(store.site_dir, "content/posts/hello.md", title="Hello")
    posts = [
        Post(
            slug="hello", path="content/posts/hello.md", title="Hello", date="2026-01-01", sha="x"
        ),
        Post(
            slug="ghost",
            path="content/posts/ghost.md",
            title="Ghost",
            date="2026-01-01",
            sha="y",
        ),
    ]
    counts = store.apply_digest("chronicle-digest", posts)

    assert counts["published_created"] == 1
    assert {d.slug for d in store.list_drafts()} == {"hello"}
