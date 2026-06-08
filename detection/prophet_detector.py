from __future__ import annotations

import contextlib
import io
from typing import Any

import pandas as pd
import structlog
from prophet import Prophet

from detection.base import AnomalyResult, Detector

log = structlog.get_logger(__name__)


class ProphetDetector:
    """
    Anomaly detection using Prophet's uncertainty intervals.
    Points outside [yhat_lower, yhat_upper] are anomalous; score is
    proportional to how far the actual value falls outside the band.

    Handles trend changepoints, weekly/daily seasonality, and holidays
    automatically — no manual feature engineering needed.

    Python skills:
        - __slots__ for memory efficiency (no __dict__ per instance)
        - contextlib.redirect_stderr to silence noisy library output
        - Dataframe reshaping and tz-aware datetime normalisation
        - Lazy evaluation: model is None until fit() is called
    """
    # NOTE: "name" must NOT be in __slots__ — it is a class-level constant below,
    # and a slot of the same name conflicts with a class variable (ValueError).
    __slots__ = ("_model", "_forecast", "changepoint_prior_scale")
    name = "prophet"

    def __init__(self, changepoint_prior_scale: float = 0.05) -> None:
        self.changepoint_prior_scale = changepoint_prior_scale
        self._model: Prophet | None = None
        self._forecast: pd.DataFrame | None = None

    def fit(self, series: pd.Series) -> None:
        """
        Fit a Prophet model to the series.
        Prophet expects columns: 'ds' (datetime) and 'y' (float value).
        tz-aware timestamps must be stripped — Prophet uses naive UTC internally.
        """
        df = series.reset_index()
        df.columns = pd.Index(["ds", "y"])
        # Prophet does not support tz-aware datetimes
        df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)

        self._model = Prophet(
            changepoint_prior_scale=self.changepoint_prior_scale,
            yearly_seasonality=False,
            weekly_seasonality=True,
            daily_seasonality=True,
            interval_width=0.99,  # tight uncertainty band = more sensitive detection
        )

        # Prophet prints verbose Stan compilation output to stderr
        with contextlib.redirect_stderr(io.StringIO()):
            self._model.fit(df)

        future = self._model.make_future_dataframe(periods=0, freq="1min")
        forecast = self._model.predict(future)
        self._forecast = forecast.set_index("ds")

    def score(self, series: pd.Series) -> pd.Series:
        """
        Score each point by how far it falls outside the uncertainty band.
        Points inside the band score 0.0; deviation is normalised by band width.
        """
        if self._forecast is None:
            raise RuntimeError("Call fit() before score()")

        scores = pd.Series(0.0, index=series.index, name="score")
        for ts, actual in series.items():
            ts_naive = pd.Timestamp(ts).tz_localize(None)
            if ts_naive not in self._forecast.index:
                continue
            row = self._forecast.loc[ts_naive]
            lower, upper = float(row["yhat_lower"]), float(row["yhat_upper"])
            if actual < lower or actual > upper:
                band_width = max(upper - lower, 1e-9)
                deviation  = max(actual - upper, lower - actual, 0.0)
                scores[ts] = min(deviation / band_width, 1.0)

        return scores

    def get_state(self) -> dict[str, Any]:
        """JSON-serializable metadata for persistence (MM-10.1).

        Prophet's fitted model is a Stan object that cannot be JSON-serialized,
        so only the hyperparameter is stored (for audit / retraining); the model
        is always re-fit rather than reused. ``fitted`` records whether a fit ran.
        """
        return {
            "changepoint_prior_scale": self.changepoint_prior_scale,
            "fitted": self._forecast is not None,
        }

    def detect(
        self,
        series: pd.Series,
        metric_name: str = "",
        threshold: float = 0.5,
    ) -> list[AnomalyResult]:
        scores = self.score(series)
        return [
            AnomalyResult(
                timestamp=ts,
                metric_name=metric_name,
                value=float(series.loc[ts]),
                score=float(score),
                detector=self.name,
                method="prophet_interval",
            )
            for ts, score in scores.items()
            if score >= threshold
        ]


assert isinstance(ProphetDetector(), Detector), (
    "ProphetDetector must satisfy the Detector Protocol"
)
