from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import structlog
from statsmodels.tsa.seasonal import STL

from detection.base import AnomalyResult, Detector

log = structlog.get_logger(__name__)

# Absolute-mode references (MM-3.6). In "absolute" scoring these fixed scales
# replace the per-batch divisor so a given threshold means the same thing across
# windows: a z-score of _ZSCORE_ABS_SIGMA maps to 1.0, and STL residuals are
# scaled by a robust (MAD-based) sigma estimate rather than the batch's p99.
_ZSCORE_ABS_SIGMA = 6.0
_STL_ABS_SIGMAS = 3.0


class StatisticalDetector:
    """
    Three univariate detectors in one class, dispatched via structural pattern matching.

    - zscore   : rolling z-score; good for stationary series with gaussian noise
    - iqr      : interquartile-range fences; robust to heavy tails, no normality assumption
    - stl      : STL seasonal decomposition; residuals are scored; handles seasonality

    Python skills:
        - structural pattern matching (match/case) — Python 3.10+
        - NumPy broadcasting for vectorised operations
        - Pandas rolling windows with min_periods
        - Method dispatch without if/elif chains
    """
    name = "statistical"

    def __init__(
        self,
        method: str = "zscore",
        window: int = 60,
        scoring_mode: str = "relative",
    ) -> None:
        if method not in {"zscore", "iqr", "stl"}:
            raise ValueError(f"Unknown method: {method!r}. Choose from zscore, iqr, stl")
        self.method = method
        self.window = window
        # "relative" (per-batch normalised) or "absolute" (fixed reference) — MM-3.6.
        self.scoring_mode = scoring_mode
        self._params: dict[str, Any] = {}

    def fit(self, series: pd.Series) -> None:
        """Compute and cache parameters from training portion of the series."""
        clean = series.dropna()
        match self.method:
            case "zscore":
                self._params = {
                    "mean": float(clean.rolling(self.window, min_periods=10).mean().iloc[-1]),
                    "std":  float(clean.rolling(self.window, min_periods=10).std().iloc[-1]),
                }
            case "iqr":
                q1, q3 = float(np.percentile(clean, 25)), float(np.percentile(clean, 75))
                self._params = {"q1": q1, "q3": q3, "iqr": q3 - q1}
            case "stl":
                self._params = {}   # STL is computed fresh in score()

    def score(self, series: pd.Series) -> pd.Series:
        """Return anomaly scores in [0, 1]. 1 = most anomalous."""
        match self.method:
            case "zscore":
                mean = series.rolling(self.window, min_periods=5).mean()
                std  = series.rolling(self.window, min_periods=5).std().replace(0, 1e-9)
                z    = ((series - mean) / std).abs()
                # Absolute: map sigma directly against a fixed reference so a
                # 4-sigma spike scores the same regardless of the rest of the
                # batch. Relative: normalise by the batch max (default).
                divisor = _ZSCORE_ABS_SIGMA if self.scoring_mode == "absolute" else z.max() + 1e-9
                # The leading rows of a rolling window are NaN; fill them with 0
                # so scores are always valid in [0, 1] (NaN >= threshold is False
                # but also breaks downstream range checks).
                return (z / divisor).clip(0, 1).fillna(0.0).rename("score")

            case "iqr":
                q1   = self._params.get("q1", float(series.quantile(0.25)))
                q3   = self._params.get("q3", float(series.quantile(0.75)))
                iqr  = self._params.get("iqr", q3 - q1) or 1e-9
                fence = 1.5 * iqr
                dist  = np.maximum(series - q3, q1 - series)
                return pd.Series(
                    (dist / fence).clip(0, 1).fillna(0.0).values,
                    index=series.index,
                    name="score",
                )

            case "stl":
                if len(series.dropna()) < 4:
                    return pd.Series(0.0, index=series.index, name="score")
                try:
                    period    = min(60, len(series) // 2)
                    result    = STL(series.dropna(), period=period, robust=True).fit()
                    residuals = result.resid.abs()
                    if self.scoring_mode == "absolute":
                        # Robust sigma from MAD (median absolute deviation), so the
                        # scale tracks the metric's noise level rather than the
                        # current batch's 99th percentile — stable across windows.
                        mad      = float((residuals - residuals.median()).abs().median())
                        divisor  = _STL_ABS_SIGMAS * 1.4826 * mad or 1e-9
                    else:
                        divisor  = float(residuals.quantile(0.99)) or 1e-9
                    scores    = (residuals / divisor).clip(0, 1)
                    return scores.reindex(series.index, fill_value=0.0).rename("score")
                except Exception as exc:
                    log.warning("stl.decomposition.failed", error=str(exc))
                    return pd.Series(0.0, index=series.index, name="score")

    def get_state(self) -> dict[str, Any]:
        """JSON-serializable fitted state for persistence/reuse (MM-10.1).

        For ``iqr`` the cached fences (``q1``/``q3``/``iqr``) fully define the
        model; ``zscore``/``stl`` carry their (small) fitted params too. The
        method and window are included so reuse can validate compatibility.
        """
        return {"method": self.method, "window": self.window, "params": self._params}

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore fitted params from a persisted state, skipping re-fit.

        Raises if the stored model was trained for a different method, so a
        reused model can never be applied to the wrong detector.
        """
        stored_method = state.get("method")
        if stored_method != self.method:
            raise ValueError(
                f"Cannot reuse {stored_method!r} model for {self.method!r} detector"
            )
        self._params = dict(state.get("params", {}))

    def detect(
        self,
        series: pd.Series,
        metric_name: str = "",
        threshold: float = 0.8,
    ) -> list[AnomalyResult]:
        scores = self.score(series)
        results = []
        for ts, score in scores.items():
            if score >= threshold:
                results.append(
                    AnomalyResult(
                        timestamp=ts,
                        metric_name=metric_name,
                        value=float(series.loc[ts]),
                        score=float(score),
                        detector=self.name,
                        method=self.method,
                    )
                )
        return results


# Runtime Protocol compliance check
assert isinstance(StatisticalDetector(), Detector), (
    "StatisticalDetector must satisfy the Detector Protocol"
)
