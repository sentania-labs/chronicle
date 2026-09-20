"""A real data directory and a real git repository per test.

Nothing here is mocked: the store writes files, commits them, and indexes
them exactly as it does in the container, because that is the behaviour under
test.
"""

from __future__ import annotations

import io
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api.deps import Services
from chronicle.api.main import DATA_DIR_ENV, create_app
from chronicle.api.store import Store
from chronicle.api.tokens import UI_TOKEN_FILE_NAME

# Tests that need a tool the Python environment does not install: node for
# `tests/*.test.mjs`, a headless Chrome for `test_js_dom.py`, and Hugo for
# ADR 017's real `hugo config` derivation and the builder's integration test.
# On a developer machine a missing tool is a skip with the reason stated.
# CI sets CHRONICLE_REQUIRE_TEST_TOOLS=1, which turns the same absence into a
# failure, so a runner image that loses one cannot turn its tests into skips
# behind a green pipeline (issue 38). Mark a test with
# `pytest.mark.requires_tool("hugo")` (or use `requires_hugo`); `_required_tools`
# below does the rest.
REQUIRE_TOOLS_ENV = "CHRONICLE_REQUIRE_TEST_TOOLS"
CHROME_ENV = "CHRONICLE_TEST_CHROME"


def find_chrome() -> str | None:
    """A Chrome or Chromium binary: `CHRONICLE_TEST_CHROME`, else the first on PATH."""
    return os.environ.get(CHROME_ENV) or next(
        (
            found
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
            if (found := shutil.which(name))
        ),
        None,
    )


TOOL_FINDERS: dict[str, Callable[[], str | None]] = {
    "node": lambda: shutil.which("node"),
    "hugo": lambda: shutil.which("hugo"),
    "chrome": find_chrome,
}

requires_hugo = pytest.mark.requires_tool("hugo")


@pytest.fixture(autouse=True)
def _required_tools(request: pytest.FixtureRequest) -> None:
    for marker in request.node.iter_markers("requires_tool"):
        for tool in marker.args:
            if TOOL_FINDERS[tool]() is not None:
                continue
            reason = f"{tool} is not available (and this test needs it)"
            if os.environ.get(REQUIRE_TOOLS_ENV):
                pytest.fail(
                    f"{reason}; {REQUIRE_TOOLS_ENV} is set, so this is a failure", pytrace=False
                )
            pytest.skip(reason)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    return tmp_path


@pytest.fixture
def store(data_dir: Path) -> Iterator[Store]:
    opened = Store.open(data_dir)
    yield opened
    opened.close()


@pytest.fixture
def services(data_dir: Path) -> Iterator[Services]:
    built = Services(data_dir)
    yield built
    built.close()


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app) as opened:
        yield opened
    app.state.services.close()


@pytest.fixture
def ui_token(data_dir: Path, client: TestClient) -> str:
    return (data_dir / "state" / UI_TOKEN_FILE_NAME).read_text(encoding="utf-8").strip()


@pytest.fixture
def agent_token(client: TestClient) -> str:
    return client.app.state.services.tokens.issue("ghostwriter")  # type: ignore[attr-defined]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ADMIN_PASSWORD = "correct horse battery staple"


def claim_code(data_dir: Path) -> str:
    return (data_dir / "state" / "claim-code").read_text(encoding="utf-8").strip()


def claim_and_login(client: TestClient, data_dir: Path, password: str = ADMIN_PASSWORD) -> None:
    code = claim_code(data_dir)
    response = client.post("/admin/claim", data={"code": code, "password": password})
    assert response.status_code == 200
    response = client.post("/admin/login", data={"password": password})
    assert response.status_code == 200


@pytest.fixture
def admin_client(data_dir: Path, client: TestClient) -> TestClient:
    claim_and_login(client, data_dir)
    return client


def png_bytes(color: tuple[int, int, int] = (10, 20, 30), size: int = 8) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()
