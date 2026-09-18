"""ADR 017: content/image/taxonomy conventions derived from a site's own
`hugo config`, instead of the hardcoded `content/posts` directory guess.

Runs against the real `hugo` binary (pinned the same version the api and
builder images install), the same way the rest of `digest.py`'s tests run
against real git plumbing rather than a mock.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from chronicle.api import digest

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git_init_and_commit(repo: Path) -> None:
    """`discover_posts` reads blob shas via `git ls-tree`, so every fixture
    it walks has to be a real, committed git repository, the same as
    `test_digest.py`'s `blog_repo` fixture."""
    env = {
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "-C", str(repo), "init", "--initial-branch=main"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], env=env, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "fixture"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _bin_dir_without_hugo(tmp_path: Path) -> Path:
    """A `PATH` entry with `git` (via symlink) but no `hugo`, so
    `read_hugo_conventions` hits a real `FileNotFoundError` the way it
    would on a host that never installed Hugo, while `discover_posts`'s own
    `git` calls still work."""
    bin_dir = tmp_path / "no-hugo-bin"
    bin_dir.mkdir()
    git_path = shutil.which("git")
    assert git_path, "git must be on PATH for this test to mean anything"
    (bin_dir / "git").symlink_to(git_path)
    return bin_dir


@pytest.fixture
def custom_layout_site(tmp_path: Path) -> Path:
    """A site whose content lives under `content2/`, not `content/posts/`,
    classifying archive content by frontmatter `type` through
    `mainSections`, the same way Scott's own Blowfish theme does."""
    site_dir = tmp_path / "site"
    (site_dir / "content2" / "blog").mkdir(parents=True)
    (site_dir / "hugo.toml").write_text(
        'contentDir = "content2"\n'
        "[params]\n"
        'mainSections = ["post"]\n'
        "[taxonomies]\n"
        'tag = "tags"\n'
        'category = "categories"\n'
        'series = "series"\n',
        encoding="utf-8",
    )
    (site_dir / "content2" / "blog" / "x.md").write_text(
        "---\ntitle: X\ntype: post\ndate: 2024-01-01\n---\nbody\n", encoding="utf-8"
    )
    (site_dir / "content2" / "about.md").write_text(
        "---\ntitle: About\ntype: page\n---\nabout body\n", encoding="utf-8"
    )
    _git_init_and_commit(site_dir)
    return site_dir


def test_conventions_derived_from_real_hugo_config(custom_layout_site: Path) -> None:
    conventions = digest.read_hugo_conventions(custom_layout_site)
    assert conventions.source == "hugo_config"
    assert conventions.contentdir == "content2"
    assert conventions.staticdir == "static"
    assert conventions.mainsections == ("post",)
    assert conventions.taxonomies == {
        "tag": "tags",
        "category": "categories",
        "series": "series",
    }
    assert conventions.fallback_reason is None


def test_discover_posts_walks_a_content_dir_outside_content_posts_and_skips_pages(
    custom_layout_site: Path,
) -> None:
    """The walk root comes from `contentdir`, wider than `content/posts`,
    but `mainsections` still keeps a `type: page` bundle like an About page
    out of the discovered posts, the same as `content/posts` never included
    it before."""
    conventions = digest.read_hugo_conventions(custom_layout_site)
    posts = digest.discover_posts(custom_layout_site, conventions)
    slugs = {p.slug for p in posts}
    assert slugs == {"x"}


def test_unconfigured_taxonomy_keys_reports_what_this_site_never_defined(
    tmp_path: Path,
) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    (site_dir / "hugo.toml").write_text('[taxonomies]\ntag = "tags"\n', encoding="utf-8")
    conventions = digest.read_hugo_conventions(site_dir)
    assert conventions.source == "hugo_config"
    assert set(digest.unconfigured_taxonomy_keys(conventions)) == {"categories", "series"}


def test_unconfigured_taxonomy_keys_is_empty_on_the_fallback_path(tmp_path: Path) -> None:
    """Nothing real to compare against on the fallback path, so nothing is
    reported as unconfigured; that would just be noise."""
    conventions = digest.read_hugo_conventions(tmp_path, environment="x")
    # Force a fallback deterministically rather than depending on whether
    # `hugo` happens to be on PATH in this environment: the shape under
    # test is the reporting function's behaviour on a fallback value, not
    # how a fallback gets produced (covered separately below).
    fallback = digest.HugoConventions(
        contentdir=digest.FALLBACK_CONTENT_DIR,
        staticdir=digest.FALLBACK_STATIC_DIR,
        mainsections=(),
        taxonomies={},
        environment=conventions.environment,
        source="fallback",
        fallback_reason="test",
    )
    assert digest.unconfigured_taxonomy_keys(fallback) == ()


def test_falls_back_when_the_hugo_binary_cannot_be_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site"
    (site_dir / "content" / "posts").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(_bin_dir_without_hugo(tmp_path)))

    conventions = digest.read_hugo_conventions(site_dir)

    assert conventions.source == "fallback"
    assert conventions.contentdir == digest.FALLBACK_CONTENT_DIR
    assert conventions.staticdir == digest.FALLBACK_STATIC_DIR
    assert conventions.mainsections == ()
    assert conventions.fallback_reason


def test_falls_back_when_hugo_config_output_is_not_parseable_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    class FakeResult:
        stdout = "not json at all"

    def fake_run(*args: object, **kwargs: object) -> FakeResult:
        return FakeResult()

    monkeypatch.setattr(digest.subprocess, "run", fake_run)

    conventions = digest.read_hugo_conventions(site_dir)

    assert conventions.source == "fallback"
    assert "json" in (conventions.fallback_reason or "")


def test_discover_posts_falls_back_to_content_posts_with_no_type_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A digest against a site whose Hugo config could not be read still
    finds posts the same way this module always did: walking
    `content/posts` with no `type` filter."""
    site_dir = tmp_path / "site"
    (site_dir / "content" / "posts").mkdir(parents=True)
    (site_dir / "content" / "posts" / "a.md").write_text(
        "---\ntitle: A\ndate: 2024-01-01\n---\nbody\n", encoding="utf-8"
    )
    _git_init_and_commit(site_dir)
    monkeypatch.setenv("PATH", str(_bin_dir_without_hugo(tmp_path)))

    conventions = digest.read_hugo_conventions(site_dir)
    posts = digest.discover_posts(site_dir, conventions)

    assert {p.slug for p in posts} == {"a"}


def test_environment_defaults_to_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(digest.HUGO_ENVIRONMENT_ENV, raising=False)
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    conventions = digest.read_hugo_conventions(site_dir)
    assert conventions.environment == "production"


def test_environment_override_from_chronicle_hugo_environment_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(digest.HUGO_ENVIRONMENT_ENV, "devel")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    conventions = digest.read_hugo_conventions(site_dir)
    assert conventions.environment == "devel"


def test_explicit_environment_argument_wins_over_the_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(digest.HUGO_ENVIRONMENT_ENV, "devel")
    site_dir = tmp_path / "site"
    site_dir.mkdir()
    conventions = digest.read_hugo_conventions(site_dir, environment="staging")
    assert conventions.environment == "staging"


def test_as_dict_round_trips_into_the_toolchain_state_shape(custom_layout_site: Path) -> None:
    """`digest_runner.py` writes this straight into `admin.write_toolchain`,
    so it has to be plain JSON-safe values, not tuples or a dataclass."""
    conventions = digest.read_hugo_conventions(custom_layout_site)
    payload = conventions.as_dict()
    assert payload["mainsections"] == ["post"]
    assert payload["unconfigured_taxonomy_keys"] == []
    assert isinstance(payload["taxonomies"], dict)


def test_read_static_dir_from_state_reads_the_last_digest_conventions(tmp_path: Path) -> None:
    """`convert.py`'s callers (publish, preview, the cli dry run) read the
    `staticdir` the last digest already derived from this file, rather than
    re-invoking `hugo config` (ADR 017)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "toolchain.json").write_text(
        '{"conventions": {"staticdir": "assets"}}', encoding="utf-8"
    )
    assert digest.read_static_dir_from_state(tmp_path) == "assets"


def test_read_static_dir_from_state_falls_back_with_no_digest_yet(tmp_path: Path) -> None:
    assert digest.read_static_dir_from_state(tmp_path) == digest.FALLBACK_STATIC_DIR


def test_read_static_dir_from_state_falls_back_on_unparseable_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "toolchain.json").write_text("not json", encoding="utf-8")
    assert digest.read_static_dir_from_state(tmp_path) == digest.FALLBACK_STATIC_DIR
