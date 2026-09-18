"""The submission detail page's edit form, posting through the ui consumer."""

from __future__ import annotations

import re
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chronicle.api.deps import Services
from chronicle.api.models import Submission

from .conftest import auth


def make_submission(client: TestClient, token: str) -> str:
    response = client.post(
        "/v1/submissions",
        json={
            "brief": "original brief",
            "materials": [
                {"name": "notes", "text": "line one\nline two"},
                {"name": "link", "url": "https://example.com/a"},
            ],
        },
        headers=auth(token),
    )
    submission_id: str = response.json()["id"]
    return submission_id


def form(base_version: int, **overrides: list[str] | str) -> dict[str, list[str] | str]:
    data: dict[str, list[str] | str] = {
        "base_version": str(base_version),
        "brief": "edited brief",
        "material_name": ["notes", "link", ""],
        "material_url": ["", "https://example.com/a", ""],
        "material_text": ["line one\r\nline two", "", ""],
    }
    data.update(overrides)
    return data


def test_detail_page_shows_the_edit_form_at_the_current_version(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    page = client.get(f"/content/submissions/{submission_id}")
    assert page.status_code == 200
    assert f'action="/content/submissions/{submission_id}/edit"' in page.text
    assert '<input type="hidden" name="base_version" value="1">' in page.text
    assert "original brief" in page.text


def test_detail_page_renders_created_as_local_time_not_the_raw_stamp(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    stamp = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()[
        "created_at"
    ]
    page = client.get(f"/content/submissions/{submission_id}")
    assert stamp not in page.text
    assert re.search(r"created: (\d{4}-\d\d-\d\d )?\d\d:\d\d C[SD]T,", page.text)


def test_list_page_renders_created_as_local_time_matching_the_detail_page(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    stamp = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()[
        "created_at"
    ]
    listing = client.get("/content/submissions").text
    detail = client.get(f"/content/submissions/{submission_id}").text
    assert stamp not in listing
    match = re.search(r"<td>((?:\d{4}-\d\d-\d\d )?\d\d:\d\d C[SD]T)</td>", listing)
    assert match is not None
    assert f"created: {match.group(1)}," in detail


def test_edit_form_saves_through_the_ui_consumer_and_normalises_line_breaks(
    client: TestClient, agent_token: str, services: Services
) -> None:
    submission_id = make_submission(client, agent_token)

    response = client.post(
        f"/content/submissions/{submission_id}/edit",
        data=form(
            1,
            material_name=["notes", "link", "extra"],
            material_url=["", "https://example.com/a", ""],
            material_text=["line one\r\nline two", "", "a new note"],
        ),
    )

    assert response.status_code == 200
    assert "saved" in response.text
    record = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert record["version_no"] == 2
    assert record["brief"] == "edited brief"
    assert [m["name"] for m in record["materials"]] == ["notes", "link", "extra"]
    assert record["materials"][0]["text"] == "line one\nline two"
    assert record["materials"][2]["text"] == "a new note"
    version = services.store.get_submission_version(submission_id, 2)
    assert version.author == "scott"


def test_clearing_a_material_row_removes_it(client: TestClient, agent_token: str) -> None:
    submission_id = make_submission(client, agent_token)
    client.post(
        f"/content/submissions/{submission_id}/edit",
        data=form(
            1,
            material_name=["notes", "", ""],
            material_url=["", "", ""],
            material_text=["line one", "", ""],
        ),
    )
    record = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert [m["name"] for m in record["materials"]] == ["notes"]


def test_stale_edit_renders_409_with_diff_and_the_attempted_text(
    client: TestClient, agent_token: str, services: Services
) -> None:
    submission_id = make_submission(client, agent_token)
    services.store.revise_submission(
        submission_id, "ghostwriter", 1, "someone else's brief", [], []
    )

    response = client.post(f"/content/submissions/{submission_id}/edit", data=form(1))

    assert response.status_code == 409
    assert "What changed underneath you" in response.text
    assert "someone else&#x27;s brief" in response.text
    assert "edited brief" in response.text  # the attempted text survives
    assert '<input type="hidden" name="base_version" value="2">' in response.text
    assert services.store.get_submission(submission_id).brief == "someone else's brief"


def test_conflict_page_diff_and_form_come_from_the_same_snapshot(
    client: TestClient,
    agent_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submission_id = make_submission(client, agent_token)
    store = client.app.state.services.store  # type: ignore[attr-defined]
    store.revise_submission(submission_id, "ghostwriter", 1, "second brief", [], [])

    real_get = store.get_submission
    real_revise = store.revise_submission
    state = {"armed": False}

    def get_then_land_a_revision(sid: str) -> Submission:
        record = real_get(sid)
        if state["armed"]:
            # Every read the handler makes after the conflict is followed by
            # another revision landing, so two reads can never agree.
            state["armed"] = False
            latest = real_get(sid)
            n = latest.version_no
            real_revise(sid, "ghostwriter", n, f"brief {n + 1}", [], [])
            state["armed"] = True
        return record

    def revise_then_arm(*args: Any, **kwargs: Any) -> Submission:
        try:
            return real_revise(*args, **kwargs)
        finally:
            state["armed"] = True

    monkeypatch.setattr(store, "get_submission", get_then_land_a_revision)
    monkeypatch.setattr(store, "revise_submission", revise_then_arm)

    response = client.post(f"/content/submissions/{submission_id}/edit", data=form(1))

    assert response.status_code == 409
    form_version = re.search(r'name="base_version" value="(\d+)"', response.text)
    diff_end = re.search(r"\+\+\+ v(\d+)", response.text)
    assert form_version is not None and diff_end is not None
    assert form_version.group(1) == diff_end.group(1)


def test_a_drafted_submission_shows_no_form_and_refuses_the_post_by_name(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    client.post(f"/content/submissions/{submission_id}/draft")

    page = client.get(f"/content/submissions/{submission_id}")
    assert "/edit" not in page.text

    response = client.post(f"/content/submissions/{submission_id}/edit", data=form(1))
    assert response.status_code == 409
    assert "edit the draft instead" in response.text


def test_cross_origin_edit_is_refused(client: TestClient, agent_token: str) -> None:
    submission_id = make_submission(client, agent_token)
    response = client.post(
        f"/content/submissions/{submission_id}/edit",
        data=form(1),
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_a_non_integer_base_version_is_a_422_page_not_a_crash(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    bad = form(1)
    bad["base_version"] = "abc"
    response = client.post(f"/content/submissions/{submission_id}/edit", data=bad)
    assert response.status_code == 422
    assert "base_version must be a whole number" in response.text


def test_a_leading_blank_line_in_a_textarea_survives_the_html_parser(
    client: TestClient, agent_token: str, services: Services
) -> None:
    submission_id = make_submission(client, agent_token)
    services.store.revise_submission(
        submission_id, "ghostwriter", 1, "\nstarts blank", [], [], "leading newline"
    )
    page = client.get(f"/content/submissions/{submission_id}").text
    # A browser drops exactly one newline right after <textarea>, so the
    # template emits one extra: the stored text's own leading newline stays.
    assert 'cols="80">\n\nstarts blank</textarea>' in page


def test_a_post_missing_material_fields_is_a_422_and_changes_nothing(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    response = client.post(
        f"/content/submissions/{submission_id}/edit",
        data={"base_version": "1", "brief": "only a brief"},
    )
    assert response.status_code == 422
    assert "missing form fields: material_name, material_url, material_text" in response.text
    record = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert record["version_no"] == 1
    assert record["brief"] == "original brief"
    assert [m["name"] for m in record["materials"]] == ["notes", "link"]


def test_a_post_missing_the_brief_is_a_422_and_does_not_blank_it(
    client: TestClient, agent_token: str
) -> None:
    submission_id = make_submission(client, agent_token)
    data = form(1)
    del data["brief"]
    response = client.post(f"/content/submissions/{submission_id}/edit", data=data)
    assert response.status_code == 422
    assert "missing form fields: brief" in response.text
    record = client.get(f"/v1/submissions/{submission_id}", headers=auth(agent_token)).json()
    assert record["version_no"] == 1
    assert record["brief"] == "original brief"


def test_an_edit_on_a_submission_drafted_in_another_tab_keeps_what_was_typed(
    client: TestClient, agent_token: str, services: Services
) -> None:
    submission_id = make_submission(client, agent_token)
    services.store.act_on_submission(submission_id, "claim", "scott")
    services.store.create_draft("scott", from_submission=submission_id)

    response = client.post(
        f"/content/submissions/{submission_id}/edit",
        data=form(
            1,
            brief="my carefully typed brief",
            material_name=["notes", ""],
            material_url=["", ""],
            material_text=["a paragraph I do not want to lose", ""],
        ),
    )

    assert response.status_code == 409
    assert "can no longer be edited" in response.text
    assert "Your attempted text" in response.text
    assert "my carefully typed brief" in response.text
    assert "a paragraph I do not want to lose" in response.text
    assert "What changed underneath you" not in response.text
