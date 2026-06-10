# MetricMesh — Product Requirements Document (PRD)

| | |
|---|---|
| **Product** | MetricMesh — Time-Series Anomaly Detection Platform |
| **Version** | 1.0 (living document) |
| **Status** | Draft for engineering build-out |
| **Owner** | Product Management |
| **Last updated** | 2026-06-03 |

---

## 1. Problem statement

Modern systems emit thousands of metrics (latency, error rate, CPU, memory, business KPIs). Teams
discover problems too late because:

- **Static thresholds don't scale.** Hand-tuned alert rules (`cpu > 80%`) are brittle, ignore
  seasonality, and rot as systems change. Every new metric needs a human to define "normal."
- **Seasonality and trends fool simple rules.** Traffic that is normal at 9am is anomalous at 3am.
  A single threshold can't express that.
- **Alert fatigue is real.** The same spike fires hundreds of duplicate pages, so on-call engineers
  start ignoring alerts.
- **No single place to ask "is this metric behaving abnormally right now?"** across heterogeneous
  detection techniques.

**MetricMesh** solves this by automatically learning each metric's normal behavior with multiple
complementary detection algorithms, scoring anomalies in real time, deduplicating noise, and routing
high-signal alerts to the channels teams already use.

## 2. Vision

> Any team can point a metric stream at MetricMesh and, with zero per-metric tuning, get
> accurate, low-noise anomaly alerts that respect seasonality and trend — backed by a queryable
> history and explainable detector scores.

## 3. Goals & non-goals

### 3.1 Goals (what we are building)

- **G1** — Ingest time-series metrics at high throughput via HTTP and Prometheus scraping.
- **G2** — Persist metrics durably with automatic downsampling (continuous aggregates) and
  compression for cost-efficient retention.
- **G3** — Detect anomalies using an extensible set of algorithms (statistical + ML) behind one
  common interface, requiring no per-metric configuration to start.
- **G4** — Run detection continuously and on-demand, at scale, without a slow detector blocking a
  fast one.
- **G5** — Suppress duplicate/noisy alerts and route the rest to multiple destinations
  (Slack, PagerDuty, webhooks, logs) with severity.
- **G6** — Persist a queryable **history of anomalies and alerts** for investigation and tuning.
- **G7** — Be operable: health/readiness probes, task monitoring, dashboards, structured logs.
- **G8** — Be secure and multi-tenant: authenticated ingestion/query, isolation between teams.

### 3.2 Non-goals (explicitly out of scope for now)

- **N1** — Not a general-purpose metrics store competing with Prometheus/Datadog for arbitrary
  PromQL querying. MetricMesh stores what it needs to detect anomalies.
- **N2** — Not a full incident-management platform. We *route* to PagerDuty; we don't replace it.
- **N3** — Not a log or trace analytics product. Metrics (numeric time series) only.
- **N4** — Not a forecasting/capacity-planning product, though detectors use forecasting internally.
- **N5** — No custom front-end dashboard in the first releases; we lean on Grafana.

## 4. Personas

| Persona | Role | Goals | Pain today | How MetricMesh helps |
|---------|------|-------|------------|----------------------|
| **Priya — SRE / On-call** | Keeps services healthy | Get paged only for real, actionable anomalies | Drowning in duplicate, threshold-based pages | Deduplicated, seasonality-aware alerts with severity routed to PagerDuty/Slack |
| **Devansh — Backend Engineer** | Ships a service that emits metrics | Push metrics easily; know when his service misbehaves | Has to define and maintain alert rules manually | Simple ingest API; detectors learn normal automatically |
| **Mei — Data/ML Engineer** | Owns detection quality | Add/tune detection algorithms; measure precision/recall | No common interface; no labeled feedback loop | `Detector` Protocol + registry; (planned) feedback labeling + model store |
| **Carlos — Platform/DevOps** | Runs MetricMesh itself | Deploy, scale, monitor the platform | Opaque background processing | Docker Compose stack, Flower, health probes, structured logs |
| **Ana — Engineering Manager** | Cares about reliability outcomes | See anomaly trends, MTTA/MTTR impact | No historical view of anomalies | (Planned) anomaly history API + Grafana dashboards |

## 5. Success metrics (KPIs)

| Metric | Definition | Target |
|--------|------------|--------|
| **Detection coverage** | % of active metrics evaluated each sweep | ≥ 99% |
| **Alert precision** | % of routed alerts marked useful (requires feedback loop) | ≥ 70% at GA |
| **Noise reduction** | Duplicate alerts suppressed ÷ raw anomalies | ≥ 80% |
| **Ingestion success rate** | 2xx ingest responses ÷ total | ≥ 99.9% |
| **Detection latency** | Time from data point to alert (scheduled path) | ≤ 90s p95 |
| **On-demand detection latency** | `POST /anomalies/detect` response time | ≤ 2s p95 (statistical) |
| **Platform availability** | API + worker uptime | ≥ 99.5% |
| **Time-to-onboard a metric** | From first ingest to first detection | ≤ 1 sweep (60s), zero config |

> The **precision** KPI is now measurable via the feedback loop (MM-10.3 ✅): label alerts at
> `POST /api/v1/anomalies/{id}/feedback` and read precision per detector at
> `GET /api/v1/anomalies/stats`. True **recall** still needs a ground-truth set of *missed* anomalies
> (not derivable from alert feedback); track noise reduction and coverage alongside precision.

## 6. Scope & system context

### 6.1 High-level flow

```
Producers ──HTTP──▶  Ingest API ──▶ TimescaleDB ◀── Prometheus scraper
                                        │
                         Celery Beat (every 60s) ── sweep
                                        │
                 ┌──────────── chord(group(detector tasks)) ───────────┐
                 ▼                ▼                  ▼                  ▼
            Statistical        Prophet        Isolation Forest    (future detectors)
            (zscore/iqr/stl)  (forecast)      (multivariate)
                 └──────────────── aggregate_and_alert ───────────────┘
                                        │ dedup
                                        ▼
                              route_alert (per anomaly)
                                        │
                    ┌──────────┬────────┴────────┬──────────┐
                    ▼          ▼                 ▼          ▼
                  Slack     PagerDuty          Webhook     Log
                                        │
                              (Planned) persist to alerts table → Anomaly History API
```

### 6.2 In scope

Ingestion, storage/retention, detection engine, scheduling/async processing, dedup, alert routing,
anomaly/alert history, query API, observability, configuration/deployment, security & multi-tenancy,
detector lifecycle & feedback.

### 6.3 Component → capability map (current code)

| Component | Path | Capability |
|-----------|------|-----------|
| Ingest API | `ingestion/api.py`, `ingestion/validators.py` | G1 |
| Prometheus scraper | `ingestion/scraper.py` | G1 |
| Storage | `storage/timescale.py`, `storage/migrations/001_initial.sql` | G2, G6 |
| Detectors | `detection/*.py` | G3 |
| Workers / scheduling | `workers/celery_app.py`, `workers/tasks.py` | G4 |
| Dedup | `alerting/dedup.py` | G5 |
| Alert router/sinks | `alerting/router.py` | G5 |
| Query/health API | `api/routes/anomalies.py`, `api/routes/health.py` | G6, G7 |
| Config | `config.py` | all |
| Deployment | `docker-compose.yml`, `Dockerfile`, `Makefile` | G7 |

## 7. Functional requirements (summary)

Full, testable detail lives in [USER_STORIES.md](./USER_STORIES.md). Summary by capability:

- **FR-1 Ingestion** — Accept validated metric points (batch ≤ 10,000, single), reject malformed
  input with actionable errors, bulk-insert efficiently, support Prometheus pull.
- **FR-2 Storage** — Hypertable with `(metric_name, time)` indexing, 1-minute continuous aggregate,
  compression policy, configurable retention.
- **FR-3 Detection** — Five algorithms behind a `Detector` Protocol; normalized `[0,1]` scores;
  registry-based extensibility; train/test split per run.
- **FR-4 Scheduling** — 60s sweep over active metrics; parallel fan-out via Celery chord; per-queue
  isolation (fast/slow/alerts); retries with backoff + jitter; time limits.
- **FR-5 Dedup** — Fingerprint by `(metric, detector, 5-min bucket)`; cooldown window; thread-safe;
  bounded memory.
- **FR-6 Alerting** — Fan-out to Slack/PagerDuty/Webhook/Log/Teams/Email/SMS; severity from score;
  partial-failure handling; always-on log fallback.
- **FR-7 History & Query** — Persist anomalies/alerts; query anomalies by metric/time/detector;
  list active metrics; on-demand detection.
- **FR-8 Observability** — `/health`, `/readiness` (DB **and** Redis), Flower, Grafana dashboards,
  structured JSON logs, app metrics.
- **FR-9 Security & Tenancy** — Authenticated API, per-tenant API keys, data isolation, secrets
  management, CORS lockdown.
- **FR-10 Detector lifecycle** — Persist trained models/params; per-metric threshold overrides;
  feedback labeling (true/false positive) to measure and improve precision.

## 8. Assumptions

- Producers can send metrics over HTTP or expose a Prometheus endpoint.
- Metric values are numeric and finite; names follow `^[a-z_][a-z0-9_.]*$`.
- A 1-minute resolution is sufficient for detection (continuous aggregate bucket).
- Redis is available as the Celery broker/result backend; TimescaleDB as the store.
- Deployment target supports Docker Compose for MVP; Kubernetes is a later concern.

## 9. Dependencies

- **TimescaleDB** (PG16) — hypertables, continuous aggregates, compression/columnstore.
- **Redis** — Celery broker (db 0) and result backend (db 1).
- **Celery + Beat + Flower** — async execution, scheduling, monitoring.
- **Prophet / scikit-learn / statsmodels / pandas / numpy** — detection.
- **FastAPI / Pydantic v2 / SQLAlchemy 2 async / asyncpg** — API & data access.
- **Grafana / Prometheus** — visualization & platform metrics.

## 10. Risks & mitigations

| Risk | Impact | Likelihood | Mitigation |
|------|--------|-----------|------------|
| **Per-batch score normalization makes thresholds relative** (z-score ÷ batch max) | Detector behavior changes with lookback window; thresholds not absolute | High | Document clearly; consider absolute scoring option; surface in tuning UI (Epic 10) |
| Prophet tasks are slow and CPU-heavy | Slow queue saturation; delayed alerts | Medium | Dedicated `slow` queue, `prefetch=1`, time limits, min-data guard (≥20 rows) |
| No alert persistence today | Can't investigate or measure precision | High | Epic 7: write to `alerts` table in `route_alert` |
| Open API (no auth, CORS `*`) | Data tampering, exfiltration | High | Epic 9 before any non-local deployment |
| Dedup state is in-process per worker | Duplicate alerts across workers/restarts | Medium | Move fingerprint store to Redis (shared, TTL-based) |
| Scraper not wired to a service | Prometheus pull unused | Low | Add a `scraper` compose service or document opt-in |
| `latest` image tags | Non-reproducible builds | Medium | Pin TimescaleDB/Grafana/Prometheus image versions |

## 11. Milestones (see ROADMAP for detail)

| Milestone | Theme | Exit criteria |
|-----------|-------|---------------|
| **M0 — Foundation** (done) | Ingest → store → detect → alert happy path | `make up && make seed` works; scheduled alerts fire |
| **M1 — MVP** | History + reliability | Alerts persisted & queryable; readiness checks Redis; dashboards provisioned |
| **M2 — Trustworthy** | Security + dedup hardening | AuthN/Z; shared dedup; CORS locked; pinned images |
| **M3 — Tunable** | Detector lifecycle + feedback | Model store; per-metric thresholds; feedback labeling; precision KPI live |
| **M4 — Scale** | Multi-tenancy + K8s | API keys & isolation; horizontal scaling; SLOs met under load |

## 12. Open questions

1. Should anomaly scores have an **absolute** mode (not per-batch normalized) for stable thresholds?
2. Retention policy target (e.g., raw 30 days, 1-min aggregate 1 year)? Needs cost input.
3. Is Grafana the long-term UI, or do we build a dedicated anomaly console?
4. Tenancy model: API-key-per-team vs. full org/RBAC?
5. Do we need exactly-once ingestion (dedupe on `(time, metric_name, labels)`) or is at-least-once OK?
