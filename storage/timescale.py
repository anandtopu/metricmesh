from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import pandas as pd
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config import get_settings

log = structlog.get_logger(__name__)

# ── Module-level engine + session factory (created once at startup) ────────
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def make_engine(dsn: str | None = None) -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        dsn or settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,           # detect stale connections before use
        pool_recycle=settings.db_pool_recycle,
        echo=False,
    )


def init_db() -> None:
    """Call once at application startup."""
    global _engine, _session_factory
    _engine = make_engine()
    _session_factory = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )


async def close_db() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory (for non-FastAPI callers like the scraper)."""
    assert _session_factory is not None, "Call init_db() at startup"
    return _session_factory


# ── FastAPI dependency ─────────────────────────────────────────────────────
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield a transactional session; rolls back on error."""
    assert _session_factory is not None, "Call init_db() at startup"
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Schema DDL ────────────────────────────────────────────────────────────
SETUP_SQL = """
CREATE TABLE IF NOT EXISTS metrics (
    time         TIMESTAMPTZ      NOT NULL,
    metric_name  TEXT             NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    labels       JSONB            DEFAULT '{}',
    source       TEXT             DEFAULT 'api',
    tenant       TEXT             NOT NULL DEFAULT 'default'
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

-- Multi-tenancy (MM-9.3): tag every metric row with its tenant. Idempotent add
-- for pre-tenancy tables (existing rows fall back to the default tenant).
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_metrics_name_time
    ON metrics (metric_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_tenant_name_time
    ON metrics (tenant, metric_name, time DESC);

CREATE MATERIALIZED VIEW IF NOT EXISTS metrics_1min
WITH (timescaledb.continuous) AS
    SELECT
        time_bucket('1 minute', time) AS bucket,
        tenant,
        metric_name,
        avg(value)   AS avg_value,
        max(value)   AS max_value,
        min(value)   AS min_value,
        count(*)     AS sample_count
    FROM metrics
    GROUP BY bucket, tenant, metric_name
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'metrics_1min',
    start_offset      => INTERVAL '10 minutes',
    end_offset        => INTERVAL '30 seconds',
    schedule_interval => INTERVAL '30 seconds',
    if_not_exists     => TRUE
);

ALTER TABLE metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby   = 'time DESC'
);

SELECT add_compression_policy(
    'metrics',
    INTERVAL '7 days',
    if_not_exists => TRUE
);

CREATE TABLE IF NOT EXISTS alerts (
    id           BIGSERIAL        PRIMARY KEY,
    created_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    metric_name  TEXT             NOT NULL,
    detector     TEXT             NOT NULL,
    score        DOUBLE PRECISION NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    fingerprint  TEXT             NOT NULL,
    routed_to    TEXT[]           DEFAULT '{}',
    label        TEXT             NOT NULL DEFAULT 'unlabeled',
    method       TEXT             NOT NULL DEFAULT '',
    tenant       TEXT             NOT NULL DEFAULT 'default'
);

-- Idempotent add for alerts tables created before the label column (MM-10.3).
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS label TEXT NOT NULL DEFAULT 'unlabeled';
-- Idempotent add for the detector method (MM-7.5): lets alert detail resolve the
-- exact threshold that fired (zscore/iqr/stl share detector='statistical').
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS method TEXT NOT NULL DEFAULT '';
-- Multi-tenancy (MM-9.3): tag every alert with its tenant.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts (fingerprint);
CREATE INDEX IF NOT EXISTS idx_alerts_metric_time ON alerts (metric_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_tenant_time ON alerts (tenant, created_at DESC);

CREATE TABLE IF NOT EXISTS detector_models (
    id            BIGSERIAL   PRIMARY KEY,
    metric_name   TEXT        NOT NULL,
    detector_type TEXT        NOT NULL,
    trained_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parameters    JSONB       DEFAULT '{}',
    tenant        TEXT        NOT NULL DEFAULT 'default'
);

-- Multi-tenancy (MM-9.3 Phase C): one model per (tenant, metric, detector). The
-- old UNIQUE(metric_name, detector_type) is replaced by a tenant-scoped unique
-- index so two tenants can each have a model for the same metric.
ALTER TABLE detector_models ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
ALTER TABLE detector_models DROP CONSTRAINT IF EXISTS detector_models_metric_name_detector_type_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_detector_models_tenant
    ON detector_models (tenant, metric_name, detector_type);

-- Dead-letter store (MM-4.6): a task that exhausts its retries is recorded here
-- (via the Celery task_failure signal) so failures are inspectable, not lost.
CREATE TABLE IF NOT EXISTS dead_letters (
    id          BIGSERIAL    PRIMARY KEY,
    failed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    task_name   TEXT         NOT NULL,
    task_id     TEXT,
    queue       TEXT,
    retries     INT          NOT NULL DEFAULT 0,
    exception   TEXT,
    args        JSONB        DEFAULT '[]',
    kwargs      JSONB        DEFAULT '{}',
    traceback   TEXT
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_failed_at ON dead_letters (failed_at DESC);
CREATE INDEX IF NOT EXISTS idx_dead_letters_task_name ON dead_letters (task_name);

-- Audit trail (MM-9.5): who/what/when for auth denials, feedback submissions,
-- and alert routing. principal is a non-reversible key id, never the raw key.
CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL    PRIMARY KEY,
    at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    action      TEXT         NOT NULL,
    principal   TEXT,
    outcome     TEXT,
    resource    TEXT,
    source_ip   TEXT,
    detail      JSONB        DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action);
"""


async def setup_schema(engine: AsyncEngine | None = None) -> None:
    """Run DDL to create hypertable, continuous aggregate, compression policy.

    Two driver constraints force statement-by-statement execution in
    AUTOCOMMIT mode rather than one transactional batch:
      * asyncpg prepares every query, and a prepared statement cannot contain
        multiple ';'-separated commands.
      * TimescaleDB's CREATE MATERIALIZED VIEW ... WITH (timescaledb.continuous)
        cannot run inside a transaction block.
    All statements use IF NOT EXISTS, so re-running is idempotent.
    """
    eng = engine or _engine
    assert eng is not None
    settings = get_settings()
    statements = [s.strip() for s in SETUP_SQL.split(";") if s.strip()]
    autocommit_engine = eng.execution_options(isolation_level="AUTOCOMMIT")
    async with autocommit_engine.connect() as conn:
        await _migrate_cagg_for_tenant(conn)
        for stmt in statements:
            await conn.execute(text(stmt))
        await _apply_retention_policy(conn, settings.metrics_retention_days)
    log.info(
        "schema.setup.complete",
        statements=len(statements),
        retention_days=settings.metrics_retention_days,
    )


async def _migrate_cagg_for_tenant(conn: Any) -> None:
    """Recreate the metrics_1min continuous aggregate tenant-aware (MM-9.3 Phase C).

    A continuous aggregate's GROUP BY cannot be altered in place, so if an old
    (pre-tenant) metrics_1min exists we drop it here (CASCADE removes its policy)
    and let the tenant-aware definition in SETUP_SQL recreate it. Guarded on the
    presence of a ``tenant`` column so it runs at most once. No-op on a fresh DB.
    """
    has_tenant = (
        await conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'metrics_1min' AND column_name = 'tenant'"
            )
        )
    ).first()
    cagg_exists = (
        await conn.execute(
            text(
                "SELECT 1 FROM timescaledb_information.continuous_aggregates "
                "WHERE view_name = 'metrics_1min'"
            )
        )
    ).first()
    if cagg_exists is not None and has_tenant is None:
        await conn.execute(text("DROP MATERIALIZED VIEW IF EXISTS metrics_1min CASCADE"))
        log.info("schema.migrate.cagg_recreated_for_tenant")


async def _apply_retention_policy(conn: Any, retention_days: int) -> None:
    """
    Configure the raw-metrics retention policy (MM-2.4).

    Re-applied on every startup so a changed ``METRICS_RETENTION_DAYS`` actually
    takes effect: we drop any existing policy first, then add the configured one.
    ``retention_days == 0`` disables retention (keep data forever).
    """
    await conn.execute(text("SELECT remove_retention_policy('metrics', if_exists => TRUE)"))
    if retention_days > 0:
        await conn.execute(
            text(
                "SELECT add_retention_policy('metrics', "
                "drop_after => make_interval(days => :days))"
            ),
            {"days": retention_days},
        )


# ── Bulk insert via unnest() ───────────────────────────────────────────────
async def bulk_insert(
    session: AsyncSession,
    points: list[dict[str, Any]],
    tenant: str = "default",
) -> int:
    """
    High-throughput bulk insert using PostgreSQL unnest().
    Avoids N individual INSERTs; pushes all data in a single round-trip.

    ``tenant`` (MM-9.3) is the authenticated caller's tenant and is applied to
    every row in the batch — it comes from the API key, never from the request
    body, so a client can't write into another tenant's data.

    Python skill: list comprehension for columnar transposition,
    named bind parameters with SQLAlchemy text().
    """
    if not points:
        return 0

    import json

    times  = [p["timestamp"] for p in points]
    names  = [p["metric_name"] for p in points]
    values = [float(p["value"]) for p in points]
    labels = [json.dumps(p.get("labels", {})) for p in points]
    sources = [p.get("source", "api") for p in points]

    result = await session.execute(
        text("""
            INSERT INTO metrics (time, metric_name, value, labels, source, tenant)
            SELECT
                to_timestamp(t),
                n,
                v,
                l::jsonb,
                s,
                :tenant
            FROM unnest(
                CAST(:times   AS float8[]),
                CAST(:names   AS text[]),
                CAST(:values  AS float8[]),
                CAST(:labels  AS text[]),
                CAST(:sources AS text[])
            ) AS r(t, n, v, l, s)
            ON CONFLICT DO NOTHING
        """),
        {
            "times":   times,
            "names":   names,
            "values":  values,
            "labels":  labels,
            "sources": sources,
            "tenant":  tenant,
        },
    )
    return int(result.rowcount)  # type: ignore[attr-defined]  # CursorResult.rowcount


# ── Series fetch — feeds detectors ────────────────────────────────────────
async def fetch_series(
    session: AsyncSession,
    metric_name: str,
    lookback_hours: int | None = None,
    tenant: str = "default",
) -> pd.Series:
    """
    Fetch a 1-minute bucketed series for a metric, scoped to ``tenant`` (MM-9.3).
    Returns a pd.Series of values indexed by tz-aware UTC timestamp — this is
    exactly what the detectors' fit()/score()/detect() expect.

    Python skill: async query -> pandas Series, tz-aware datetime handling.
    """
    hours = lookback_hours or get_settings().detection_lookback_hours
    rows = await session.execute(
        text("""
            SELECT
                time_bucket('1 minute', time) AS ts,
                avg(value) AS value
            FROM metrics
            WHERE metric_name = :name
              AND tenant = :tenant
              AND time > NOW() - make_interval(hours => :hours)
            GROUP BY ts
            ORDER BY ts ASC
        """),
        {"name": metric_name, "hours": hours, "tenant": tenant},
    )
    df = pd.DataFrame(rows.fetchall(), columns=["ts", "value"])
    if df.empty:
        return pd.Series(dtype="float64", name="value")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")["value"]


def fetch_series_sync(
    metric_name: str,
    lookback_hours: int | None = None,
    tenant: str = "default",
) -> pd.Series:
    """
    Synchronous wrapper for use inside Celery tasks (no running event loop),
    scoped to ``tenant`` (MM-9.3 Phase C). Returns a pd.Series of values indexed
    by tz-aware UTC timestamp, matching the detector contract.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    from sqlalchemy import text as t_
    hours = lookback_hours or settings.detection_lookback_hours

    with Session() as session:
        rows = session.execute(
            t_("""
                SELECT
                    time_bucket('1 minute', time) AS ts,
                    avg(value) AS value
                FROM metrics
                WHERE metric_name = :name
                  AND tenant = :tenant
                  AND time > NOW() - make_interval(hours => :hours)
                GROUP BY ts
                ORDER BY ts ASC
            """),
            {"name": metric_name, "hours": hours, "tenant": tenant},
        )
        df = pd.DataFrame(rows.fetchall(), columns=["ts", "value"])
    if df.empty:
        return pd.Series(dtype="float64", name="value")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")["value"]


async def list_active_metrics(
    session: AsyncSession,
    lookback_hours: int = 1,
    tenant: str = "default",
) -> list[str]:
    """Return metric names that received data in the last N hours for ``tenant``."""
    rows = await session.execute(
        text("""
            SELECT DISTINCT metric_name
            FROM metrics
            WHERE time > NOW() - make_interval(hours => :hours)
              AND tenant = :tenant
            ORDER BY metric_name
        """),
        {"hours": lookback_hours, "tenant": tenant},
    )
    return [r[0] for r in rows.fetchall()]


def list_active_metrics_sync(lookback_hours: int = 1) -> list[tuple[str, str]]:
    """Synchronous version for the Celery beat scheduler.

    Returns ``(tenant, metric_name)`` pairs (MM-9.3 Phase C) so the sweep runs
    detectors per tenant — the same metric name under two tenants is detected and
    alerted independently.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import text as t_
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    with Session() as session:
        rows = session.execute(
            t_("SELECT DISTINCT tenant, metric_name FROM metrics "
               "WHERE time > NOW() - make_interval(hours => :h)"),
            {"h": lookback_hours},
        )
        return [(r[0], r[1]) for r in rows.fetchall()]


# ── Alert history persistence ──────────────────────────────────────────────
def persist_alert_sync(anomaly: dict[str, Any], routed_to: list[str]) -> None:
    """
    Insert a routed alert into the `alerts` table (history for the query API).

    Synchronous, for use inside the Celery `route_alert` task (no event loop).
    `routed_to` is the list of sink names that successfully delivered the alert.
    psycopg2 adapts the Python list to a PostgreSQL TEXT[] automatically.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import text as t_
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    with Session() as session:
        session.execute(
            t_("""
                INSERT INTO alerts
                    (metric_name, detector, method, score, value, fingerprint, routed_to, tenant)
                VALUES
                    (:metric_name, :detector, :method, :score, :value, :fingerprint,
                     :routed_to, :tenant)
            """),
            {
                "metric_name": anomaly.get("metric_name", ""),
                "detector":    anomaly.get("detector", ""),
                "method":      anomaly.get("method", ""),
                "score":       float(anomaly.get("score", 0.0)),
                "value":       float(anomaly.get("value", 0.0)),
                "fingerprint": anomaly.get("fingerprint", ""),
                "routed_to":   routed_to,
                "tenant":      anomaly.get("tenant", "default"),
            },
        )
        session.commit()


# ── Dead-letter store (MM-4.6) ─────────────────────────────────────────────
def persist_dead_letter_sync(
    task_name: str,
    task_id: str | None,
    queue: str | None,
    retries: int,
    exception: str,
    args: Any,
    kwargs: Any,
    traceback: str | None,
) -> None:
    """Record a task that exhausted its retries into ``dead_letters``.

    Synchronous (called from the Celery ``task_failure`` signal, no event loop).
    ``args``/``kwargs`` are JSON-encoded with ``default=str`` so even
    non-serializable payloads are captured rather than lost.
    """
    import json

    from sqlalchemy import create_engine
    from sqlalchemy import text as t_
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    with Session() as session:
        session.execute(
            t_("""
                INSERT INTO dead_letters
                    (task_name, task_id, queue, retries, exception, args, kwargs, traceback)
                VALUES
                    (:task_name, :task_id, :queue, :retries, :exception,
                     CAST(:args AS JSONB), CAST(:kwargs AS JSONB), :traceback)
            """),
            {
                "task_name": task_name,
                "task_id": task_id,
                "queue": queue,
                "retries": int(retries or 0),
                "exception": exception,
                "args": json.dumps(args, default=str),
                "kwargs": json.dumps(kwargs, default=str),
                "traceback": traceback,
            },
        )
        session.commit()


# ── Audit trail (MM-9.5) ───────────────────────────────────────────────────
_AUDIT_INSERT = """
    INSERT INTO audit_log (action, principal, outcome, resource, source_ip, detail)
    VALUES (:action, :principal, :outcome, :resource, :source_ip, CAST(:detail AS JSONB))
"""


def _audit_params(
    action: str,
    principal: str | None,
    outcome: str | None,
    resource: str | None,
    source_ip: str | None,
    detail: dict[str, Any] | None,
) -> dict[str, Any]:
    import json

    return {
        "action": action,
        "principal": principal,
        "outcome": outcome,
        "resource": resource,
        "source_ip": source_ip,
        "detail": json.dumps(detail or {}, default=str),
    }


def record_audit_sync(
    action: str,
    principal: str | None = None,
    outcome: str | None = None,
    resource: str | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit insert from a sync (Celery) context. Gated on
    ``audit_enabled``; never raises — auditing must not break the audited op."""
    settings = get_settings()
    if not settings.audit_enabled:
        return
    try:
        from sqlalchemy import create_engine
        from sqlalchemy import text as t_
        from sqlalchemy.orm import sessionmaker

        sync_engine = create_engine(settings.database_url_sync)
        Session = sessionmaker(sync_engine)
        with Session() as session:
            session.execute(
                t_(_AUDIT_INSERT),
                _audit_params(action, principal, outcome, resource, source_ip, detail),
            )
            session.commit()
    except Exception as exc:  # pragma: no cover - defensive
        import structlog

        structlog.get_logger(__name__).error("audit.persist_failed", action=action, error=str(exc))


async def record_audit_async(
    action: str,
    principal: str | None = None,
    outcome: str | None = None,
    resource: str | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit insert from an async (FastAPI) context using the module
    engine. Gated on ``audit_enabled``; never raises."""
    settings = get_settings()
    if not settings.audit_enabled:
        return
    try:
        assert _engine is not None, "database not initialised"
        async with _engine.begin() as conn:
            await conn.execute(
                text(_AUDIT_INSERT),
                _audit_params(action, principal, outcome, resource, source_ip, detail),
            )
    except Exception as exc:
        import structlog

        structlog.get_logger(__name__).error("audit.persist_failed", action=action, error=str(exc))


# ── Detector model persistence (MM-10.1) ───────────────────────────────────
def persist_detector_model_sync(
    metric_name: str,
    detector_type: str,
    parameters: dict[str, Any],
    tenant: str = "default",
) -> None:
    """Upsert a trained detector's params/metadata into ``detector_models``.

    One row per ``(tenant, metric_name, detector_type)`` (MM-9.3 Phase C); a
    re-fit overwrites the parameters and bumps ``trained_at``. Synchronous, for
    the Celery detector tasks (no event loop). ``parameters`` is stored as JSONB.
    """
    import json

    from sqlalchemy import create_engine
    from sqlalchemy import text as t_
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    with Session() as session:
        session.execute(
            t_("""
                INSERT INTO detector_models
                    (tenant, metric_name, detector_type, parameters, trained_at)
                VALUES (:tenant, :metric_name, :detector_type, CAST(:parameters AS jsonb), NOW())
                ON CONFLICT (tenant, metric_name, detector_type)
                DO UPDATE SET parameters = EXCLUDED.parameters, trained_at = NOW()
            """),
            {
                "tenant": tenant,
                "metric_name": metric_name,
                "detector_type": detector_type,
                "parameters": json.dumps(parameters),
            },
        )
        session.commit()


def fetch_detector_model_sync(
    metric_name: str,
    detector_type: str,
    max_age_seconds: int,
    tenant: str = "default",
) -> dict[str, Any] | None:
    """Return a recently-trained model's params, or ``None`` if missing/stale.

    A model is reusable only if it was trained within ``max_age_seconds``; older
    rows return ``None`` so the caller re-fits. Scoped to ``tenant`` (MM-9.3
    Phase C). Synchronous, for Celery tasks.
    """
    import json

    from sqlalchemy import create_engine
    from sqlalchemy import text as t_
    from sqlalchemy.orm import sessionmaker

    settings = get_settings()
    sync_engine = create_engine(settings.database_url_sync)
    Session = sessionmaker(sync_engine)

    with Session() as session:
        row = session.execute(
            t_("""
                SELECT parameters
                FROM detector_models
                WHERE metric_name = :metric_name
                  AND detector_type = :detector_type
                  AND tenant = :tenant
                  AND trained_at > NOW() - make_interval(secs => :age)
            """),
            {
                "metric_name": metric_name,
                "detector_type": detector_type,
                "tenant": tenant,
                "age": max_age_seconds,
            },
        ).first()

    if row is None:
        return None
    params = row[0]
    # psycopg2 usually adapts jsonb to a dict already; tolerate a raw string too.
    parsed: dict[str, Any] = json.loads(params) if isinstance(params, str) else params
    return parsed


# ── Alert history query (MM-7.2) ───────────────────────────────────────────
async def fetch_alerts(
    session: AsyncSession,
    metric_name: str | None = None,
    detector: str | None = None,
    min_score: float | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    tenant: str = "default",
) -> tuple[list[dict[str, Any]], int]:
    """
    Query persisted alerts with optional filters, newest first, paginated.
    Always scoped to ``tenant`` (MM-9.3).

    Returns ``(items, total)`` where ``total`` is the full match count (ignoring
    limit/offset) so callers can build pagination. All filter values are bound
    parameters — only the static column names are interpolated.
    """
    clauses: list[str] = ["tenant = :tenant"]
    params: dict[str, Any] = {"tenant": tenant}
    if metric_name:
        clauses.append("metric_name = :metric_name")
        params["metric_name"] = metric_name
    if detector:
        clauses.append("detector = :detector")
        params["detector"] = detector
    if min_score is not None:
        clauses.append("score >= :min_score")
        params["min_score"] = min_score
    if start is not None:
        clauses.append("created_at >= :start")
        params["start"] = start
    if end is not None:
        clauses.append("created_at <= :end")
        params["end"] = end

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    total = (
        await session.execute(text(f"SELECT count(*) FROM alerts{where}"), params)
    ).scalar_one()

    result = await session.execute(
        text(f"""
            SELECT id, created_at, metric_name, detector,
                   score, value, fingerprint, routed_to, label
            FROM alerts{where}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    )
    items = [
        {
            "id": m["id"],
            "created_at": m["created_at"].isoformat(),
            "metric_name": m["metric_name"],
            "detector": m["detector"],
            "score": float(m["score"]),
            "value": float(m["value"]),
            "fingerprint": m["fingerprint"],
            "routed_to": list(m["routed_to"] or []),
            "label": m["label"],
        }
        for m in result.mappings()
    ]
    return items, int(total)


async def fetch_alert_by_id(
    session: AsyncSession, alert_id: int, tenant: str = "default"
) -> dict[str, Any] | None:
    """Return a single alert row as a dict, or ``None`` if it does not exist
    *for this tenant* (MM-9.3 — a tenant cannot read another's alert by id).

    Includes ``method`` (MM-7.5) so callers can resolve the exact threshold the
    detector used (zscore/iqr/stl all store detector='statistical').
    """
    result = await session.execute(
        text("""
            SELECT id, created_at, metric_name, detector, method,
                   score, value, fingerprint, routed_to, label
            FROM alerts
            WHERE id = :id AND tenant = :tenant
        """),
        {"id": alert_id, "tenant": tenant},
    )
    m = result.mappings().first()
    if m is None:
        return None
    return {
        "id": m["id"],
        "created_at": m["created_at"].isoformat(),
        "created_at_dt": m["created_at"],
        "metric_name": m["metric_name"],
        "detector": m["detector"],
        "method": m["method"] or "",
        "score": float(m["score"]),
        "value": float(m["value"]),
        "fingerprint": m["fingerprint"],
        "routed_to": list(m["routed_to"] or []),
        "label": m["label"],
    }


async def fetch_dead_letters(
    session: AsyncSession,
    task_name: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Query the dead-letter store, newest first, paginated (MM-4.6).

    Returns ``(items, total)`` where ``total`` is the full match count. The
    ``args``/``kwargs`` JSONB columns are decoded to Python objects.
    """
    import json

    def _decode(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v

    where = ""
    params: dict[str, Any] = {}
    if task_name:
        where = " WHERE task_name = :task_name"
        params["task_name"] = task_name

    total = (
        await session.execute(text(f"SELECT count(*) FROM dead_letters{where}"), params)
    ).scalar_one()

    result = await session.execute(
        text(f"""
            SELECT id, failed_at, task_name, task_id, queue, retries,
                   exception, args, kwargs, traceback
            FROM dead_letters{where}
            ORDER BY failed_at DESC, id DESC
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    )
    items = [
        {
            "id": m["id"],
            "failed_at": m["failed_at"].isoformat(),
            "task_name": m["task_name"],
            "task_id": m["task_id"],
            "queue": m["queue"],
            "retries": int(m["retries"]),
            "exception": m["exception"],
            "args": _decode(m["args"]),
            "kwargs": _decode(m["kwargs"]),
            "traceback": m["traceback"],
        }
        for m in result.mappings()
    ]
    return items, int(total)


async def fetch_audit_log(
    session: AsyncSession,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Query the audit trail, newest first, paginated (MM-9.5)."""
    import json

    def _decode(v: Any) -> Any:
        if isinstance(v, str):
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v

    where = ""
    params: dict[str, Any] = {}
    if action:
        where = " WHERE action = :action"
        params["action"] = action

    total = (
        await session.execute(text(f"SELECT count(*) FROM audit_log{where}"), params)
    ).scalar_one()

    result = await session.execute(
        text(f"""
            SELECT id, at, action, principal, outcome, resource, source_ip, detail
            FROM audit_log{where}
            ORDER BY at DESC, id DESC
            LIMIT :limit OFFSET :offset
        """),
        {**params, "limit": limit, "offset": offset},
    )
    items = [
        {
            "id": m["id"],
            "at": m["at"].isoformat(),
            "action": m["action"],
            "principal": m["principal"],
            "outcome": m["outcome"],
            "resource": m["resource"],
            "source_ip": m["source_ip"],
            "detail": _decode(m["detail"]),
        }
        for m in result.mappings()
    ]
    return items, int(total)


# ── Feedback labeling (MM-10.3) ────────────────────────────────────────────
async def set_alert_label(
    session: AsyncSession, alert_id: int, label: str, tenant: str = "default"
) -> bool:
    """Set the feedback label on an alert. Returns False if no such alert *for
    this tenant* (MM-9.3 — a tenant cannot label another's alert)."""
    result = await session.execute(
        text("UPDATE alerts SET label = :label WHERE id = :id AND tenant = :tenant"),
        {"label": label, "id": alert_id, "tenant": tenant},
    )
    return bool(result.rowcount > 0)  # type: ignore[attr-defined]  # CursorResult.rowcount


async def feedback_stats_by_detector(
    session: AsyncSession, tenant: str = "default"
) -> list[dict[str, Any]]:
    """
    Per-detector feedback summary with precision = TP / (TP + FP) over labeled
    alerts, scoped to ``tenant`` (MM-9.3). (Recall needs ground-truth missed
    anomalies, which alert feedback alone can't provide.)
    """
    rows = await session.execute(
        text("""
            SELECT detector,
                   count(*) FILTER (WHERE label = 'true_positive')  AS tp,
                   count(*) FILTER (WHERE label = 'false_positive') AS fp,
                   count(*) FILTER (WHERE label = 'unlabeled')      AS unlabeled,
                   count(*)                                         AS total
            FROM alerts
            WHERE tenant = :tenant
            GROUP BY detector
            ORDER BY detector
        """),
        {"tenant": tenant},
    )
    stats = []
    for m in rows.mappings():
        tp, fp = int(m["tp"]), int(m["fp"])
        labeled = tp + fp
        stats.append({
            "detector": m["detector"],
            "true_positive": tp,
            "false_positive": fp,
            "unlabeled": int(m["unlabeled"]),
            "total": int(m["total"]),
            "precision": round(tp / labeled, 4) if labeled else None,
        })
    return stats


async def detector_report_rows(
    session: AsyncSession,
    metric_name: str | None = None,
    bucket_minutes: int = 5,
    tenant: str = "default",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Raw aggregates for the per-metric detector A/B report (MM-10.4).

    Returns ``(counts, recall)`` where:

    - ``counts`` is one row per ``(metric_name, detector)`` with tp/fp/unlabeled
      /total — the basis for **precision**.
    - ``recall`` is one row per ``(metric_name, detector)`` with the number of
      distinct time buckets in which that detector produced a *confirmed*
      true-positive (``covered_buckets``) and the total distinct buckets where
      **any** detector did for that metric (``total_buckets``) — the basis for a
      **comparative recall** (coverage of the human-confirmed anomaly set). This
      is *not* true recall: it cannot see anomalies that no detector caught.

    The assembly/ranking is done by a pure helper in the route layer so it stays
    unit-testable without a DB.
    """
    # Tenant is always applied (MM-9.3); metric_name is an optional extra filter.
    where = " AND tenant = :tenant"
    params: dict[str, Any] = {"bucket_minutes": bucket_minutes, "tenant": tenant}
    count_where = " WHERE tenant = :tenant"
    if metric_name:
        where += " AND metric_name = :metric_name"
        count_where += " AND metric_name = :metric_name"
        params["metric_name"] = metric_name

    count_result = await session.execute(
        text(f"""
            SELECT metric_name, detector,
                   count(*) FILTER (WHERE label = 'true_positive')  AS tp,
                   count(*) FILTER (WHERE label = 'false_positive') AS fp,
                   count(*) FILTER (WHERE label = 'unlabeled')      AS unlabeled,
                   count(*)                                         AS total
            FROM alerts{count_where}
            GROUP BY metric_name, detector
            ORDER BY metric_name, detector
        """),
        params,
    )
    counts = [
        {
            "metric_name": m["metric_name"],
            "detector": m["detector"],
            "tp": int(m["tp"]),
            "fp": int(m["fp"]),
            "unlabeled": int(m["unlabeled"]),
            "total": int(m["total"]),
        }
        for m in count_result.mappings()
    ]

    recall_result = await session.execute(
        text(f"""
            WITH tp AS (
                SELECT metric_name, detector,
                       time_bucket(make_interval(mins => :bucket_minutes), created_at) AS bucket
                FROM alerts
                WHERE label = 'true_positive'{where}
            )
            SELECT metric_name, detector,
                   count(DISTINCT bucket) AS covered_buckets,
                   (SELECT count(DISTINCT bucket) FROM tp t2
                    WHERE t2.metric_name = t1.metric_name) AS total_buckets
            FROM tp t1
            GROUP BY metric_name, detector
        """),
        params,
    )
    recall = [
        {
            "metric_name": m["metric_name"],
            "detector": m["detector"],
            "covered_buckets": int(m["covered_buckets"]),
            "total_buckets": int(m["total_buckets"]),
        }
        for m in recall_result.mappings()
    ]
    return counts, recall


# ── Metric series read (MM-7.4) ────────────────────────────────────────────
async def fetch_metric_series(
    session: AsyncSession,
    metric_name: str,
    start: datetime | None = None,
    end: datetime | None = None,
    resolution: str = "1m",
    limit: int = 10_000,
    tenant: str = "default",
) -> list[dict[str, Any]]:
    """
    Read a metric's time series over an optional [start, end] window.

    - ``resolution="raw"`` → individual stored points: ``{ts, value}``.
    - ``resolution="1m"``  → 1-minute downsample: ``{ts, avg, max, min, count}``.

    The 1m view is computed on the fly with ``time_bucket`` (identical in shape
    and meaning to the ``metrics_1min`` continuous aggregate) so it covers the
    full retention window, not just the materialised rolling region.

    Time bounds and the metric name are bound parameters; ``resolution`` is a
    controlled value chosen by the caller and never interpolated into SQL.
    """
    params: dict[str, Any] = {"name": metric_name, "limit": limit, "tenant": tenant}
    where = ["metric_name = :name", "tenant = :tenant"]
    if start is not None:
        where.append("time >= :start")
        params["start"] = start
    if end is not None:
        where.append("time <= :end")
        params["end"] = end
    where_sql = " AND ".join(where)

    if resolution == "raw":
        result = await session.execute(
            text(f"""
                SELECT time AS ts, value
                FROM metrics
                WHERE {where_sql}
                ORDER BY time ASC
                LIMIT :limit
            """),
            params,
        )
        return [
            {"ts": m["ts"].isoformat(), "value": float(m["value"])}
            for m in result.mappings()
        ]

    result = await session.execute(
        text(f"""
            SELECT time_bucket('1 minute', time) AS ts,
                   avg(value) AS avg_value,
                   max(value) AS max_value,
                   min(value) AS min_value,
                   count(*)   AS sample_count
            FROM metrics
            WHERE {where_sql}
            GROUP BY ts
            ORDER BY ts ASC
            LIMIT :limit
        """),
        params,
    )
    return [
        {
            "ts": m["ts"].isoformat(),
            "avg": float(m["avg_value"]),
            "max": float(m["max_value"]),
            "min": float(m["min_value"]),
            "count": int(m["sample_count"]),
        }
        for m in result.mappings()
    ]
