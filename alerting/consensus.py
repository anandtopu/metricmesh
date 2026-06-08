from __future__ import annotations

from collections import defaultdict
from typing import Any

import structlog

from alerting.dedup import time_bucket

log = structlog.get_logger(__name__)


def apply_consensus(
    anomalies: list[dict[str, Any]], min_detectors: int, bucket_seconds: int | None = None
) -> list[dict[str, Any]]:
    """Keep only anomalies confirmed by an ensemble of detectors (MM-10.5).

    An anomaly survives when at least ``min_detectors`` *distinct* detectors
    flag the same ``(tenant, metric_name, time bucket)`` (MM-9.3 — consensus is
    per tenant). Because every detector on a sweep scores the same series
    timestamps, agreeing detectors land in the same bucket and reinforce each
    other; a lone-detector spike is dropped as noise.

    ``min_detectors <= 1`` disables consensus and returns the input unchanged
    (the default), so the feature is opt-in and backward compatible. The
    surviving dicts are returned untouched and still flow through dedup +
    routing downstream. ``bucket_seconds`` (MM-5.3) controls the grouping width
    and defaults to ``config.dedup_bucket_seconds`` — the same value dedup uses.
    """
    if min_detectors <= 1:
        return anomalies

    def _group(a: dict[str, Any]) -> tuple[str, str, int]:
        return (
            a.get("tenant", "default"),
            a.get("metric_name", ""),
            time_bucket(a.get("timestamp"), bucket_seconds),
        )

    # Distinct detectors seen per (tenant, metric, time bucket).
    detectors_by_group: dict[tuple[str, str, int], set[str]] = defaultdict(set)
    for a in anomalies:
        detectors_by_group[_group(a)].add(a.get("detector", ""))

    survivors = [a for a in anomalies if len(detectors_by_group[_group(a)]) >= min_detectors]

    log.info(
        "consensus.filter",
        min_detectors=min_detectors,
        total=len(anomalies),
        passed=len(survivors),
        suppressed=len(anomalies) - len(survivors),
    )
    return survivors
