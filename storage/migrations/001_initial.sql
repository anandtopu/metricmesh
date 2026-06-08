-- MetricMesh — initial migration
-- Run with: psql $DATABASE_URL -f migrations/001_initial.sql

-- Ensure TimescaleDB extension is present
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- Core metrics hypertable
CREATE TABLE IF NOT EXISTS metrics (
    time         TIMESTAMPTZ      NOT NULL,
    metric_name  TEXT             NOT NULL,
    value        DOUBLE PRECISION NOT NULL,
    labels       JSONB            DEFAULT '{}',
    source       TEXT             DEFAULT 'api',
    tenant       TEXT             NOT NULL DEFAULT 'default'
);

SELECT create_hypertable('metrics', 'time', if_not_exists => TRUE);

-- Multi-tenancy (MM-9.3): idempotent add for pre-tenancy tables.
ALTER TABLE metrics ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_metrics_name_time
    ON metrics (metric_name, time DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_tenant_name_time
    ON metrics (tenant, metric_name, time DESC);

-- 1-minute continuous aggregate (auto-refreshed)
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

-- Alert history table
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

ALTER TABLE alerts ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_alerts_fingerprint ON alerts (fingerprint);
CREATE INDEX IF NOT EXISTS idx_alerts_metric_time ON alerts (metric_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_tenant_time ON alerts (tenant, created_at DESC);

-- Detector model metadata (one row per tenant, metric, detector — MM-9.3)
CREATE TABLE IF NOT EXISTS detector_models (
    id            BIGSERIAL   PRIMARY KEY,
    metric_name   TEXT        NOT NULL,
    detector_type TEXT        NOT NULL,
    trained_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parameters    JSONB       DEFAULT '{}',
    tenant        TEXT        NOT NULL DEFAULT 'default'
);

ALTER TABLE detector_models ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
ALTER TABLE detector_models DROP CONSTRAINT IF EXISTS detector_models_metric_name_detector_type_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_detector_models_tenant
    ON detector_models (tenant, metric_name, detector_type);

-- Dead-letter store (MM-4.6): tasks that exhaust their retries land here.
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

-- Audit trail (MM-9.5): who/what/when for auth/feedback/routing events.
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

-- Compression: enable columnstore on the hypertable, then compress chunks > 7 days
ALTER TABLE metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby   = 'time DESC'
);

SELECT add_compression_policy('metrics', INTERVAL '7 days', if_not_exists => TRUE);
