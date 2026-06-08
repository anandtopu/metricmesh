# MetricMesh — Non-Functional Requirements (NFRs) & SLAs

These requirements apply across all epics in [USER_STORIES.md](./USER_STORIES.md). They define *how
well* the system must behave, independent of any single feature. Each NFR has a measurable target and
a verification method.

**Status legend:** ✅ Met · 🟡 Partial · ⬜ Not yet addressed

---

## 1. Performance & latency

| ID | Requirement | Target | Status | Verification |
|----|-------------|--------|--------|--------------|
| NFR-P1 | Batch ingest throughput | ≥ 10,000 points/request via single `unnest` insert; ≥ 50k points/s sustained per API replica | 🟡 | Load test; `bulk_insert` is single round-trip ✅, throughput unbenchmarked |
| NFR-P2 | Ingest API latency | p95 ≤ 200ms for a 500-point batch | ⬜ | k6/Locust against `/api/v1/metrics` |
| NFR-P3 | On-demand detection latency | p95 ≤ 2s (statistical), ≤ 30s (Prophet) | 🟡 | Time `POST /anomalies/detect`; bounded by detector `time_limit`s |
| NFR-P4 | Scheduled detection latency | p95 ≤ 90s from data point to alert | 🟡 | 60s sweep + queue time; measure end-to-end once MM-7.1 lands |
| NFR-P5 | Query API latency | p95 ≤ 500ms for anomaly history queries | 🟡 | `GET /api/v1/anomalies` implemented (MM-7.2), indexed on `(metric_name, created_at DESC)`; latency unbenchmarked |

## 2. Scalability

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-S1 | Horizontal worker scaling | Add `fast`/`slow`/`alerts` workers without code change | ✅ | Per-queue workers in compose; `prefetch=1` prevents hoarding |
| NFR-S2 | Metric cardinality | Support ≥ 10,000 active metrics per sweep | 🟡 | Sweep dispatches per-metric; validate chord size + Redis load |
| NFR-S3 | Storage growth | Bounded by compression + retention | ✅ | Compression (7d) + configurable retention policy (MM-2.4) |
| NFR-S4 | API horizontal scaling | Stateless API replicas behind a load balancer | ✅ | API holds no per-request state; engine pool per replica |
| NFR-S5 | Dedup correctness under scale-out | No duplicate alerts across workers | ✅ | Redis `SET NX EX` shared across all alert workers (MM-5.2) |

## 3. Reliability & availability

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-R1 | Platform availability | ≥ 99.5% (API + workers) | 🟡 | Building blocks shipped — Helm chart with liveness/readiness probes, HPA, multi-replica API/workers (MM-8.2, MM-11.5); the 99.5% target itself needs a real cluster + production monitoring |
| NFR-R2 | No silent task loss | At-least-once task execution | ✅ | `task_acks_late=True`, `task_reject_on_worker_lost=True`; retry-exhausted tasks captured in the `dead_letters` store (MM-4.6) instead of vanishing |
| NFR-R3 | Transient-failure resilience | Auto-retry with backoff + jitter | ✅ | `autoretry_for`, `retry_backoff`, `retry_jitter` |
| NFR-R4 | Graceful degradation | A failing detector/sink doesn't fail the whole sweep | ✅ | Per-task try/except; router tolerates partial sink failure |
| NFR-R5 | Startup resilience | Idempotent schema setup; DB/Redis dependency ordering | ✅ | `IF NOT EXISTS` DDL; compose `depends_on: service_healthy` |
| NFR-R6 | Data durability | Persisted metrics survive restarts | ✅ | TimescaleDB volume `tsdb_data` |
| NFR-R7 | Connection robustness | Detect stale DB connections | ✅ | `pool_pre_ping=True`, `pool_recycle` |

## 4. Security & privacy

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-SEC1 | Authenticated API | All non-health endpoints require auth | ✅ | `X-API-Key` on data routers when `API_KEYS` set (MM-9.1) |
| NFR-SEC2 | Restricted CORS | Allow-list origins in prod | ✅ | Configurable allow-list, no `*` (MM-9.2) |
| NFR-SEC3 | Secrets handling | No secrets in repo; injected at runtime | ✅ | `.env` git-ignored; `.env.example` placeholders; secret-store via `secrets_dir` (MM-9.4) |
| NFR-SEC4 | Tenant isolation | No cross-tenant data access | ✅ | Key→tenant tag on ingest + `WHERE tenant=` on every read; detection pipeline runs per tenant (alerts/models isolated). Verified live: cross-tenant read → 404/empty (MM-9.3). App-level filtering; RLS is a future hardening |
| NFR-SEC5 | Transport security | TLS for external traffic | ⬜ | Terminate at ingress/LB in front of the `mm-api` Service (documented in the Helm chart README, MM-11.5) |
| NFR-SEC6 | Input safety | Reject malformed/oversized input | ✅ | Pydantic strict validation; batch size cap |
| NFR-SEC7 | Audit trail | Record who/what/when for security-relevant events | ✅ | `audit_log` table: auth denials, feedback, alert routing; `GET /api/v1/audit`; key id hashed (MM-9.5) |
| NFR-SEC7 | Least-privilege DB | App DB user scoped to needed objects | 🟡 | Single `mm` superuser in dev; scope down for prod |

## 5. Observability

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-O1 | Liveness probe | `/health` 200 when serving | ✅ | `api/routes/health.py` |
| NFR-O2 | Readiness probe | `/readiness` checks DB **and** Redis | ✅ | Pings both; returns 503 degraded (MM-8.2) |
| NFR-O3 | Task visibility | Flower shows queues/tasks/workers | ✅ | `:5555` |
| NFR-O4 | Structured logs | JSON/event logs with key fields | ✅ | structlog / celery task logger |
| NFR-O5 | App metrics exposition | Prometheus-scrapable app metrics | ✅ | `GET /metrics` exposes ingest/detection/anomaly/alert + queue-depth metrics; cross-process via shared multiproc dir (MM-8.6) |
| NFR-O6 | Dashboards | Provisioned Grafana dashboards | ✅ | TimescaleDB datasource + overview dashboard auto-provisioned (MM-8.5) |
| NFR-O7 | Traceability | Correlate an alert back to its data/series | ✅ | `GET /anomalies/{id}` returns the alert + resolved threshold + explanation + surrounding series window (MM-7.5, on MM-7.1) |

## 6. Maintainability & quality

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-M1 | Lint clean | `ruff check` passes (E,F,I,UP,B,SIM) | ✅ | `make lint` |
| NFR-M2 | Type safety | `mypy --strict` passes | ✅ | 0 errors over shipping code (tests excluded); strict per `pyproject.toml` (MM-11.6) |
| NFR-M3 | Test coverage | Unit + integration; coverage reported | 🟡 | `tests/unit`, `tests/integration` exist; expand coverage with each story |
| NFR-M4 | CI gate | Lint/type/test block merges | ✅ | GitHub Actions: ruff + pytest + mypy --strict all block merges (MM-11.4, MM-11.6) |
| NFR-M5 | Extensibility | New detector/sink = additive change | ✅ | `Detector` Protocol + registry; `AlertSink` ABC + router |
| NFR-M6 | Reproducible builds | Pinned dependency & image versions | 🟡 | Images pinned (MM-11.3); Python deps still lower-bound pins (`>=`) — exact pinning/lockfile is future work |

## 7. Data integrity

| ID | Requirement | Target | Status | Notes |
|----|-------------|--------|--------|-------|
| NFR-D1 | Valid metrics only | Strict schema before persist | ✅ | Pydantic strict mode |
| NFR-D2 | Idempotent ingest | Duplicate points don't corrupt aggregates | 🟡 | `ON CONFLICT DO NOTHING` set, but no unique constraint on `(time, metric_name, labels)` — re-seeding appends. Decide exactly-once vs at-least-once (PRD Open Q5) |
| NFR-D3 | Time-zone correctness | All timestamps tz-aware UTC | ✅ | `enable_utc`, tz-aware pandas conversion |
| NFR-D4 | Bounded in-memory state | Dedup seen-set evicts stale entries | ✅ | 2× cooldown eviction |

## 8. Compliance / operability targets (SLOs)

| SLO | Objective | Error budget |
|-----|-----------|--------------|
| Ingestion success | 99.9% of ingest requests return 2xx | 0.1% / 30 days |
| Detection coverage | 99% of active metrics evaluated each sweep | 1% / sweep |
| Alert delivery | 99% of routed alerts reach ≥1 sink | 1% (LogSink is the always-on floor) |
| API availability | 99.5% monthly | ~3.6h / 30 days |

> App-metrics exposition (MM-8.6 ✅) and alert persistence (MM-7.1 ✅) are now in place, so these SLOs
> are measurable from Prometheus (ingest/detection/anomaly/alert counters + per-detector latency
> histogram). Wiring Prometheus **alerting rules** on these series is the remaining step to make them
> enforceable.
