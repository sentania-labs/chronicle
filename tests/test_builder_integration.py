"""Real Hugo, real files: build a two-post fixture site and read the output.

Skipped when `hugo` is not on PATH, the same convention `tests/test_digest.py`
would use for a real-network digest test: everything else in this suite runs
against a fake hugo binary so it never depends on the real one being
installed, but at least one test has to prove the pieces fit together with
the actual toolchain.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from chronicle.api.store import Store
from chronicle.builder import runner
from chronicle.builder.leases import LeaseDirectory
from chronicle.builder.settings import BuilderSettings
from tests.conftest import png_bytes

pytestmark = pytest.mark.skipif(shutil.which("hugo") is None, reason="hugo is not on PATH")


def _fixture_settings(data_dir: Path, work_dir: Path) -> BuilderSettings:
    return BuilderSettings(
        data_dir=data_dir,
        external_url="http://localhost:8080",
        builder_id="integration-builder",
        poll_seconds=1,
        lease_seconds=60,
        work_dir=work_dir,
        hugo_bin="hugo",
        build_timeout_seconds=60,
        once=True,
    )


def test_real_hugo_builds_two_posts_with_rewritten_images(store: Store) -> None:
    site = store.site_dir
    (site / "content" / "posts").mkdir(parents=True)
    (site / "layouts" / "_default").mkdir(parents=True)
    (site / "hugo.toml").write_text('baseURL = "/"\ntitle = "Fixture"\n', encoding="utf-8")
    (site / "layouts" / "_default" / "single.html").write_text(
        "<html><body><h1>{{ .Title }}</h1>{{ .Content }}</body></html>", encoding="utf-8"
    )
    (site / "layouts" / "_default" / "list.html").write_text(
        "<html><body>list</body></html>", encoding="utf-8"
    )
    (site / "content" / "posts" / "existing-post.md").write_text(
        "---\ntitle: Existing Post\ndate: 2024-01-01\n---\nalready on main\n", encoding="utf-8"
    )

    draft, _ = store.create_draft("ghostwriter")
    store.save_draft(
        draft.id,
        "ghostwriter",
        0,
        {"title": "New Post From A Draft", "date": "2026-08-01"},
        "look at this ![alt](fresh.png) picture",
    )
    image, _ = store.put_image(png_bytes(), "fresh.png")
    store.attach_image(draft.id, image.image_id, "inline", "ghostwriter")
    _, run = store.act_on_draft(draft.id, "preview", "ghostwriter", actor_is_ui=False)
    assert run is not None

    settings = _fixture_settings(store.data_dir, store.data_dir / "builder-work")
    leases = LeaseDirectory(settings.leases_dir, settings.lease_seconds)
    lease = leases.claim(run.id, settings.builder_id)
    assert lease is not None
    runner.build_one(store, settings, run, leases, lease)

    finished = store.get_run(run.id)
    assert finished.status == "succeeded", store.run_log(run.id)

    slug = store.get_draft(draft.id).slug
    assert slug is not None
    output_root = store.preview_dir / slug

    post_page = output_root / "2026" / "08" / "new-post-from-a-draft" / "index.html"
    assert post_page.exists()
    text = post_page.read_text(encoding="utf-8")
    assert "New Post From A Draft" in text
    assert f"/images/{slug}/fresh.png" in text
    assert (output_root / "images" / slug / "fresh.png").exists()
