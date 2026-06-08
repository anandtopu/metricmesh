from __future__ import annotations

from typing import Any

import structlog
from celery import chord, group, shared_task

from config import get_settings

# structlog (used across the codebase) accepts structured kwargs like
# log.info("event", metric=..., count=...). The stdlib celery task logger does
# not and raises TypeError when INFO is enabled, which silently broke the sweep.
log = structlog.get_logger(__name__)


# ── Statistical detector task ─────────────────────────────────────────────
@shared_task(
    bind=True,
    name="workers.tasks.run_statistical",
    max_retries=3,
    default_retry_delay=10,
    autoretry_for=(Exception,),
    retry_backoff=True,    # 10s → 20s → 40s
    retry_jitter=True,     # randomise to avoid thundering herd on Redis restart
    soft_time_limit=30,
    time_limit=60,
)
def run_statistical(
    self: Any, metric_name: str, method: str = "zscore", tenant: str = "default"
) -> dict[str, Any]:
    """
    Python skill: bind=True gives access to self (the task instance),
    enabling self.retry() with state. autoretry_for with exponential
    backoff + jitter is the production-correct retry pattern.

    Scoped to ``tenant`` (MM-9.3 Phase C): series, persisted model, and the
    emitted anomalies are all tenant-specific.
    """
    import time

    from detection.statistical import StatisticalDetector
    from detection.thresholds import resolve_threshold
    from monitoring.metrics import ANOMALIES_DETECTED, DETECTION_DURATION
    from storage.timescale import (
        fetch_detector_model_sync,
        fetch_series_sync,
        persist_detector_model_sync,
    )

    settings = get_settings()

    try:
        series = fetch_series_sync(metric_name, tenant=tenant)
        if series.empty:
            return {"metric": metric_name, "anomalies": [], "detector": "statistical"}

        _t0 = time.perf_counter()
        detector = StatisticalDetector(method=method, scoring_mode=settings.scoring_mode)
        split = max(int(len(series) * 0.8), 1)

        # MM-10.1: reuse a recently-fitted model instead of re-fitting, when
        # enabled and one is fresh. On a miss (or any error) fall back to fit()
        # and persist the new model for next time (best-effort — a DB hiccup
        # must never break detection).
        reused = False
        if settings.model_reuse_max_age_seconds > 0:
            state = fetch_detector_model_sync(
                metric_name, method, settings.model_reuse_max_age_seconds, tenant=tenant
            )
            if state is not None:
                try:
                    detector.load_state(state)
                    reused = True
                    log.info("model.reused", metric=metric_name, detector=method)
                except Exception as exc:
                    log.warning("model.reuse_failed", metric=metric_name, error=str(exc))
        if not reused:
            detector.fit(series.iloc[:split])
            try:
                persist_detector_model_sync(
                    metric_name, method, detector.get_state(), tenant=tenant
                )
            except Exception as exc:
                log.error("model.persist_failed", metric=metric_name, detector=method,
                          error=str(exc))

        default = getattr(settings, f"{method}_threshold", settings.zscore_threshold)
        threshold = resolve_threshold(metric_name, method, default)
        anomalies = detector.detect(
            series.iloc[split:],
            metric_name=metric_name,
            threshold=threshold,
        )
        DETECTION_DURATION.labels(detector=method).observe(time.perf_counter() - _t0)
        ANOMALIES_DETECTED.labels(detector=method).inc(len(anomalies))
        log.info("statistical.done", metric=metric_name, method=method, count=len(anomalies))
        return {
            "metric": metric_name,
            "detector": "statistical",
            "method": method,
            "anomalies": [{**a.to_dict(), "tenant": tenant} for a in anomalies],
        }
    except Exception as exc:
        log.error("statistical.failed", metric=metric_name, error=str(exc))
        raise self.retry(exc=exc) from exc


# ── Prophet detector task ─────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="workers.tasks.run_prophet",
    max_retries=2,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    soft_time_limit=300,
    time_limit=360,
)
def run_prophet(self: Any, metric_name: str, tenant: str = "default") -> dict[str, Any]:
    import time

    from detection.prophet_detector import ProphetDetector
    from detection.thresholds import resolve_threshold
    from monitoring.metrics import ANOMALIES_DETECTED, DETECTION_DURATION
    from storage.timescale import fetch_series_sync, persist_detector_model_sync

    settings = get_settings()

    try:
        series = fetch_series_sync(metric_name, tenant=tenant)
        if len(series) < 20:
            log.warning("prophet.insufficient_data", metric=metric_name, rows=len(series))
            return {"metric": metric_name, "anomalies": [], "detector": "prophet"}

        _t0 = time.perf_counter()
        detector = ProphetDetector()
        split = max(int(len(series) * 0.8), 10)
        detector.fit(series.iloc[:split])
        try:
            persist_detector_model_sync(metric_name, "prophet", detector.get_state(), tenant=tenant)
        except Exception as exc:
            log.error("model.persist_failed", metric=metric_name, detector="prophet",
                      error=str(exc))
        anomalies = detector.detect(
            series.iloc[split:],
            metric_name=metric_name,
            threshold=resolve_threshold(metric_name, "prophet", settings.prophet_threshold),
        )
        DETECTION_DURATION.labels(detector="prophet").observe(time.perf_counter() - _t0)
        ANOMALIES_DETECTED.labels(detector="prophet").inc(len(anomalies))
        log.info("prophet.done", metric=metric_name, count=len(anomalies))
        return {
            "metric": metric_name,
            "detector": "prophet",
            "anomalies": [{**a.to_dict(), "tenant": tenant} for a in anomalies],
        }
    except Exception as exc:
        log.error("prophet.failed", metric=metric_name, error=str(exc))
        raise self.retry(exc=exc) from exc


# ── Isolation Forest task ─────────────────────────────────────────────────
@shared_task(
    bind=True,
    name="workers.tasks.run_isolation_forest",
    max_retries=2,
    default_retry_delay=30,
    autoretry_for=(Exception,),
    retry_backoff=True,
    soft_time_limit=120,
    time_limit=180,
)
def run_isolation_forest(self: Any, metric_name: str, tenant: str = "default") -> dict[str, Any]:
    import time

    from detection.isolation_forest import IsolationForestDetector
    from detection.thresholds import resolve_threshold
    from monitoring.metrics import ANOMALIES_DETECTED, DETECTION_DURATION
    from storage.timescale import fetch_series_sync, persist_detector_model_sync

    settings = get_settings()

    try:
        series = fetch_series_sync(metric_name, tenant=tenant)
        if len(series) < 10:
            return {"metric": metric_name, "anomalies": [], "detector": "isolation_forest"}

        _t0 = time.perf_counter()
        detector = IsolationForestDetector(
            contamination=settings.isolation_forest_contamination,
            scoring_mode=settings.scoring_mode,
        )
        split = max(int(len(series) * 0.8), 5)
        detector.fit(series.iloc[:split])
        try:
            persist_detector_model_sync(
                metric_name, "isolation_forest", detector.get_state(), tenant=tenant
            )
        except Exception as exc:
            log.error("model.persist_failed", metric=metric_name, detector="isolation_forest",
                      error=str(exc))
        anomalies = detector.detect(
            series.iloc[split:],
            metric_name=metric_name,
            threshold=resolve_threshold(
                metric_name, "isolation_forest", settings.isolation_forest_threshold
            ),
        )
        DETECTION_DURATION.labels(detector="isolation_forest").observe(
            time.perf_counter() - _t0
        )
        ANOMALIES_DETECTED.labels(detector="isolation_forest").inc(len(anomalies))
        log.info("isolation_forest.done", metric=metric_name, count=len(anomalies))
        return {
            "metric": metric_name,
            "detector": "isolation_forest",
            "anomalies": [{**a.to_dict(), "tenant": tenant} for a in anomalies],
        }
    except Exception as exc:
        log.error("isolation_forest.failed", metric=metric_name, error=str(exc))
        raise self.retry(exc=exc) from exc


# ── Beat scheduler entry point ────────────────────────────────────────────
@shared_task(name="workers.tasks.schedule_detection_sweep")
def schedule_detection_sweep() -> None:
    """
    Fired every 60 seconds by Celery Beat.
    Dispatches all detectors for all active metrics in parallel using
    Celery canvas primitives.

    Python skill:
        chord  = parallel group of tasks + a callback task
        group  = set of tasks dispatched to the queue concurrently
        .s()   = task signature (lazy task descriptor, not yet executed)
    """
    from storage.timescale import list_active_metrics_sync

    # MM-9.3 Phase C: (tenant, metric) pairs — detectors run per tenant so the
    # same metric name under two tenants is detected and alerted independently.
    pairs = list_active_metrics_sync()
    if not pairs:
        log.info("sweep.no_metrics")
        return

    log.info("sweep.start", metric_count=len(pairs))

    all_tasks = []
    for tenant, m in pairs:
        all_tasks.extend([
            run_statistical.s(m, "zscore", tenant),
            run_isolation_forest.s(m, tenant),
        ])
        # Prophet is expensive — only run on metrics with enough history
        all_tasks.append(run_prophet.s(m, tenant))

    # chord: run all in parallel, then call aggregate_and_alert with combined results
    chord(group(all_tasks))(aggregate_and_alert.s())


# ── Aggregation + alert routing ───────────────────────────────────────────
@shared_task(name="workers.tasks.aggregate_and_alert")
def aggregate_and_alert(results: list[dict[str, Any]]) -> None:
    """
    Receives all detector outputs (list of dicts from chord callback).
    Applies ensemble consensus, deduplicates, then dispatches one route_alert
    task per surviving anomaly.

    Python skill: list comprehension flattening nested lists,
    filter() + map() chain over heterogeneous result dicts.
    """
    from alerting.consensus import apply_consensus
    from alerting.dedup import AlertDeduplicator

    settings = get_settings()
    dedup = AlertDeduplicator()
    all_anomalies = [
        anomaly
        for result in results
        if isinstance(result, dict)
        for anomaly in result.get("anomalies", [])
    ]
    # MM-10.5: require N detectors to agree before an anomaly is eligible to
    # alert (no-op when consensus_min_detectors <= 1). Runs before dedup so a
    # suppressed (non-consensus) anomaly never consumes a cooldown claim.
    confirmed = apply_consensus(all_anomalies, settings.consensus_min_detectors)
    unique = dedup.filter(confirmed)
    log.info(
        "aggregate.done",
        total=len(all_anomalies),
        confirmed=len(confirmed),
        unique=len(unique),
    )

    for anomaly in unique:
        route_alert.delay(anomaly)


# ── Alert routing task ────────────────────────────────────────────────────
@shared_task(
    name="workers.tasks.route_alert",
    max_retries=3,
    default_retry_delay=15,
    autoretry_for=(Exception,),
    retry_backoff=True,
)
def route_alert(anomaly: dict[str, Any]) -> None:
    """Route a single anomaly dict to all configured sinks, then persist it.

    route() raises only if every sink fails, so the task retries a fully
    undelivered alert. Persistence to the alerts table is best-effort: a DB
    error must NOT bubble up, or the retry would re-deliver notifications to
    sinks that already succeeded.
    """
    from alerting.router import build_default_router
    from monitoring.metrics import ALERTS_ROUTED
    from storage.timescale import persist_alert_sync, record_audit_sync

    router = build_default_router(anomaly.get("metric_name"))
    routed_to = router.route(anomaly)
    for sink in routed_to:
        ALERTS_ROUTED.labels(sink=sink).inc()

    # MM-9.5: audit what was routed where (who = the system).
    record_audit_sync(
        "alert.routed",
        principal="system",
        outcome="success" if routed_to else "failure",
        resource=anomaly.get("metric_name"),
        detail={
            "tenant": anomaly.get("tenant", "default"),
            "sinks": routed_to,
            "detector": anomaly.get("detector"),
            "score": round(float(anomaly.get("score", 0)), 4),
            "fingerprint": anomaly.get("fingerprint"),
        },
    )

    try:
        persist_alert_sync(anomaly, routed_to)
    except Exception as exc:
        log.error("alert.persist_failed", metric=anomaly.get("metric_name"), error=str(exc))

    log.info(
        "alert.routed",
        metric=anomaly.get("metric_name"),
        score=round(anomaly.get("score", 0), 3),
        routed_to=routed_to,
    )
