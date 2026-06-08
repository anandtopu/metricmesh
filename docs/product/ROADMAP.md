# MetricMesh — Delivery Roadmap

This roadmap sequences the epics and stories from [USER_STORIES.md](./USER_STORIES.md) into
releases. It is outcome-oriented: each milestone has a theme, the stories it delivers, and explicit
exit criteria. Estimates are relative (story points), not calendar commitments.

**Status legend:** ✅ Built · 🟡 Partial · ⬜ Planned

---

## Milestone map

```
M0 Foundation ──▶ M1 MVP ──▶ M2 Trustworthy ──▶ M3 Tunable ──▶ M4 Scale
  (done)          history     security/dedup     feedback/      multi-tenant
                  + reliability                   model store    + K8s
```

---

## M0 — Foundation ✅ (complete)

**Theme:** Prove the end-to-end happy path: ingest → store → detect → dedup → alert.

**Delivered**
- MM-1.1, MM-1.2, MM-1.3 — Ingestion + strict validation
- MM-2.1, MM-2.2, MM-2.3, MM-2.5 — Storage, continuous aggregate, compression, series fetch
- MM-3.1–MM-3.5 — Detector contract + 5 detectors + registry
- MM-4.1–MM-4.5 — Sweep, chord fan-out, queue isolation, retries, on-demand detection
- MM-5.1 — Dedup with cooldown
- MM-6.1–MM-6.4 — Multi-sink routing (Slack/PagerDuty/Webhook/Log)
- MM-7.3 — List active metrics
- MM-8.1, MM-8.3, MM-8.4 — Liveness, Flower, structured logs
- MM-11.1, MM-11.2 — One-command stack, env config

**Exit criteria (met)**
- `make up` brings up the full stack; `make seed` ingests synthetic data.
- Beat sweep dispatches detectors; anomalies route to the Log sink (and Slack/PagerDuty if
  configured).

---

## M1 — MVP: History & Reliability ✅ (complete)

**Theme:** Make anomalies durable, queryable, and the platform observably healthy. This is the
release that turns a demo into a usable product.

**Scope**

| Story | Title | Pts | Priority |
|-------|-------|-----|----------|
| MM-7.1 | Persist routed alerts to `alerts` table | 3 | P0 |
| MM-7.2 | Query anomalies/alerts API (filters + pagination) | 5 | P0 |
| MM-7.4 | Read raw/aggregated series API | 3 | P1 |
| MM-8.2 | Readiness probe checks DB **and** Redis | 2 | P0 |
| MM-8.5 | Provision Grafana datasource + dashboards | 3 | P1 |
| MM-1.4 | Wire Prometheus scraper as a managed service | 5 | P1 |
| MM-2.4 | Configurable retention policy | 3 | P1 |

**Exit criteria**
- Every routed alert is written to `alerts` and retrievable via the query API.
- `/readiness` reflects true DB + Redis health.
- Operators see ingest rate, anomalies-over-time, and alert volume in Grafana out of the box.
- Documented retention bounds storage growth.

**Dependencies:** MM-7.2/7.4 depend on MM-7.1.

---

## M2 — Trustworthy: Security & Dedup Hardening ✅ (complete)

**Theme:** Safe to run beyond localhost; no duplicate alerts when scaled out; reproducible builds.

**Scope**

| Story | Title | Pts | Priority |
|-------|-------|-----|----------|
| MM-9.1 | API authentication (API key/JWT) | 5 | P0 |
| MM-9.2 | Lock down CORS to allow-list | 1 | P0 |
| MM-9.4 | Secrets management integration | 2 | P1 |
| MM-5.2 | Shared Redis-backed dedup state | 5 | P1 |
| MM-6.6 | Per-tenant/per-metric routing rules | 5 | P1 |
| MM-11.3 | Pin image versions | 1 | P1 |
| MM-11.4 | CI: lint + type-check + test gate | 3 | P1 |
| MM-1.5 | Ingestion rate limiting / backpressure | 5 | P2 |

**Exit criteria**
- No unauthenticated access to ingest/query endpoints; CORS restricted.
- Dedup is correct across multiple `alerts` workers and survives restarts.
- CI blocks merges on lint/type/test failures; images are pinned.

**Dependencies:** MM-6.6 routing rules pair with tenancy groundwork in M4 but can ship metric-prefix
rules first.

---

## M3 — Tunable: Detector Lifecycle & Feedback ✅

**Theme:** Measure and improve detection quality. Unlocks the precision/recall KPIs in the PRD.

**Scope**

| Story | Title | Pts | Priority |
|-------|-------|-----|----------|
| MM-10.3 | Anomaly feedback labeling (true/false positive) | 5 | P0 |
| MM-10.1 | Persist trained detector models/params | 5 | P1 |
| MM-10.2 | Per-metric threshold overrides | 3 | P1 |
| MM-3.6 | Absolute scoring mode (stable thresholds) | 3 | P1 |
| MM-7.5 | Alert detail with explanation | 3 | P2 ✅ |
| MM-8.6 | App/business metrics exposition (Prometheus) | 3 | P2 ✅ |
| MM-10.4 | Detector precision reporting / A-B | 5 | P2 ✅ |
| MM-10.5 | Ensemble/consensus scoring | 5 | P2 |

**Exit criteria** — all met ✅
- On-call can label alerts; per-detector precision (MM-10.3) and a per-metric detector A/B report with
  precision + comparative recall (MM-10.4) are computed. (True recall needs ground-truth missed
  anomalies — documented, not faked.)
- Thresholds can be tuned per metric (MM-10.2); an absolute-score mode exists (MM-3.6). ✅
- App metrics are scrapable (MM-8.6), making the PRD SLOs measurable. ✅
- Alert detail/traceability shipped (MM-7.5). ✅

**Dependencies:** MM-10.3 and MM-10.4 depend on MM-7.1 (persistence). MM-8.6 enables SLO tracking.

---

## M4 — Scale: Multi-tenancy & Orchestration ⬜

**Theme:** Run MetricMesh as shared infrastructure for many teams, on Kubernetes, meeting SLOs under
load.

**Scope**

| Story | Title | Pts | Priority |
|-------|-------|-----|----------|
| MM-9.3 | Per-tenant API keys & data isolation | 8 | P1 |
| MM-9.5 | Audit logging | 3 | P2 |
| MM-11.5 | Kubernetes deployment (Helm, HPA, probes) | 8 | P2 |
| MM-4.6 | Dead-letter handling for poison tasks | 3 | P2 |

**Exit criteria**
- Tenants are isolated end-to-end (ingest → storage → query → routing).
- Horizontal autoscaling meets NFR latency/availability targets under load tests.
- Failed tasks are inspectable, not lost.

**Dependencies:** Tenancy (MM-9.3) builds on auth (MM-9.1) and routing rules (MM-6.6).

---

## Release decision checklist (per milestone)

Before declaring a milestone shipped, confirm:

- [ ] All P0 stories meet acceptance criteria with tests.
- [ ] NFRs tagged for this milestone are ✅ (see [NFRS.md](./NFRS.md)).
- [ ] `ruff`, `mypy --strict`, `pytest` green in CI.
- [ ] Status tags in [USER_STORIES.md](./USER_STORIES.md) and the snapshot table in
      [README.md](./README.md) updated.
- [ ] No new `latest` image tags or unauthenticated endpoints for non-local targets.
- [ ] Rollback path documented for any schema migration.

## Sequencing rationale (why this order)

1. **History first (M1).** Almost everything valuable — investigation, feedback, precision metrics,
   dashboards — depends on persisting alerts (MM-7.1). It's small and unblocks the most.
2. **Security before exposure (M2).** The platform must not leave localhost while the API is open and
   CORS is `*`. Dedup hardening pairs here because scaling out is what exposes the in-process gap.
3. **Quality loop (M3).** With history + auth in place, invest in measuring and improving precision —
   the core product promise (low-noise, accurate alerts).
4. **Scale last (M4).** Multi-tenancy and K8s are highest-cost and only matter once the single-tenant
   product is trustworthy and tunable.
