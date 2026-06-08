from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Secret-store integration (MM-9.4): when a secrets directory is mounted
# (Docker secrets / Kubernetes secret volumes default to /run/secrets), each
# setting can be supplied as a file named after the field — e.g.
# /run/secrets/slack_webhook_url — instead of an env var. Disabled when the
# directory is absent, so local/dev runs fall back to .env / env vars.
_SECRETS_DIR = os.environ.get("SECRETS_DIR", "/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        secrets_dir=_SECRETS_DIR if os.path.isdir(_SECRETS_DIR) else None,
    )

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = Field(
        default="postgresql+asyncpg://mm:mm@localhost:5432/metricmesh"
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://mm:mm@localhost:5432/metricmesh"
    )
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_recycle: int = 3600
    # Drop raw metric chunks older than this many days (0 = keep forever).
    metrics_retention_days: int = Field(default=30, ge=0)

    # ── Ingestion / Prometheus scraper ────────────────────────────────────
    # When set, the (opt-in) scraper service polls this /metrics endpoint.
    prometheus_scrape_url: str = ""
    prometheus_scrape_interval: float = Field(default=15.0, gt=0)
    prometheus_scrape_batch_size: int = Field(default=200, ge=1)

    # ── Redis / Celery ────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── Alerting ──────────────────────────────────────────────────────────
    slack_webhook_url: str = ""
    pagerduty_routing_key: str = ""
    # Additional alert sinks (MM-6.5), all opt-in (empty = sink not registered).
    teams_webhook_url: str = ""           # Microsoft Teams Incoming Webhook URL
    generic_webhook_url: str = ""         # arbitrary JSON POST target
    generic_webhook_headers: str = ""     # optional JSON object of HTTP headers
    # Email/SMTP sink — registered only when smtp_host AND alert_email_to are set.
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    alert_email_from: str = ""
    alert_email_to: str = ""              # comma-separated recipient list
    alert_cooldown_seconds: int = 300
    # Dedup/consensus time-bucket width (MM-5.3). Anomalies whose timestamps fall
    # in the same `floor(epoch / dedup_bucket_seconds)` bucket are treated as the
    # same event for deduplication (alerting/dedup.py) and consensus grouping
    # (alerting/consensus.py). Tunable per environment; default 300s (5 min).
    dedup_bucket_seconds: int = Field(default=300, ge=1)
    # Optional JSON routing rules (MM-6.6): route anomalies to specific sinks by
    # metric-name glob. First match wins; empty = fan out to all configured sinks.
    # e.g. '[{"match":"db.*","sinks":["pagerduty","log"]},{"match":"*","sinks":["log"]}]'
    alert_routing_rules: str = ""
    # Ensemble/consensus scoring (MM-10.5): only route an anomaly when at least
    # this many *distinct* detectors flag the same metric at the same time
    # bucket. 1 = disabled (route every detector's anomaly — backward compatible);
    # 2+ suppresses single-detector noise.
    consensus_min_detectors: int = Field(default=1, ge=1)

    # ── Detection thresholds ──────────────────────────────────────────────
    zscore_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    iqr_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    prophet_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    isolation_forest_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    isolation_forest_contamination: float = Field(default=0.05, ge=0.001, le=0.5)
    detection_lookback_hours: int = 24
    # Per-metric threshold overrides (MM-10.2): JSON object mapping a metric-name
    # glob to per-detector threshold overrides. First matching glob wins; missing
    # detectors fall back to the global *_threshold above. Empty = all global.
    # e.g. '{"db.*": {"zscore": 0.9, "isolation_forest": 0.85}, "cpu.usage": {"prophet": 0.6}}'
    metric_thresholds: str = ""
    # Detector model reuse (MM-10.1): when a fitted model persisted to
    # detector_models is newer than this many seconds, the statistical detector
    # reuses it instead of re-fitting. 0 = disabled (always re-fit — default).
    # Prophet/Isolation Forest always re-fit (their fitted state isn't
    # JSON-serializable), but their metadata is still persisted.
    model_reuse_max_age_seconds: int = Field(default=0, ge=0)
    # Score normalisation mode (MM-3.6). "relative" (default) normalises each
    # detector's scores per-batch (divide by the batch max), so thresholds are
    # relative to the current window. "absolute" maps scores against a fixed
    # statistical reference, so a given threshold means the same thing across
    # windows. iqr/prophet are inherently absolute and unaffected.
    scoring_mode: str = "relative"

    # ── Grafana ───────────────────────────────────────────────────────────
    grafana_url: str = "http://localhost:3000"
    grafana_api_key: str = ""

    # ── API security ──────────────────────────────────────────────────────
    # Comma-separated CORS allow-list (never "*"). Defaults to local origins.
    cors_allow_origins: str = "http://localhost:3000,http://localhost:8000"
    # Comma-separated valid API keys. When empty, auth is DISABLED (dev mode);
    # when set, data endpoints require a matching X-API-Key header.
    api_keys: str = ""
    # Audit logging (MM-9.5): record who/what/when for auth denials, feedback
    # submissions, and alert routing into the audit_log table. Best-effort — a
    # write failure never blocks the audited operation. On by default (it is a
    # security feature); set AUDIT_ENABLED=false to disable.
    audit_enabled: bool = True
    # Multi-tenancy (MM-9.3). Each request is attributed to a tenant, and metrics
    # are tagged/queried per tenant so one tenant never sees another's data.
    # `tenant_api_keys` is a JSON map of API key -> tenant for tenant-scoped keys;
    # keys listed in `api_keys` map to `default_tenant`. With no keys configured
    # (dev mode) every request uses `default_tenant`, so the demo stays single-
    # tenant and frictionless. e.g. {"acme-key":"acme","globex-key":"globex"}
    default_tenant: str = "default"
    tenant_api_keys: str = ""
    # Max ingest requests per minute per client (API key, else IP). 0 = disabled.
    rate_limit_per_minute: int = Field(default=0, ge=0)

    # ── App ───────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    app_name: str = "MetricMesh"
    app_version: str = "0.1.0"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def api_keys_set(self) -> set[str]:
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}

    @property
    def key_tenant_map(self) -> dict[str, str]:
        """Authoritative API key -> tenant mapping (MM-9.3).

        Plain ``api_keys`` map to ``default_tenant``; ``tenant_api_keys`` (JSON)
        overrides/extends with tenant-scoped keys. Empty result = auth disabled
        (dev mode). Malformed ``tenant_api_keys`` JSON fails safe to the plain
        keys rather than locking everyone out.
        """
        import json

        mapping: dict[str, str] = {k: self.default_tenant for k in self.api_keys_set}
        raw = self.tenant_api_keys.strip()
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, dict):
                    for k, v in data.items():
                        key = str(k).strip()
                        if key:
                            mapping[key] = str(v)
            except (ValueError, TypeError):
                pass  # fail-safe: ignore malformed map, keep plain api_keys
        return mapping

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return upper

    @field_validator("scoring_mode")
    @classmethod
    def validate_scoring_mode(cls, v: str) -> str:
        lower = v.lower()
        if lower not in {"relative", "absolute"}:
            raise ValueError("scoring_mode must be 'relative' or 'absolute'")
        return lower


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance — import this everywhere."""
    return Settings()
