from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    service: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is up and serving. No external dependencies."""
    from config import get_settings

    s = get_settings()
    return HealthResponse(status="ok", version=s.app_version, service=s.app_name)


async def _check_database() -> str:
    """Return 'ok' if TimescaleDB answers a trivial query, else 'error: ...'."""
    from sqlalchemy import text

    from storage.timescale import _engine

    try:
        assert _engine is not None, "database not initialised"
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:
        return f"error: {exc}"


async def _check_redis() -> str:
    """Return 'ok' if Redis answers PING, else 'error: ...'."""
    import redis.asyncio as aioredis

    from config import get_settings

    client = aioredis.from_url(get_settings().redis_url)
    try:
        await client.ping()
        return "ok"
    except Exception as exc:
        return f"error: {exc}"
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()


@router.get("/readiness")
async def readiness(response: Response) -> dict[str, Any]:
    """
    Readiness: the service can do real work — both TimescaleDB and Redis must be
    reachable. Returns 200 when ready and 503 (degraded) if either dependency
    fails, so orchestrators can route traffic accordingly.
    """
    checks = {
        "database": await _check_database(),
        "redis": await _check_redis(),
    }
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    if overall != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": overall, "checks": checks}
