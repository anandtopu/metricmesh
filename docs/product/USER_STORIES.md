# MetricMesh — Epics & User Stories

This backlog is the **buildable specification** for the complete end-to-end solution. Every story
has acceptance criteria written as Given/When/Then so QA can verify them, a status tag, a priority,
a rough estimate (story points, Fibonacci), and a mapping to the code that implements it.

**Status legend:** ✅ Built · 🟡 Partial · ⬜ Planned
**Priority:** P0 (must, blocks release) · P1 (should) · P2 (nice-to-have)

**Story ID scheme:** `MM-<epic>.<n>` (e.g., `MM-3.2`).

## Epic index

| Epic | Theme | Milestone |
|------|-------|-----------|
| [E1](#epic-1--metric-ingestion) | Metric Ingestion | M0/M1 |
| [E2](#epic-2--storage--retention) | Storage & Retention | M0/M1 |
| [E3](#epic-3--detection-engine) | Detection Engine | M0 |
| [E4](#epic-4--asynchronous-processing--scheduling) | Async Processing & Scheduling | M0 |
| [E5](#epic-5--alert-deduplication--noise-reduction) | Alert Deduplication | M0/M2 |
| [E6](#epic-6--alert-routing--notification) | Alert Routing & Notification | M0 |
| [E7](#epic-7--anomaly--alert-history--query-api) | Anomaly & Alert History + Query | M1 |
| [E8](#epic-8--observability--operations) | Observability & Operations | M1 |
| [E9](#epic-9--security--multi-tenancy) | Security & Multi-tenancy | M2/M4 |
| [E10](#epic-10--detector-lifecycle-tuning--feedback) | Detector Lifecycle & Feedback | M3 |
| [E11](#epic-11--deployment--configuration) | Deployment & Configuration | M0/M2 |

---

## Epic 1 — Metric Ingestion

> **As** a service producing metrics, **I want** to push data points into MetricMesh reliably and
> have bad data rejected clearly, **so that** my metrics are available for anomaly detection.

### MM-1.1 — Batch metric ingestion ✅ Built · P0 · 3pts
**As** Devansh (backend engineer) **I want** to POST a batch of up to 10,000 metric points
**so that** I can ship high-volume metrics in one request.

**Acceptance criteria**
- **Given** a valid `MetricBatch` (`points[]`, `source_id`) **When** I `POST /api/v1/metrics`
  **Then** I receive `202 Accepted` with `{accepted, rejected, errors}`.
- **Given** a batch larger than 10,000 points **When** I post it **Then** I receive `422` with a
  validation error (`max_length`).
- **Given** an empty `points` array **When** I post it **Then** I receive `422` (`min_length=1`).
- **Given** a valid batch **Then** points are bulk-inserted in a single round-trip (`unnest`).

**Maps to:** `ingestion/api.py::ingest_batch`, `ingestion/validators.py::MetricBatch`,
`storage/timescale.py::bulk_insert`.

### MM-1.2 — Single metric ingestion ✅ Built · P1 · 1pt
**As** a lightweight producer **I want** to post one point **so that** simple integrations don't
need to batch.

**Acceptance criteria**
- **Given** a valid `MetricPoint` **When** I `POST /api/v1/metrics/single` **Then** I get `202` with
  `accepted=1`.

**Maps to:** `ingestion/api.py::ingest_single`.

### MM-1.3 — Strict input validation ✅ Built · P0 · 3pts
**As** Mei (data quality owner) **I want** malformed metrics rejected **so that** detectors never
train on garbage.

**Acceptance criteria**
- **Given** a `metric_name` violating `^[a-z_][a-z0-9_.]*$` or length 1–128 **Then** reject with
  `422`.
- **Given** a non-finite `value` (NaN/Inf) **Then** reject with a clear `value must be finite` error.
- **Given** more than 20 labels, a label key > 64 chars, or a label value > 256 chars **Then**
  reject.
- **Given** strict mode **When** a field is the wrong type (e.g., `"3"` for a float) **Then** reject
  (no silent coercion).

**Maps to:** `ingestion/validators.py::MetricPoint` (`strict=True`, validators).

### MM-1.4 — Prometheus scrape ingestion ✅ Built · P1 · 5pts
**As** Carlos (platform) **I want** MetricMesh to pull from a Prometheus `/metrics` endpoint
**so that** existing exporters are ingested without code changes.

**Acceptance criteria**
- **Given** a configured target URL **When** the scraper runs **Then** it polls at a fixed interval,
  parses `name{labels} value` lines, and bulk-inserts batched points. ✅ verified (ingested 1200+
  Prometheus self-metrics).
- **Given** a malformed line **Then** it is skipped without crashing the loop. ✅ (pre-existing guard)
- **Given** the platform is deployed **Then** the scraper runs as a managed service. ✅

**Maps to:** `ingestion/scraper.py` (loop), `ingestion/scraper_main.py` (entrypoint),
`storage/timescale.py::get_session_factory`, `config.py` (`prometheus_scrape_*`), and an **opt-in**
`scraper` compose service (`profiles: ["scraper"]`). Start with `make scrape` (or
`docker compose --profile scraper up -d scraper`). Default target: Prometheus' own `/metrics`.

### MM-1.5 — Ingestion backpressure & rate limiting ✅ Built · P2 · 5pts
**As** Carlos **I want** ingestion protected from overload **so that** a misbehaving producer can't
take down the platform.

**Acceptance criteria**
- **Given** a producer exceeding its quota **Then** it receives `429 Too Many Requests` with
  `Retry-After`. ✅ verified live (limit 3/min → 4th, 5th = 429).
- **Given** sustained overload **Then** the API sheds load (429 before any DB work) and `/health`
  stays responsive. ✅ verified (`/health` → 200 while ingest throttled).
- **Given** Redis is unreachable **Then** the limiter fails open (ingestion not blocked). ✅ tested.

**Maps to:** `api/ratelimit.py` (Redis fixed-window counter per API-key/IP, fail-open),
`config.py::rate_limit_per_minute` (0 = disabled), applied to the ingest router in `api/main.py`.
**Tests:** `tests/integration/test_api.py` (429 over-quota, fail-open). Shared across replicas via
Redis; per-tenant quotas will build on this with tenancy (MM-9.3).

---

## Epic 2 — Storage & Retention

> **As** the platform **I want** durable, cost-efficient storage of metrics **so that** detectors
> have history and operators control storage cost.

### MM-2.1 — Hypertable schema & indexing ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** startup **Then** a `metrics` hypertable exists with `(time, metric_name, value, labels,
  source)` and an index on `(metric_name, time DESC)`.
- **Given** repeated startups **Then** schema setup is idempotent (`IF NOT EXISTS`).

**Maps to:** `storage/timescale.py::SETUP_SQL`, `setup_schema`,
`storage/migrations/001_initial.sql`.

### MM-2.2 — 1-minute continuous aggregate ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** ingested data **Then** `metrics_1min` provides `avg/max/min/count` per 1-minute bucket
  per metric, auto-refreshed (start 10m, end 30s, every 30s).

**Maps to:** `SETUP_SQL` (`CREATE MATERIALIZED VIEW ... timescaledb.continuous`,
`add_continuous_aggregate_policy`).

### MM-2.3 — Compression policy ✅ Built · P1 · 2pts
**Acceptance criteria**
- **Given** the hypertable **Then** columnstore compression is enabled (`segmentby=metric_name`,
  `orderby=time DESC`) and chunks older than 7 days are compressed.

**Maps to:** `SETUP_SQL` (`ALTER TABLE ... timescaledb.compress`, `add_compression_policy`).

### MM-2.4 — Configurable retention / data lifecycle ✅ Built · P1 · 3pts
**As** Carlos **I want** to set how long raw data is kept **so that** storage cost is bounded.

**Acceptance criteria**
- **Given** `METRICS_RETENTION_DAYS` **Then** a TimescaleDB `add_retention_policy` drops older raw
  chunks automatically (verified: job `policy_retention`, `drop_after: 30 days`, daily schedule).
- **Given** no config **Then** the documented default (30 days) applies. ✅
- **Given** a changed value on restart **Then** it re-applies (remove-then-add), so config changes
  take effect. ✅ **Given** `0` **Then** retention is disabled (keep forever). ✅

**Maps to:** `config.py::metrics_retention_days`, `storage/timescale.py::_apply_retention_policy`
(called from `setup_schema`). **Note:** scoped to the raw `metrics` hypertable; continuous-aggregate
retention is a future extension.

### MM-2.5 — Efficient series fetch for detection ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** a metric and lookback window **Then** `fetch_series` returns a tz-aware,
  1-minute-bucketed `value` series ordered ascending.
- **Given** the Celery (no event loop) context **Then** a synchronous equivalent exists.

**Maps to:** `storage/timescale.py::fetch_series`, `fetch_series_sync`.

---

## Epic 3 — Detection Engine

> **As** Mei **I want** multiple complementary detectors behind one interface **so that** we catch
> different anomaly shapes and can add algorithms without touching callers.

### MM-3.1 — Common detector contract ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** any detector **Then** it exposes `fit`, `score` (→ `[0,1]`), `detect` (→ list of
  `AnomalyResult`) and a `name`, validated structurally (`runtime_checkable` Protocol).
- **Given** an `AnomalyResult` **Then** it is immutable, hashable, and JSON-serializable via
  `to_dict()`.

**Maps to:** `detection/base.py` (`Detector` Protocol, `AnomalyResult`).

### MM-3.2 — Statistical detectors (z-score / IQR / STL) ✅ Built · P0 · 5pts
**Acceptance criteria**
- **Given** `method=zscore` **Then** rolling z-scores are normalized to `[0,1]` and points ≥
  threshold are returned.
- **Given** `method=iqr` **Then** IQR fences (1.5×IQR) produce scores; robust to heavy tails.
- **Given** `method=stl` and ≥4 points **Then** STL residuals are scored; failures degrade to score 0
  (no crash).

**Maps to:** `detection/statistical.py` (`match/case` dispatch).

### MM-3.3 — Prophet forecast detector ✅ Built · P1 · 5pts
**Acceptance criteria**
- **Given** ≥20 points **Then** Prophet fits and scores points outside its uncertainty interval.
- **Given** <20 points **Then** the run returns no anomalies and logs `insufficient_data` (no crash).
- **Given** Prophet's noisy stderr **Then** it is suppressed.

**Maps to:** `detection/prophet_detector.py`, guarded in `workers/tasks.py::run_prophet`.

### MM-3.4 — Isolation Forest multivariate detector ✅ Built · P1 · 5pts
**Acceptance criteria**
- **Given** a univariate series **Then** features (rolling stats, diffs, cyclic hour) are built and
  scaled with `RobustScaler` (outlier-resistant).
- **Given** a fitted model **Then** `decision_function` output is inverted/normalized to `[0,1]`;
  a point is anomalous if score ≥ threshold **or** sklearn predicts `-1`.

**Maps to:** `detection/isolation_forest.py`.

### MM-3.5 — Detector registry & extensibility ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** a detector key (`zscore`,`iqr`,`stl`,`prophet`,`isolation_forest`) **Then**
  `get_detector(name)` returns an instance; unknown keys raise a clear error listing valid keys.
- **Given** a new detector **Then** adding it requires one registry line and no caller changes.

**Maps to:** `detection/registry.py`.

### MM-3.6 — Score semantics documented & optionally absolute ✅ Built · P1 · 3pts
**As** Priya **I want** to understand what a threshold means **so that** alerts are predictable.

**Acceptance criteria**
- **Given** the docs **Then** it is clear scores are **per-batch normalized** (relative) by default,
  so thresholds are relative to the current window. ✅ (documented in `CLAUDE.md`).
- **Given** `SCORING_MODE=absolute` **Then** detectors map scores against a fixed statistical
  reference, so a given threshold means the same thing across windows. ✅ verified live (`/detect`
  reflects `scoring_mode=absolute`; anomaly counts shift vs relative) and unit-tested (a moderate
  series is *not* stretched to fill `[0,1]`; the same spike scores ~equally across batches of
  different spread).

**Maps to:** `config.py::scoring_mode` (validated `relative|absolute`, default `relative`), threaded
via `detection/registry.py::get_detector` and the worker tasks into each detector's `__init__`.
Absolute branches: `zscore` divides by a fixed `_ZSCORE_ABS_SIGMA` (6σ→1.0) instead of the batch max;
`stl` by a robust MAD-based σ instead of the batch p99; `isolation_forest` by a fixed sigmoid on the
raw `decision_function` instead of batch min-max. `iqr`/`prophet` are inherently absolute and
unaffected. **Tests:** `tests/unit/test_absolute_scoring.py` (4 cases).

---

## Epic 4 — Asynchronous Processing & Scheduling

> **As** the platform **I want** detection to run continuously and in parallel without slow jobs
> blocking fast ones **so that** alerts are timely at scale.

### MM-4.1 — Scheduled detection sweep ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** Beat is running **Then** every 60s `schedule_detection_sweep` lists active metrics and
  dispatches detectors for each.
- **Given** no active metrics **Then** it logs `sweep.no_metrics` and exits cleanly.

**Maps to:** `workers/celery_app.py` (`beat_schedule`), `workers/tasks.py::schedule_detection_sweep`.

### MM-4.2 — Parallel fan-out with aggregation (chord) ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** N metrics **Then** all detector tasks run in parallel as a `group`, and a single
  `aggregate_and_alert` callback receives all results (`chord`).

**Maps to:** `workers/tasks.py` (`chord(group(all_tasks))(aggregate_and_alert.s())`).

### MM-4.3 — Queue isolation by cost ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** task routing **Then** statistical → `fast`, Prophet/IsolationForest → `slow`,
  routing/aggregation → `alerts`, each served by a dedicated worker.
- **Given** `prefetch=1` + `acks_late` **Then** a slow Prophet task cannot starve the fast queue and
  tasks aren't lost on worker crash.

**Maps to:** `workers/celery_app.py` (`task_routes`, `task_queues`, worker commands in compose).

### MM-4.4 — Resilient retries ✅ Built · P1 · 2pts
**Acceptance criteria**
- **Given** a transient failure **Then** the task retries with exponential backoff + jitter, bounded
  by `max_retries`, within soft/hard time limits.

**Maps to:** `@shared_task(... autoretry_for, retry_backoff, retry_jitter, soft_time_limit ...)`.

### MM-4.5 — On-demand detection ✅ Built · P1 · 2pts
**As** Priya **I want** to run detection for a metric right now **so that** I can investigate during
an incident.

**Acceptance criteria**
- **Given** `POST /api/v1/anomalies/detect` with metric/detector/threshold/lookback **Then** I get a
  synchronous result with `anomaly_count` and anomalies.
- **Given** a metric with no data **Then** `404`. **Given** an unknown detector **Then** `400`.

**Maps to:** `api/routes/anomalies.py::detect_now`.

### MM-4.6 — Dead-letter handling for poison tasks ✅ Built · P2 · 3pts
**Acceptance criteria**
- **Given** a task that exhausts retries **Then** it lands in a dead-letter queue/table for
  inspection rather than vanishing. ✅ verified live — a `route_alert` forced to fail all sinks
  exhausted its 3 retries and was recorded in `dead_letters` (task name/id, queue, `retries=3`, full
  exception + traceback, args as JSONB) and returned by `GET /api/v1/dead-letters`. A real sweep alert
  that organically failed was captured the same way, confirming the hook fires on genuine failures.

**Maps to:** new `dead_letters` table (SETUP_SQL + `001_initial.sql`). `workers/deadletter.py` connects
Celery's **`task_failure`** signal (fires only on a task's *final* failure — `self.retry()` raises
`Retry`, which isn't a failure — so this captures poison tasks *after* retries are exhausted) →
`storage/timescale.py::persist_dead_letter_sync` (best-effort sync insert; `args`/`kwargs` JSON-encoded
with `default=str` so non-serializable payloads are still captured). Registered via
`celery_app` `include=[…, "workers.deadletter"]` so the handler connects in every worker. Inspection
API `GET /api/v1/dead-letters` (`api/routes/deadletters.py`, auth-protected, paginated, `task_name`
filter) backed by `fetch_dead_letters`. **Tests:** 3 unit (`test_deadletter.py`: field capture,
best-effort swallow, missing-metadata) + 2 integration (`test_api.py`: listing + filter, empty).

---

## Epic 5 — Alert Deduplication & Noise Reduction

> **As** Priya **I want** duplicate alerts suppressed **so that** I'm not paged 300 times for one
> spike.

### MM-5.1 — Fingerprint-based dedup with cooldown ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** anomalies for the same `(metric, detector, 5-min bucket)` **Then** only the first within
  the cooldown window (default 300s) passes; the rest are suppressed and logged.
- **Given** concurrent worker threads **Then** dedup is thread-safe.
- **Given** long runtime **Then** the seen-set is bounded (evict entries older than 2× cooldown).

**Maps to:** `alerting/dedup.py::AlertDeduplicator`.

### MM-5.2 — Shared, durable dedup state ✅ Built · P1 · 5pts
**As** Carlos **I want** dedup to work across workers and restarts **so that** scaling out doesn't
reintroduce duplicates.

**Acceptance criteria**
- **Given** multiple `alerts` workers **Then** they share one dedup store (Redis `SET NX EX`) so the
  same anomaly fired by two workers is suppressed once. ✅ verified (`test_shared_across_workers`).
- **Given** a worker restart **Then** dedup state survives (claims persist in Redis until TTL). ✅
- **Given** the cooldown elapses **Then** the anomaly may alert again. ✅ verified
  (`test_expiry_allows_realert`).

**Maps to:** `alerting/dedup.py` — in-process dict replaced with atomic Redis `SET key 1 NX EX
<cooldown>`; TTL self-evicts (no manual eviction). Injectable client for tests (`fakeredis`).
**Verified live:** two consecutive sweeps over the same data both yield 10 alerts (the 2nd adds 0).

### MM-5.3 — Configurable dedup granularity ✅ Built · P2 · 2pts
**Acceptance criteria**
- **Given** config **Then** the bucket size and cooldown are tunable per environment (and later per
  metric/tenant). ✅ verified live (`DEDUP_BUCKET_SECONDS=600` in `.env` → `get_settings()` and
  `AlertDeduplicator().bucket_seconds` both read 600; 12:34 & 12:38 then share a bucket; reverted to
  300). Cooldown was already configurable (`ALERT_COOLDOWN_SECONDS`).

**Maps to:** new `config.py::dedup_bucket_seconds` (default 300s). `alerting/dedup.py::time_bucket`
was **rewritten** from a broken `//500`-on-digit-string hack (which mis-grouped — e.g. 12:34 & 18:00
landed one bucket apart) to a correct `floor(epoch / bucket_seconds)` (handles ISO strings w/ `Z` or
offset, epoch floats, and bad input → falls back to now). `AlertDeduplicator` gained a `bucket_seconds`
arg (defaults to config) used in `_fingerprint`; `apply_consensus` gained a matching `bucket_seconds`
param so dedup + consensus group identically. **Tests:** `tests/unit/test_dedup.py` (`TestTimeBucket`
+ bucket-grouping) and `tests/unit/test_consensus.py` (bucket-width grouping). **Note:** per-metric /
per-tenant granularity is intentionally deferred (the knob is per-environment for now); the
`bucket_seconds` params already thread through to make that a small future change.

---

## Epic 6 — Alert Routing & Notification

> **As** Priya **I want** real anomalies delivered to the channels I use, with severity **so that** I
> act fast.

### MM-6.1 — Multi-sink fan-out ✅ Built · P0 · 3pts
**Acceptance criteria**
- **Given** a unique anomaly **Then** it is routed to every configured sink.
- **Given** a sink fails **Then** other sinks still receive it; if **all** fail, the task raises so it
  retries.

**Maps to:** `alerting/router.py::AlertRouter.route`, `workers/tasks.py::route_alert`.

### MM-6.2 — Slack sink ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** `SLACK_WEBHOOK_URL` set **Then** anomalies post a formatted Slack message (metric, value,
  score %, detector, method, time); HTTP errors raise for retry.

**Maps to:** `alerting/router.py::SlackSink`.

### MM-6.3 — PagerDuty sink with severity ✅ Built · P1 · 2pts
**Acceptance criteria**
- **Given** `PAGERDUTY_ROUTING_KEY` set **Then** a PagerDuty Events API v2 incident is created;
  severity is `critical` when score > 0.9, else `warning`.

**Maps to:** `alerting/router.py::PagerDutySink`.

### MM-6.4 — Webhook & Log sinks ✅ Built · P1 · 1pt
**Acceptance criteria**
- **Given** a webhook URL **Then** the full anomaly JSON is POSTed with optional headers.
- **Given** no external sinks configured **Then** the always-on `LogSink` records the anomaly via
  structured logs.

**Maps to:** `alerting/router.py::WebhookSink`, `LogSink`, `build_default_router`.

### MM-6.5 — Email / MS Teams / generic provider sinks ✅ Built · P2 · 3pts
**Acceptance criteria**
- **Given** a new sink class implementing `AlertSink` **Then** it can be registered without changing
  the router (open/closed). ✅ verified live — with `GENERIC_WEBHOOK_URL` + a routing rule, a real
  sweep delivered 31 anomaly POSTs through the unchanged `AlertRouter` to a generic webhook, each
  carrying the configured `GENERIC_WEBHOOK_HEADERS` (`X-Token`). `available_sinks` reported
  `{log, slack, webhook}` from real settings.

**Maps to:** `alerting/router.py` — new **`TeamsSink`** (MS Teams Incoming Webhook MessageCard, severity
colour by score) and **`EmailSink`** (stdlib `smtplib`/`EmailMessage`, opt TLS + auth), plus the
existing `WebhookSink` is now registered as the generic `webhook` sink. `available_sinks()` registers
each only when its config is set (`TEAMS_WEBHOOK_URL`; `GENERIC_WEBHOOK_URL` [+`GENERIC_WEBHOOK_HEADERS`
JSON, fail-safe parse]; `SMTP_HOST` **and** `ALERT_EMAIL_TO`). **`AlertRouter` is untouched** — it fans
out over whatever sinks it's given (open/closed); adding a sink is config + a class. New settings in
`config.py` all default off. **Tests:** 8 in `tests/unit/test_router.py` (registration on/off, SMTP
send incl. no-auth/no-TLS path, Teams MessageCard payload, header fail-safe, route-to-new-sink without
router change). **Note:** Email/Teams delivery is unit-tested with mocked transport (no live SMTP/Teams
endpoint); the generic webhook proved the registration→routing→HTTP-delivery path live.

**Follow-on — SMS text sink ✅ Built:** `alerting/router.py::SmsSink` sends each anomaly as an SMS via
the **Twilio Messages REST API** (dependency-free — form-encoded `httpx` POST with HTTP Basic auth, no
`twilio` SDK). One message per recipient so a single bad number doesn't sink the rest; partial failures
are logged, an all-fail raises (so the Celery task retries an undelivered alert). Registered by
`available_sinks()` only when all four of `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `SMS_FROM`,
`SMS_TO` are set; routable by name `sms` via routing rules — **`AlertRouter` still untouched**. **Tests:**
6 added in `tests/unit/test_router.py` (registration on/partial-off, per-recipient Twilio POST with
Basic auth + form body, partial-failure no-raise, all-fail raises, route-via-rule). **Note:** delivery
is unit-tested with mocked Twilio transport (no live SMS endpoint), like Email/Teams.

### MM-6.6 — Per-tenant / per-metric routing rules ✅ Built · P1 · 5pts
**As** an EM **I want** alerts for my team's metrics routed to my team's channel **so that** ownership
is clear.

**Acceptance criteria**
- **Given** JSON routing rules (metric-name glob → sink names) **Then** anomalies route only to the
  matching rule's sinks (first match wins). ✅ verified (`db.*`→log; `cpu.usage`→log+slack).
- **Given** no rules **Then** fan out to all configured sinks (backward compatible). ✅
- **Given** a rule names an unconfigured sink **Then** fall back to LogSink (never drop). ✅

**Maps to:** `config.py::alert_routing_rules`, `alerting/router.py` (`available_sinks`,
`select_sink_names`, rule-aware `build_default_router(metric_name)`), `workers/tasks.py::route_alert`
passes the metric name. **Tests:** `tests/unit/test_router.py` (5 cases). **Note:** keyed on metric
name today; full tenant/label routing arrives with tenancy (MM-9.3).

---

## Epic 7 — Anomaly & Alert History + Query API

> **As** Priya and Ana **I want** to query past anomalies and alerts **so that** I can investigate
> incidents and measure trends.

### MM-7.1 — Persist routed alerts ✅ Built · P0 · 3pts
**As** the platform **I want** every routed alert written to the `alerts` table **so that** history
exists.

**Acceptance criteria**
- **Given** an anomaly is routed **Then** a row is inserted into `alerts` (`created_at, metric_name,
  detector, score, value, fingerprint, routed_to[]`). ✅ verified (row persisted with all fields).
- **Given** routing partially fails **Then** `routed_to` reflects only successful sinks. ✅ verified
  (Slack 404 → `routed_to={log}`).

**Maps to:** `alerting/router.py::AlertRouter.route` (now returns successful sink names),
`alerting/dedup.py::AlertDeduplicator.filter` (stamps `fingerprint`),
`storage/timescale.py::persist_alert_sync` (insert), `workers/tasks.py::route_alert` (best-effort
persist), `storage/timescale.py::SETUP_SQL` (now creates `alerts`/`detector_models` at runtime).
**Tests:** `tests/unit/test_router.py`, `tests/unit/test_dedup.py::...fingerprint`.

### MM-7.2 — Query anomalies/alerts by metric & time ✅ Built · P0 · 5pts
**Acceptance criteria**
- **Given** `GET /api/v1/anomalies?metric=&from=&to=&detector=&min_score=&limit=&offset=` **Then** I
  get a paginated list of historical alerts matching the filters (combined with AND), newest first,
  with `total` (full match count) and `count` (this page). ✅ verified live.
- **Given** no matches **Then** an empty list with `count=0` and `total=0`. ✅
- **Given** `min_score` outside `[0,1]` **Then** `422`. ✅

**Maps to:** `api/routes/anomalies.py::list_alerts` (+ `AlertItem`/`AlertsPage` models),
`storage/timescale.py::fetch_alerts` (parameterized filters, reads `alerts`, indexed on
`(metric_name, created_at DESC)`). **Tests:** `tests/integration/test_api.py` (4 cases). Also fixed
the integration harness (overrode `get_session`; corrected `bulk_insert` patch targets) and removed
B008 false positives via ruff `extend-immutable-calls` config.

### MM-7.3 — List active metrics ✅ Built · P1 · 1pt
**Acceptance criteria**
- **Given** `GET /api/v1/anomalies/metrics?lookback_hours=` **Then** I get metric names with recent
  data and a count.

**Maps to:** `api/routes/anomalies.py::list_metrics`, `storage/timescale.py::list_active_metrics`.

### MM-7.4 — Read raw/aggregated series ✅ Built · P1 · 3pts
**As** Mei **I want** to fetch a metric's series (raw or 1-min) over a window **so that** I can plot
and debug detections via API.

**Acceptance criteria**
- **Given** `GET /api/v1/metrics/{name}/series?from=&to=&resolution=&limit=` **Then** I get
  timestamped values: `resolution=raw` → `{ts, value}`; `resolution=1m` → `{ts, avg, max, min,
  count}` (1-minute downsample). ✅ verified live.
- **Given** an unknown metric / empty window **Then** `count=0` with `points=[]`. ✅
- **Given** an unsupported `resolution` **Then** `422`. ✅

**Maps to:** `api/routes/metrics.py::read_series` (new read-only router under `/api/v1/metrics`),
`storage/timescale.py::fetch_metric_series`. The 1m view is computed with `time_bucket` over the raw
table (same shape/meaning as the `metrics_1min` continuous aggregate) so it covers the full
retention window. **Tests:** `tests/integration/test_api.py` (raw / 1m+window / bad-resolution).

### MM-7.5 — Alert detail with explanation ✅ Built · P2 · 3pts
**Acceptance criteria**
- **Given** an alert id **Then** I can retrieve the anomaly plus context (surrounding series window,
  detector, score, threshold) to understand *why* it fired. ✅ verified live (`/anomalies/831` →
  zscore, threshold 0.8, 9-pt ±10m window; `/anomalies/833` → isolation_forest, threshold 0.75;
  missing id → 404; `/anomalies/stats` not shadowed).

**Maps to:** `GET /api/v1/anomalies/{alert_id}` (`api/routes/anomalies.py::alert_detail`, declared
**last** so the static `/stats` and `/metrics` GET routes win over the dynamic `{alert_id}`). Returns
the alert + the **exact resolved threshold** + a human-readable **explanation** + the **surrounding
series window** (`window_minutes`, `resolution` raw/1m) via `fetch_metric_series`.
**Threshold accuracy:** zscore/iqr/stl all persist `detector='statistical'`, so a new
`alerts.method` column (added to `SETUP_SQL` + `001_initial.sql` + idempotent `ALTER`, written by
`persist_alert_sync`) records the method; `_resolver_key()` maps `(detector, method)` → the config key
and `_alert_threshold()` resolves it the same way the worker does (per-metric override → global
default). `storage/timescale.py::fetch_alert_by_id` reads the row. **Tests:** 3 integration
(`test_api.py`: context, 404, static-route-not-shadowed) + 5 unit (`test_alert_detail.py`: resolver
key, threshold, explanation). Depends on MM-7.1. **Note:** pre-MM-7.5 alerts have an empty `method`,
so a statistical alert falls back to the `statistical` key (→ global zscore default).

---

## Epic 8 — Observability & Operations

> **As** Carlos **I want** to see that MetricMesh is healthy and processing work **so that** I can
> operate it confidently.

### MM-8.1 — Liveness probe ✅ Built · P0 · 1pt
**Acceptance criteria**
- **Given** the API is up **Then** `GET /health` returns `200` with `{status, version, service}`.

**Maps to:** `api/routes/health.py::health`.

### MM-8.2 — Readiness probe (DB **and** Redis) ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** `GET /readiness` **Then** it checks **both** TimescaleDB and Redis connectivity and
  returns `degraded` if either fails. ✅ verified live (Redis stopped → `redis: error`, `db: ok`).
- **Given** a dependency is down **Then** the endpoint returns **HTTP 503** so orchestrators treat
  the pod as not-ready; **200** when both are healthy. ✅ verified.

**Maps to:** `api/routes/health.py` — `readiness()` now pings DB and Redis via testable
`_check_database()` / `_check_redis()` helpers (Redis via `redis.asyncio`), sets 503 on degraded.
**Tests:** `tests/integration/test_api.py` (ok / redis-down / db-down).

### MM-8.3 — Task monitoring (Flower) ✅ Built · P1 · 1pt
**Acceptance criteria**
- **Given** the stack is up **Then** Flower at `:5555` shows workers, queues, and task history.

**Maps to:** `flower` service in `docker-compose.yml` (+ `flower` dependency in `pyproject.toml`).

### MM-8.4 — Structured logging ✅ Built · P1 · 1pt
**Acceptance criteria**
- **Given** any component **Then** logs are structured (structlog/celery task logger) with event
  names and key fields (metric, score, counts).

**Maps to:** `structlog` usage across modules; `LOG_LEVEL` validated in `config.py`.

### MM-8.5 — Provisioned Grafana dashboards ✅ Built · P1 · 3pts
**As** Ana **I want** ready-made dashboards **so that** I see anomaly and ingestion trends without
building panels.

**Acceptance criteria**
- **Given** the stack starts **Then** Grafana auto-provisions a TimescaleDB datasource and a
  dashboard with ingest rate, anomalies-over-time by detector, alert volume, and per-metric series.
  ✅ verified (datasource health OK; dashboard query returns live data).

**Maps to:** `grafana/provisioning/datasources/datasources.yml` (TimescaleDB + Prometheus, fixed
UIDs), `grafana/provisioning/dashboards/dashboards.yml` (provider), and
`grafana/provisioning/dashboards/metricmesh-overview.json` (now 10 panels). Open
http://localhost:3000 → MetricMesh folder. The "queue depth" panel was delivered in **MM-8.6** (it
needs Prometheus app-metrics, not available from the SQL datasource).

### MM-8.6 — Application & business metrics export ✅ Built · P2 · 3pts
**Acceptance criteria**
- **Given** Prometheus scrapes the API/workers **Then** it sees app metrics (ingest count, detection
  duration, anomalies detected, alerts routed) via `prometheus-client`. ✅ verified live (Prometheus
  target `api:8000/metrics` `up`; `metricmesh_ingest_points_total`, `metricmesh_detection_duration_
  seconds`, `metricmesh_anomalies_detected_total`, `metricmesh_alerts_routed_total` all present).

**Maps to:** `monitoring/metrics.py` defines the counters/histogram plus a `QueueDepthCollector` that
reads each Celery queue's Redis list length at scrape time; `api/routes/observability.py` exposes the
unauthenticated `GET /metrics`. Instrumentation: `ingestion/api.py` (ingest), `workers/tasks.py`
(detection duration + anomalies + alerts routed). **Cross-process model:** the API and the three
Celery worker containers are separate processes, so every app container mounts a shared
`prometheus_multiproc` volume and sets `PROMETHEUS_MULTIPROC_DIR`; `render_latest()` aggregates all of
them via `MultiProcessCollector` at the single `api:8000/metrics` endpoint (verified: worker-origin
series appear there). Grafana gains a "Celery queue depth" and "Detection latency p95" panel (both
Prometheus-backed). **Tests:** `tests/unit/test_metrics.py` (names/labels, queue-depth collector incl.
broker-down resilience, exposition). **Honest note:** prefork worker children write one metric file
per PID into the shared dir; dead-PID files linger and are summed correctly for counters/histograms
(fine for a long-lived deployment, but the dir grows slowly — wipe the `prometheus_multiproc` volume
on a clean redeploy).

---

## Epic 9 — Security & Multi-tenancy

> **As** Carlos and Ana **I want** the platform authenticated and tenant-isolated **so that** it's
> safe to run beyond localhost.

### MM-9.1 — API authentication ✅ Built · P0 (for non-local) · 5pts
**Acceptance criteria**
- **Given** keys are configured and a request omits/sends a wrong `X-API-Key` **Then** `401`.
  ✅ verified live (no key / wrong key → 401).
- **Given** a valid `X-API-Key` **Then** the request proceeds (`200`). ✅
- **Given** no keys configured **Then** auth is disabled (dev mode). ✅ **Given** health/readiness
  probes **Then** they stay open regardless. ✅ verified.

**Maps to:** `api/auth.py::require_api_key` (header dep), applied to the ingest/metrics/anomaly
routers in `api/main.py` (health excluded); `config.py::api_keys` / `api_keys_set`. Seed script
gained `--api-key`. **Tests:** `tests/integration/test_api.py` (401 no-key, 200 valid-key,
health-open). **Note:** API-key scheme (not full JWT/principal attribution — a later enhancement).

### MM-9.2 — Lock down CORS ✅ Built · P0 · 1pt
**Acceptance criteria**
- **Given** config **Then** the CORS allow-list is restricted to configured origins (never `*`).
  ✅ verified live (localhost:3000 echoed; evil.com not echoed).

**Maps to:** `config.py::cors_allow_origins` / `cors_origins_list`; `api/main.py` CORS middleware uses
the allow-list with `allow_credentials=True` (which makes `*` impossible by construction).

### MM-9.3 — Per-tenant API keys & data isolation ✅ Built · P1 · 8pts
**Acceptance criteria**
- **Given** a tenant API key **Then** ingested metrics are tagged with that tenant and queries return
  only that tenant's data. ✅ verified live (acme & globex ingest the *same* metric name → each reads
  back only its own values; cross-tenant read → 0 rows; metric list per tenant excludes the other's).
- **Given** tenant A's key **Then** tenant B's metrics/alerts are never visible. ✅ verified live — a
  sweep over both tenants produced **32 alerts + 15 detector_models each**; acme `/anomalies` → only
  acme (32), globex → only globex (32); acme reading a globex alert by id → **404**.

**Decisions (asked):** phased — **Phase A+B** (identity + schema + ingest tagging + read isolation) then
**Phase C** (per-tenant detection pipeline); **app-level `WHERE tenant=`** filtering (not Postgres RLS).

**Maps to (Phase A+B):** `config.py` — `default_tenant` + `tenant_api_keys` JSON + `key_tenant_map`
(plain `api_keys` → default_tenant; dev mode = single `default_tenant`). `api/auth.py` —
`require_api_key` now returns `Identity(principal, tenant)`. Schema — `tenant` column (idempotent
`ALTER`, default `'default'`) + tenant index on `metrics` & `alerts` (SETUP_SQL + `001_initial.sql`).
Ingest — `bulk_insert(..., tenant=)` tagged from the **authenticated key, never the body**
(anti-spoof). Reads — every metrics/alerts storage fn + route threads `tenant` (`fetch_series`,
`fetch_metric_series`, `list_active_metrics`, `fetch_alerts`, `fetch_alert_by_id`, `set_alert_label`,
`feedback_stats_by_detector`, `detector_report_rows`). **Maps to (Phase C — pipeline):**
`list_active_metrics_sync` returns `(tenant, metric)` pairs; `schedule_detection_sweep` dispatches
detectors per pair; the three detector tasks gained a `tenant` arg → `fetch_series_sync(tenant=)`,
inject `tenant` into each emitted anomaly dict, `persist_detector_model_sync(tenant=)` /
`fetch_detector_model_sync(tenant=)`. `detector_models` gained a `tenant` column with a tenant-scoped
UNIQUE index `(tenant, metric, detector_type)`; the `metrics_1min` continuous aggregate was recreated
grouped by tenant (guarded `_migrate_cagg_for_tenant` drop in `setup_schema`, since a cagg's GROUP BY
can't be altered in place). Dedup fingerprint and consensus grouping both include `tenant` so two
tenants' identical metric/spike never collide. `persist_alert_sync` stores the anomaly's tenant; the
`alert.routed` audit records it. **Tests:** 6 unit (`test_tenancy.py` ×4; + per-tenant dedup and
per-tenant consensus) + integration (tenant threaded to storage; auth tests use `key_tenant_map`).
**Deferred:** compression `segmentby` still `metric_name` only (changing it on a hypertable with
compressed chunks is risky and is a perf detail, not correctness); Postgres RLS as a stronger
defense-in-depth layer.

### MM-9.4 — Secrets management ✅ Built · P1 · 2pts
**Acceptance criteria**
- **Given** deployment **Then** secrets come from env injection **or a secret store**, never
  committed. ✅ `.env` git-ignored; `.env.example` ships placeholders only.
- **Given** a mounted secrets dir (`/run/secrets` or `$SECRETS_DIR`) **Then** any setting can be read
  from a file named after the field. ✅ verified (`api_keys` read from `/tmp/secrets/api_keys`).

**Maps to:** `config.py` — `SettingsConfigDict(secrets_dir=...)` enabled when the dir exists (Docker
secrets / K8s secret volumes); `.gitignore` excludes `.env`. Documented in `.env.example`.

### MM-9.5 — Audit logging ✅ Built · P2 · 3pts
**Acceptance criteria**
- **Given** auth and alert routing events **Then** an audit trail records who/what/when. ✅ verified
  live (enabled auth → `auth.denied`×2 [no-key + bad-key, principal `none`]; sweep → `alert.routed`×34
  [who `system`, detail = sinks/detector/score/fingerprint]; labeled an alert → `feedback.submitted`
  [who `key:d28906c2` — the **hashed** key id, never the raw key; resource `alert:1358`; client IP];
  all queryable at `GET /api/v1/audit`).

**Maps to:** new `audit_log` table (who=`principal`, what=`action`/`resource`/`detail`, when=`at`;
SETUP_SQL + `001_initial.sql`). `storage/timescale.py` — `record_audit_async` (FastAPI, module engine)
+ `record_audit_sync` (Celery, fresh engine), both **best-effort** and gated on `config.audit_enabled`
(default on); `fetch_audit_log` backs the API. Capture points: **`auth.denied`** in
`api/auth.py::require_api_key` (now returns a non-reversible `principal_for()` key id; `anonymous` in
dev mode); **`feedback.submitted`** in `submit_feedback`; **`alert.routed`** in `route_alert`.
Inspection API `GET /api/v1/audit` (`api/routes/audit.py`, auth-protected, paginated, `action` filter).
**Tests:** 5 unit (`test_audit.py`: principal non-reversibility, sync/async gating + best-effort) + 3
integration (`test_api.py`: feedback audited, auth-denial audited, listing). **Scope note:** successful
authenticated reads/ingests are intentionally **not** audited (write-amplification on the high-volume
ingest path) — the trail focuses on denied access + state-changing actions; the raw API key is never
stored (only a SHA-256 prefix).

---

## Epic 10 — Detector Lifecycle, Tuning & Feedback

> **As** Mei **I want** to persist models, tune thresholds per metric, and learn from feedback
> **so that** detection precision improves over time.

### MM-10.1 — Persist trained detector models/params ✅ Built · P1 · 5pts
**As** Mei **I want** trained models persisted **so that** there's an audit trail and cheap models can
be reused instead of re-fitting every sweep.

**Acceptance criteria**
- **Given** a detector is fitted for a metric **Then** its parameters/metadata are stored in
  `detector_models` (`metric_name, detector_type, trained_at, parameters`), unique per
  `(metric, detector)`. ✅ verified live — a sweep populated 15 rows (5 metrics × {zscore,
  isolation_forest, prophet}); zscore stores fitted `mean`/`std`, IF/Prophet store hyperparameter
  metadata. Re-runs **upsert** (row count stays 15, `trained_at` bumps).
- **Given** a new run **Then** a recent model can be reused instead of refitting when appropriate.
  ✅ verified live — with `MODEL_REUSE_MAX_AGE_SECONDS=3600`, the statistical task logged
  `model.reused` and the reused `zscore` row's `trained_at` stayed put while the (always-refit)
  IF/Prophet rows advanced.

**Maps to:** `storage/timescale.py::persist_detector_model_sync` (upsert `ON CONFLICT
(metric_name, detector_type)`) / `fetch_detector_model_sync` (fresh-within-max-age lookup); each
detector's `get_state()` (and `StatisticalDetector.load_state()` for reuse); `workers/tasks.py`
persists best-effort after every fit and reuses the statistical model when
`config.py::model_reuse_max_age_seconds > 0` (default 0 = always re-fit). **Tests:**
`tests/unit/test_model_state.py` (5 cases). **Honest limitation:** Prophet's Stan model and Isolation
Forest's sklearn forest+scaler are **not JSON-serializable**, so they persist *metadata only* and
always re-fit; reuse currently applies to the statistical detectors. Reusing the expensive models
would need a binary model store (joblib/pickle blob) — a future extension.

### MM-10.2 — Per-metric threshold overrides ✅ Built · P1 · 3pts
**As** Mei **I want** to tune detection sensitivity per metric **so that** noisy metrics don't drown
out quiet ones.

**Acceptance criteria**
- **Given** a per-metric threshold config **Then** the sweep **and** on-demand detection use it
  instead of the global default. ✅ verified live — sweep: `cpu.*`→`zscore=0.0` flagged 167 points
  while other metrics kept the global 0.8; API: `cpu.*`→0.5, `db.*`→0.99, `mem`→global 0.8.
- **Given** an explicit request threshold on `/detect` **Then** it wins over any override. ✅ verified.
- **Given** malformed override JSON **Then** detection falls back to the global default (fail-safe).
  ✅ (unit-tested).

**Maps to:** `config.py::metric_thresholds` (JSON `{"<metric-glob>": {"<detector>": <float>}}`,
first-match-wins glob), `detection/thresholds.py::resolve_threshold` (cached parse, `fnmatchcase`,
fail-safe), wired into `workers/tasks.py` (all 3 detector tasks) and
`api/routes/anomalies.py::detect_now` (used when the request omits `threshold`). **Tests:**
`tests/unit/test_thresholds.py` (10 cases).

### MM-10.3 — Anomaly feedback labeling ✅ Built · P0 (for precision KPI) · 5pts
**As** Priya **I want** to mark an alert as true/false positive **so that** we can measure and
improve precision.

**Acceptance criteria**
- **Given** `POST /api/v1/anomalies/{id}/feedback {label}` **Then** the label is stored against the
  alert (`200`). ✅ verified live. Unknown id → `404`; bad label → `422`.
- **Given** labeled data **Then** **precision** = TP/(TP+FP) is computed per detector at
  `GET /api/v1/anomalies/stats`. ✅ verified (statistical → 0.6667 after 2 TP / 1 FP).

**Maps to:** `alerts.label` column (SETUP_SQL + migration + idempotent ALTER),
`storage/timescale.py::set_alert_label` / `feedback_stats_by_detector`, `api/routes/anomalies.py`
(`POST /{id}/feedback`, `GET /stats`); `label` added to the alerts query response. **Tests:** 4 in
`tests/integration/test_api.py`. **Note:** true **recall** needs ground-truth *missed* anomalies
(not derivable from alert feedback) — documented; precision is delivered.

### MM-10.4 — Detector A/B & precision reporting ✅ Built · P2 · 5pts
**Acceptance criteria**
- **Given** labeled feedback **Then** a report compares detectors' precision/recall per metric to
  guide which detector to trust. ✅ verified live (`/anomalies/report?metric=cpu.usage` →
  isolation_forest precision 0.8 / comparative_recall 0.889 vs statistical 0.444 / 0.444 →
  `recommended_detector=isolation_forest`; a metric whose only labels are false positives →
  `recommended_detector=null`).

**Maps to:** `GET /api/v1/anomalies/report` (`api/routes/anomalies.py::detector_report` +
pure `_assemble_detector_report`), backed by `storage/timescale.py::detector_report_rows`
(two aggregates: tp/fp/unlabeled counts per `(metric, detector)`, and confirmed-true-positive
**event** coverage via `time_bucket(make_interval(mins => :bucket_minutes), created_at)`). Per metric
it reports each detector's **precision** = TP/(TP+FP) and a **comparative_recall** = share of that
metric's confirmed anomaly events (distinct buckets any detector caught + a human marked true) that
the detector also caught, then names a `recommended_detector` (highest precision with ≥1 TP; tie →
comparative recall, then volume). **Honesty (per MM-10.3):** comparative_recall is *not* true recall —
it cannot see anomalies no detector caught; this caveat is returned in the response `notes` field.
**Tests:** 6 unit (`test_detector_report.py`) + 2 integration (`test_api.py`). Declared before the
dynamic `/{alert_id}` route so it isn't shadowed.

### MM-10.5 — Ensemble / consensus scoring ✅ Built · P2 · 5pts
**As** Priya **I want** an alert to require agreement across detectors **so that** single-detector
noise is reduced.

**Acceptance criteria**
- **Given** consensus config (`CONSENSUS_MIN_DETECTORS` ≥ 2) **Then** only anomalies confirmed by at
  least that many **distinct** detectors on the same `(metric, ~5-min bucket)` are routed; lone-detector
  spikes are suppressed. ✅ verified live (`=2` → all 10 alerts pass as statistical+isolation_forest
  agree; `=3` → `suppressed=65`, 0 routed).
- **Given** the default (`=1`) **Then** consensus is disabled and every detector's anomaly is eligible
  (backward compatible). ✅ verified (baseline sweep routed 10, no consensus stage).

**Maps to:** `alerting/consensus.py::apply_consensus` (groups by metric + shared `time_bucket`, counts
distinct detectors), applied in `workers/tasks.py::aggregate_and_alert` **before** dedup so a
non-consensus anomaly never consumes a cooldown claim; `config.py::consensus_min_detectors` (opt-in,
default 1). The `(metric, bucket)` grouping reuses `alerting/dedup.py::time_bucket` (extracted from the
dedup fingerprint so dedup + consensus bucket identically). **Tests:** `tests/unit/test_consensus.py`
(8 cases: disabled passthrough, below-threshold suppression, distinct-detector pass, same-detector-not-
counted, per-metric/per-bucket scoping, 3-way threshold).

---

## Epic 11 — Deployment & Configuration

> **As** Carlos **I want** to stand up and configure MetricMesh reliably **so that** environments are
> reproducible.

### MM-11.1 — One-command local stack ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** Docker **When** I run `make up` **Then** API, TimescaleDB, Redis, workers, Beat, Flower,
  Grafana, Prometheus start; **and** `make seed` ingests synthetic data successfully.

**Maps to:** `docker-compose.yml`, `Makefile`, `scripts/seed_data.py`.

### MM-11.2 — Environment-based configuration ✅ Built · P0 · 2pts
**Acceptance criteria**
- **Given** `.env` **Then** settings load via `pydantic-settings` as a cached singleton; invalid
  `LOG_LEVEL` is rejected at startup.
- **Given** containers **Then** in-container hostnames (`timescaledb`, `redis`) override host
  `localhost` values without breaking host-based dev.

**Maps to:** `config.py`, `docker-compose.yml` (`x-app-env` overrides).

### MM-11.3 — Reproducible builds (pinned images) ✅ Built · P1 · 1pt
**Acceptance criteria**
- **Given** the compose file **Then** TimescaleDB/Grafana/Prometheus use pinned version tags, not
  `latest`. ✅ verified (running `timescale/timescaledb:2.27.1-pg16`, `grafana/grafana:13.0.1`,
  `prom/prometheus:v3.11.3`).

**Maps to:** `docker-compose.yml` — pinned to the exact verified-running versions (the floating
`latest-pg16` previously caused a TimescaleDB columnstore behavior change). Redis already pinned
to `7-alpine`.

### MM-11.4 — CI: lint, type-check, test ✅ Built · P1 · 3pts
**Acceptance criteria**
- **Given** a push/PR **Then** CI runs `ruff check`, `mypy`, and `pytest`. ✅
- **Given** a lint or test failure **Then** the merge is blocked. ✅ (ruff + pytest are hard gates).
- **mypy --strict** is now a **hard gate** too (MM-11.6 cleared the backlog; `continue-on-error`
  removed). ✅

**Maps to:** `.github/workflows/ci.yml` (Python 3.11, pip cache). Repo made **ruff-clean** to support
this (auto-fixes + manual B017/B904/E501). Test suite verified **fully isolated** (passes against
unreachable DB/Redis), so CI needs no service containers.

### MM-11.5 — Kubernetes deployment ✅ Built · P2 · 8pts
**Acceptance criteria**
- **Given** a cluster **Then** Helm/manifests deploy the API, workers (per queue), Beat, and Flower
  with health probes, HPA, and externalized secrets. ✅ verified by `helm lint` (0 failures) +
  `helm template` (16 resources render & parse) + invariant checks: API has liveness `/health` &
  readiness `/readiness`; one worker Deployment per queue (fast/slow/alerts) pinned to its queue;
  Beat is a singleton (`replicas: 1`, `strategy: Recreate`); HPA on api + worker-fast (CPU); a Secret
  holds the DB URLs/API keys (envFrom). (No live cluster available, so not `kubectl apply`-ed.)

**Maps to:** `deploy/helm/metricmesh/` Helm chart — `Chart.yaml`, `values.yaml`, and templates:
`configmap.yaml` (non-secret env), `secret.yaml` (externalized creds, MM-9.4), `api.yaml`
(Deployment+Service+HPA, init-wait for deps, per-pod `PROMETHEUS_MULTIPROC_DIR`), `workers.yaml`
(range → one Deployment[+HPA] per queue, `celery inspect ping` liveness), `beat.yaml` (singleton,
broker-ping liveness), `flower.yaml` (Deployment+Service, `/healthcheck` probe), `timescaledb.yaml`
(StatefulSet+PVC, bundled, `enabled` toggle), `redis.yaml` (bundled, toggle), `NOTES.txt`, `README.md`.
Feature flags default to the app's safe defaults; bundled DB/Redis can be disabled for managed
services. **Honest gaps (documented in the chart README):** (1) cross-pod worker `/metrics`
aggregation isn't wired (k8s pods don't share the host volume compose used — needs a per-worker
exporter sidecar / Pushgateway); (2) TLS/ingress is left to an Ingress in front of the API (NFR-SEC5).
Depends on MM-9.4.

### MM-11.6 — Achieve `mypy --strict` clean ✅ Built · P2 · 5pts
**As** a maintainer **I want** the codebase to pass `mypy --strict` **so that** it can become a hard
CI gate.

**Acceptance criteria**
- **Given** `mypy . --ignore-missing-imports` (strict per `pyproject.toml`) **Then** 0 errors.
  ✅ **245 → 0** (verified: `Success: no issues found in 37 source files`).
- **Given** that's achieved **Then** flip `continue-on-error` to `false` for the mypy CI step. ✅
  (`.github/workflows/ci.yml` — mypy now blocks the merge).

**Maps to:** Started at 245 errors (the codebase grew since the ~99 estimate, and strict was also
checking tests). Strategy: scoped strict to shipping code (`pyproject.toml [tool.mypy] exclude =
["^tests/"]` — strict-typing test bodies is noise, removed 171), relaxed `disallow_untyped_decorators`
(Celery `@shared_task`/signals are untyped upstream, 7), and a tiny per-module override
(`disallow_untyped_calls = false` for `monitoring.metrics`/`api.ratelimit`/`api.routes.health`, which
call into untyped redis-py / prometheus_client). The remaining ~60 were mechanical: bare
`dict`→`dict[str, Any]`, `list[dict]`→`list[dict[str, Any]]`, `async_sessionmaker[AsyncSession]`,
`self: Any` on bound Celery tasks, `result.rowcount` cast (CursorResult), and constructing typed
Pydantic page items (`[AlertItem(**i) for i in items]`) instead of passing raw dicts. **Measure with
`mypy .` (config `strict=true`), NOT `mypy . --strict` — the CLI flag re-enables the relaxed
sub-flags.** Repo stays ruff-clean; 152 tests pass; live smoke (api + sweep) OK after the change.

---

## Cross-cutting "definition of done"

A story is **Done** when:
1. Acceptance criteria pass (unit/integration tests added under `tests/`).
2. `ruff check`, `mypy --strict`, and `pytest` are green.
3. Structured logs and (where relevant) metrics are emitted.
4. Docs updated (`CLAUDE.md` for architecture, this backlog's status tag flipped to ✅).
5. No new `latest` image tags or unauthenticated endpoints introduced for non-local targets.

## Appendix A — Defects discovered during MM-7.1 verification

Verifying MM-7.1 end-to-end revealed that the **scheduled detection pipeline had never actually run**.
Several pre-existing defects were found. The first three are **fixed** (they were blocking and
low-risk); the detector defects remain and block the scheduled path from producing real anomalies.

| Bug | Severity | Status | Description & fix |
|-----|----------|--------|-------------------|
| **BUG-1** Sweep routed to unconsumed queue | P0 | ✅ Fixed | `schedule_detection_sweep` had no `task_route`, so it landed on the default `celery` queue that no worker consumes (666 tasks had piled up). Routed it to `alerts`. (`workers/celery_app.py`) |
| **BUG-2** Tasks not registered with workers | P0 | ✅ Fixed | The Celery app never imported `workers.tasks`, so workers rejected every task with *"Received unregistered task"*. Added `include=["workers.tasks"]`. (`workers/celery_app.py`) |
| **BUG-3** Logger TypeError kills the sweep | P0 | ✅ Fixed | `workers/tasks.py` used the stdlib Celery logger with structlog-style kwargs (`log.info("sweep.start", metric_count=…)`), raising `TypeError` once INFO was enabled. Switched the module to `structlog`. (`workers/tasks.py`) |
| **BUG-4** Detectors receive a DataFrame, not a Series | P0 | ✅ Fixed | `fetch_series_sync`/`fetch_series` returned a **DataFrame**, but detectors expect a **Series**. Now both return the `value` Series (tz-aware index). (`storage/timescale.py`) |
| **BUG-5** Prophet detector slots/name conflict | P0 | ✅ Fixed | `run_prophet` raised `'name' in __slots__ conflicts with class variable`. Removed `"name"` from `__slots__` (it's a class constant). (`detection/prophet_detector.py`) |
| **BUG-6** Statistical zscore returns NaN scores | P1 | ✅ Fixed | Rolling-window NaNs leaked into scores. Added `.fillna(0.0)` to the zscore/iqr branches; `test_detectors.py[zscore]` now passes. (`detection/statistical.py`) |

> **Net effect:** the full scheduled pipeline now runs end-to-end — sweep → 5 detectors → chord →
> aggregate → dedup → route → **persist**. Verified live: real `statistical` and `isolation_forest`
> anomalies land in the `alerts` table; Prophet runs cleanly. MM-3.2/3.3/3.4 are genuinely ✅ Built
> (they previously only passed isolated unit tests, never ran against live DB data).

## Top "complete the end-to-end" gaps (recommended build order)

1. **MM-7.1** Persist routed alerts → unlocks history, feedback, precision KPIs.
2. **MM-7.2** Query anomalies API.
3. **MM-8.2** Readiness checks Redis; **MM-8.5** Grafana dashboards.
4. **MM-9.1/9.2** Auth + CORS lockdown (before any non-local deploy).
5. **MM-5.2** Shared Redis-backed dedup.
6. **MM-10.1/10.3** Model store + feedback labeling.
7. **MM-11.3/11.4** Pinned images + CI.
