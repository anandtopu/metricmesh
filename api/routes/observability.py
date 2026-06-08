from __future__ import annotations

from fastapi import APIRouter, Response

from monitoring.metrics import render_latest

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition endpoint (MM-8.6).

    Unauthenticated like the health probes so Prometheus can scrape it without
    an API key. Aggregates app metrics across the API and Celery worker
    processes plus a live Celery queue-depth gauge.
    """
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)
