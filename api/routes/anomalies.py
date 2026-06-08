from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import Identity, require_api_key
from storage.timescale import get_session

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


class DetectRequest(BaseModel):
    metric_name: str
    detector: str = "zscore"
    # When omitted, the per-metric override (MM-10.2) or the detector's global
    # default is used; an explicit value always wins.
    threshold: float | None = None
    lookback_hours: int = 24


class AlertItem(BaseModel):
    id: int
    created_at: str
    metric_name: str
    detector: str
    score: float
    value: float
    fingerprint: str
    routed_to: list[str]
    label: str


class AlertsPage(BaseModel):
    items: list[AlertItem]
    count: int    # number of items in this page
    total: int    # total matching the filter (across all pages)
    limit: int
    offset: int


class FeedbackRequest(BaseModel):
    label: Literal["true_positive", "false_positive"]


class AlertDetail(BaseModel):
    """An alert plus the context needed to understand *why* it fired (MM-7.5)."""

    id: int
    created_at: str
    metric_name: str
    detector: str
    method: str
    score: float
    value: float
    threshold: float
    fingerprint: str
    routed_to: list[str]
    label: str
    scoring_mode: str
    explanation: str
    window: dict[str, Any]
    series: list[dict[str, Any]]


def _resolver_key(detector: str, method: str) -> str:
    """The config key the firing threshold is stored under.

    zscore/iqr/stl all report ``detector='statistical'`` and differ only by
    ``method``, so the threshold is keyed by the method for those; the ML
    detectors are keyed by their detector name. Pre-MM-7.5 alerts have no method
    — fall back to the detector name.
    """
    return method if detector == "statistical" and method else detector


def _alert_threshold(metric_name: str, detector: str, method: str) -> float:
    """Resolve the exact threshold this alert's detector used, the same way the
    worker does (per-metric override → global default)."""
    from config import get_settings
    from detection.thresholds import resolve_threshold

    settings = get_settings()
    key = _resolver_key(detector, method)
    default = getattr(settings, f"{key}_threshold", settings.zscore_threshold)
    return resolve_threshold(metric_name, key, default)


def _explanation(alert: dict[str, Any], threshold: float, scoring_mode: str) -> str:
    """Build a human-readable reason string for the alert."""
    label = alert["method"] or alert["detector"]
    return (
        f"Detector '{label}' scored {alert['score']:.3f} "
        f"(≥ threshold {threshold:.3f}) for metric '{alert['metric_name']}' "
        f"at value {alert['value']:.4f} on {alert['created_at']}. "
        f"Scores are normalised to [0,1] in '{scoring_mode}' mode; a score at or "
        f"above the threshold fires an alert."
    )


@router.get("", response_model=AlertsPage, summary="Query historical anomaly alerts")
async def list_alerts(
    metric: str | None = Query(None, description="Filter by exact metric name"),
    detector: str | None = Query(None, description="Filter by detector name"),
    min_score: float | None = Query(None, ge=0.0, le=1.0, description="Minimum anomaly score"),
    from_: datetime | None = Query(None, alias="from", description="ISO start time (inclusive)"),
    to: datetime | None = Query(None, description="ISO end time (inclusive)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> AlertsPage:
    """
    Paginated history of routed anomaly alerts, newest first, scoped to the
    caller's tenant (MM-9.3). All filters are optional and combine with AND.
    """
    from storage.timescale import fetch_alerts

    items, total = await fetch_alerts(
        session,
        metric_name=metric,
        detector=detector,
        min_score=min_score,
        start=from_,
        end=to,
        limit=limit,
        offset=offset,
        tenant=identity.tenant,
    )
    rows = [AlertItem(**i) for i in items]
    return AlertsPage(items=rows, count=len(rows), total=total, limit=limit, offset=offset)


@router.get("/stats", summary="Detection-quality stats from feedback")
async def feedback_stats(
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Per-detector feedback summary for the caller's tenant (MM-9.3), including
    **precision** = TP / (TP + FP) over labeled alerts (MM-10.3).

    Note: true **recall** requires ground-truth knowledge of *missed* anomalies,
    which alert feedback alone cannot provide — so only precision is reported
    here until a labeled ground-truth set exists.
    """
    from storage.timescale import feedback_stats_by_detector

    return {"by_detector": await feedback_stats_by_detector(session, tenant=identity.tenant)}


@router.post("/{alert_id}/feedback", summary="Label an alert as a true/false positive")
async def submit_feedback(
    alert_id: int,
    req: FeedbackRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> dict[str, Any]:
    """Record human feedback on an alert so detection precision can be measured.
    Scoped to the caller's tenant (MM-9.3) — you cannot label another's alert."""
    from storage.timescale import record_audit_async, set_alert_label

    updated = await set_alert_label(session, alert_id, req.label, tenant=identity.tenant)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )
    # MM-9.5: audit who labeled which alert and how.
    await record_audit_async(
        "feedback.submitted",
        principal=identity.principal,
        outcome="success",
        resource=f"alert:{alert_id}",
        source_ip=request.client.host if request.client else None,
        detail={"label": req.label, "tenant": identity.tenant},
    )
    return {"id": alert_id, "label": req.label}


@router.post("/detect", summary="Run on-demand detection for a metric")
async def detect_now(
    req: DetectRequest,
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Synchronous on-demand detection endpoint, over the caller's tenant data
    (MM-9.3). Useful for ad-hoc investigation outside the beat schedule.
    """
    from config import get_settings
    from detection.registry import get_detector
    from detection.thresholds import resolve_threshold
    from storage.timescale import fetch_series

    series = await fetch_series(
        session, req.metric_name, req.lookback_hours, tenant=identity.tenant
    )
    if series.empty:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No data found for metric {req.metric_name!r}",
        )

    settings = get_settings()
    try:
        detector = get_detector(req.detector, scoring_mode=settings.scoring_mode)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    # Explicit request threshold wins; otherwise resolve the per-metric override
    # (MM-10.2), falling back to the detector's global default.
    if req.threshold is not None:
        threshold = req.threshold
    else:
        default = getattr(settings, f"{req.detector}_threshold", 0.8)
        threshold = resolve_threshold(req.metric_name, req.detector, default)

    split = max(int(len(series) * 0.8), 1)
    detector.fit(series.iloc[:split])
    anomalies = detector.detect(
        series.iloc[split:],
        metric_name=req.metric_name,
        threshold=threshold,
    )

    return {
        "metric_name": req.metric_name,
        "detector": req.detector,
        "threshold": threshold,
        "scoring_mode": settings.scoring_mode,
        "series_points": len(series),
        "anomaly_count": len(anomalies),
        "anomalies": [a.to_dict() for a in anomalies],
    }


@router.get("/metrics", summary="List active metric names")
async def list_metrics(
    lookback_hours: int = Query(default=1, ge=1, le=168),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> dict[str, Any]:
    from storage.timescale import list_active_metrics
    metrics = await list_active_metrics(session, lookback_hours, tenant=identity.tenant)
    return {"metrics": metrics, "count": len(metrics)}


def _assemble_detector_report(
    counts: list[dict[str, Any]], recall: list[dict[str, Any]], bucket_minutes: int
) -> dict[str, Any]:
    """Combine the raw count/recall aggregates into a per-metric detector A/B
    report (MM-10.4). Pure (no I/O) so it is unit-testable without a DB.

    Per ``(metric, detector)`` it reports **precision** = TP/(TP+FP) and a
    **comparative recall** = the share of that metric's human-confirmed anomaly
    *events* (distinct time buckets where any detector scored a true positive)
    that this detector also caught. Each metric gets a ``recommended_detector``
    = the labeled detector with the highest precision (tie → comparative recall,
    then alert volume).
    """
    covered = {(r["metric_name"], r["detector"]): r["covered_buckets"] for r in recall}
    total_buckets = {r["metric_name"]: r["total_buckets"] for r in recall}

    by_metric: dict[str, list[dict[str, Any]]] = {}
    for c in counts:
        metric = c["metric_name"]
        labeled = c["tp"] + c["fp"]
        metric_total = total_buckets.get(metric, 0)
        cov = covered.get((metric, c["detector"]), 0)
        by_metric.setdefault(metric, []).append({
            "detector": c["detector"],
            "alerts": c["total"],
            "true_positive": c["tp"],
            "false_positive": c["fp"],
            "unlabeled": c["unlabeled"],
            "precision": round(c["tp"] / labeled, 4) if labeled else None,
            "comparative_recall": round(cov / metric_total, 4) if metric_total else None,
            "confirmed_events_covered": cov,
        })

    metrics = []
    for metric, detectors in sorted(by_metric.items()):
        detectors.sort(
            key=lambda d: (
                d["precision"] is not None,
                d["precision"] or 0.0,
                d["comparative_recall"] or 0.0,
                d["alerts"],
            ),
            reverse=True,
        )
        # Only recommend a detector that has demonstrated at least one true
        # positive — "recommending" a detector whose labeled alerts were all
        # false positives (precision 0) would be misleading.
        recommendable = [
            d for d in detectors if d["precision"] is not None and d["precision"] > 0
        ]
        recommended = (
            max(
                recommendable,
                key=lambda d: (d["precision"], d["comparative_recall"] or 0.0, d["alerts"]),
            )["detector"]
            if recommendable
            else None
        )
        metrics.append({
            "metric_name": metric,
            "confirmed_tp_events": total_buckets.get(metric, 0),
            "recommended_detector": recommended,
            "detectors": detectors,
        })

    return {
        "bucket_minutes": bucket_minutes,
        "metrics": metrics,
        "notes": (
            "precision = TP/(TP+FP) over human-labeled alerts. comparative_recall "
            "is each detector's coverage of the confirmed anomaly events (distinct "
            f"{bucket_minutes}-minute buckets) that at least one detector caught and "
            "a human marked true; it CANNOT account for anomalies no detector "
            "caught, so it is a relative A/B signal, not true recall (which needs "
            "ground-truth missed anomalies)."
        ),
    }


@router.get(
    "/report",
    summary="Per-metric detector A/B report (precision + comparative recall)",
)
async def detector_report(
    metric: str | None = Query(None, description="Restrict the report to one metric name"),
    bucket_minutes: int = Query(
        5, ge=1, le=1440, description="Time-bucket width for grouping confirmed anomaly events"
    ),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> dict[str, Any]:
    """
    Compare detectors per metric over labeled feedback (the caller's tenant only,
    MM-9.3) to guide which detector to trust (MM-10.4). Reports precision and a
    clearly-bounded comparative recall; see the ``notes`` field for the caveat.
    """
    from storage.timescale import detector_report_rows

    counts, recall = await detector_report_rows(
        session, metric_name=metric, bucket_minutes=bucket_minutes, tenant=identity.tenant
    )
    return _assemble_detector_report(counts, recall, bucket_minutes)


# Declared last so the static GET routes above (/stats, /metrics, /report) are
# matched before this dynamic /{alert_id} path.
@router.get(
    "/{alert_id}",
    response_model=AlertDetail,
    summary="Alert detail with explanation (surrounding series, detector, score, threshold)",
)
async def alert_detail(
    alert_id: int,
    window_minutes: int = Query(
        15, ge=1, le=1440, description="± minutes of series context around the alert"
    ),
    resolution: Literal["raw", "1m"] = Query(
        "1m", description="'raw' = stored points; '1m' = 1-minute buckets"
    ),
    session: AsyncSession = Depends(get_session),
    identity: Identity = Depends(require_api_key),
) -> AlertDetail:
    """
    Retrieve one alert plus the context needed to understand why it fired: the
    detector/method, score, the exact resolved threshold, and the surrounding
    metric series window (MM-7.5). Scoped to the caller's tenant (MM-9.3).
    """
    from datetime import timedelta

    from config import get_settings
    from storage.timescale import fetch_alert_by_id, fetch_metric_series

    alert = await fetch_alert_by_id(session, alert_id, tenant=identity.tenant)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Alert {alert_id} not found",
        )

    settings = get_settings()
    threshold = _alert_threshold(alert["metric_name"], alert["detector"], alert["method"])
    explanation = _explanation(alert, threshold, settings.scoring_mode)

    center = alert["created_at_dt"]
    start = center - timedelta(minutes=window_minutes)
    end = center + timedelta(minutes=window_minutes)
    series = await fetch_metric_series(
        session, alert["metric_name"], start=start, end=end,
        resolution=resolution, tenant=identity.tenant,
    )

    return AlertDetail(
        id=alert["id"],
        created_at=alert["created_at"],
        metric_name=alert["metric_name"],
        detector=alert["detector"],
        method=alert["method"],
        score=alert["score"],
        value=alert["value"],
        threshold=threshold,
        fingerprint=alert["fingerprint"],
        routed_to=alert["routed_to"],
        label=alert["label"],
        scoring_mode=settings.scoring_mode,
        explanation=explanation,
        window={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "minutes": window_minutes,
            "resolution": resolution,
        },
        series=series,
    )
