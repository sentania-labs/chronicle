"""The Chronicle API service process.

`/healthz` and `/readyz` are the only anonymous routes, forever. Everything
under `/v1` requires a consumer token (spec sections 6 and 11, ADR 004).
`/readyz` says the api can do its job: the data directory named by
CHRONICLE_DATA_DIR exists and is writable, git is on PATH, and the derived
index opens at the schema version this code expects. The GitHub App is
reported "not configured" rather than checked, honestly, because bootstrap
and the App connection do not exist until C2.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from .deps import Services
from .errors import ApiError, api_error_handler, http_error_handler, validation_error_handler
from .index import SCHEMA_VERSION
from .routes import build_v1_router

log = logging.getLogger("chronicle.api")

SERVICE = "chronicle-api"
DATA_DIR_ENV = "CHRONICLE_DATA_DIR"
# Above the 5 MB image ceiling (chronicle.api.images.MAX_IMAGE_BYTES) to leave
# room for multipart overhead, but bounded so a large declared body is
# rejected on its Content-Length before FastAPI spools it into an UploadFile.
# This is a declared-size check, not a stream cap: a request that lies with
# chunked transfer encoding and no Content-Length is not caught here.
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024


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


def _index_check(services: Services | None) -> Check:
    if services is None:
        return Check(name="index", ok=False, detail="the data directory is not ready")
    found = services.store.index.schema_version()
    if found != SCHEMA_VERSION:
        return Check(
            name="index",
            ok=False,
            detail=f"index schema version {found}, this build expects {SCHEMA_VERSION}",
        )
    return Check(name="index", ok=True, detail=f"schema version {found}")


def _bootstrap(app: FastAPI) -> None:
    app.state.services = None
    path = os.environ.get(DATA_DIR_ENV)
    # Bootstrap never creates the data directory itself: an unmounted volume
    # must stay visibly unready rather than be papered over with an empty one.
    if not path or not os.path.isdir(path):
        return
    try:
        services = Services(Path(path))
        # The index is derived and the backup bundle carries none (ADR 006), so
        # a restored data directory would otherwise come up ready while serving
        # empty lists and checking slug collisions against nothing.
        if services.store.index.schema_version() == SCHEMA_VERSION:
            services.store.reindex()
        app.state.services = services
    except (OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        # A read-only or missing mount is a real operational state, not a
        # crash: readyz reports it and the process stays up to say so.
        log.warning("data directory not usable, /v1 will report 503: %s", exc)


def create_app() -> FastAPI:
    # docs_url, redoc_url, and openapi_url are disabled because health and
    # readiness are the only anonymous routes this service ever serves; the
    # schema routes come back once they sit behind the consumer-token layer
    # the /v1 routes use, not before.
    app = FastAPI(
        title="Chronicle",
        version="0.1.0.dev0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)

    @app.middleware("http")
    async def _reject_oversized_body(request: Request, call_next: Any) -> Any:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "error": "request_too_large",
                        "message": f"request body is {declared} bytes, ceiling is "
                        f"{MAX_REQUEST_BODY_BYTES}",
                    },
                )
        return await call_next(request)

    _bootstrap(app)
    app.include_router(build_v1_router())

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE)

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        checks = [
            _data_dir_check(),
            _git_check(),
            _index_check(app.state.services),
            _github_app_check(),
        ]
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
