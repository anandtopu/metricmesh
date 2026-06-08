"""Unit tests for Redis-backed alert deduplication (MM-5.1 / MM-5.2)."""
from __future__ import annotations

import time

import fakeredis
import pytest

from alerting.dedup import _KEY_PREFIX, AlertDeduplicator, time_bucket


def _anomaly(metric: str = "cpu.usage", score: float = 0.9) -> dict:
    return {
        "metric_name": metric,
        "detector": "zscore",
        "score": score,
        "value": 99.5,
        "timestamp": time.time(),
    }


@pytest.fixture
def fake_redis():
    # A shared FakeServer lets multiple clients (i.e. multiple "workers") see
    # the same data, exactly like a real shared Redis.
    server = fakeredis.FakeServer()
    return lambda: fakeredis.FakeStrictRedis(server=server, decode_responses=True)


def _dedup(client_factory, cooldown: int = 300) -> AlertDeduplicator:
    return AlertDeduplicator(cooldown_seconds=cooldown, redis_client=client_factory())


class TestAlertDeduplicator:
    def test_first_alert_passes(self, fake_redis):
        d = _dedup(fake_redis)
        assert len(d.filter([_anomaly()])) == 1

    def test_duplicate_within_cooldown_suppressed(self, fake_redis):
        d = _dedup(fake_redis)
        a = _anomaly()
        assert len(d.filter([a])) == 1
        assert len(d.filter([a])) == 0   # suppressed

    def test_different_metrics_both_pass(self, fake_redis):
        d = _dedup(fake_redis)
        result = d.filter([_anomaly("cpu.usage"), _anomaly("mem.usage")])
        assert len(result) == 2

    def test_passing_anomaly_is_stamped_with_fingerprint(self, fake_redis):
        # MM-7.1: dedup stamps the fingerprint so the alerts table can store it.
        d = _dedup(fake_redis)
        result = d.filter([_anomaly()])
        fp = result[0].get("fingerprint")
        assert isinstance(fp, str) and len(fp) == 12

    def test_reset_clears_state(self, fake_redis):
        d = _dedup(fake_redis)
        a = _anomaly()
        d.filter([a])
        d.reset()
        assert len(d.filter([a])) == 1   # passes again after reset

    def test_shared_across_workers(self, fake_redis):
        # MM-5.2: two deduplicators backed by the same Redis (two alert workers)
        # must suppress the same anomaly across instances, not just within one.
        worker_a = _dedup(fake_redis)
        worker_b = _dedup(fake_redis)
        a = _anomaly()
        assert len(worker_a.filter([a])) == 1   # worker A alerts
        assert len(worker_b.filter([a])) == 0   # worker B suppresses

    def test_claim_has_ttl_for_durability(self, fake_redis):
        # MM-5.2: the claim must carry a TTL so it self-evicts and survives
        # restarts only until cooldown elapses.
        client = fake_redis()
        d = AlertDeduplicator(cooldown_seconds=300, redis_client=client)
        result = d.filter([_anomaly()])
        ttl = client.ttl(_KEY_PREFIX + result[0]["fingerprint"])
        assert 0 < ttl <= 300

    def test_expiry_allows_realert(self, fake_redis):
        # After the cooldown elapses, the same anomaly may alert again.
        d = _dedup(fake_redis, cooldown=1)
        a = _anomaly()
        assert len(d.filter([a])) == 1
        assert len(d.filter([a])) == 0
        time.sleep(1.1)
        assert len(d.filter([a])) == 1   # claim expired → re-alert

    def test_bucket_width_controls_dedup_grouping(self, fake_redis):
        # MM-5.3: a wider bucket merges two nearby spikes into one alert; a
        # narrower bucket keeps them distinct.
        a = {"metric_name": "cpu.usage", "detector": "zscore", "score": 0.9,
             "value": 1.0, "timestamp": "2026-06-04T12:30:00+00:00"}
        b = {**a, "timestamp": "2026-06-04T12:34:00+00:00"}   # 4 minutes later

        wide = AlertDeduplicator(cooldown_seconds=300, redis_client=fake_redis(),
                                 bucket_seconds=300)
        assert len(wide.filter([a])) == 1
        assert len(wide.filter([b])) == 0      # same 5-min bucket → suppressed

        narrow = AlertDeduplicator(cooldown_seconds=300, redis_client=fake_redis(),
                                   bucket_seconds=60)
        assert len(narrow.filter([a])) == 1
        assert len(narrow.filter([b])) == 1    # distinct 1-min buckets → both pass

    def test_tenant_isolates_dedup(self, fake_redis):
        # MM-9.3: the same metric/detector/bucket for two tenants must NOT dedup
        # each other — tenant is part of the fingerprint.
        d = _dedup(fake_redis)
        base = {"metric_name": "cpu.usage", "detector": "zscore", "score": 0.9,
                "value": 1.0, "timestamp": "2026-06-08T12:00:00+00:00"}
        acme = {**base, "tenant": "acme"}
        globex = {**base, "tenant": "globex"}
        assert len(d.filter([acme])) == 1
        assert len(d.filter([globex])) == 1     # different tenant → passes
        assert len(d.filter([acme])) == 0       # same tenant repeat → suppressed

    def test_thread_safety(self, fake_redis):
        """Concurrent calls against the shared store should not raise."""
        import threading
        d = _dedup(fake_redis)
        errors: list[Exception] = []

        def run():
            try:
                for _ in range(50):
                    d.filter([_anomaly()])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


class TestTimeBucket:
    """MM-5.3: configurable, epoch-based time bucketing."""

    def test_width_is_configurable(self):
        a = "2026-06-04T12:30:00+00:00"
        b = "2026-06-04T12:34:00+00:00"   # 4 minutes later
        assert time_bucket(a, 300) == time_bucket(b, 300)   # same 5-min window
        assert time_bucket(a, 60) != time_bucket(b, 60)     # different 1-min windows

    def test_accepts_epoch_float(self):
        now = 1_700_000_000.0
        assert time_bucket(now, 300) == int(now // 300)

    def test_handles_z_suffix_and_offset_equivalently(self):
        assert time_bucket("2026-06-04T12:30:00Z", 300) == time_bucket(
            "2026-06-04T12:30:00+00:00", 300
        )

    def test_unparseable_timestamp_does_not_raise(self):
        # Falls back to "now" rather than crashing dedup.
        assert isinstance(time_bucket("not-a-timestamp", 300), int)
