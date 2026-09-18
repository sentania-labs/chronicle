"""Creating a draft from a submission seeds it with what the submission holds."""

from __future__ import annotations

from fastapi.testclient import TestClient

from chronicle.api.models import Material
from chronicle.api.store import Store

from .conftest import auth, png_bytes

POST_TEXT = (
    "---\n"
    "title: Why the lab drifted\n"
    "tags: [lab, drift]\n"
    "description: what moved\n"
    "mood: smug\n"
    "---\n"
    "# Why the lab drifted\n"
    "\n" + "It started with a manual change. " * 200
)


def make_claimed(
    store: Store, materials: list[Material], image_ids: list[str] | None = None
) -> str:
    submission = store.create_submission("ghostwriter", "write this up", materials, image_ids or [])
    store.act_on_submission(submission.id, "claim", "ghostwriter")
    return submission.id


def test_frontmatter_material_seeds_body_title_and_allowlisted_frontmatter(store: Store) -> None:
    submission_id = make_claimed(store, [Material(name="the post", text=POST_TEXT)])

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert draft.title == "Why the lab drifted"
    assert draft.frontmatter == {
        "title": "Why the lab drifted",
        "tags": ["lab", "drift"],
        "description": "what moved",
    }
    assert draft.body == POST_TEXT.split("\n---\n", 1)[1]
    assert len(draft.body) > 6000
    assert warnings == ["dropped unknown frontmatter key 'mood'"]
    # The record on disk, not just the returned object.
    assert store.get_draft(draft.id).body == draft.body
    assert store.get_draft(draft.id).frontmatter == draft.frontmatter


def test_post_shaped_material_is_chosen_over_an_earlier_plain_one(store: Store) -> None:
    submission_id = make_claimed(
        store,
        [
            Material(name="notes", text="just some notes, no heading"),
            Material(name="the post", text=POST_TEXT),
        ],
    )

    draft, _ = store.create_draft("scott", from_submission=submission_id)

    assert draft.title == "Why the lab drifted"
    assert "just some notes" not in draft.body
    assert [entry.text for entry in store.list_feedback(draft.id)] == [
        "Material: notes\n\njust some notes, no heading"
    ]


def test_heading_only_material_seeds_body_and_a_title_from_the_heading(store: Store) -> None:
    text = "# A Post Without Frontmatter\n\nBody paragraph.\n"
    submission_id = make_claimed(store, [Material(name="draft text", text=text)])

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert draft.body == text
    assert draft.title == "A Post Without Frontmatter"
    assert draft.frontmatter == {"title": "A Post Without Frontmatter"}
    assert warnings == []


def test_falls_back_to_the_first_material_with_text_when_none_looks_like_a_post(
    store: Store,
) -> None:
    submission_id = make_claimed(
        store,
        [
            Material(name="link", url="https://example.com/thread"),
            Material(name="notes", text="plain notes"),
            Material(name="more notes", text="other notes"),
        ],
    )

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert draft.body == "plain notes"
    assert draft.title == ""
    assert draft.frontmatter == {}
    assert warnings == []


def test_submission_with_no_materials_still_creates_a_blank_draft(store: Store) -> None:
    submission_id = make_claimed(store, [])

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert (draft.body, draft.title, draft.frontmatter, draft.images) == ("", "", {}, [])
    assert warnings == []
    assert store.list_feedback(draft.id) == []
    assert store.get_submission(submission_id).status == "drafted"


def test_other_materials_become_feedback_and_never_reach_the_body(store: Store) -> None:
    submission_id = make_claimed(
        store,
        [
            Material(name="link", url="https://example.com/thread"),
            Material(name="the post", text=POST_TEXT),
            Material(name="run notes", text="step one\nstep two"),
            Material(name="both", text="context", url="https://example.com/x"),
        ],
    )

    draft, _ = store.create_draft("scott", from_submission=submission_id)

    entries = store.list_feedback(draft.id)
    assert [entry.text for entry in entries] == [
        "Material: link\nURL: https://example.com/thread",
        "Material: run notes\n\nstep one\nstep two",
        "Material: both\nURL: https://example.com/x\n\ncontext",
    ]
    assert {entry.author for entry in entries} == {"scott"}
    assert {entry.action for entry in entries} == {"material"}
    for leaked in ("example.com", "step one", "context", "run notes"):
        assert leaked not in draft.body


def test_image_ids_are_attached_as_inline_images(store: Store) -> None:
    image, _ = store.put_image(png_bytes(), "shot.png")
    submission_id = make_claimed(
        store, [Material(name="notes", text="x")], [image.image_id, image.image_id]
    )

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert [(i.image_id, i.filename, i.role) for i in draft.images] == [
        (image.image_id, "shot.png", "inline")
    ]
    assert warnings == []
    assert store.get_draft(draft.id).images == draft.images


def test_an_unknown_image_id_is_a_warning_not_a_failure(store: Store) -> None:
    submission_id = make_claimed(store, [Material(name="notes", text="x")], ["f" * 64])

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert draft.images == []
    assert len(warnings) == 1
    assert "not in the image store" in warnings[0]


def test_unparseable_frontmatter_keeps_the_whole_text_as_body_with_a_warning(
    store: Store,
) -> None:
    text = "---\ntitle: [unclosed\n---\n# Heading\n\nbody\n"
    submission_id = make_claimed(store, [Material(name="post", text=text)])

    draft, warnings = store.create_draft("scott", from_submission=submission_id)

    assert draft.body == text
    assert len(warnings) == 1
    assert "frontmatter did not parse" in warnings[0]


def test_warnings_reach_the_http_response(client: TestClient, agent_token: str) -> None:
    created = client.post(
        "/v1/submissions",
        json={"brief": "b", "materials": [{"name": "post", "text": POST_TEXT}]},
        headers=auth(agent_token),
    ).json()
    client.post(f"/v1/submissions/{created['id']}/claim", headers=auth(agent_token))

    response = client.post(
        "/v1/drafts", json={"from_submission": created["id"]}, headers=auth(agent_token)
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Why the lab drifted"
    assert body["warnings"] == ["dropped unknown frontmatter key 'mood'"]
