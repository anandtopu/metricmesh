"""
Entry point for the (opt-in) Prometheus scraper service (MM-1.4).

Polls ``PROMETHEUS_SCRAPE_URL`` at a fixed interval, parses the exposition
format, and bulk-inserts points into TimescaleDB. Run as a managed service:

    docker compose --profile scraper up -d scraper
    # or locally:
    PROMETHEUS_SCRAPE_URL=http://localhost:9090/metrics python -m ingestion.scraper_main
"""
from __future__ import annotations

import asyncio

import structlog

from config import get_settings
from ingestion.scraper import run_scraper_loop
from storage import timescale

log = structlog.get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    target = settings.prometheus_scrape_url.strip()

    if not target:
        # Idle instead of crash-looping when the service is started without a
        # target configured. Operators set PROMETHEUS_SCRAPE_URL to enable it.
        log.warning("scraper.disabled", reason="PROMETHEUS_SCRAPE_URL not set")
        while True:
            await asyncio.sleep(3600)

    timescale.init_db()
    log.info(
        "scraper.start",
        target=target,
        interval=settings.prometheus_scrape_interval,
        batch_size=settings.prometheus_scrape_batch_size,
    )
    await run_scraper_loop(
        target,
        timescale.get_session_factory(),
        interval_seconds=settings.prometheus_scrape_interval,
        batch_size=settings.prometheus_scrape_batch_size,
    )


if __name__ == "__main__":
    asyncio.run(main())
