from __future__ import annotations

import json
from fnmatch import fnmatchcase
from functools import lru_cache

import structlog

log = structlog.get_logger(__name__)


@lru_cache(maxsize=8)
def _parse(raw: str) -> tuple[tuple[str, dict[str, float]], ...]:
    """Parse the ``metric_thresholds`` JSON into ordered (glob, overrides) pairs.

    Cached on the raw string (config is a stable singleton). Malformed JSON or
    entries fail safe to "no overrides" with a warning rather than crashing the
    sweep — a typo must never take detection down.
    """
    if not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("metric_thresholds.invalid_json", error=str(exc))
        return ()
    if not isinstance(data, dict):
        log.warning("metric_thresholds.not_an_object")
        return ()

    pairs: list[tuple[str, dict[str, float]]] = []
    for glob, overrides in data.items():
        if not isinstance(overrides, dict):
            log.warning("metric_thresholds.bad_entry", match=glob)
            continue
        try:
            pairs.append((glob, {k: float(v) for k, v in overrides.items()}))
        except (TypeError, ValueError) as exc:
            log.warning("metric_thresholds.bad_value", match=glob, error=str(exc))
    return tuple(pairs)


def resolve_threshold(metric_name: str, detector_key: str, default: float) -> float:
    """Return the per-metric threshold for ``(metric, detector)`` or ``default``.

    ``detector_key`` is the registry/method key the threshold is stored under
    (``zscore``/``iqr``/``stl``/``prophet``/``isolation_forest``). The first
    glob that matches ``metric_name`` *and* defines that detector wins; case is
    significant (metric names are lower-cased by validation).
    """
    from config import get_settings

    for glob, overrides in _parse(get_settings().metric_thresholds):
        if detector_key in overrides and fnmatchcase(metric_name, glob):
            return overrides[detector_key]
    return default
