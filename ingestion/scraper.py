from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


async def scrape_prometheus(
    target_url: str,
    interval_seconds: float = 15.0,
) -> AsyncIterator[dict[str, Any]]:
    """
    Async generator that polls a Prometheus /metrics endpoint at a fixed interval.
    Yields parsed metric dicts compatible with MetricPoint.

    Python skill: async generators with try/finally for cleanup,
    httpx AsyncClient as a context manager, generator-based pipeline.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                response = await client.get(target_url)
                response.raise_for_status()
                for line in response.text.splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.rsplit(" ", 1)
                    if len(parts) != 2:
                        continue
                    name_labels, value_str = parts
                    try:
                        value = float(value_str)
                    except ValueError:
                        continue
                    # Parse labels from name{label="val"} format
                    if "{" in name_labels:
                        name, label_str = name_labels.split("{", 1)
                        label_str = label_str.rstrip("}")
                        labels: dict[str, str] = {}
                        for part in label_str.split(","):
                            if "=" in part:
                                lk, lv = part.split("=", 1)
                                labels[lk.strip()] = lv.strip().strip('"')
                    else:
                        name = name_labels
                        labels = {}

                    safe_name = name.lower().replace("-", "_")
                    if safe_name and safe_name[0].isalpha():
                        yield {
                            "metric_name": safe_name,
                            "value": value,
                            "timestamp": time.time(),
                            "labels": labels,
                            "source": "prometheus_scrape",
                        }
            except Exception as exc:
                log.warning("scraper.error", target=target_url, error=str(exc))

            await asyncio.sleep(interval_seconds)


async def run_scraper_loop(
    target_url: str,
    session_factory: Any,  # callable returning AsyncSession
    interval_seconds: float = 15.0,
    batch_size: int = 500,
) -> None:
    """
    Drives the scraper generator and bulk-inserts collected points.
    Python skill: async for over an async generator, batching with list slice.
    """
    from storage.timescale import bulk_insert

    buffer: list[dict[str, Any]] = []
    async for point in scrape_prometheus(target_url, interval_seconds):
        buffer.append(point)
        if len(buffer) >= batch_size:
            async with session_factory() as session:
                await bulk_insert(session, buffer)
                await session.commit()
            log.info("scraper.flushed", count=len(buffer))
            buffer.clear()
