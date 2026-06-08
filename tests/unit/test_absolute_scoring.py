"""Unit tests for absolute scoring mode (MM-3.6).

The defining property of absolute mode: scores are mapped against a *fixed*
reference, so a moderate series is NOT stretched to fill [0, 1] the way per-batch
(relative) normalisation does.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from detection.isolation_forest import IsolationForestDetector
from detection.statistical import StatisticalDetector


def _series(values: np.ndarray) -> pd.Series:
    idx = pd.date_range("2026-06-01", periods=len(values), freq="1min", tz="UTC")
    return pd.Series(values, index=idx)


def _moderate_series() -> pd.Series:
    # Gentle gaussian noise with one moderate (~4 sigma) bump — max z stays well
    # under the absolute reference of 6 sigma, so absolute scores stay < 1.
    rng = np.random.default_rng(0)
    vals = 100 + rng.normal(0, 1.0, size=180)
    vals[150] = 104.0
    return _series(vals)


class TestZScoreAbsolute:
    def test_relative_fills_the_range_absolute_does_not(self):
        s = _moderate_series()
        rel = StatisticalDetector(method="zscore", scoring_mode="relative").score(s)
        ab = StatisticalDetector(method="zscore", scoring_mode="absolute").score(s)

        # Relative normalises by the batch max → top score pinned at 1.0
        # (the denominator carries a tiny +1e-9 guard).
        assert rel.max() == pytest.approx(1.0)
        # Absolute maps against a fixed 6-sigma reference → a ~4-sigma series
        # never reaches 1.0; the threshold means the same across windows.
        assert ab.max() < 1.0
        assert (ab >= 0).all() and (ab <= 1).all()

    def test_absolute_score_is_scale_invariant_across_batches(self):
        # The same spike embedded in two batches of different overall spread
        # should score (almost) identically in absolute mode — the point of MM-3.6.
        rng = np.random.default_rng(1)
        base = 100 + rng.normal(0, 1.0, size=120)
        calm = base.copy()
        calm[100] = 105.0
        noisy = base.copy()
        noisy += rng.normal(0, 0.5, size=120)  # extra wiggle elsewhere
        noisy[100] = 105.0

        det = StatisticalDetector(method="zscore", scoring_mode="absolute")
        s_calm = det.score(_series(calm))
        s_noisy = det.score(_series(noisy))
        # Scores at the shared spike are close because neither depends on the
        # batch max (they'd diverge under relative normalisation).
        assert abs(float(s_calm.iloc[100]) - float(s_noisy.iloc[100])) < 0.1


class TestIsolationForestAbsolute:
    def test_absolute_uses_sigmoid_not_minmax(self):
        s = _moderate_series()
        rel = IsolationForestDetector(scoring_mode="relative")
        rel.fit(s)
        rel_scores = rel.score(s)
        # Min-max normalisation pins the extremes at 0 and 1.
        assert rel_scores.min() == 0.0
        assert rel_scores.max() == 1.0

        ab = IsolationForestDetector(scoring_mode="absolute")
        ab.fit(s)
        ab_scores = ab.score(s)
        # The fixed sigmoid lives strictly inside (0, 1) — never pinned.
        assert ab_scores.min() > 0.0
        assert ab_scores.max() < 1.0
        assert (ab_scores >= 0).all() and (ab_scores <= 1).all()


class TestStlAbsolute:
    def test_absolute_scores_in_range_and_differ_from_relative(self):
        # Seasonal-ish series with an off-phase spike (127 % 30 != 0, so STL's
        # cycle-subseries smoothing doesn't absorb it).
        x = np.arange(180)
        vals = 100 + 5 * np.sin(2 * np.pi * x / 30) + np.random.default_rng(2).normal(0, 0.5, 180)
        vals[127] = 130.0
        s = _series(vals)

        rel = StatisticalDetector(method="stl", scoring_mode="relative").score(s)
        ab = StatisticalDetector(method="stl", scoring_mode="absolute").score(s)

        # Valid [0,1] scores and the absolute branch produced a real signal.
        assert (ab >= 0).all() and (ab <= 1).all()
        assert ab.max() > 0
        # Absolute uses a robust-MAD scale, not the batch p99 → different scores.
        assert not np.allclose(rel.values, ab.values)
