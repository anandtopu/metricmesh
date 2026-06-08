from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import pandas as pd


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    """
    Immutable result from any detector.
    slots=True: Python allocates __slots__ instead of __dict__, saving ~50 bytes/instance.
    frozen=True: hashable, safe to put in sets/dict keys, prevents accidental mutation.
    """
    timestamp: pd.Timestamp
    metric_name: str
    value: float
    score: float            # normalised [0.0, 1.0]
    detector: str
    method: str = ""
    labels: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "metric_name": self.metric_name,
            "value": self.value,
            "score": round(self.score, 6),
            "detector": self.detector,
            "method": self.method,
            "labels": self.labels,
        }


@runtime_checkable
class Detector(Protocol):
    """
    Structural subtyping via Protocol — any class exposing these three methods
    is a Detector. No inheritance required (duck typing compatible with
    third-party libraries).

    Python skill: runtime_checkable Protocol lets you do isinstance() checks
    without forcing an inheritance relationship. Staff-level design: prefer
    Protocol over ABC for interface-only contracts.
    """
    name: str

    def fit(self, series: pd.Series) -> None:
        """Train or warm up the detector on historical data."""
        ...

    def score(self, series: pd.Series) -> pd.Series:
        """Return anomaly scores in [0.0, 1.0] for each point in the series."""
        ...

    def detect(
        self,
        series: pd.Series,
        metric_name: str = "",
        threshold: float = 0.8,
    ) -> list[AnomalyResult]:
        """Return list of AnomalyResult for points that exceed the threshold."""
        ...
