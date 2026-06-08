from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Identity, require_api_key
from ingestion.validators import IngestResponse, MetricBatch, MetricPoint
from monitoring.metrics import INGEST_POINTS
from storage.timescale import bulk_insert, get_session

router = APIRouter(prefix="/metrics", tags=["ingest"])
log = structlog.get_logger(__name__)


@router.post(
    "",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of metric points",
)
async def ingest_batch(
    batch: MetricBatch,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> IngestResponse:
    """
    Accepts up to 10 000 MetricPoint objects per request.
    Inserts via TimescaleDB unnest() for high-throughput bulk insert.
    Rows are tagged with the caller's tenant (MM-9.3), taken from the API key.
    """
    points_dicts = [
        {
            "timestamp": p.timestamp,
            "metric_name": p.metric_name,
            "value": p.value,
            "labels": p.labels,
            "source": p.source,
        }
        for p in batch.points
    ]
    try:
        inserted = await bulk_insert(session, points_dicts, tenant=identity.tenant)
        rejected = len(batch.points) - inserted
        INGEST_POINTS.labels(status="accepted").inc(inserted)
        if rejected:
            INGEST_POINTS.labels(status="rejected").inc(rejected)
        log.info("ingest.batch", count=inserted, source_id=batch.source_id)
        return IngestResponse(
            accepted=inserted,
            rejected=rejected,
        )
    except Exception as exc:
        log.error("ingest.batch.error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist metric batch",
        ) from exc


@router.post(
    "/single",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single metric point",
)
async def ingest_single(
    point: MetricPoint,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> IngestResponse:
    inserted = await bulk_insert(session, [point.model_dump()], tenant=identity.tenant)
    INGEST_POINTS.labels(status="accepted").inc(inserted)
    if inserted < 1:
        INGEST_POINTS.labels(status="rejected").inc(1 - inserted)
    return IngestResponse(accepted=inserted, rejected=1 - inserted)
