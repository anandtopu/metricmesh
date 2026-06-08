from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import get_settings
from storage.timescale import close_db, init_db, setup_schema

log = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    FastAPI lifespan context manager — replaces deprecated on_startup/on_shutdown.
    Code before yield runs on startup; code after runs on shutdown.

    Python skill: asynccontextmanager decorator, async resource management.
    """
    settings = get_settings()
    log.info("startup", app=settings.app_name, version=settings.app_version)

    init_db()
    await setup_schema()
    log.info("db.ready")

    yield

    await close_db()
    log.info("shutdown")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Distributed time-series anomaly detection platform.\n\n"
            "Ingests metrics, stores them in TimescaleDB, and runs "
            "statistical + ML detectors asynchronously via Celery."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS restricted to a configured allow-list (never "*"). allow_credentials
    # requires explicit origins, so this also rules out wildcard by construction.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routers ────────────────────────────────────────────────────────────
    from fastapi import Depends

    from api.auth import require_api_key
    from api.ratelimit import rate_limit
    from api.routes.anomalies import router as anomaly_router
    from api.routes.audit import router as audit_router
    from api.routes.deadletters import router as deadletter_router
    from api.routes.health import router as health_router
    from api.routes.metrics import router as metrics_query_router
    from api.routes.observability import router as observability_router
    from ingestion.api import router as ingest_router

    # Data endpoints are auth-protected; health/readiness probes are not.
    # The high-volume ingest path is additionally rate-limited.
    protected = [Depends(require_api_key)]
    app.include_router(
        ingest_router, prefix="/api/v1",
        dependencies=[Depends(require_api_key), Depends(rate_limit)],
    )
    app.include_router(metrics_query_router, prefix="/api/v1", dependencies=protected)
    app.include_router(anomaly_router,       prefix="/api/v1", dependencies=protected)
    app.include_router(deadletter_router,    prefix="/api/v1", dependencies=protected)
    app.include_router(audit_router,         prefix="/api/v1", dependencies=protected)
    app.include_router(health_router)
    # /metrics is scraped by Prometheus — left unauthenticated like the probes.
    app.include_router(observability_router)

    return app


app = create_app()
