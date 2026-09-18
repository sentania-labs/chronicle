"""The submission detail page's edit form, posting through the ui consumer."""

from __future__ import annotations

from fastapi.testclient import TestClient

from chronicle.api.deps import Services

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
