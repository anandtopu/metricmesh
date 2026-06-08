"""Application & business metrics exposition (MM-8.6).

Exposes Prometheus metrics for the platform's own behaviour — points ingested,
detection latency, anomalies found, alerts routed — plus a live Celery
queue-depth gauge, so the PRD SLOs become measurable and the Grafana "queue
depth" panel (deferred from MM-8.5) is unblocked.

**Multi-process model.** Metrics originate in several processes: the FastAPI
app (ingest counts) and the Celery prefork workers (detection / anomaly / alert
counts, each a separate child PID). To make all of them visible at the single
``api:8000/metrics`` endpoint Prometheus scrapes, every app container mounts a
shared volume and sets ``PROMETHEUS_MULTIPROC_DIR``; ``prometheus_client``
writes per-process files there and ``render_latest`` aggregates across all of
them via ``MultiProcessCollector``. When the env var is absent (host dev / unit
tests) the module degrades gracefully to single-process mode against the
default registry.

Queue depth is intentionally **not** a stored metric: it is broker state, so a
custom collector reads the Redis list length of each Celery queue at scrape
time. This sidesteps the awkward multiprocess-gauge semantics and is always
accurate.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import structlog
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = structlog.get_logger(__name__)

_MULTIPROC_DIR = os.environ.get("PROMETHEUS_MULTIPROC_DIR")

# Celery queues served by the workers (see workers/celery_app.py task_queues).
# With the Redis broker, each queue is a Redis LIST keyed by the queue name, so
# its length is the number of tasks waiting to be picked up.
_CELERY_QUEUES = ("fast", "slow", "alerts")


# ── Stored metrics ─────────────────────────────────────────────────────────
# Points accepted vs rejected by the ingest API.
INGEST_POINTS = Counter(
    "metricmesh_ingest_points_total",
    "Metric points ingested, partitioned by outcome.",
    ["status"],  # accepted | rejected
)

# Wall-clock time a detector spends fitting + scoring one metric. Buckets span
# sub-second statistical runs up to multi-minute Prophet fits.
DETECTION_DURATION = Histogram(
    "metricmesh_detection_duration_seconds",
    "Detector fit+detect duration per run.",
    ["detector"],  # zscore | iqr | stl | prophet | isolation_forest
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)

# Anomalies surfaced by each detector (pre-consensus, pre-dedup).
ANOMALIES_DETECTED = Counter(
    "metricmesh_anomalies_detected_total",
    "Anomalies emitted by detectors, before consensus/dedup.",
    ["detector"],
)

# Alerts successfully delivered, partitioned by sink.
ALERTS_ROUTED = Counter(
    "metricmesh_alerts_routed_total",
    "Alert deliveries that succeeded, per sink.",
    ["sink"],  # log | slack | pagerduty | webhook
)


# ── Live queue-depth collector ─────────────────────────────────────────────
class QueueDepthCollector(Collector):
    """Yield ``metricmesh_queue_depth{queue=...}`` by reading Redis at scrape time.

    Resilient by design: if Redis is unreachable it logs and emits no samples,
    so ``/metrics`` never fails because the broker is down.
    """

    def collect(self) -> Iterator[GaugeMetricFamily]:
        gauge = GaugeMetricFamily(
            "metricmesh_queue_depth",
            "Tasks waiting in each Celery queue (Redis list length).",
            labels=["queue"],
        )
        try:
            import redis

            from config import get_settings

            client = redis.from_url(get_settings().redis_url)
            try:
                for queue in _CELERY_QUEUES:
                    gauge.add_metric([queue], float(client.llen(queue)))
            finally:
                client.close()
        except Exception as exc:  # pragma: no cover - broker-down path
            log.debug("queue_depth.unavailable", error=str(exc))
            return
        yield gauge


# ── Exposition ─────────────────────────────────────────────────────────────
_qd_registered = False


def render_latest() -> tuple[bytes, str]:
    """Return ``(payload, content_type)`` for the ``/metrics`` endpoint.

    In multiprocess mode a fresh registry aggregates every process's stored
    metrics via ``MultiProcessCollector``; the queue-depth collector is attached
    to that throwaway registry. In single-process mode the default registry
    already holds the stored metrics and the collector is registered once.
    """
    global _qd_registered

    if _MULTIPROC_DIR:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(QueueDepthCollector())
    else:
        registry = REGISTRY
        if not _qd_registered:
            registry.register(QueueDepthCollector())
            _qd_registered = True

    return generate_latest(registry), CONTENT_TYPE_LATEST
