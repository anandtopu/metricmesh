"""Unit tests for the MM-7.5 alert-detail pure helpers (no DB/HTTP)."""

from __future__ import annotations

from api.routes.anomalies import _alert_threshold, _explanation, _resolver_key


def test_resolver_key_statistical_uses_method():
    # zscore/iqr/stl share detector='statistical' and differ only by method.
    assert _resolver_key("statistical", "zscore") == "zscore"
    assert _resolver_key("statistical", "iqr") == "iqr"
    assert _resolver_key("statistical", "stl") == "stl"


def test_resolver_key_ml_detectors_use_detector_name():
    # prophet reports method='prophet_interval' — the threshold key is the detector.
    assert _resolver_key("prophet", "prophet_interval") == "prophet"
    assert _resolver_key("isolation_forest", "isolation_forest") == "isolation_forest"


def test_resolver_key_falls_back_when_method_missing():
    # Pre-MM-7.5 alerts have no method.
    assert _resolver_key("statistical", "") == "statistical"


def test_alert_threshold_uses_global_default():
    # Default config: zscore_threshold = 0.8, no per-metric override.
    assert _alert_threshold("cpu.usage", "statistical", "zscore") == 0.8


def test_explanation_mentions_detector_score_and_threshold():
    alert = {
        "detector": "statistical",
        "method": "zscore",
        "metric_name": "cpu.usage",
        "score": 0.94,
        "value": 187.4,
        "created_at": "2026-06-03T15:30:00+00:00",
    }
    text = _explanation(alert, threshold=0.8, scoring_mode="relative")
    assert "zscore" in text
    assert "0.940" in text  # score, 3 dp
    assert "0.800" in text  # threshold, 3 dp
    assert "cpu.usage" in text
    assert "relative" in text
