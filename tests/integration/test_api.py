"""
Integration tests for the FastAPI ingest and anomaly endpoints.
Uses httpx AsyncClient with FastAPI's ASGITransport — no real server needed.

Python skill: pytest-asyncio async fixtures, TestClient alternative for async apps,
monkeypatching dependencies with pytest's monkeypatch.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    """
    Provide an httpx AsyncClient pointed at the FastAPI app.
    Patches out DB init and overrides the get_session dependency so tests don't
    need a real TimescaleDB (ASGITransport does not run the app lifespan, so the
    session factory is never initialised — the storage layer is mocked instead).
    """
    with (
        patch("storage.timescale.init_db"),
        patch("storage.timescale.setup_schema", new_callable=AsyncMock),
        patch("storage.timescale.close_db", new_callable=AsyncMock),
    ):
        from api.main import create_app
        from storage.timescale import get_session

        async def _fake_session():
            yield None   # storage functions are mocked, so the session is unused

        app = create_app()
        app.dependency_overrides[get_session] = _fake_session
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.asyncio
async def test_readiness_ok_when_deps_healthy(client: AsyncClient):
    with (
        patch("api.routes.health._check_database", new_callable=AsyncMock, return_value="ok"),
        patch("api.routes.health._check_redis", new_callable=AsyncMock, return_value="ok"),
    ):
        resp = await client.get("/readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"] == {"database": "ok", "redis": "ok"}


@pytest.mark.asyncio
async def test_readiness_degraded_when_redis_down(client: AsyncClient):
    # MM-8.2: Redis failure alone must mark the service not-ready (503).
    with (
        patch("api.routes.health._check_database", new_callable=AsyncMock, return_value="ok"),
        patch(
            "api.routes.health._check_redis",
            new_callable=AsyncMock,
            return_value="error: connection refused",
        ),
    ):
        resp = await client.get("/readiness")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"].startswith("error")


@pytest.mark.asyncio
async def test_readiness_degraded_when_db_down(client: AsyncClient):
    with (
        patch(
            "api.routes.health._check_database",
            new_callable=AsyncMock,
            return_value="error: timeout",
        ),
        patch("api.routes.health._check_redis", new_callable=AsyncMock, return_value="ok"),
    ):
        resp = await client.get("/readiness")
    assert resp.status_code == 503
    assert resp.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_ingest_single_valid(client: AsyncClient):
    with patch("ingestion.api.bulk_insert", new_callable=AsyncMock, return_value=1):
        resp = await client.post(
            "/api/v1/metrics/single",
            json={"metric_name": "test.latency", "value": 42.5},
        )
    assert resp.status_code == 202
    assert resp.json()["accepted"] == 1


@pytest.mark.asyncio
async def test_ingest_invalid_metric_name(client: AsyncClient):
    resp = await client.post(
        "/api/v1/metrics/single",
        json={"metric_name": "Invalid-Name!", "value": 1.0},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_batch(client: AsyncClient):
    points = [
        {"metric_name": "cpu.usage", "value": float(i), "timestamp": time.time()}
        for i in range(10)
    ]
    with patch("ingestion.api.bulk_insert", new_callable=AsyncMock, return_value=10):
        resp = await client.post(
            "/api/v1/metrics",
            json={"points": points, "source_id": "test-runner"},
        )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 10
    assert body["rejected"] == 0


def _alert_row(metric: str = "cpu.usage", score: float = 0.97) -> dict:
    return {
        "id": 1,
        "created_at": "2026-06-03T15:30:00+00:00",
        "metric_name": metric,
        "detector": "statistical",
        "score": score,
        "value": 187.4,
        "fingerprint": "deadbeef0001",
        "routed_to": ["log"],
        "label": "unlabeled",
    }


@pytest.mark.asyncio
async def test_list_alerts_returns_page(client: AsyncClient):
    rows = [_alert_row(), _alert_row(metric="mem.usage", score=0.81)]
    with patch(
        "storage.timescale.fetch_alerts",
        new_callable=AsyncMock,
        return_value=(rows, 5),
    ):
        resp = await client.get("/api/v1/anomalies?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["total"] == 5          # full match count, ignoring pagination
    assert body["limit"] == 2 and body["offset"] == 0
    assert body["items"][0]["routed_to"] == ["log"]


@pytest.mark.asyncio
async def test_list_alerts_passes_filters(client: AsyncClient):
    with patch(
        "storage.timescale.fetch_alerts",
        new_callable=AsyncMock,
        return_value=([], 0),
    ) as mock_fetch:
        resp = await client.get(
            "/api/v1/anomalies",
            params={
                "metric": "cpu.usage",
                "detector": "statistical",
                "min_score": 0.9,
                "from": "2026-06-01T00:00:00Z",
                "limit": 10,
                "offset": 20,
            },
        )
    assert resp.status_code == 200
    kwargs = mock_fetch.call_args.kwargs
    assert kwargs["metric_name"] == "cpu.usage"
    assert kwargs["detector"] == "statistical"
    assert kwargs["min_score"] == 0.9
    assert kwargs["start"] is not None          # 'from' alias parsed to datetime
    assert kwargs["limit"] == 10 and kwargs["offset"] == 20


@pytest.mark.asyncio
async def test_list_alerts_empty(client: AsyncClient):
    with patch(
        "storage.timescale.fetch_alerts",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        resp = await client.get("/api/v1/anomalies?metric=does.not.exist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == [] and body["count"] == 0 and body["total"] == 0


@pytest.mark.asyncio
async def test_list_alerts_rejects_bad_min_score(client: AsyncClient):
    resp = await client.get("/api/v1/anomalies?min_score=2.0")   # > 1.0
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_submit_feedback_ok(client: AsyncClient):
    with patch("storage.timescale.set_alert_label", new_callable=AsyncMock, return_value=True):
        resp = await client.post("/api/v1/anomalies/5/feedback", json={"label": "true_positive"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 5, "label": "true_positive"}


@pytest.mark.asyncio
async def test_submit_feedback_404_when_missing(client: AsyncClient):
    with patch("storage.timescale.set_alert_label", new_callable=AsyncMock, return_value=False):
        resp = await client.post("/api/v1/anomalies/999/feedback", json={"label": "false_positive"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_submit_feedback_rejects_bad_label(client: AsyncClient):
    resp = await client.post("/api/v1/anomalies/5/feedback", json={"label": "maybe"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_feedback_stats(client: AsyncClient):
    stats = [
        {"detector": "statistical", "true_positive": 8, "false_positive": 2,
         "unlabeled": 5, "total": 15, "precision": 0.8},
    ]
    with patch(
        "storage.timescale.feedback_stats_by_detector", new_callable=AsyncMock, return_value=stats
    ):
        resp = await client.get("/api/v1/anomalies/stats")
    assert resp.status_code == 200
    assert resp.json()["by_detector"][0]["precision"] == 0.8


# ── Alert detail with explanation (MM-7.5) ─────────────────────────────────
def _alert_detail_row(detector: str = "statistical", method: str = "zscore") -> dict:
    from datetime import UTC, datetime

    return {
        "id": 7,
        "created_at": "2026-06-03T15:30:00+00:00",
        "created_at_dt": datetime(2026, 6, 3, 15, 30, tzinfo=UTC),
        "metric_name": "cpu.usage",
        "detector": detector,
        "method": method,
        "score": 0.94,
        "value": 187.4,
        "fingerprint": "deadbeef0007",
        "routed_to": ["log"],
        "label": "unlabeled",
    }


@pytest.mark.asyncio
async def test_alert_detail_returns_context(client: AsyncClient):
    series = [
        {"ts": "2026-06-03T15:29:00+00:00", "avg": 90.0, "max": 95.0, "min": 88.0, "count": 6}
    ]
    with (
        patch(
            "storage.timescale.fetch_alert_by_id",
            new_callable=AsyncMock,
            return_value=_alert_detail_row(),
        ),
        patch(
            "storage.timescale.fetch_metric_series",
            new_callable=AsyncMock,
            return_value=series,
        ) as mock_series,
    ):
        resp = await client.get("/api/v1/anomalies/7?window_minutes=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 7
    assert body["detector"] == "statistical" and body["method"] == "zscore"
    # Default config: zscore_threshold = 0.8, no per-metric override.
    assert body["threshold"] == 0.8
    assert "zscore" in body["explanation"] and "0.94" in body["explanation"]
    assert body["series"] == series
    assert body["window"]["minutes"] == 10 and body["window"]["resolution"] == "1m"
    # Series window is centred on the alert time (± window_minutes).
    kwargs = mock_series.call_args.kwargs
    assert kwargs["start"] is not None and kwargs["end"] is not None


@pytest.mark.asyncio
async def test_alert_detail_404_when_missing(client: AsyncClient):
    with patch(
        "storage.timescale.fetch_alert_by_id", new_callable=AsyncMock, return_value=None
    ):
        resp = await client.get("/api/v1/anomalies/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_alert_detail_does_not_shadow_static_routes(client: AsyncClient):
    # /anomalies/stats must still hit the stats route, not /{alert_id}.
    with patch(
        "storage.timescale.feedback_stats_by_detector",
        new_callable=AsyncMock,
        return_value=[],
    ):
        resp = await client.get("/api/v1/anomalies/stats")
    assert resp.status_code == 200
    assert "by_detector" in resp.json()


# ── Detector A/B report (MM-10.4) ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_detector_report_assembles_per_metric(client: AsyncClient):
    counts = [
        {"metric_name": "cpu.usage", "detector": "isolation_forest",
         "tp": 8, "fp": 2, "unlabeled": 0, "total": 10},
        {"metric_name": "cpu.usage", "detector": "statistical",
         "tp": 3, "fp": 7, "unlabeled": 0, "total": 10},
    ]
    recall = [
        {"metric_name": "cpu.usage", "detector": "isolation_forest",
         "covered_buckets": 9, "total_buckets": 10},
        {"metric_name": "cpu.usage", "detector": "statistical",
         "covered_buckets": 3, "total_buckets": 10},
    ]
    with patch(
        "storage.timescale.detector_report_rows",
        new_callable=AsyncMock,
        return_value=(counts, recall),
    ) as mock_rows:
        resp = await client.get("/api/v1/anomalies/report?bucket_minutes=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket_minutes"] == 10
    cpu = body["metrics"][0]
    assert cpu["metric_name"] == "cpu.usage"
    assert cpu["recommended_detector"] == "isolation_forest"
    assert cpu["detectors"][0]["precision"] == 0.8
    assert "not true recall" in body["notes"]
    # bucket_minutes is passed through to the storage layer.
    assert mock_rows.call_args.kwargs["bucket_minutes"] == 10


@pytest.mark.asyncio
async def test_detector_report_does_not_shadow_alert_detail(client: AsyncClient):
    # /report is static; /{alert_id} is dynamic — both must resolve correctly.
    with patch(
        "storage.timescale.detector_report_rows",
        new_callable=AsyncMock,
        return_value=([], []),
    ):
        report = await client.get("/api/v1/anomalies/report")
    with patch(
        "storage.timescale.fetch_alert_by_id", new_callable=AsyncMock, return_value=None
    ):
        detail = await client.get("/api/v1/anomalies/42")
    assert report.status_code == 200 and report.json()["metrics"] == []
    assert detail.status_code == 404


@pytest.mark.asyncio
async def test_read_series_raw(client: AsyncClient):
    pts = [
        {"ts": "2026-06-03T10:00:00+00:00", "value": 50.1},
        {"ts": "2026-06-03T10:01:00+00:00", "value": 51.3},
    ]
    with patch(
        "storage.timescale.fetch_metric_series", new_callable=AsyncMock, return_value=pts
    ):
        resp = await client.get("/api/v1/metrics/cpu.usage/series?resolution=raw")
    assert resp.status_code == 200
    body = resp.json()
    assert body["metric_name"] == "cpu.usage"
    assert body["resolution"] == "raw"
    assert body["count"] == 2
    assert body["points"][0]["value"] == 50.1


@pytest.mark.asyncio
async def test_read_series_defaults_to_1m_and_passes_window(client: AsyncClient):
    agg = [{"ts": "2026-06-03T10:00:00+00:00", "avg": 50.0, "max": 60.0, "min": 40.0, "count": 6}]
    with patch(
        "storage.timescale.fetch_metric_series", new_callable=AsyncMock, return_value=agg
    ) as mock_fetch:
        resp = await client.get(
            "/api/v1/metrics/cpu.usage/series",
            params={"from": "2026-06-03T00:00:00Z", "to": "2026-06-03T12:00:00Z", "limit": 500},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolution"] == "1m"          # default
    assert body["points"][0]["avg"] == 50.0
    kwargs = mock_fetch.call_args.kwargs
    assert kwargs["resolution"] == "1m"
    assert kwargs["start"] is not None and kwargs["end"] is not None
    assert kwargs["limit"] == 500


@pytest.mark.asyncio
async def test_read_series_rejects_bad_resolution(client: AsyncClient):
    resp = await client.get("/api/v1/metrics/cpu.usage/series?resolution=5m")
    assert resp.status_code == 422


# ── Dead-letter inspection (MM-4.6) ────────────────────────────────────────
@pytest.mark.asyncio
async def test_dead_letters_listing(client: AsyncClient):
    rows = [
        {
            "id": 1,
            "failed_at": "2026-06-06T10:00:00+00:00",
            "task_name": "workers.tasks.route_alert",
            "task_id": "abc-123",
            "queue": "alerts",
            "retries": 3,
            "exception": "RuntimeError: All sinks failed",
            "args": [{"metric_name": "cpu.usage"}],
            "kwargs": {},
            "traceback": "Traceback (most recent call last): ...",
        }
    ]
    with patch(
        "storage.timescale.fetch_dead_letters",
        new_callable=AsyncMock,
        return_value=(rows, 1),
    ) as mock_fetch:
        resp = await client.get("/api/v1/dead-letters?task_name=workers.tasks.route_alert&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and body["count"] == 1
    item = body["items"][0]
    assert item["task_name"] == "workers.tasks.route_alert"
    assert item["retries"] == 3
    assert item["exception"].startswith("RuntimeError")
    kwargs = mock_fetch.call_args.kwargs
    assert kwargs["task_name"] == "workers.tasks.route_alert" and kwargs["limit"] == 10


@pytest.mark.asyncio
async def test_dead_letters_empty(client: AsyncClient):
    with patch(
        "storage.timescale.fetch_dead_letters",
        new_callable=AsyncMock,
        return_value=([], 0),
    ):
        resp = await client.get("/api/v1/dead-letters")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# ── Audit trail (MM-9.5) ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_feedback_is_audited(client: AsyncClient):
    with (
        patch("storage.timescale.set_alert_label", new_callable=AsyncMock, return_value=True),
        patch("storage.timescale.record_audit_async", new_callable=AsyncMock) as audit,
    ):
        resp = await client.post("/api/v1/anomalies/5/feedback", json={"label": "true_positive"})
    assert resp.status_code == 200
    audit.assert_awaited_once()
    assert audit.call_args.args[0] == "feedback.submitted"   # action (positional)
    kw = audit.call_args.kwargs
    assert kw["resource"] == "alert:5"
    assert kw["detail"]["label"] == "true_positive"
    assert kw["principal"] == "anonymous"   # auth disabled in this test


@pytest.mark.asyncio
async def test_auth_denial_is_audited(client: AsyncClient):
    with (
        patch(
            "api.auth.get_settings",
            return_value=SimpleNamespace(
                key_tenant_map={"secret123": "acme"}, default_tenant="default"
            ),
        ),
        patch("storage.timescale.record_audit_async", new_callable=AsyncMock) as audit,
    ):
        resp = await client.get("/api/v1/anomalies")   # no key → denied
    assert resp.status_code == 401
    audit.assert_awaited_once()
    assert audit.call_args.args[0] == "auth.denied"   # action (positional)
    kw = audit.call_args.kwargs
    assert kw["outcome"] == "denied"
    assert kw["resource"] == "/api/v1/anomalies"
    assert kw["principal"] == "none"        # no key presented


@pytest.mark.asyncio
async def test_audit_listing(client: AsyncClient):
    rows = [
        {
            "id": 1,
            "at": "2026-06-07T09:00:00+00:00",
            "action": "alert.routed",
            "principal": "system",
            "outcome": "success",
            "resource": "cpu.usage",
            "source_ip": None,
            "detail": {"sinks": ["log"]},
        }
    ]
    with patch(
        "storage.timescale.fetch_audit_log",
        new_callable=AsyncMock,
        return_value=(rows, 1),
    ) as mock_fetch:
        resp = await client.get("/api/v1/audit?action=alert.routed&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["action"] == "alert.routed"
    assert body["items"][0]["detail"]["sinks"] == ["log"]
    assert mock_fetch.call_args.kwargs["action"] == "alert.routed"


# ── Auth (MM-9.1) ──────────────────────────────────────────────────────────
from types import SimpleNamespace  # noqa: E402


def _auth_settings(tenant: str = "acme"):
    # MM-9.3: auth resolves a key -> tenant via key_tenant_map.
    return SimpleNamespace(key_tenant_map={"secret123": tenant}, default_tenant="default")


@pytest.mark.asyncio
async def test_protected_endpoint_401_without_key(client: AsyncClient):
    # When keys are configured, a data endpoint requires X-API-Key.
    with patch("api.auth.get_settings", return_value=_auth_settings()):
        resp = await client.get("/api/v1/anomalies")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_endpoint_ok_with_valid_key(client: AsyncClient):
    with (
        patch("api.auth.get_settings", return_value=_auth_settings()),
        patch("storage.timescale.fetch_alerts", new_callable=AsyncMock, return_value=([], 0)),
    ):
        resp = await client.get("/api/v1/anomalies", headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_valid_key_scopes_reads_to_its_tenant(client: AsyncClient):
    # MM-9.3: the caller's tenant (from the key) is passed through to storage.
    with (
        patch("api.auth.get_settings", return_value=_auth_settings(tenant="acme")),
        patch("storage.timescale.fetch_alerts", new_callable=AsyncMock, return_value=([], 0)) as f,
    ):
        resp = await client.get("/api/v1/anomalies", headers={"X-API-Key": "secret123"})
    assert resp.status_code == 200
    assert f.call_args.kwargs["tenant"] == "acme"


@pytest.mark.asyncio
async def test_health_open_when_auth_enabled(client: AsyncClient):
    # Probes stay unauthenticated even with keys configured.
    with patch("api.auth.get_settings", return_value=_auth_settings()):
        resp = await client.get("/health")
    assert resp.status_code == 200


# ── Rate limiting (MM-1.5) ─────────────────────────────────────────────────
import fakeredis  # noqa: E402


@pytest.mark.asyncio
async def test_ingest_rate_limited_over_quota(client: AsyncClient):
    fake = fakeredis.FakeAsyncRedis(decode_responses=True)
    body = {"metric_name": "test.latency", "value": 1.0}
    with (
        patch("api.ratelimit.get_settings", return_value=SimpleNamespace(rate_limit_per_minute=2)),
        patch("api.ratelimit._get_redis", return_value=fake),
        patch("ingestion.api.bulk_insert", new_callable=AsyncMock, return_value=1),
    ):
        r1 = await client.post("/api/v1/metrics/single", json=body)
        r2 = await client.post("/api/v1/metrics/single", json=body)
        r3 = await client.post("/api/v1/metrics/single", json=body)
    assert r1.status_code == 202
    assert r2.status_code == 202
    assert r3.status_code == 429
    assert r3.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_ingest_rate_limit_fails_open_on_redis_error(client: AsyncClient):
    class _BoomRedis:
        async def incr(self, *a):
            raise RuntimeError("redis down")

        async def expire(self, *a):
            return None

    body = {"metric_name": "t.m", "value": 1.0}
    with (
        patch("api.ratelimit.get_settings", return_value=SimpleNamespace(rate_limit_per_minute=1)),
        patch("api.ratelimit._get_redis", return_value=_BoomRedis()),
        patch("ingestion.api.bulk_insert", new_callable=AsyncMock, return_value=1),
    ):
        r1 = await client.post("/api/v1/metrics/single", json=body)
        r2 = await client.post("/api/v1/metrics/single", json=body)
    assert r1.status_code == 202 and r2.status_code == 202   # fail open, not 429/500
