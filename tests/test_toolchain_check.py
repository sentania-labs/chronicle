"""The admin Toolchain page and its check (issue #67, ADR 026).

Upstream is always a local git repository reached over `file://` (the
module's protocol allowlist is widened for these tests only); the blog repo
the actions open PRs on is `FakeRepoOps`. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import dataclasses
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from chronicle.api import toolchain_check as tc
from chronicle.api.publisher import RepoTarget
from chronicle.api.routes import admin as admin_routes

from .conftest import requires_hugo
from .fakes import FakeRepoOps

GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def make_upstream(path: Path, tags: list[str], extra_commit: bool = True) -> dict[str, str]:
    """A repo whose commits are tagged in order; returns tag -> commit, plus
    "head" (one untagged commit past the last tag when `extra_commit`)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    shas: dict[str, str] = {}
    for tag in tags:
        (path / "f.txt").write_text(tag, encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-q", "-m", tag)
        git(path, "tag", "-a", tag, "-m", tag)
        shas[tag] = git(path, "rev-parse", "HEAD")
    if extra_commit:
        (path / "f.txt").write_text("after", encoding="utf-8")
        git(path, "commit", "-q", "-am", "after")
    shas["head"] = git(path, "rev-parse", "HEAD")
    return shas


@pytest.fixture
def file_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "ALLOWED_PROTOCOLS", "file")


@pytest.fixture
def world(tmp_path: Path, file_protocol: None, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Hugo upstream, two theme upstreams, and a site clone pinning both."""
    hugo = make_upstream(tmp_path / "up" / "hugo", ["v0.150.0", "v0.164.0", "v0.165.1"], False)
    blowfish = make_upstream(tmp_path / "up" / "blowfish", ["v2.80.0", "v2.81.0"])
    clarity = make_upstream(tmp_path / "up" / "clarity", ["v1.0.0"])
    monkeypatch.setattr(tc, "HUGO_REPO_URL", f"file://{tmp_path / 'up' / 'hugo'}")
    return {"root": tmp_path, "hugo": hugo, "blowfish": blowfish, "clarity": clarity}


def make_site(site: Path, world: dict[str, Any], blowfish_pin: str) -> None:
    site.mkdir(parents=True, exist_ok=True)
    git(site, "init", "-q", "-b", "main")
    up = world["root"] / "up"
    (site / ".gitmodules").write_text(
        '[submodule "themes/blowfish"]\n'
        "\tpath = themes/blowfish\n"
        f"\turl = file://{up / 'blowfish'}\n"
        '[submodule "themes/hugo-clarity"]\n'
        "\tpath = themes/hugo-clarity\n"
        f"\turl = file://{up / 'clarity'}\n",
        encoding="utf-8",
    )
    git(site, "add", ".gitmodules")
    git(site, "update-index", "--add", "--cacheinfo", f"160000,{blowfish_pin},themes/blowfish")
    git(
        site,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{world['clarity']['v1.0.0']},themes/hugo-clarity",
    )
    git(site, "commit", "-q", "-m", "site")


@pytest.fixture
def admin(client: TestClient) -> Any:
    return client.app.state.admin_services  # type: ignore[attr-defined]


@pytest.fixture
def store(client: TestClient) -> Any:
    return client.app.state.services.store  # type: ignore[attr-defined]


def _config(monkeypatch: pytest.MonkeyPatch, value: dict[str, Any] | None) -> None:
    monkeypatch.setattr(tc, "read_site_hugo_config", lambda site_dir: value)


# --- Parsing ----------------------------------------------------------------


GITMODULES = (
    '[submodule "themes/a"]\n\tpath = themes/a\n\turl = https://github.com/o/a\n'
    '[submodule "themes/b"]\n\tpath = themes/b\n\turl = git@github.com:o/b.git\n'
)


def test_parse_gitmodules_reads_name_path_and_url() -> None:
    assert tc.parse_gitmodules(GITMODULES) == [
        {"name": "themes/a", "path": "themes/a", "url": "https://github.com/o/a"},
        {"name": "themes/b", "path": "themes/b", "url": "git@github.com:o/b.git"},
    ]


def test_removing_a_gitmodules_section_keeps_the_rest_byte_for_byte() -> None:
    assert tc.remove_gitmodules_section(GITMODULES, "themes/a") == (
        '[submodule "themes/b"]\n\tpath = themes/b\n\turl = git@github.com:o/b.git\n'
    )
    assert tc.remove_gitmodules_section(GITMODULES, "themes/nope") is None


def test_parse_go_mod_reads_block_and_single_requires() -> None:
    text = (
        "module example.com/site\n\ngo 1.22\n\n"
        "require github.com/o/single v1.0.0\n"
        "require (\n\tgithub.com/o/theme v2.3.4 // indirect\n\texample.org/x v0.1.0\n)\n"
    )
    assert tc.parse_go_mod(text) == [
        {"module": "github.com/o/single", "version": "v1.0.0"},
        {"module": "github.com/o/theme", "version": "v2.3.4"},
        {"module": "example.org/x", "version": "v0.1.0"},
    ]


def test_latest_tag_is_the_highest_release_and_uses_the_peeled_commit() -> None:
    pairs = [
        ("t1", "refs/tags/v1.9.0"),
        ("c1", "refs/tags/v1.9.0^{}"),
        ("t2", "refs/tags/v1.10.0"),
        ("c2", "refs/tags/v1.10.0^{}"),
        ("c3", "refs/tags/v2.0.0-rc1"),
        ("c4", "refs/tags/latest"),
        ("c5", "refs/heads/main"),
    ]
    assert tc.latest_tag(pairs) == {"tag": "v1.10.0", "sha": "c2"}
    assert tc.latest_tag([("c", "refs/tags/nightly")]) is None


def test_only_https_and_github_ssh_urls_are_queried() -> None:
    assert tc.https_url("git@github.com:o/b.git") == "https://github.com/o/b.git"
    assert tc.https_url("https://gitlab.com/o/c") == "https://gitlab.com/o/c"
    assert tc.https_url("../relative") is None
    assert tc.https_url("http://example.com/o/c") is None
    assert tc.https_url("git@gitlab.com:o/c.git") is None


def test_a_theme_imported_by_the_configured_theme_counts_as_used(tmp_path: Path) -> None:
    (tmp_path / "themes" / "main").mkdir(parents=True)
    (tmp_path / "themes" / "main" / "theme.toml").write_text(
        'theme = ["component"]\n', encoding="utf-8"
    )
    (tmp_path / "themes" / "component").mkdir()
    assert tc.used_themes(tmp_path, "themes", ["main"]) == {"main", "component"}


# --- The check --------------------------------------------------------------


def test_a_check_compares_pins_with_upstream_and_flags_the_unused_theme(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_site(store.site_dir, world, world["blowfish"]["v2.80.0"])
    _config(monkeypatch, {"themes": ["blowfish"], "themesdir": "themes"})

    result = tc.run_check(store, admin)

    assert result["hugo"]["latest"] == "0.165.1"
    rows = {row["path"]: row for row in result["themes"]}
    blowfish = rows["themes/blowfish"]
    assert "error" not in blowfish, blowfish.get("error")
    assert blowfish["pinned"] == world["blowfish"]["v2.80.0"]
    assert blowfish["latest_tag"] == {"tag": "v2.81.0", "sha": world["blowfish"]["v2.81.0"]}
    assert blowfish["head"] == world["blowfish"]["head"]
    assert blowfish["unused"] is False
    assert rows["themes/hugo-clarity"]["unused"] is True
    assert tc.load_result(admin.state_dir) == result


def test_without_the_site_config_no_theme_is_called_unused(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_site(store.site_dir, world, world["blowfish"]["v2.80.0"])
    _config(monkeypatch, None)
    result = tc.run_check(store, admin)
    assert all(row["unused"] is False for row in result["themes"])


def test_an_unreachable_upstream_is_reported_on_its_row_not_raised(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_site(store.site_dir, world, world["blowfish"]["v2.80.0"])
    _config(monkeypatch, {"themes": ["blowfish"], "themesdir": "themes"})
    monkeypatch.setattr(tc, "HUGO_REPO_URL", f"file://{world['root'] / 'missing'}")
    subprocess.run(["rm", "-rf", str(world["root"] / "up" / "clarity")], check=True)

    result = tc.run_check(store, admin)

    assert result["hugo"]["latest"] is None
    assert result["errors"] and result["errors"][0].startswith("Hugo releases:")
    rows = {row["path"]: row for row in result["themes"]}
    assert rows["themes/hugo-clarity"]["error"].startswith("upstream query failed")
    assert "error" not in rows["themes/blowfish"]


def test_a_check_with_no_site_checkout_says_so(
    world: dict[str, Any], store: Any, admin: Any
) -> None:
    result = tc.run_check(store, admin)
    assert result["site_missing"] is True
    assert result["hugo"]["latest"] == "0.165.1"


def test_the_background_loop_waits_before_its_first_check(
    store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[bool] = []
    monkeypatch.setattr(tc, "run_check_logged", lambda s, a: ran.append(True))
    stop = threading.Event()
    stop.set()
    tc.run_loop(store, admin, stop)
    assert ran == []


def test_a_check_is_due_after_a_day(admin: Any) -> None:
    assert tc.is_due(admin.state_dir)
    tc._save_json(admin.state_dir / tc.STATE_FILE, {"checked_at": "2026-09-01T00:00:00+00:00"})
    now = tc.datetime.fromisoformat("2026-09-01T23:00:00+00:00")
    assert not tc.is_due(admin.state_dir, now)
    assert tc.is_due(admin.state_dir, tc.datetime.fromisoformat("2026-09-02T00:00:00+00:00"))


# --- Actions ------------------------------------------------------------------


def _checked(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> FakeRepoOps:
    make_site(store.site_dir, world, world["blowfish"]["v2.80.0"])
    _config(monkeypatch, {"themes": ["blowfish"], "themesdir": "themes"})
    tc.run_check(store, admin)
    ops = FakeRepoOps()
    # main is at the commit the check read.
    head = git(store.site_dir, "rev-parse", "HEAD")
    ops.refs["heads/main"] = head
    ops.commits[head] = {"tree": {"sha": "base-tree-1"}}
    ops.contents[".gitmodules"] = base64.b64encode(
        (store.site_dir / ".gitmodules").read_bytes()
    ).decode("ascii")
    ops.contents["themes/blowfish"] = {"type": "submodule", "sha": world["blowfish"]["v2.80.0"]}
    ops.contents["themes/hugo-clarity"] = {"type": "submodule", "sha": world["clarity"]["v1.0.0"]}
    return ops


def test_moving_a_theme_to_its_latest_tag_opens_one_pr(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _checked(world, store, admin, monkeypatch)

    record = tc.bump_submodule(admin.state_dir, ops, "main", "themes/blowfish", "tag")

    tree = next(entries for entries in ops.trees.values() if entries)
    assert tree == [
        {
            "path": "themes/blowfish",
            "mode": "160000",
            "type": "commit",
            "sha": world["blowfish"]["v2.81.0"],
        }
    ]
    branch = next(ref for ref in ops.refs if ref.startswith("heads/chronicle/toolchain/"))
    assert branch.startswith("heads/chronicle/toolchain/bump-tag-themes-blowfish-")
    assert ops.pulls[1]["base"]["ref"] == "main"
    assert record["pr_url"] == ops.pulls[1]["html_url"]
    # nothing pushed to the default branch
    assert ops.refs["heads/main"] == git(store.site_dir, "rev-parse", "HEAD")

    tc.bump_submodule(admin.state_dir, ops, "main", "themes/blowfish", "tag")
    assert len(ops.pulls) == 1  # the second click refreshes the same PR
    assert ops.calls.count("update_pull_body") == 1


@pytest.mark.parametrize(
    ("path", "which", "message"),
    [
        ("themes/blowfish", "sha", "unknown target"),
        ("themes/other", "tag", "not in the last toolchain check"),
        ("themes/hugo-clarity", "tag", "already at v1.0.0"),
    ],
)
def test_a_bump_moves_only_to_a_commit_the_check_offered(
    world: dict[str, Any],
    store: Any,
    admin: Any,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    which: str,
    message: str,
) -> None:
    ops = _checked(world, store, admin, monkeypatch)
    with pytest.raises(tc.ToolchainActionError, match=message):
        tc.bump_submodule(admin.state_dir, ops, "main", path, which)
    assert ops.pulls == {}


def test_removing_an_unused_theme_drops_its_gitlink_and_gitmodules_section(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _checked(world, store, admin, monkeypatch)

    tc.remove_theme(admin.state_dir, ops, "main", "themes/hugo-clarity")

    entries = next(entries for entries in ops.trees.values() if entries)
    assert entries[0] == {
        "path": "themes/hugo-clarity",
        "mode": "160000",
        "type": "commit",
        "sha": None,
    }
    gitmodules = base64.b64decode(ops.blobs[entries[1]["sha"]]).decode("utf-8")
    assert "hugo-clarity" not in gitmodules
    assert "path = themes/blowfish" in gitmodules
    assert ops.pulls[1]["title"] == "Remove unused theme hugo-clarity"


def test_a_theme_in_use_cannot_be_removed(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _checked(world, store, admin, monkeypatch)
    with pytest.raises(tc.ToolchainActionError, match="not flagged unused"):
        tc.remove_theme(admin.state_dir, ops, "main", "themes/blowfish")
    assert ops.pulls == {}


def test_removing_the_last_submodule_deletes_gitmodules() -> None:
    ops = FakeRepoOps()
    one = '[submodule "themes/a"]\n\tpath = themes/a\n\turl = https://github.com/o/a\n'
    assert tc.remove_gitmodules_section(one, "themes/a") == ""
    assert ops.pulls == {}


def test_an_unknown_image_version_is_never_called_behind() -> None:
    assert tc.hugo_behind("0.164.0", "0.165.1")
    assert not tc.hugo_behind("0.165.1", "0.165.1")
    assert not tc.hugo_behind("unknown", "0.165.1")
    assert not tc.hugo_behind("0.164.0", None)


def test_the_hugo_issue_link_names_both_versions() -> None:
    url = tc.hugo_issue_url("0.164.0", "0.165.1")
    assert url.startswith(tc.CHRONICLE_ISSUE_URL + "?title=")
    assert "0.164.0" in url and "0.165.1" in url


# --- The page -----------------------------------------------------------------


def test_the_page_before_any_check_offers_check_now(admin_client: TestClient) -> None:
    response = admin_client.get("/admin/toolchain")
    assert response.status_code == 200
    assert "No toolchain check has run yet" in response.text
    assert 'action="/admin/toolchain/check"' in response.text


def test_the_page_needs_an_admin_session(client: TestClient) -> None:
    response = client.get("/admin/toolchain", follow_redirects=False)
    assert response.status_code in (302, 303, 401)


def test_the_page_shows_the_check_and_its_actions(
    world: dict[str, Any],
    store: Any,
    admin: Any,
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_site(store.site_dir, world, world["blowfish"]["v2.80.0"])
    _config(monkeypatch, {"themes": ["blowfish"], "themesdir": "themes"})
    monkeypatch.setattr(
        admin, "settings", dataclasses.replace(admin.settings, builder_hugo_version="0.164.0")
    )
    tc.run_check(store, admin)

    text = admin_client.get("/admin/toolchain").text
    assert "0.165.1" in text
    assert "PR: move to v2.81.0" in text
    assert "PR: remove this theme" in text
    assert "file a Chronicle issue" in text  # the image runs 0.164.0


def test_a_cross_origin_action_is_refused(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/toolchain/bump",
        data={"path": "themes/blowfish", "which": "tag"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_an_action_with_no_github_configured_is_a_409(admin_client: TestClient) -> None:
    response = admin_client.post(
        "/admin/toolchain/bump", data={"path": "themes/blowfish", "which": "tag"}
    )
    assert response.status_code == 409
    assert "No GitHub App" in response.text


def test_an_opened_pr_is_linked_and_its_button_stays(
    world: dict[str, Any],
    store: Any,
    admin: Any,
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pressing the button again refreshes the same PR, and a PR Scott closed
    must not leave the row with nothing to press."""
    ops = _checked(world, store, admin, monkeypatch)
    target = RepoTarget(ops=ops, owner="o", repo="r", default_branch="main")
    monkeypatch.setattr(admin_routes, "build_repo_target", lambda a: target)

    response = admin_client.post(
        "/admin/toolchain/bump", data={"path": "themes/blowfish", "which": "tag"}
    )
    assert response.status_code == 200
    assert "PR opened: https://github.com/o/r/pull/1" in response.text
    assert "PR open: " in response.text
    assert "PR: move to v2.81.0" in response.text


def test_a_network_failure_on_an_action_is_a_502(
    world: dict[str, Any],
    store: Any,
    admin: Any,
    admin_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ops = _checked(world, store, admin, monkeypatch)

    def unreachable(base_tree: str, entries: list[dict[str, Any]]) -> str:
        raise httpx.ConnectError("no route to api.github.com")

    monkeypatch.setattr(ops, "create_tree", unreachable)
    target = RepoTarget(ops=ops, owner="o", repo="r", default_branch="main")
    monkeypatch.setattr(admin_routes, "build_repo_target", lambda a: target)
    response = admin_client.post(
        "/admin/toolchain/bump", data={"path": "themes/blowfish", "which": "tag"}
    )
    assert response.status_code == 502
    assert "no route to api.github.com" in response.text


def test_check_now_runs_one_check_in_the_background(
    admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def fake_check(store: Any, admin: Any) -> None:
        started.set()
        release.wait(5)

    monkeypatch.setattr(tc, "run_check_logged", fake_check)
    first = admin_client.post("/admin/toolchain/check")
    assert first.status_code == 200
    assert started.wait(5)
    second = admin_client.post("/admin/toolchain/check")
    assert second.status_code == 409
    release.set()


def test_a_bump_is_refused_when_main_moved_since_the_check(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops = _checked(world, store, admin, monkeypatch)
    ops.contents["themes/blowfish"] = {"type": "submodule", "sha": world["blowfish"]["v2.81.0"]}
    with pytest.raises(tc.ToolchainActionError, match="changed since the last check"):
        tc.bump_submodule(admin.state_dir, ops, "main", "themes/blowfish", "head")
    assert ops.pulls == {}


def test_a_theme_loaded_by_module_import_counts_as_used(tmp_path: Path) -> None:
    (tmp_path / "themes" / "main").mkdir(parents=True)
    (tmp_path / "themes" / "main" / "hugo.toml").write_text(
        '[module]\n[[module.imports]]\npath = "component"\n', encoding="utf-8"
    )
    assert tc.used_themes(tmp_path, "themes", ["main"]) == {"main", "component"}
    assert set(
        tc._imported_names({"module": {"imports": [{"path": "github.com/o/blowfish"}]}})
    ) == {
        "github.com/o/blowfish",
        "blowfish",
    }


def _hugo_site(site: Path, config: str) -> Path:
    site.mkdir(parents=True)
    (site / "hugo.toml").write_text('baseURL = "https://example.com/"\n' + config, "utf-8")
    for name in ("blowfish", "hugo-clarity"):
        (site / "themes" / name).mkdir(parents=True)
    return site


@requires_hugo
def test_real_hugo_config_names_the_configured_theme(tmp_path: Path) -> None:
    site = _hugo_site(tmp_path / "site", 'theme = "blowfish"\n')
    assert tc.read_site_hugo_config(site) == {"themes": ["blowfish"], "themesdir": "themes"}


@requires_hugo
def test_real_hugo_config_counts_a_module_import(tmp_path: Path) -> None:
    site = _hugo_site(tmp_path / "site", '[module]\n[[module.imports]]\npath = "blowfish"\n')
    config = tc.read_site_hugo_config(site)
    assert config is not None
    assert "blowfish" in config["themes"]


@requires_hugo
def test_real_hugo_config_with_no_theme_marks_nothing_unused(tmp_path: Path) -> None:
    site = _hugo_site(tmp_path / "site", "")
    assert tc.read_site_hugo_config(site) is None


def test_date_tags_do_not_outrank_releases_and_a_newer_image_is_not_behind() -> None:
    assert tc.latest_tag([("a", "refs/tags/20240101"), ("b", "refs/tags/v2.1.0")]) == {
        "tag": "v2.1.0",
        "sha": "b",
    }
    assert not tc.hugo_behind("0.166.0", "0.165.1")


def test_quoted_and_mixed_case_gitmodules_values_parse() -> None:
    text = '[submodule "t"]\n\tPath = "themes/t"\n\turl = "https://github.com/o/t"\n'
    assert tc.parse_gitmodules(text) == [
        {"name": "t", "path": "themes/t", "url": "https://github.com/o/t"}
    ]
    assert tc.remove_gitmodules_section(text, "themes/t") == ""


def test_a_removal_is_refused_once_main_moved_past_the_check(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round: the theme was unused at the checked commit; main may use
    it now."""
    ops = _checked(world, store, admin, monkeypatch)
    ops.refs["heads/main"] = "a-later-commit"
    with pytest.raises(tc.ToolchainActionError, match="changed since the last check"):
        tc.remove_theme(admin.state_dir, ops, "main", "themes/hugo-clarity")
    assert ops.pulls == {}


def test_a_bump_is_refused_when_the_submodule_url_changed(
    world: dict[str, Any], store: Any, admin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex round: the target commit came from the old upstream."""
    ops = _checked(world, store, admin, monkeypatch)
    moved = (
        (store.site_dir / ".gitmodules")
        .read_text("utf-8")
        .replace(str(world["root"] / "up" / "blowfish"), "https://github.com/fork/blowfish")
    )
    ops.contents[".gitmodules"] = base64.b64encode(moved.encode("utf-8")).decode("ascii")
    with pytest.raises(tc.ToolchainActionError, match="url on the default branch has changed"):
        tc.bump_submodule(admin.state_dir, ops, "main", "themes/blowfish", "tag")
    assert ops.pulls == {}


def test_paths_that_slug_alike_get_different_branches() -> None:
    assert tc._branch_name("remove", "themes/a+b") != tc._branch_name("remove", "themes/a b")
