from __future__ import annotations

import hashlib
import time
from typing import Any

import redis
import structlog

log = structlog.get_logger(__name__)

# Namespace for dedup keys so they never collide with Celery's own Redis keys.
_KEY_PREFIX = "mm:dedup:"


def _to_epoch(ts_raw: object) -> float:
    """Best-effort convert an anomaly timestamp to epoch seconds.

    Accepts a numeric epoch (``time.time()``) or an ISO-8601 string (with or
    without timezone / ``Z`` / fractional seconds). Falls back to *now* for
    anything unparseable so dedup never crashes on a bad timestamp.
    """
    if isinstance(ts_raw, (int, float)):
        return float(ts_raw)
    try:
        from datetime import datetime

        return datetime.fromisoformat(str(ts_raw).strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def time_bucket(ts_raw: object, bucket_seconds: int | None = None) -> int:
    """Map an anomaly timestamp to an integer ``floor(epoch / bucket_seconds)``.

    Detectors flagging the same spike on a sweep share the exact timestamp, so
    this groups them together for both dedup (MM-5.1) and consensus (MM-10.5).
    The bucket width is configurable (MM-5.3): ``bucket_seconds`` defaults to
    ``config.dedup_bucket_seconds`` (300s = 5 min) so the same value is used
    consistently by dedup and consensus.
    """
    if bucket_seconds is None:
        from config import get_settings

        bucket_seconds = get_settings().dedup_bucket_seconds
    return int(_to_epoch(ts_raw) // bucket_seconds)


class AlertDeduplicator:
    """
    Suppresses duplicate alerts using a SHA-1 fingerprint keyed on
    (metric_name, detector, 5-minute time bucket).

    State lives in **Redis** via an atomic ``SET key 1 NX EX <cooldown>``:

      - **Shared** — every `alerts` worker hits the same Redis, so the same
        spike fired by two workers (or re-detected on the next 60s sweep) is
        alerted on exactly once within the cooldown window.
      - **Durable** — survives worker restarts; the claim lives until its TTL.
      - **Self-evicting** — Redis expires keys after `cooldown` seconds, so
        there is no in-process state to grow or sweep.

    The Redis client is injectable so tests can pass a fake in-memory client.
    """

    def __init__(
        self,
        cooldown_seconds: int | None = None,
        redis_client: redis.Redis | None = None,
        bucket_seconds: int | None = None,
    ) -> None:
        from config import get_settings

        settings = get_settings()
        self.cooldown = cooldown_seconds or settings.alert_cooldown_seconds
        # MM-5.3: bucket width for the dedup fingerprint, tunable via config.
        self.bucket_seconds = bucket_seconds or settings.dedup_bucket_seconds
        self._redis = redis_client or redis.Redis.from_url(
            settings.redis_url, decode_responses=True
        )

    def _fingerprint(self, anomaly: dict[str, Any]) -> str:
        """Compute a stable 12-char hex fingerprint for an anomaly."""
        # Bucket the timestamp (default 5 min) so the same spike doesn't fire
        # once per detection — width is configurable (MM-5.3). The tenant is part
        # of the key (MM-9.3) so two tenants' identical metric/spike never
        # collide and dedup each other.
        bucket = time_bucket(anomaly.get("timestamp", time.time()), self.bucket_seconds)
        key = (
            f"{anomaly.get('tenant', 'default')}:{anomaly.get('metric_name', '')}:"
            f"{anomaly.get('detector', '')}:{bucket}"
        )
        return hashlib.sha1(key.encode()).hexdigest()[:12]

    def filter(self, anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Return only anomalies not already seen within the cooldown window.

        Each fingerprint is claimed atomically in Redis with SET NX EX, so the
        check-and-mark is race-free across concurrent workers.
        """
        unique: list[dict[str, Any]] = []

        for anomaly in anomalies:
            fp = self._fingerprint(anomaly)
            # Stamp the fingerprint so downstream persistence (the alerts table)
            # can store it without recomputing.
            anomaly["fingerprint"] = fp

            # Returns True only for the first caller to claim this fingerprint
            # within the cooldown window; later calls see the existing key.
            claimed = self._redis.set(_KEY_PREFIX + fp, "1", nx=True, ex=self.cooldown)
            if claimed:
                unique.append(anomaly)
                log.debug("dedup.pass", fp=fp, metric=anomaly.get("metric_name"))
            else:
                log.debug("dedup.suppress", fp=fp, metric=anomaly.get("metric_name"))

        return unique

    def reset(self) -> None:
        """Clear all dedup keys (useful in tests)."""
        keys = list(self._redis.scan_iter(match=_KEY_PREFIX + "*"))
        if keys:
            self._redis.delete(*keys)
