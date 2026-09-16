"""The Chronicle API service process, round C0.

Only `/healthz` and `/readyz` are real in this round. `/healthz` says the
process is up. `/readyz` says the api can do its job: the data directory
named by CHRONICLE_DATA_DIR exists and is writable, and git is on PATH. The
GitHub App is reported "not configured" rather than checked, honestly,
because bootstrap and the App connection do not exist until C2.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

SERVICE = "chronicle-api"
DATA_DIR_ENV = "CHRONICLE_DATA_DIR"


class Health(BaseModel):
    service: str
    status: str = "ok"


class Check(BaseModel):
    name: str
    ok: bool
    detail: str = ""


class Readiness(BaseModel):
    ready: bool
    checks: list[Check]


def _data_dir_check() -> Check:
    path = os.environ.get(DATA_DIR_ENV)
    if not path:
        return Check(name="data_dir", ok=False, detail=f"{DATA_DIR_ENV} is not set")
    if not os.path.isdir(path):
        return Check(name="data_dir", ok=False, detail=f"{path} does not exist")
    # A permission bit check can pass on a read-only mount even though no
    # write will actually succeed, so this probes with a real file instead.
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            pass
    except OSError as exc:
        return Check(name="data_dir", ok=False, detail=f"{path} is not writable: {exc}")
    return Check(name="data_dir", ok=True, detail=path)


def _git_check() -> Check:
    found = shutil.which("git") is not None
    return Check(name="git", ok=found, detail="" if found else "git binary not found on PATH")


def _github_app_check() -> Check:
    # Bootstrap and the App manifest flow arrive in C2. Reporting "not
    # configured" here is the honest answer, not a stand-in for a real check.
    return Check(name="github_app", ok=True, detail="not configured")


def create_app() -> FastAPI:
    # docs_url, redoc_url, and openapi_url are disabled because health and
    # readiness are the only anonymous routes this service ever serves; the
    # schema routes come back once they sit behind the consumer-token layer
    # the /v1 routes will use, not before.
    app = FastAPI(
        title="Chronicle",
        version="0.1.0.dev0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE)

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        checks = [_data_dir_check(), _git_check(), _github_app_check()]
        # The GitHub App is not yet part of the readiness gate: it is
        # explicitly not configured in this round, not a failing dependency.
        ready = all(check.ok for check in checks if check.name != "github_app")
        readiness = Readiness(ready=ready, checks=checks)
        return JSONResponse(
            status_code=200 if ready else 503,
            content=readiness.model_dump(mode="json"),
        )

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("chronicle.api.main:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
