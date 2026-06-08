from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import structlog
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from detection.base import AnomalyResult, Detector

log = structlog.get_logger(__name__)

# Absolute-mode temperature (MM-3.6): sharpness of the fixed sigmoid that maps
# sklearn's raw decision_function (negative = anomalous) to [0, 1]. Smaller =
# sharper transition around the decision boundary. Unlike the relative min-max
# normalisation, this does not depend on the batch's score spread.
_IF_ABS_TEMPERATURE = 0.1


class IsolationForestDetector:
    """
    Multivariate anomaly detection: scores each point based on a feature
    vector derived from the raw value PLUS rolling statistical context.

    Isolation Forest isolates anomalies by randomly partitioning the feature
    space — anomalies require fewer splits to isolate (shorter average path
    length) because they sit in sparse regions.

    RobustScaler normalises features using IQR rather than std — making it
    resistant to the outliers we're trying to detect (no data leakage).

    Python skills:
        - NumPy vectorised feature engineering
        - sklearn Pipeline-compatible transformer pattern
        - n_jobs=-1 for parallel tree building across all CPU cores
        - Careful score normalisation to map sklearn's output to [0, 1]
    """
    name = "isolation_forest"

    def __init__(
        self,
        n_estimators: int = 100,
        contamination: float = 0.05,
        random_state: int = 42,
        scoring_mode: str = "relative",
    ) -> None:
        self._model = IsolationForest(
            n_estimators=n_estimators,
            contamination=contamination,
            random_state=random_state,
            n_jobs=-1,
        )
        self._scaler   = RobustScaler()
        self._is_fitted = False
        # "relative" (per-batch min-max) or "absolute" (fixed sigmoid) — MM-3.6.
        self.scoring_mode = scoring_mode

    def _build_features(self, series: pd.Series) -> pd.DataFrame:
        """
        Construct a feature matrix from a univariate series.
        Each row contains the raw value + rolling statistics for that time step.
        This converts the problem from univariate to multivariate.

        Python skill: chained pandas operations, fillna strategies,
        conditional attribute access for DatetimeIndex.
        """
        df = pd.DataFrame({"value": series})
        df["rolling_mean_5"]  = df["value"].rolling( 5, min_periods=1).mean()
        df["rolling_std_5"]   = df["value"].rolling( 5, min_periods=1).std().fillna(0)
        df["rolling_mean_15"] = df["value"].rolling(15, min_periods=1).mean()
        df["rolling_std_15"]  = df["value"].rolling(15, min_periods=1).std().fillna(0)
        df["rolling_max_15"]  = df["value"].rolling(15, min_periods=1).max()
        df["rolling_min_15"]  = df["value"].rolling(15, min_periods=1).min()
        df["diff_1"]          = df["value"].diff(1).fillna(0)
        df["diff_5"]          = df["value"].diff(5).fillna(0)
        df["diff_15"]         = df["value"].diff(15).fillna(0)
        # Encode time-of-day as a cyclical feature (sine/cosine)
        if hasattr(df.index, "hour"):
            hour = df.index.hour.astype(float)
            df["hour_sin"] = np.sin(2 * np.pi * hour / 24)
            df["hour_cos"] = np.cos(2 * np.pi * hour / 24)
        return df.fillna(0)

    def fit(self, series: pd.Series) -> None:
        X = self._build_features(series)
        X_scaled = self._scaler.fit_transform(X)
        self._model.fit(X_scaled)
        self._is_fitted = True
        log.info("isolation_forest.fitted", samples=len(X))

    def score(self, series: pd.Series) -> pd.Series:
        """
        Map sklearn decision_function output to [0, 1].
        decision_function: negative = anomalous, positive = normal.
        We invert and normalise so that 1.0 = most anomalous.
        """
        if not self._is_fitted:
            raise RuntimeError("Call fit() before score()")
        X = self._build_features(series)
        X_scaled = self._scaler.transform(X)
        raw = self._model.decision_function(X_scaled)

        if self.scoring_mode == "absolute":
            # Fixed sigmoid on the (centered) decision score: raw < 0 → score > 0.5,
            # raw > 0 → score < 0.5. Independent of the batch's score spread, so a
            # given threshold maps to the same decision-boundary distance every run.
            scores = 1.0 / (1.0 + np.exp(raw / _IF_ABS_TEMPERATURE))
            return pd.Series(scores, index=series.index, name="score")

        # Relative (default): invert (lower decision score → higher anomaly score)
        # and min-max normalise within the batch.
        inverted = -raw
        min_v, max_v = inverted.min(), inverted.max()
        if max_v - min_v < 1e-9:
            return pd.Series(0.0, index=series.index, name="score")
        normalised = (inverted - min_v) / (max_v - min_v)
        return pd.Series(normalised, index=series.index, name="score")

    def get_state(self) -> dict[str, Any]:
        """JSON-serializable metadata for persistence (MM-10.1).

        The fitted sklearn forest + RobustScaler are not JSON-serializable, so
        only the hyperparameters are stored (for audit / retraining); the model
        is always re-fit rather than reused.
        """
        return {
            "n_estimators": int(self._model.n_estimators),
            "contamination": float(self._model.contamination),
            "random_state": int(self._model.random_state),
            "fitted": self._is_fitted,
        }

    def detect(
        self,
        series: pd.Series,
        metric_name: str = "",
        threshold: float = 0.75,
    ) -> list[AnomalyResult]:
        scores = self.score(series)
        X = self._build_features(series)
        X_scaled = self._scaler.transform(X)
        # sklearn labels: -1 = anomaly, 1 = normal
        predictions = self._model.predict(X_scaled)
        pred_series = pd.Series(predictions, index=series.index)

        results = []
        for ts in series.index:
            score = float(scores.loc[ts])
            is_anomaly = (score >= threshold) or (pred_series.loc[ts] == -1)
            if is_anomaly:
                results.append(
                    AnomalyResult(
                        timestamp=ts,
                        metric_name=metric_name,
                        value=float(series.loc[ts]),
                        score=score,
                        detector=self.name,
                        method="isolation_forest",
                    )
                )
        return results


assert isinstance(IsolationForestDetector(), Detector), (
    "IsolationForestDetector must satisfy the Detector Protocol"
)
