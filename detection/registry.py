from __future__ import annotations

from collections.abc import Callable

from detection.base import Detector
from detection.isolation_forest import IsolationForestDetector
from detection.prophet_detector import ProphetDetector
from detection.statistical import StatisticalDetector

# Python skill: registry pattern — map string keys to factory callables.
# Adding a new detector requires only one line here. Each factory takes the
# scoring_mode (MM-3.6); iqr/prophet are inherently absolute and ignore it.
_REGISTRY: dict[str, Callable[[str], Detector]] = {
    "zscore":            lambda sm: StatisticalDetector(method="zscore", scoring_mode=sm),
    "iqr":               lambda sm: StatisticalDetector(method="iqr", scoring_mode=sm),
    "stl":               lambda sm: StatisticalDetector(method="stl", scoring_mode=sm),
    "prophet":           lambda sm: ProphetDetector(),
    "isolation_forest":  lambda sm: IsolationForestDetector(scoring_mode=sm),
}


def get_detector(name: str, scoring_mode: str = "relative") -> Detector:
    """Instantiate a detector by registry key."""
    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"Unknown detector {name!r}. Available: {list(_REGISTRY)}"
        )
    return factory(scoring_mode)


def all_detector_names() -> list[str]:
    return list(_REGISTRY)
