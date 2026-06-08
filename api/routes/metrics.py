from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Identity, require_api_key
from storage.timescale import get_session

# Read-only queries over stored metrics. Shares the /api/v1/metrics base with
# the ingest router (distinct paths/methods), kept separate to keep read and
# write concerns apart.
router = APIRouter(prefix="/metrics", tags=["metrics"])


class SeriesResponse(BaseModel):
    metric_name: str
    resolution: str
    count: int
    points: list[dict[str, Any]]


@router.get(
    "/{name}/series",
    response_model=SeriesResponse,
    summary="Read a metric's time series (raw or 1-minute downsample)",
)
async def read_series(
    name: str,
    from_: datetime | None = Query(None, alias="from", description="ISO start time (inclusive)"),
    to: datetime | None = Query(None, description="ISO end time (inclusive)"),
    resolution: Literal["raw", "1m"] = Query(
        "1m", description="'raw' = stored points; '1m' = 1-minute buckets"
    ),
    limit: int = Query(10_000, ge=1, le=50_000),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> SeriesResponse:
    """
    Return a metric's series over an optional window, oldest first, scoped to the
    caller's tenant (MM-9.3).

    - ``resolution=raw`` → ``{ts, value}`` per stored point.
    - ``resolution=1m``  → ``{ts, avg, max, min, count}`` per 1-minute bucket.
    """
    from storage.timescale import fetch_metric_series

    points = await fetch_metric_series(
        session, name, start=from_, end=to, resolution=resolution,
        limit=limit, tenant=identity.tenant,
    )
    return SeriesResponse(
        metric_name=name, resolution=resolution, count=len(points), points=points
    )
