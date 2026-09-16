"""The one error type the domain raises and the API renders.

Store and transition code raises `ApiError` with the status code the caller
should see, so a route handler never has to translate a domain failure into
an HTTP one. `extra` carries the fields a specific failure needs (the stale
save's current version and diff summary, the collision's slug).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return JSONResponse(status_code=exc.status_code, content=exc.body())


# The framework's own failures render in the same envelope as a domain error,
# so a consumer parses one shape and never has to tell them apart by key.
async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    error = ApiError(
        422,
        "invalid_request",
        "the request body or parameters did not validate",
        detail=jsonable_encoder(exc.errors()),
    )
    return JSONResponse(status_code=error.status_code, content=error.body())


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = "not_found" if exc.status_code == 404 else "request_failed"
    error = ApiError(exc.status_code, code, str(exc.detail))
    return JSONResponse(
        status_code=error.status_code, content=error.body(), headers=exc.headers or None
    )
