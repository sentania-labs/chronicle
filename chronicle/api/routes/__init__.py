"""The `/v1` router: every route behind one consumer-token dependency."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import require_consumer
from . import catalog, drafts, images, submissions


def build_v1_router() -> APIRouter:
    router = APIRouter(prefix="/v1", dependencies=[Depends(require_consumer)])
    for module in (submissions, drafts, images, catalog):
        router.include_router(module.router)
    return router
