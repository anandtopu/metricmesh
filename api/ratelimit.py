"""Redis-backed ingestion rate limiting (MM-1.5).

A fixed-window counter per client (API key if present, else source IP). Shared
across API replicas via Redis and **fail-open**: if Redis is unreachable the
request is allowed, so a cache blip never blocks ingestion. Disabled when
``RATE_LIMIT_PER_MINUTE`` is 0 (the default).
"""
from __future__ import annotations

import time

import redis.asyncio as aioredis
import structlog
from fastapi import Header, HTTPException, Request, status

from config import get_settings

log = structlog.get_logger(__name__)

_KEY_PREFIX = "mm:ratelimit:"
_client: aioredis.Redis | None = None


def _get_redis() -> aioredis.Redis:
    """Lazily create and reuse one async Redis client for the process."""
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


async def rate_limit(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    limit = settings.rate_limit_per_minute
    if limit <= 0:
        return  # disabled

    identifier = x_api_key or (request.client.host if request.client else "unknown")
    window = int(time.time()) // 60
    key = f"{_KEY_PREFIX}{identifier}:{window}"

    try:
        count = await _get_redis().incr(key)
        if count == 1:
            await _get_redis().expire(key, 60)
    except Exception as exc:
        # Fail open: never let a Redis problem break ingestion.
        log.warning("ratelimit.redis_error", error=str(exc))
        return

    if count > limit:
        log.warning("ratelimit.exceeded", identifier=identifier, count=count, limit=limit)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": "60"},
        )
