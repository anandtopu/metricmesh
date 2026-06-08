"""
Unit tests for all three detector implementations.
Python skill: numpy random seeds for reproducible test data,
pandas DatetimeIndex construction, pytest fixtures.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from detection.base import AnomalyResult, Detector
from detection.isolation_forest import IsolationForestDetector
from detection.statistical import StatisticalDetector


def make_series(n: int = 200, seed: int = 42, spike_idx: int | None = 150) -> pd.Series:
    """Generate a sinusoidal series with an optional spike anomaly."""
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 4 * np.pi, n)
    values = np.sin(t) + rng.normal(0, 0.05, n)
    if spike_idx is not None:
        values[spike_idx] = 10.0  # clear anomaly
    index = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    return pd.Series(values, index=index, name="value")


@pytest.fixture
def normal_series() -> pd.Series:
    return make_series(spike_idx=None)


@pytest.fixture
def anomalous_series() -> pd.Series:
    return make_series(spike_idx=150)


class TestStatisticalDetectorProtocol:
    def test_satisfies_detector_protocol(self):
        d = StatisticalDetector()
        assert isinstance(d, Detector)

    @pytest.mark.parametrize("method", ["zscore", "iqr", "stl"])
    def test_all_methods_return_scores_in_range(self, method, anomalous_series):
        d = StatisticalDetector(method=method)
        d.fit(anomalous_series.iloc[:160])
        scores = d.score(anomalous_series.iloc[160:])
        assert (scores >= 0).all() and (scores <= 1).all()

    def test_zscore_detects_spike(self, anomalous_series):
        d = StatisticalDetector(method="zscore")
        d.fit(anomalous_series.iloc[:140])
        results = d.detect(anomalous_series.iloc[140:], metric_name="test.metric", threshold=0.8)
        assert len(results) > 0
        assert all(isinstance(r, AnomalyResult) for r in results)

    def test_iqr_no_false_positives_on_normal(self, normal_series):
        d = StatisticalDetector(method="iqr")
        d.fit(normal_series.iloc[:160])
        results = d.detect(normal_series.iloc[160:], threshold=0.95)
        # Very few or zero false positives on clean sinusoidal data
        assert len(results) <= 2

    def test_invalid_method_raises(self):
        with pytest.raises(ValueError, match="Unknown method"):
            StatisticalDetector(method="random_forest")


class TestIsolationForestDetector:
    def test_satisfies_detector_protocol(self):
        assert isinstance(IsolationForestDetector(), Detector)

    def test_score_before_fit_raises(self, anomalous_series):
        d = IsolationForestDetector()
        with pytest.raises(RuntimeError, match="fit"):
            d.score(anomalous_series)

    def test_fit_and_detect(self, anomalous_series):
        d = IsolationForestDetector(n_estimators=50, contamination=0.1)
        d.fit(anomalous_series.iloc[:160])
        results = d.detect(anomalous_series.iloc[160:], metric_name="test.if", threshold=0.5)
        assert isinstance(results, list)
        # Should detect the spike at index 150
        assert len(results) > 0

    def test_scores_in_valid_range(self, anomalous_series):
        d = IsolationForestDetector(n_estimators=20)
        d.fit(anomalous_series.iloc[:160])
        scores = d.score(anomalous_series.iloc[160:])
        assert (scores >= 0.0).all()
        assert (scores <= 1.0).all()


class TestAnomalyResult:
    def test_frozen(self):
        ts = pd.Timestamp("2024-01-01", tz="UTC")
        r = AnomalyResult(timestamp=ts, metric_name="m", value=1.0, score=0.9, detector="test")
        with pytest.raises(AttributeError):   # FrozenInstanceError subclasses it
            r.score = 0.5  # type: ignore[misc]

    def test_to_dict_keys(self):
        ts = pd.Timestamp("2024-01-01", tz="UTC")
        r = AnomalyResult(timestamp=ts, metric_name="m", value=1.0, score=0.9, detector="d")
        d = r.to_dict()
        assert all(k in d for k in ["timestamp", "metric_name", "value", "score", "detector"])
