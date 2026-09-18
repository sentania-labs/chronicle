"""Digest of main: fixture repo, idempotence, toolchain parse and drift.

Builds a real local git repository (two posts, one page bundle, a submodule
stub, and a hugo.yml) so `digest.py`'s clone/fetch, frontmatter parsing, and
toolchain parsing all run against real git plumbing, the same as they will
against the actual blog repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from chronicle.api import digest
from chronicle.api.admin_deps import AdminServices
from chronicle.api.digest_runner import DigestNotConfigured
from chronicle.api.digest_runner import run as run_digest
from chronicle.api.store import Store

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        env={
            **GIT_ENV,
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.com",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.com",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def blog_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "blog.git-src"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")

    posts = repo / "content" / "posts"
    posts.mkdir(parents=True)
    (posts / "first-post.md").write_text(
        "---\ntitle: First Post\ndate: 2024-01-01\ntags:\n  - meta\n---\nbody one\n",
        encoding="utf-8",
    )
    bundle = posts / "bundled-post"
    bundle.mkdir()
    (bundle / "index.md").write_text(
        "---\ntitle: Bundled Post\ndate: 2024-02-02\nslug: custom-bundle-slug\n"
        "featureImage: cover.jpg\n---\nbundle body\n",
        encoding="utf-8",
    )
    (bundle / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0not a real jpeg but a stand-in")

    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "hugo.yml").write_text(
        "name: hugo\n"
        "on: push\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      HUGO_VERSION: 0.164.0\n"
        "    steps:\n"
        "      - run: echo build\n",
        encoding="utf-8",
    )

    theme_src = tmp_path / "theme-src"
    theme_src.mkdir()
    _git(theme_src, "init", "--initial-branch=main")
    (theme_src / "theme.txt").write_text("stub theme\n", encoding="utf-8")
    _git(theme_src, "add", "-A")
    _git(theme_src, "commit", "-m", "theme stub")

    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial posts")
    _git(
        repo,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(theme_src),
        "themes/stub-theme",
    )
    _git(repo, "commit", "-m", "add theme submodule")
    return repo


def test_discover_posts_finds_both_a_flat_post_and_a_bundle(
    tmp_path: Path, blog_repo: Path
) -> None:
    site_dir = tmp_path / "site"
    digest.clone_or_update(site_dir, str(blog_repo), "main")
    posts = digest.discover_posts(site_dir)
    slugs = {p.slug for p in posts}
    assert slugs == {"first-post", "custom-bundle-slug"}
    for post in posts:
        assert post.sha  # every post has a real git blob sha


def test_slug_rule_prefers_frontmatter_then_bundle_dir_then_filename() -> None:
    assert digest.slug_for("content/posts/x.md", {}) == "x"
    assert digest.slug_for("content/posts/x/index.md", {}) == "x"
    assert digest.slug_for("content/posts/x.md", {"slug": "explicit"}) == "explicit"


def test_toolchain_reports_hugo_version_and_theme_submodule(
    tmp_path: Path, blog_repo: Path
) -> None:
    site_dir = tmp_path / "site"
    digest.clone_or_update(site_dir, str(blog_repo), "main")
    toolchain = digest.parse_toolchain(site_dir)
    assert toolchain.hugo_version == "0.164.0"
    assert len(toolchain.submodules) == 1
    assert toolchain.submodules[0]["path"] == "themes/stub-theme"
    assert toolchain.submodules[0]["commit"]


def _build(
    data_dir: Path, blog_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Store, AdminServices]:
    monkeypatch.setenv("CHRONICLE_DIGEST_REPO_URL", str(blog_repo))
    store = Store.open(data_dir)
    admin = AdminServices.build(data_dir)
    return store, admin


def test_digest_without_any_source_configured_fails_honestly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CHRONICLE_DIGEST_REPO_URL", raising=False)
    data_dir = tmp_path / "data"
    store = Store.open(data_dir)
    admin = AdminServices.build(data_dir)
    with pytest.raises(DigestNotConfigured):
        run_digest(store, "chronicle", admin)


def test_digest_repo_url_carries_the_test_token_in_test_token_mode(
    tmp_path: Path, blog_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 012: CHRONICLE_DIGEST_REPO_URL alone clones anonymously, which a
    private repo (chronicle-target) refuses; test-token mode must carry its
    token into the same clone, not just into GitHubRepoOps calls."""
    monkeypatch.setenv("CHRONICLE_DIGEST_REPO_URL", str(blog_repo))
    monkeypatch.setenv("CHRONICLE_ALLOW_TEST_TOKEN", "1")
    monkeypatch.setenv("CHRONICLE_GITHUB_TEST_TOKEN", "fake-test-token")
    store = Store.open(tmp_path / "data")
    admin = AdminServices.build(tmp_path / "data")

    captured: dict[str, str | None] = {}
    original = digest.clone_or_update

    def spy(
        site_dir: Path, repo_url: str, branch: str | None = None, token: str | None = None
    ) -> str:
        captured["token"] = token
        return original(site_dir, repo_url, branch, token=token)

    monkeypatch.setattr(digest, "clone_or_update", spy)
    run_digest(store, "test", admin)

    assert captured["token"] == "fake-test-token"


def test_digest_run_creates_posts_and_a_second_run_is_a_no_op(
    tmp_path: Path, blog_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, admin = _build(tmp_path / "data", blog_repo, monkeypatch)

    first = run_digest(store, "chronicle", admin)
    assert first.created == 2
    assert first.updated == 0
    assert first.hugo_version == "0.164.0"
    assert first.submodule_count == 1

    posts = {p.slug for p in store.list_posts()}
    assert posts == {"first-post", "custom-bundle-slug"}

    before_head = subprocess.run(
        ["git", "-C", str(store.repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    second = run_digest(store, "chronicle", admin)
    assert second.created == 0
    assert second.updated == 0
    assert second.unchanged == 2

    after_head = subprocess.run(
        ["git", "-C", str(store.repo_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert before_head == after_head, "an unchanged digest must make no commit"

    status = admin.read_digest_status()
    assert status is not None
    assert status["created"] == 0

    toolchain = admin.read_toolchain()
    assert toolchain is not None
    assert toolchain["hugo_version"] == "0.164.0"
    assert toolchain["submodules"][0]["path"] == "themes/stub-theme"

    # ADR 017: `blog_repo` carries no hugo.toml/config of its own, so Hugo
    # answers from its own built-in defaults (real `hugo config`, not the
    # fallback path); this is what a digest against `content/posts` alone
    # still reports in the same toolchain state the admin status page
    # reads.
    conventions = toolchain["conventions"]
    assert conventions["source"] == "hugo_config"
    assert conventions["contentdir"] == "content"
    assert conventions["fallback_reason"] is None

    store.close()


def test_reconcile_refreshes_toolchain_state_the_same_way_a_manual_digest_does(
    tmp_path: Path, blog_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round C4 review, P2: scheduled and post-merge reconciliation share
    `digest_runner.refresh_from_target`, which used to skip
    `parse_toolchain`/`write_toolchain` entirely, so a Hugo version bump on
    main never reached the admin status page until someone ran a manual
    digest."""
    from types import SimpleNamespace

    from chronicle.api import reconcile

    store, admin = _build(tmp_path / "data", blog_repo, monkeypatch)
    run_digest(store, "chronicle", admin)
    before = admin.read_toolchain()
    assert before is not None
    assert before["hugo_version"] == "0.164.0"

    workflow_path = blog_repo / ".github" / "workflows" / "hugo.yml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace("0.164.0", "0.165.0"),
        encoding="utf-8",
    )
    _git(blog_repo, "add", "-A")
    _git(blog_repo, "commit", "-m", "bump hugo")

    fake_target = SimpleNamespace(
        repo_url=str(blog_repo), default_branch="main", token_provider=lambda: None
    )
    monkeypatch.setattr(reconcile, "build_repo_target", lambda admin: fake_target)

    reconcile.run(store, admin)

    after = admin.read_toolchain()
    assert after is not None
    assert after["hugo_version"] == "0.165.0"

    store.close()


def test_clone_or_update_never_persists_the_token_and_origin_is_credential_free(
    tmp_path: Path, blog_repo: Path
) -> None:
    site_dir = tmp_path / "site"
    token = "ghs_supersecrettoken123"  # noqa: S105 - fixture value, not a real credential
    digest.clone_or_update(site_dir, str(blog_repo), "main", token=token)

    origin_url = subprocess.run(
        ["git", "-C", str(site_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert origin_url == str(blog_repo)
    assert "@" not in origin_url

    for path in (site_dir / ".git").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert token not in text, f"{path} contains the digest token"

    # A second call (the fetch branch) must not resurrect the token either.
    (blog_repo / "content" / "posts" / "another.md").write_text(
        "---\ntitle: Another\ndate: 2024-03-03\n---\nmore body\n", encoding="utf-8"
    )
    _git(blog_repo, "add", "-A")
    _git(blog_repo, "commit", "-m", "another post")
    digest.clone_or_update(site_dir, str(blog_repo), "main", token=token)
    for path in (site_dir / ".git").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert token not in text, f"{path} contains the digest token after a fetch"


def test_clone_or_update_resets_origin_when_the_selected_repo_changes(
    tmp_path: Path, blog_repo: Path
) -> None:
    other_repo = tmp_path / "other-blog.git-src"
    other_repo.mkdir()
    _git(other_repo, "init", "--initial-branch=main")
    (other_repo / "README.md").write_text("hi\n", encoding="utf-8")
    _git(other_repo, "add", "-A")
    _git(other_repo, "commit", "-m", "init")

    site_dir = tmp_path / "site"
    digest.clone_or_update(site_dir, str(blog_repo), "main")
    digest.clone_or_update(site_dir, str(other_repo), "main")

    origin_url = subprocess.run(
        ["git", "-C", str(site_dir), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert origin_url == str(other_repo)


def test_digest_reports_an_update_when_a_post_changes(
    tmp_path: Path, blog_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, admin = _build(tmp_path / "data", blog_repo, monkeypatch)
    run_digest(store, "chronicle", admin)

    post_path = blog_repo / "content" / "posts" / "first-post.md"
    post_path.write_text(
        "---\ntitle: First Post Revised\ndate: 2024-01-01\n---\nbody one, edited\n",
        encoding="utf-8",
    )
    _git(blog_repo, "add", "-A")
    _git(blog_repo, "commit", "-m", "revise first post")

    second = run_digest(store, "chronicle", admin)
    assert second.created == 0
    assert second.updated == 1
    assert second.unchanged == 1
    assert store.get_post("first-post").title == "First Post Revised"
    store.close()


def test_digest_then_from_post_import_of_a_real_shaped_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real blog's posts are dated filenames with no `slug:` key, keep
    their images under `static/images/<name>/`, and reference them
    root-relative in both `featureImage` and the body. This is the shape
    that produced zero images and a dropped `url` in the live check against
    the real repo; digest and from_post together should now round-trip it."""
    from tests.conftest import png_bytes

    repo = tmp_path / "blog.git-src"
    repo.mkdir()
    _git(repo, "init", "--initial-branch=main")

    posts = repo / "content" / "posts"
    posts.mkdir(parents=True)
    slug = "2026-08-01-vcf-operations-can-now-see-my-unifi-network"
    (posts / f"{slug}.md").write_text(
        "---\n"
        "title: VCF Operations Can Now See My Unifi Network\n"
        "url: /vcf-operations-can-now-see-my-unifi-network/\n"
        "type: post\n"
        "date: 2026-08-01\n"
        "author: scott\n"
        "featureImage: /images/vcf-operations-can-now-see-my-unifi-network/featured.png\n"
        "---\n"
        "body text\n"
        "![diagram](/images/vcf-operations-can-now-see-my-unifi-network/diagram.png)\n",
        encoding="utf-8",
    )
    image_dir = repo / "static" / "images" / "vcf-operations-can-now-see-my-unifi-network"
    image_dir.mkdir(parents=True)
    (image_dir / "featured.png").write_bytes(png_bytes((1, 2, 3)))
    (image_dir / "diagram.png").write_bytes(png_bytes((4, 5, 6)))

    workflows = repo / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "hugo.yml").write_text(
        "name: hugo\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    env:\n      HUGO_VERSION: 0.164.0\n    steps:\n      - run: echo build\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "real-shaped post")

    store, admin = _build(tmp_path / "data", repo, monkeypatch)
    digested = run_digest(store, "chronicle", admin)
    assert digested.created == 1

    draft, warnings = store.create_draft("chronicle", from_post=slug)
    assert warnings == []
    assert draft.frontmatter["url"] == "/vcf-operations-can-now-see-my-unifi-network/"
    assert draft.frontmatter["featureImage"] == (
        "/images/vcf-operations-can-now-see-my-unifi-network/featured.png"
    )

    assert len(draft.images) == 2
    by_role = {img.role: img for img in draft.images}
    assert by_role["feature"].filename == "featured.png"
    assert by_role["feature"].source_ref == (
        "/images/vcf-operations-can-now-see-my-unifi-network/featured.png"
    )
    assert by_role["inline"].filename == "diagram.png"
    assert by_role["inline"].source_ref == (
        "/images/vcf-operations-can-now-see-my-unifi-network/diagram.png"
    )
    store.close()


def test_git_config_global_grants_a_safe_directory_exception() -> None:
    path = Path(digest.GIT_ENV["GIT_CONFIG_GLOBAL"])
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "[safe]" in content
    assert "directory = *" in content


def test_clone_of_a_source_owned_by_a_different_uid_is_not_refused(tmp_path: Path) -> None:
    """Regression: git's dubious-ownership check ignores GIT_CONFIG_COUNT env
    injection for `safe.directory` on purpose, so the fix has to be a real
    config file (see `digest._safe_directory_config`), not the same
    per-invocation trick `_auth_env` uses for the installation token. This
    test cannot fake a different uid without root, so it instead proves the
    exception is broad (`*`) rather than naming this one test repo, which is
    what actually matters for a clone owned by a different uid in a
    container.
    """
    repo = tmp_path / "source"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=digest.GIT_ENV)
    (repo / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=a@b.c", "-c", "user.name=a", "add", "."],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=a@b.c",
            "-c",
            "user.name=a",
            "commit",
            "-q",
            "-m",
            "x",
        ],
        check=True,
    )
    destination = tmp_path / "dest"
    sha = digest.clone_or_update(destination, str(repo))
    assert sha
