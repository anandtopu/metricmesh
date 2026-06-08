"""Unit tests for detector state serialization + reuse (MM-10.1)."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from detection.isolation_forest import IsolationForestDetector
from detection.prophet_detector import ProphetDetector
from detection.statistical import StatisticalDetector


def _series(n: int = 200, seed: int = 7) -> pd.Series:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    return pd.Series(100 + rng.normal(0, 2.0, n), index=idx, name="value")


class TestStatisticalState:
    def test_get_state_is_json_serializable(self):
        d = StatisticalDetector(method="iqr")
        d.fit(_series())
        state = d.get_state()
        # Round-trips through JSON unchanged (what persistence stores).
        assert json.loads(json.dumps(state)) == state
        assert state["method"] == "iqr"
        assert {"q1", "q3", "iqr"} <= set(state["params"])

    def test_load_state_reproduces_scores_without_refit(self):
        # The whole point of reuse: a freshly-loaded model scores identically to
        # the fitted one, without calling fit().
        s = _series()
        train, test = s.iloc[:160], s.iloc[160:]

        fitted = StatisticalDetector(method="iqr")
        fitted.fit(train)
        expected = fitted.score(test)

        reused = StatisticalDetector(method="iqr")
        reused.load_state(fitted.get_state())
        actual = reused.score(test)

        pd.testing.assert_series_equal(expected, actual)

    def test_load_state_rejects_method_mismatch(self):
        zscore = StatisticalDetector(method="zscore")
        zscore.fit(_series())
        iqr = StatisticalDetector(method="iqr")
        with pytest.raises(ValueError, match="Cannot reuse"):
            iqr.load_state(zscore.get_state())


class TestMetadataState:
    def test_prophet_state_is_serializable_metadata(self):
        state = ProphetDetector(changepoint_prior_scale=0.1).get_state()
        assert json.loads(json.dumps(state)) == state
        assert state["changepoint_prior_scale"] == 0.1
        assert state["fitted"] is False

    def test_isolation_forest_state_is_serializable_metadata(self):
        state = IsolationForestDetector(contamination=0.1, n_estimators=50).get_state()
        assert json.loads(json.dumps(state)) == state
        assert state["contamination"] == 0.1
        assert state["n_estimators"] == 50
        assert state["fitted"] is False
