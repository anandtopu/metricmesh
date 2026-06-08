# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

All common tasks go through the `Makefile`:

```bash
make up          # docker compose up -d  (starts the full stack)
make down
make build       # rebuild images
make seed        # python scripts/seed_data.py --api http://localhost:8000 --points 500
make logs        # tail all container logs

make test        # pytest tests/ -q --tb=short   (unit + integration, no DB needed)
make test-unit   # pytest tests/unit/ -v
make lint        # ruff check .
make fmt         # ruff format .
make typecheck   # mypy . --ignore-missing-imports
```

Local dev install: `pip install -e ".[dev]"`.

Run a single test: `pytest tests/unit/test_detectors.py::test_name -v`. Note `pyproject.toml` sets
`addopts = "--cov=. --cov-report=term-missing -q"` and `asyncio_mode = "auto"`, so async tests need
no `@pytest.mark.asyncio` decorator and coverage runs automatically.

Ruff is configured for line-length 100, py311, rules `E,F,I,UP,B,SIM`. Mypy runs in `strict` mode.

## Architecture

MetricMesh is a time-series anomaly detection platform. The data flow spans several modules and is the
key thing to understand before editing:

**Ingest → Store → Detect (scheduled) → Dedup → Alert**

1. **Ingest** (`ingestion/`, `api/`): FastAPI accepts `MetricPoint`/`MetricBatch` (Pydantic v2,
   `strict=True` + `frozen=True`) and bulk-inserts into TimescaleDB via `bulk_insert()` using a single
   `unnest()` round-trip. `ingestion/scraper.py` is an alternative Prometheus pull source.
2. **Store** (`storage/timescale.py`): a `metrics` hypertable with a 1-minute continuous aggregate and
   7-day compression policy.
3. **Detect** (`workers/`, `detection/`): Celery Beat fires `schedule_detection_sweep` every 60s. It
   lists active metrics and dispatches detector tasks via a Celery **chord** —
   `chord(group(all_tasks))(aggregate_and_alert.s())` — so all detectors run in parallel and their
   results fan into one callback.
4. **Consensus + Dedup + Alert** (`alerting/`): `aggregate_and_alert` flattens all detector outputs,
   optionally runs them through `apply_consensus` (MM-10.5 — keep only anomalies confirmed by
   ≥`consensus_min_detectors` distinct detectors on the same `(metric, time_bucket)`; default 1 =
   off), then `AlertDeduplicator`, then dispatches one `route_alert` task per surviving anomaly.
   Consensus runs **before** dedup so a non-consensus anomaly never consumes a cooldown claim.
   `AlertRouter` fans each anomaly out to every registered `AlertSink`.

### Things that are easy to get wrong

- **Sync/async DB split.** The FastAPI app uses the module-level async engine (`init_db()` at startup,
  `get_session()` dependency, `database_url` / asyncpg). Celery tasks run with **no event loop**, so
  they call the `*_sync` functions (`fetch_series_sync`, `list_active_metrics_sync`) which create a
  fresh **synchronous** psycopg2 engine per call (`database_url_sync`). Don't call async storage
  functions from inside a Celery task, and vice versa.

- **Anomaly scores are normalized per-batch by default, so thresholds are relative.** In the default
  `scoring_mode="relative"` detectors return scores in `[0,1]` by dividing by the batch max (e.g.
  z-score does `z / z.max()`; stl by batch p99; isolation_forest by batch min-max). So the configured
  thresholds in `config.py` (`zscore_threshold=0.8`, etc.) are *relative* cutoffs against the current
  batch — changing the lookback window changes what a given threshold means. **MM-3.6:** setting
  `SCORING_MODE=absolute` switches those three to a **fixed** reference (z→6σ, stl→robust-MAD σ,
  isolation_forest→fixed sigmoid on `decision_function`), so a threshold is stable across windows.
  `iqr`/`prophet` are inherently absolute and ignore the flag. The mode is passed into each detector's
  `__init__` (via `get_detector(name, scoring_mode=...)` and the worker tasks) — detectors stay pure
  and don't read settings themselves.

- **Thresholds can be overridden per metric (MM-10.2).** `detection/thresholds.py::resolve_threshold`
  consults `config.py::metric_thresholds` (JSON `{"<metric-glob>": {"<detector>": <float>}}`,
  first-match-wins) and falls back to the global `*_threshold`. Both the sweep tasks and on-demand
  `/detect` resolve through it; `/detect` only resolves when the request omits `threshold` (an explicit
  request value wins). Bad JSON fails safe to the global default.

- **fit/detect train-test split.** Every detector task fits on the first ~80% of the series and detects
  on the last ~20% (`split = max(int(len(series) * 0.8), N)`). Tasks short-circuit and return no
  anomalies when there isn't enough data (Prophet needs ≥20 rows, Isolation Forest ≥10).

- **Detector models are persisted, but only statistical ones are reusable (MM-10.1).** After each fit
  the tasks best-effort upsert `detector.get_state()` into `detector_models` (one row per
  `(metric, detector_type)`, `detector_type` = the resolver key `zscore`/`iqr`/`stl`/`prophet`/
  `isolation_forest`). Reuse (skip re-fit, `config.model_reuse_max_age_seconds > 0`) only applies to
  the statistical detector, whose state is JSON-serializable; Prophet (Stan) and Isolation Forest
  (sklearn forest + scaler) persist *metadata only* and always re-fit. Note `StatisticalDetector`'s
  `get_state()` carries fitted params but `zscore.score()` recomputes its rolling stats anyway —
  reuse is only behaviourally meaningful for `iqr` (whose `score()` reads the cached fences).

- **Anomalies travel as dicts.** `AnomalyResult` (frozen, `slots=True` dataclass) is serialized via
  `to_dict()` because Celery uses the JSON serializer. Tasks pass and receive plain dicts, not the
  dataclass — `aggregate_and_alert` and the sinks read keys like `metric_name`, `score`, `detector`.

- **Schema is created two ways.** `storage/migrations/001_initial.sql` is mounted into the TimescaleDB
  container's `docker-entrypoint-initdb.d` (runs on first container start). Separately,
  `setup_schema()` runs the `SETUP_SQL` string in `storage/timescale.py` on FastAPI startup
  (idempotent `IF NOT EXISTS`). `SETUP_SQL` is the authoritative runtime DDL — keep the two in sync.

### Detector contract and registry

`detection/base.py` defines `Detector` as a `runtime_checkable` `Protocol` (`fit` / `score` / `detect`,
plus a `name` attribute) — no inheritance required. `StatisticalDetector` even asserts Protocol
compliance at import time. To add a detector: implement the three methods, then add one line to the
`_REGISTRY` dict in `detection/registry.py`. The three statistical methods (`zscore`, `iqr`, `stl`)
share one class and dispatch via `match/case` on `self.method`.

### Celery queue topology

`workers/celery_app.py` routes tasks to three queues by cost, each served by a dedicated worker in
`docker-compose.yml`:

- `fast` (8 workers) — statistical detectors (<1s)
- `slow` (2 workers) — Prophet + Isolation Forest (seconds to minutes)
- `alerts` (4 workers) — routing + aggregation (I/O bound)

`worker_prefetch_multiplier=1` plus `task_acks_late=True` are deliberate: prevents a long Prophet task
from blocking a worker and guards against losing tasks on worker crash.

### Configuration

All settings come from `config.py` — a `pydantic-settings` `BaseSettings` loaded from `.env`, exposed
as an `@lru_cache` singleton via `get_settings()`. Import `get_settings()` rather than reading env vars
directly. Copy `.env.example` → `.env` before running.

## Services (docker compose)

| Port | Service |
|------|---------|
| 8000 | FastAPI (`/docs`, `/redoc`) |
| 5432 | TimescaleDB |
| 6379 | Redis (Celery broker on db 0, result backend on db 1) |
| 5555 | Celery Flower |
| 3000 | Grafana (admin/admin) |
| 9090 | Prometheus |
