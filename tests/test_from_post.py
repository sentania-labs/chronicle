"""from_post import: frontmatter allowlist warnings, slug pinning, images."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chronicle.api.models import Post
from chronicle.api.store import Store
from tests.conftest import auth, png_bytes


def _seed_post_on_site(store: Store, slug: str, extra_frontmatter: str = "") -> Post:
    post_dir = store.site_dir / "content" / "posts"
    post_dir.mkdir(parents=True, exist_ok=True)
    path = post_dir / f"{slug}.md"
    path.write_text(
        f"---\ntitle: A Real Post\ndate: 2024-03-03\n{extra_frontmatter}---\nsome body text\n",
        encoding="utf-8",
    )
    post = Post(
        slug=slug,
        path=f"content/posts/{slug}.md",
        title="A Real Post",
        date="2024-03-03",
        sha="abc123",
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / f"{slug}.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)
    return post


def test_from_post_import_pins_slug_and_copies_body(store: Store) -> None:
    _seed_post_on_site(store, "hello-world")
    draft, warnings = store.create_draft("ghostwriter", from_post="hello-world")
    assert draft.slug == "hello-world"
    assert draft.title == "A Real Post"
    assert "some body text" in draft.body
    assert draft.source_post == {
        "slug": "hello-world",
        "path": "content/posts/hello-world.md",
        "sha": "abc123",
    }
    assert warnings == []


def test_from_post_drops_unknown_frontmatter_keys_with_a_warning(store: Store) -> None:
    _seed_post_on_site(store, "legacy-post", extra_frontmatter="oldFieldFromTheDashboard: yes\n")
    draft, warnings = store.create_draft("ghostwriter", from_post="legacy-post")
    assert "oldFieldFromTheDashboard" not in draft.frontmatter
    assert any("oldFieldFromTheDashboard" in w for w in warnings)


def test_from_post_of_an_unknown_slug_is_404(store: Store) -> None:
    from chronicle.api.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        store.create_draft("ghostwriter", from_post="does-not-exist")
    assert excinfo.value.status_code == 404


def test_from_post_import_via_api_reports_warnings(client: TestClient, agent_token: str) -> None:
    store: Store = client.app.state.services.store  # type: ignore[attr-defined]
    _seed_post_on_site(store, "api-post", extra_frontmatter="legacyKey: true\n")
    response = client.post("/v1/drafts", json={"from_post": "api-post"}, headers=auth(agent_token))
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "api-post"
    assert any("legacyKey" in w for w in body["warnings"])


def test_from_post_import_pulls_bundle_images_and_marks_the_feature_image(store: Store) -> None:
    post_dir = store.site_dir / "content" / "posts" / "bundled"
    post_dir.mkdir(parents=True)
    (post_dir / "index.md").write_text(
        "---\ntitle: Bundled\ndate: 2024-04-04\nfeatureImage: cover.png\n---\nbody\n",
        encoding="utf-8",
    )
    (post_dir / "cover.png").write_bytes(png_bytes((1, 2, 3)))
    (post_dir / "inline.png").write_bytes(png_bytes((4, 5, 6)))

    post = Post(
        slug="bundled",
        path="content/posts/bundled/index.md",
        title="Bundled",
        date="2024-04-04",
        sha="def456",
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / "bundled.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)

    draft, warnings = store.create_draft("ghostwriter", from_post="bundled")
    assert warnings == []
    roles = {img.filename: img.role for img in draft.images}
    assert roles == {"cover.png": "feature", "inline.png": "inline"}


def test_from_post_import_without_a_digested_file_is_404(store: Store) -> None:
    post = Post(
        slug="ghost", path="content/posts/ghost.md", title="Ghost", date="2024-01-01", sha="x"
    )
    store.posts_dir.mkdir(parents=True, exist_ok=True)
    store._write_json(store.posts_dir / "ghost.json", post.model_dump(mode="json"))
    store.index.upsert_post(post)

    from chronicle.api.errors import ApiError

    with pytest.raises(ApiError) as excinfo:
        store.create_draft("ghostwriter", from_post="ghost")
    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "post_file_missing"


def test_from_submission_and_from_post_together_is_rejected(store: Store) -> None:
    from chronicle.api.errors import ApiError
    from chronicle.api.models import Material

    submission = store.create_submission("ghostwriter", "brief", [Material(name="n", text="t")], [])
    _seed_post_on_site(store, "both-post")
    with pytest.raises(ApiError) as excinfo:
        store.create_draft("ghostwriter", from_submission=submission.id, from_post="both-post")
    assert excinfo.value.status_code == 422
