"""ADR 017: content/image/taxonomy conventions derived from a site's own
`hugo config`, instead of the hardcoded `content/posts` directory guess.

Runs against the real `hugo` binary (pinned the same version the api and
builder images install), the same way the rest of `digest.py`'s tests run
against real git plumbing rather than a mock.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from chronicle.api import digest
from tests.conftest import requires_hugo

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


@requires_hugo
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


@requires_hugo
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


@requires_hugo
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


def test_falls_back_when_contentdir_is_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`site_dir / "/etc"` returns `/etc` outright (`Path.__truediv__`
    does not defend against an absolute right-hand side), so a `hugo.toml`
    on main reporting an absolute `contentDir` must fall back rather than
    hand `discover_posts` a walk root outside the clone."""
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    class FakeResult:
        stdout = '{"contentdir": "/etc", "staticdir": ["static"]}'

    monkeypatch.setattr(digest.subprocess, "run", lambda *a, **k: FakeResult())

    conventions = digest.read_hugo_conventions(site_dir)

    assert conventions.source == "fallback"
    assert conventions.contentdir == digest.FALLBACK_CONTENT_DIR
    assert "contentdir" in (conventions.fallback_reason or "")


def test_falls_back_when_contentdir_escapes_with_dot_dot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    class FakeResult:
        stdout = '{"contentdir": "../../../../etc", "staticdir": ["static"]}'

    monkeypatch.setattr(digest.subprocess, "run", lambda *a, **k: FakeResult())

    conventions = digest.read_hugo_conventions(site_dir)

    assert conventions.source == "fallback"
    assert conventions.contentdir == digest.FALLBACK_CONTENT_DIR


def test_falls_back_when_staticdir_escapes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    site_dir = tmp_path / "site"
    site_dir.mkdir()

    class FakeResult:
        stdout = '{"contentdir": "content", "staticdir": ["../outside"]}'

    monkeypatch.setattr(digest.subprocess, "run", lambda *a, **k: FakeResult())

    conventions = digest.read_hugo_conventions(site_dir)

    assert conventions.source == "fallback"
    assert conventions.staticdir == digest.FALLBACK_STATIC_DIR


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


@requires_hugo
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


def test_read_content_dir_from_state_reads_the_last_digest_conventions(tmp_path: Path) -> None:
    """Same shape as `read_static_dir_from_state` (issue #18): `convert.py`'s
    callers read the `contentdir` the last digest already derived, rather
    than re-invoking `hugo config`."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "toolchain.json").write_text(
        '{"conventions": {"contentdir": "archive"}}', encoding="utf-8"
    )
    assert digest.read_content_dir_from_state(tmp_path) == "archive"


def test_read_content_dir_from_state_falls_back_with_no_digest_yet(tmp_path: Path) -> None:
    assert digest.read_content_dir_from_state(tmp_path) == digest.FALLBACK_CONTENT_DIR


def test_read_content_dir_from_state_falls_back_on_unparseable_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "toolchain.json").write_text("not json", encoding="utf-8")
    assert digest.read_content_dir_from_state(tmp_path) == digest.FALLBACK_CONTENT_DIR


# Issue #21: where a brand-new post is written. `contentdir` is the content
# ROOT and `FALLBACK_CONTENT_DIR` is the whole pre-ADR-017 path, so the reader
# never builds a new post's directory from `contentdir` alone.


def _post(path: str) -> digest.DiscoveredPost:
    return digest.DiscoveredPost(slug=Path(path).stem, path=path, title="t", date="", sha="")


def _write_state(data_dir: Path, conventions: dict[str, object]) -> None:
    state_dir = data_dir / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "toolchain.json").write_text(
        json.dumps({"conventions": conventions}), encoding="utf-8"
    )


def test_dominant_post_dir_is_the_busiest_section_under_the_content_root() -> None:
    posts = [_post("content/posts/a.md"), _post("content/posts/b.md"), _post("content/misc/c.md")]
    assert digest.dominant_post_dir(posts, "content") == "content/posts"


def test_dominant_post_dir_counts_nested_and_bundle_posts_by_section() -> None:
    posts = [
        _post("content/blog/2024/a.md"),
        _post("content/blog/2025/b.md"),
        _post("content/blog/c/index.md"),
        _post("content/posts/d.md"),
    ]
    assert digest.dominant_post_dir(posts, "content") == "content/blog"


def test_dominant_post_dir_for_posts_at_the_content_root_is_the_root() -> None:
    assert digest.dominant_post_dir([_post("content/a.md")], "content") == "content"


def test_dominant_post_dir_tie_is_broken_by_name_and_no_posts_is_none() -> None:
    tied = [_post("content/b/x.md"), _post("content/a/y.md")]
    assert digest.dominant_post_dir(tied, "content") == "content/a"
    assert digest.dominant_post_dir([], "content") is None


def _conventions(**overrides: object) -> digest.HugoConventions:
    fields: dict[str, object] = {
        "contentdir": "content",
        "staticdir": "static",
        "mainsections": ("post",),
        "taxonomies": {},
        "environment": "production",
        "source": "hugo_config",
    }
    fields.update(overrides)
    return digest.HugoConventions(**fields)  # type: ignore[arg-type]


def test_a_fallback_read_is_never_annotated_with_an_observed_directory() -> None:
    fallback = _conventions(contentdir=digest.FALLBACK_CONTENT_DIR, source="fallback")
    annotated = digest.with_observed_post_dir(fallback, [_post("content/posts/a.md")])
    assert annotated.postdir is None
    real = digest.with_observed_post_dir(_conventions(), [_post("content/posts/a.md")])
    assert real.postdir == "content/posts"
    assert real.as_dict()["postdir"] == "content/posts"


def test_new_post_dir_with_no_state_is_the_pre_adr_017_directory(tmp_path: Path) -> None:
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


def test_new_post_dir_with_unparseable_state_is_the_pre_adr_017_directory(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "toolchain.json").write_text("not json", encoding="utf-8")
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


def test_new_post_dir_after_a_fallback_read_is_the_pre_adr_017_directory(tmp_path: Path) -> None:
    """The fallback's contentdir is the whole path; it must not become a root
    that a `posts` segment is appended to."""
    _write_state(tmp_path, _conventions(contentdir="content/posts", source="fallback").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


def test_new_post_dir_is_the_observed_section(tmp_path: Path) -> None:
    _write_state(tmp_path, _conventions(contentdir="content2", postdir="content2/blog").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content2/blog"


def test_new_post_dir_without_an_observed_section_is_posts_under_the_content_root(
    tmp_path: Path,
) -> None:
    """The real `hugo config` answer for this project's own site is
    `contentdir: content`, and `mainsections` is `post`, a type and not a
    directory: neither is a section name, so the section is Chronicle's own."""
    _write_state(tmp_path, _conventions().as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"
    _write_state(tmp_path, _conventions(contentdir="archive").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "archive/posts"


def test_new_post_dir_when_the_content_root_is_already_a_posts_directory(tmp_path: Path) -> None:
    _write_state(tmp_path, _conventions(contentdir="content/posts").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


@pytest.mark.parametrize(
    "conventions",
    [
        {"source": "hugo_config", "contentdir": "../outside"},
        {"source": "hugo_config", "contentdir": "/etc"},
        {"source": "hugo_config", "contentdir": ""},
        {"source": "hugo_config"},
    ],
)
def test_new_post_dir_never_trusts_an_unsafe_contentdir(
    tmp_path: Path, conventions: dict[str, object]
) -> None:
    _write_state(tmp_path, conventions)
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


def test_new_post_dir_ignores_an_observed_section_outside_the_content_root(
    tmp_path: Path,
) -> None:
    _write_state(tmp_path, _conventions(postdir="../elsewhere").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"
    _write_state(tmp_path, _conventions(postdir="other/blog").as_dict())
    assert digest.read_new_post_dir_from_state(tmp_path) == "content/posts"


@requires_hugo
def test_a_digest_of_a_real_site_records_where_its_posts_live(
    custom_layout_site: Path,
) -> None:
    """The custom layout keeps its posts in `content2/blog` while `mainsections`
    says `post`: the directory is observed, never read off `mainsections`."""
    conventions = digest.read_hugo_conventions(custom_layout_site)
    discovered = digest.discover_posts(custom_layout_site, conventions)
    annotated = digest.with_observed_post_dir(conventions, discovered)
    assert annotated.postdir == "content2/blog"
