# MetricMesh

A distributed time-series anomaly detection platform built with Python.
Ingests metrics via FastAPI, stores them in TimescaleDB, and runs three
detection algorithms (Z-score, Prophet, Isolation Forest) asynchronously
through a Celery worker pool.

---

## Architecture

```
Ingest (FastAPI / Prometheus scraper)
        ↓
TimescaleDB (hypertable + continuous aggregates)   Redis (broker + cache)
        ↓                                               ↓
                 Celery Worker Pool
        ┌────────────┬─────────────┬──────────────┐
        ↓            ↓             ↓              ↓
   Statistical    Prophet    Isolation Forest   Alert router
   (Z-score/IQR/STL)  (forecast bounds)  (multivariate)
        └────────────┴─────────────┴──────────────┘
                          ↓
            Deduplication + Alert routing
            (Slack / PagerDuty / Webhook / Log)
```

---

## Quick start

### Prerequisites
- Docker + Docker Compose
- Python 3.11+

### 1. Clone and configure

```bash
cp .env.example .env
# Edit .env — at minimum set SLACK_WEBHOOK_URL if you want Slack alerts
```

### 2. Start services

```bash
make up
# or: docker compose up -d
```

### 3. Seed with synthetic data

```bash
make seed
# or: python scripts/seed_data.py --api http://localhost:8000 --points 500
```

### 4. Explore

| URL | Purpose |
|-----|---------|
| http://localhost:8000/docs | FastAPI interactive docs |
| http://localhost:5555 | Celery Flower — task monitor |
| http://localhost:3000 | Grafana dashboards (admin/admin) |
| http://localhost:9090 | Prometheus |

---

## Development

### Install locally

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
make test          # all tests
make test-unit     # unit tests only (no DB needed)
```

### Lint / format

```bash
make lint
make fmt
```

### Type-check

```bash
make typecheck
```

---

## Project structure

```
metricmesh/
├── ingestion/
│   ├── validators.py       # Pydantic v2 MetricPoint, MetricBatch schemas
│   ├── api.py              # FastAPI ingest router
│   └── scraper.py          # Async Prometheus pull scraper
├── storage/
│   ├── timescale.py        # Async engine, session, bulk_insert, fetch_series
│   └── migrations/
│       └── 001_initial.sql # DDL: hypertable, continuous aggregate, compression
├── detection/
│   ├── base.py             # Detector Protocol + AnomalyResult dataclass
│   ├── statistical.py      # Z-score, IQR, STL (structural pattern matching)
│   ├── prophet_detector.py # Prophet uncertainty interval detector
│   ├── isolation_forest.py # Multivariate Isolation Forest
│   └── registry.py         # Detector factory registry
├── workers/
│   ├── celery_app.py       # Celery app factory + queue config
│   └── tasks.py            # All detection + alert tasks (chord/group canvas)
├── alerting/
│   ├── dedup.py            # Thread-safe fingerprint deduplication
│   └── router.py           # AlertSink ABC + Slack/PD/Webhook/Log sinks
├── api/
│   ├── main.py             # FastAPI app factory + lifespan
│   └── routes/
│       ├── health.py       # /health, /readiness
│       └── anomalies.py    # /detect, /metrics
├── tests/
│   ├── unit/               # Pytest unit tests (no external deps)
│   └── integration/        # Async httpx API tests
├── scripts/
│   └── seed_data.py        # Synthetic data generator + ingest
├── grafana/
│   └── prometheus.yml      # Prometheus scrape config
├── config.py               # Pydantic Settings (lru_cache singleton)
├── docker-compose.yml
├── Dockerfile
├── Makefile
└── pyproject.toml
```

---

## Python skills built

| Skill | Where |
|-------|-------|
| Pydantic v2 strict mode, field/model validators | `ingestion/validators.py` |
| `typing.Protocol` + `runtime_checkable` | `detection/base.py` |
| `asyncio` async context managers | `storage/timescale.py` |
| SQLAlchemy 2.0 async + connection pool tuning | `storage/timescale.py` |
| `match/case` structural pattern matching | `detection/statistical.py` |
| NumPy broadcasting + Pandas rolling windows | `detection/statistical.py` |
| `__slots__`, `frozen=True` dataclasses | `detection/base.py`, `detection/prophet_detector.py` |
| `contextlib.redirect_stderr` | `detection/prophet_detector.py` |
| Celery canvas: `chord`, `group`, `.s()` signatures | `workers/tasks.py` |
| Exponential backoff + retry jitter | `workers/tasks.py` |
| `threading.Lock` for thread-safe shared state | `alerting/dedup.py` |
| ABC + open/closed principle for sinks | `alerting/router.py` |
| Fluent API (method chaining) | `alerting/router.py` |
| `@lru_cache` settings singleton | `config.py` |
| Async generators | `ingestion/scraper.py` |

---

## Data / AI concepts built

| Concept | Where |
|---------|-------|
| TimescaleDB hypertables | `storage/migrations/001_initial.sql` |
| Continuous aggregates + compression | `storage/timescale.py` |
| Bulk insert via `unnest()` | `storage/timescale.py` |
| Z-score rolling anomaly detection | `detection/statistical.py` |
| IQR fence detection | `detection/statistical.py` |
| STL seasonal decomposition | `detection/statistical.py` |
| Prophet changepoint + seasonality modelling | `detection/prophet_detector.py` |
| Isolation Forest path-length scoring | `detection/isolation_forest.py` |
| RobustScaler for outlier-resistant normalisation | `detection/isolation_forest.py` |
| Cyclic time encoding (sin/cos) | `detection/isolation_forest.py` |
| Celery queue routing + beat scheduling | `workers/celery_app.py` |
| Alert deduplication + cooldown windows | `alerting/dedup.py` |
