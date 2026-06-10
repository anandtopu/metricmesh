# MetricMesh — Product Documentation

This folder is the **single source of truth** for what MetricMesh is, who it serves, and what
developers must build to deliver a complete end-to-end solution. It is written by Product for
Engineering, Design, and QA.

## How to read these documents

Read them in this order:

| # | Document | Purpose | Primary audience |
|---|----------|---------|------------------|
| 1 | [PRD.md](./PRD.md) | The **why** and **what**: problem, goals, personas, scope, success metrics, functional requirements, risks, milestones. | Everyone |
| 2 | [USER_STORIES.md](./USER_STORIES.md) | The **buildable backlog**: epics → user stories with acceptance criteria, each tagged ✅ Built / 🟡 Partial / ⬜ Planned and mapped to code. | Engineering, QA |
| 3 | [NFRS.md](./NFRS.md) | The **how well**: performance, reliability, security, scalability, observability targets and SLAs. | Engineering, SRE |
| 4 | [ROADMAP.md](./ROADMAP.md) | The **when**: epics sequenced into releases (MVP → GA → scale). | Everyone |

## Status legend (used throughout)

- ✅ **Built** — implemented in the current codebase and verified working.
- 🟡 **Partial** — partially implemented; has known gaps or bugs called out in the story.
- ⬜ **Planned** — not yet built; required for the complete end-to-end solution.

## One-paragraph product summary

MetricMesh is a distributed time-series **anomaly detection platform**. Services and teams push
metrics to it (or it scrapes Prometheus). It stores them in TimescaleDB, runs five detection
algorithms (Z-score, IQR, STL, Prophet, Isolation Forest) on a schedule via a Celery worker pool,
deduplicates the resulting anomalies, and routes alerts to Slack, PagerDuty, webhooks, and logs.
Operators explore data in Grafana and monitor task health in Flower.

## Current implementation snapshot (as of this writing)

| Capability | Status |
|------------|--------|
| Metric ingestion (batch + single, strict validation) | ✅ Built |
| Prometheus scraper service | ✅ Built (MM-1.4) — opt-in `scraper` profile, `make scrape` |
| Configurable data retention | ✅ Built (MM-2.4) — `METRICS_RETENTION_DAYS` policy |
| TimescaleDB storage, continuous aggregate, compression | ✅ Built |
| Five detectors behind a common `Detector` Protocol | ✅ Built |
| Scheduled detection sweep (Celery Beat → chord) | ✅ Built |
| Alert deduplication + cooldown | ✅ Built (MM-5.1) |
| Shared, durable dedup across workers (Redis) | ✅ Built (MM-5.2) |
| Configurable dedup/consensus bucket width | ✅ Built (MM-5.3) — `DEDUP_BUCKET_SECONDS` (default 300); cooldown via `ALERT_COOLDOWN_SECONDS` |
| Multi-sink alert routing (Slack/PagerDuty/Webhook/Log) | ✅ Built |
| Email / MS Teams / generic webhook / SMS-text sinks | ✅ Built (MM-6.5) — opt-in sinks registered without touching the router (open/closed); SMS via Twilio |
| Dead-letter store for poison tasks | ✅ Built (MM-4.6) — retry-exhausted tasks captured in `dead_letters`; inspect via `GET /api/v1/dead-letters` |
| Audit trail (who/what/when) | ✅ Built (MM-9.5) — auth denials, feedback, alert routing in `audit_log`; inspect via `GET /api/v1/audit`; `AUDIT_ENABLED` |
| On-demand detection API | ✅ Built |
| **Alert history persistence** (`alerts` table) | ✅ Built (MM-7.1) — `route_alert` persists with `routed_to` |
| **Detector model persistence** (`detector_models` table) | ✅ Built (MM-10.1) — params/metadata upserted per fit; statistical models reusable (opt-in) |
| **Anomaly query/history API** | ✅ Built (MM-7.2) — `GET /api/v1/anomalies` filters + pagination |
| **Alert detail with explanation** | ✅ Built (MM-7.5) — `GET /api/v1/anomalies/{id}` returns resolved threshold + explanation + surrounding series window |
| **Metric series read API** | ✅ Built (MM-7.4) — `GET /api/v1/metrics/{name}/series` raw or 1m |
| **Authentication (API key)** | ✅ Built (MM-9.1) — `X-API-Key` on data endpoints, opt-in via `API_KEYS` |
| **CORS lockdown** | ✅ Built (MM-9.2) — configurable allow-list, no `*` |
| **Pinned image versions** | ✅ Built (MM-11.3) — TimescaleDB/Grafana/Prometheus pinned |
| **Kubernetes deployment** | ✅ Built (MM-11.5) — Helm chart (`deploy/helm/metricmesh`): API + per-queue workers + Beat + Flower, probes, HPA, externalized secrets; bundled DB/Redis toggleable |
| **CI pipeline** | ✅ Built (MM-11.4) — ruff + pytest + **mypy --strict** all block merges (mypy gate added in MM-11.6) |
| **Secret-store integration** | ✅ Built (MM-9.4) — `secrets_dir` for Docker/K8s secrets |
| **Per-metric alert routing rules** | ✅ Built (MM-6.6) — glob → sinks, first match wins |
| **Ingestion rate limiting** | ✅ Built (MM-1.5) — Redis fixed-window, 429 + fail-open |
| **Feedback labeling + precision** | ✅ Built (MM-10.3) — `POST .../feedback`, `GET .../stats` |
| **Detector A/B report** | ✅ Built (MM-10.4) — `GET .../report` per-metric precision + comparative recall, recommends a detector |
| **Ensemble / consensus scoring** | ✅ Built (MM-10.5) — `CONSENSUS_MIN_DETECTORS` (opt-in), N detectors must agree |
| **Per-metric threshold overrides** | ✅ Built (MM-10.2) — `METRIC_THRESHOLDS` glob → per-detector threshold |
| **Absolute scoring mode** | ✅ Built (MM-3.6) — `SCORING_MODE=absolute` for stable cross-window thresholds |
| **Multi-tenancy / per-tenant isolation** | ✅ Built (MM-9.3) — `TENANT_API_KEYS` map keys→tenants; metrics tagged on ingest, all reads + the detection pipeline scoped per tenant (alerts/models isolated) |
| **Readiness check includes Redis** | ✅ Built (MM-8.2) — `/readiness` pings DB + Redis, 503 on degraded |
| Pre-provisioned Grafana dashboards | ✅ Built (MM-8.5) — datasource + overview dashboard auto-loaded |
| **Prometheus app/business metrics** | ✅ Built (MM-8.6) — `GET /metrics` exposes ingest/detection/anomaly/alert counters + per-detector latency histogram + live Celery queue depth; aggregated across API + worker processes |

See [USER_STORIES.md](./USER_STORIES.md) for the per-story breakdown.
