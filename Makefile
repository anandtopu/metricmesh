.PHONY: up down build test lint fmt seed logs

## Start all services
up:
	docker compose up -d

## Stop all services
down:
	docker compose down

## Rebuild images
build:
	docker compose build

## Run unit + integration tests (no DB required)
test:
	pytest tests/ -q --tb=short

## Run only unit tests
test-unit:
	pytest tests/unit/ -v

## Lint with ruff
lint:
	ruff check .

## Format with ruff
fmt:
	ruff format .

## Type-check with mypy
typecheck:
	mypy . --ignore-missing-imports

## Seed local API with synthetic data
seed:
	python scripts/seed_data.py --api http://localhost:8000 --points 500

## Start the opt-in Prometheus scraper service
scrape:
	docker compose --profile scraper up -d scraper

## Tail logs from all containers
logs:
	docker compose logs -f

## Open Flower task monitor in browser
flower:
	open http://localhost:5555

## Open Grafana in browser
grafana:
	open http://localhost:3000

## Open API docs in browser
docs:
	open http://localhost:8000/docs
