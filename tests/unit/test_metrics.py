"""Unit tests for the MM-8.6 prometheus_client exposition layer.

Mode-agnostic: they pass in single-process mode (host) and multiprocess mode
(inside the api container with PROMETHEUS_MULTIPROC_DIR set), and need neither a
real Redis nor a DB.
"""

from __future__ import annotations

import redis as redis_module

from monitoring import metrics
from monitoring.metrics import (
    ALERTS_ROUTED,
    ANOMALIES_DETECTED,
    DETECTION_DURATION,
    INGEST_POINTS,
    QueueDepthCollector,
    render_latest,
)


class _FakeRedis:
    """Stand-in for a redis client returning fixed list lengths."""

    def __init__(self, depths: dict[str, int]) -> None:
        self._depths = depths
        self.closed = False

    def llen(self, key: str) -> int:
        return self._depths.get(key, 0)

    def close(self) -> None:
        self.closed = True


def _patch_redis(monkeypatch, fake: object) -> None:
    monkeypatch.setattr(redis_module, "from_url", lambda *a, **k: fake)


# ── Metric definitions ─────────────────────────────────────────────────────
def test_metric_names_and_labels():
    # prometheus_client strips the `_total` suffix from a Counter's `_name`
    # (it is re-appended at exposition time); the Histogram keeps its full name.
    assert INGEST_POINTS._name == "metricmesh_ingest_points"
    assert DETECTION_DURATION._name == "metricmesh_detection_duration_seconds"
    assert ANOMALIES_DETECTED._name == "metricmesh_anomalies_detected"
    assert ALERTS_ROUTED._name == "metricmesh_alerts_routed"
    # Labels are accepted without raising.
    INGEST_POINTS.labels(status="accepted")
    DETECTION_DURATION.labels(detector="zscore")
    ANOMALIES_DETECTED.labels(detector="prophet")
    ALERTS_ROUTED.labels(sink="log")


# ── Queue-depth collector ──────────────────────────────────────────────────
def test_queue_depth_collector_reads_redis(monkeypatch):
    fake = _FakeRedis({"fast": 3, "slow": 1, "alerts": 0})
    _patch_redis(monkeypatch, fake)

    families = list(QueueDepthCollector().collect())
    assert len(families) == 1
    family = families[0]
    assert family.name == "metricmesh_queue_depth"

    samples = {s.labels["queue"]: s.value for s in family.samples}
    assert samples == {"fast": 3.0, "slow": 1.0, "alerts": 0.0}
    assert fake.closed is True  # client is always closed


def test_queue_depth_collector_resilient_when_redis_down(monkeypatch):
    def _boom(*a, **k):
        raise ConnectionError("no broker")

    monkeypatch.setattr(redis_module, "from_url", _boom)

    # Must not raise and must emit no samples — /metrics never fails on broker down.
    assert list(QueueDepthCollector().collect()) == []


# ── Exposition ─────────────────────────────────────────────────────────────
def test_render_latest_includes_queue_depth(monkeypatch):
    _patch_redis(monkeypatch, _FakeRedis({"fast": 2, "slow": 0, "alerts": 0}))

    payload, content_type = render_latest()
    assert isinstance(payload, bytes)
    assert content_type.startswith("text/plain")
    assert b"metricmesh_queue_depth" in payload


def test_render_latest_includes_incremented_counter(monkeypatch):
    _patch_redis(monkeypatch, _FakeRedis({"fast": 0, "slow": 0, "alerts": 0}))

    INGEST_POINTS.labels(status="accepted").inc(5)
    payload, _ = render_latest()
    assert b"metricmesh_ingest_points_total" in payload


def test_multiproc_dir_env_is_honored():
    # The module captures the env var once at import; assert the constant matches
    # whatever the process was started with (set in the api container, unset on host).
    import os

    assert os.environ.get("PROMETHEUS_MULTIPROC_DIR") == metrics._MULTIPROC_DIR
