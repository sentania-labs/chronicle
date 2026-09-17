"""The Chronicle API service process.

`/healthz` and `/readyz` are the only anonymous routes, forever. Everything
under `/v1` requires a consumer token (spec sections 6 and 11, ADR 004).
`/admin` requires a claimed instance and a signed session cookie instead
(ADR 004, ADR 008); it is mounted separately from `/v1` and shares none of
its dependencies. `/readyz` says the api can do its job: the data directory
named by CHRONICLE_DATA_DIR exists and is writable, git is on PATH, the
derived index opens at the schema version this code expects, and the GitHub
App is honestly reported not configured, configured but unverified, verified
at a time, or failing with the last error class.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from . import background
from .admin_deps import AdminAuthRedirect, AdminServices, admin_redirect_handler
from .deps import Services
from .errors import ApiError, api_error_handler, http_error_handler, validation_error_handler
from .github_app import readiness_state
from .index import SCHEMA_VERSION
from .routes import build_v1_router
from .routes.admin import api_router as admin_api_router
from .routes.admin import router as admin_router

log = logging.getLogger("chronicle.api")

SERVICE = "chronicle-api"
DATA_DIR_ENV = "CHRONICLE_DATA_DIR"
# Above the 5 MB image ceiling (chronicle.api.images.MAX_IMAGE_BYTES) to leave
# room for multipart overhead, but bounded so a large declared body is
# rejected on its Content-Length before FastAPI spools it into an UploadFile.
# BodySizeLimitMiddleware below also enforces this on the stream itself, so a
# chunked request with no (or a lying) Content-Length cannot get around it.
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024


class OversizedBody(Exception):
    def __init__(self, total: int) -> None:
        super().__init__(total)
        self.total = total


class BodySizeLimitMiddleware:
    """Enforce the body ceiling on the stream itself, not only Content-Length.

    A chunked request carries no Content-Length at all, and nothing stops a
    client from sending one that lies about it; this counts bytes as they
    actually arrive off the wire and aborts as soon as the ceiling is
    crossed, before a route handler ever sees the rest of the body.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > self.max_bytes:
                await _oversized_response(declared, self.max_bytes, send)
                return

        total = 0

        async def limited_receive() -> Any:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += (
                    len(message.get("body", b"")) if isinstance(message.get("body"), bytes) else 0
                )
                if total > self.max_bytes:
                    raise OversizedBody(total)
            return message

        try:
            await self.app(scope, limited_receive, send)
        except OversizedBody as exc:
            await _oversized_response(exc.total, self.max_bytes, send)


async def _oversized_response(declared: int, max_bytes: int, send: Send) -> None:
    body = JSONResponse(
        status_code=413,
        content={
            "error": "request_too_large",
            "message": f"request body is at least {declared} bytes, ceiling is {max_bytes}",
        },
    ).body
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


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


def _github_app_check(admin_services: AdminServices | None) -> Check:
    if admin_services is None:
        return Check(name="github_app", ok=True, detail="not configured")
    record = admin_services.github_store.load()
    state = readiness_state(record)
    # Drift and "not yet verified" are honest states, not failures: a fresh
    # bootstrap or a repo pick still in progress should not flip /readyz red.
    ok = not state.startswith("failing")
    return Check(name="github_app", ok=ok, detail=state)


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
    app.state.admin_services = None
    app.state.background = None
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
        admin_services = AdminServices.build(Path(path))
        app.state.admin_services = admin_services
        if admin_services.credentials.is_claimed():
            log.info("admin: claimed, admin.json present")
        else:
            log.info(
                "admin: a claim code exists at %s; visit /admin to claim this instance",
                admin_services.credentials.claim_code_path,
            )
        # ADR 013: the publisher, watcher, and reconcile loops run inside this
        # process, started once here so every caller of create_app (the real
        # server and the test suite alike) exercises the same background
        # behaviour rather than a test-only stand-in.
        app.state.background = background.start(services, admin_services)
    except (OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        # A read-only or missing mount is a real operational state, not a
        # crash: readyz reports it and the process stays up to say so.
        log.warning("data directory not usable, /v1 and /admin will report unready: %s", exc)


def _shutdown(app: FastAPI) -> None:
    if app.state.background is not None:
        app.state.background.stop()


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Bootstrap already ran synchronously in create_app(), before this
    # lifespan context ever starts: healthz and readyz have to answer
    # correctly for a caller that only ever does `create_app()` without an
    # ASGI server or TestClient's `with` block running the lifespan at all.
    # This context exists only for its shutdown half, so the background
    # threads (ADR 013) stop before the store they use is closed.
    try:
        yield
    finally:
        _shutdown(app)


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
        lifespan=_lifespan,
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(AdminAuthRedirect, admin_redirect_handler)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)

    _bootstrap(app)
    app.include_router(build_v1_router())
    app.include_router(admin_router)
    app.include_router(admin_api_router)

    @app.get("/healthz", response_model=Health, tags=["operations"])
    async def healthz() -> Health:
        return Health(service=SERVICE)

    @app.get("/readyz", tags=["operations"])
    async def readyz() -> JSONResponse:
        checks = [
            _data_dir_check(),
            _git_check(),
            _index_check(app.state.services),
            _github_app_check(app.state.admin_services),
        ]
        # The GitHub App is not yet part of the readiness gate: "not
        # configured" and "configured but unverified" are both honest,
        # in-progress states this round expects, not failing dependencies.
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
