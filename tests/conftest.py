"""A real data directory and a real git repository per test.

Nothing here is mocked: the store writes files, commits them, and indexes
them exactly as it does in the container, because that is the behaviour under
test.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image as PillowImage

from chronicle.api.deps import Services
from chronicle.api.main import DATA_DIR_ENV, create_app
from chronicle.api.store import Store
from chronicle.api.tokens import UI_TOKEN_FILE_NAME


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


def png_bytes(color: tuple[int, int, int] = (10, 20, 30), size: int = 8) -> bytes:
    buffer = io.BytesIO()
    PillowImage.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()
