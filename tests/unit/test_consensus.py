"""Unit tests for ensemble/consensus scoring (MM-10.5)."""
from __future__ import annotations

from alerting.consensus import apply_consensus

# A fixed, tz-aware ISO timestamp — detectors on one sweep share the exact ts,
# so they fall into the same consensus bucket.
_TS = "2026-06-04T12:34:00+00:00"
_TS_OTHER = "2026-06-04T18:00:00+00:00"


def _anomaly(detector: str, metric: str = "cpu.usage", ts: str = _TS, score: float = 0.9) -> dict:
    return {
        "metric_name": metric,
        "detector": detector,
        "score": score,
        "value": 99.5,
        "timestamp": ts,
    }


def test_disabled_passes_everything_through():
    # min_detectors <= 1 is a no-op: every anomaly survives, list unchanged.
    anomalies = [_anomaly("statistical")]
    assert apply_consensus(anomalies, 1) == anomalies
    assert apply_consensus(anomalies, 0) == anomalies


def test_single_detector_suppressed_below_threshold():
    # Only one detector flags the spike → dropped when 2 must agree.
    result = apply_consensus([_anomaly("statistical")], min_detectors=2)
    assert result == []


def test_two_distinct_detectors_reach_consensus():
    # statistical + isolation_forest agree on the same metric/bucket → both pass.
    anomalies = [_anomaly("statistical"), _anomaly("isolation_forest")]
    result = apply_consensus(anomalies, min_detectors=2)
    assert len(result) == 2


def test_same_detector_twice_is_not_consensus():
    # Two anomalies from the SAME detector are one vote, not two — suppressed.
    anomalies = [_anomaly("statistical"), _anomaly("statistical", score=0.8)]
    assert apply_consensus(anomalies, min_detectors=2) == []


def test_consensus_is_scoped_per_metric():
    # Two detectors agree on cpu.usage but mem.usage has only one → only the
    # cpu.usage pair survives.
    anomalies = [
        _anomaly("statistical", metric="cpu.usage"),
        _anomaly("isolation_forest", metric="cpu.usage"),
        _anomaly("statistical", metric="mem.usage"),
    ]
    result = apply_consensus(anomalies, min_detectors=2)
    assert {a["metric_name"] for a in result} == {"cpu.usage"}
    assert len(result) == 2


def test_consensus_is_scoped_per_time_bucket():
    # Same metric, different detectors, but far-apart timestamps → no agreement.
    anomalies = [
        _anomaly("statistical", ts=_TS),
        _anomaly("isolation_forest", ts=_TS_OTHER),
    ]
    assert apply_consensus(anomalies, min_detectors=2) == []


def test_three_way_threshold():
    # Two agree but three are required → suppressed.
    anomalies = [_anomaly("statistical"), _anomaly("isolation_forest")]
    assert apply_consensus(anomalies, min_detectors=3) == []
    # Add the third detector → consensus reached.
    anomalies.append(_anomaly("prophet"))
    assert len(apply_consensus(anomalies, min_detectors=3)) == 3


def test_consensus_is_per_tenant():
    # MM-9.3: two detectors agree on the same metric/bucket but for DIFFERENT
    # tenants → no consensus (each tenant saw only one detector).
    cross = [
        {**_anomaly("statistical", ts=_TS), "tenant": "acme"},
        {**_anomaly("isolation_forest", ts=_TS), "tenant": "globex"},
    ]
    assert apply_consensus(cross, min_detectors=2) == []
    # Same tenant, two detectors → consensus reached.
    same = [
        {**_anomaly("statistical", ts=_TS), "tenant": "acme"},
        {**_anomaly("isolation_forest", ts=_TS), "tenant": "acme"},
    ]
    assert len(apply_consensus(same, min_detectors=2)) == 2


def test_empty_input():
    assert apply_consensus([], min_detectors=2) == []


def test_bucket_width_controls_consensus_grouping():
    # MM-5.3: two detectors 4 minutes apart. A narrow bucket separates them
    # (no agreement); a wide bucket groups them (consensus reached).
    a = _anomaly("statistical", ts="2026-06-04T12:30:00+00:00")
    b = _anomaly("isolation_forest", ts="2026-06-04T12:34:00+00:00")
    assert apply_consensus([a, b], min_detectors=2, bucket_seconds=60) == []
    assert len(apply_consensus([a, b], min_detectors=2, bucket_seconds=600)) == 2
