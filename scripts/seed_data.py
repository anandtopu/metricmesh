"""
Seed script: generates synthetic sinusoidal + noisy metrics with injected
anomalies and POSTs them to the MetricMesh API.

Usage:
    python scripts/seed_data.py --api http://localhost:8000 --points 1000

Python skill: argparse, numpy random generators, httpx batching,
datetime arithmetic with timezone-aware timestamps.
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from typing import Any

import httpx
import numpy as np

# Windows consoles default to cp1252, which can't encode the → / … glyphs below.
# Force UTF-8 output so the script runs identically across platforms.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")


def generate_metric(
    name: str,
    n: int,
    base: float = 50.0,
    noise: float = 5.0,
    period_minutes: int = 60,
    anomaly_prob: float = 0.02,
    seed: int = 42,
) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    now = time.time()
    points = []
    for i in range(n):
        t = now - (n - i) * 60  # one point per minute going back n minutes
        cycle = math.sin(2 * math.pi * i / period_minutes)
        value = base + base * 0.3 * cycle + float(rng.normal(0, noise))
        # Inject random spike anomaly
        if rng.random() < anomaly_prob:
            value += base * 3 * (1 if rng.random() > 0.5 else -1)
        points.append({
            "metric_name": name,
            "value": round(value, 4),
            "timestamp": t,
            "labels": {"env": "dev", "host": "seed-script"},
            "source": "seed",
        })
    return points


def ingest_batch(
    api_url: str, points: list[dict[str, Any]], batch_size: int = 500, api_key: str = ""
) -> int:
    total = 0
    headers = {"X-API-Key": api_key} if api_key else {}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        for i in range(0, len(points), batch_size):
            chunk = points[i : i + batch_size]
            resp = client.post(
                f"{api_url}/api/v1/metrics",
                json={"points": chunk, "source_id": "seed-script"},
            )
            resp.raise_for_status()
            body = resp.json()
            accepted = body.get("accepted", 0)
            total += accepted
            print(f"  batch {i // batch_size + 1}: {accepted}/{len(chunk)} accepted")
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed MetricMesh with synthetic data")
    parser.add_argument("--api",    default="http://localhost:8000", help="API base URL")
    parser.add_argument("--points", type=int, default=500, help="Points per metric")
    parser.add_argument("--api-key", default="", help="X-API-Key (if auth enabled)")
    args = parser.parse_args()

    metrics = [
        ("cpu.usage",          60.0, 10.0, 60),
        ("mem.usage_bytes",    4e9,  5e8,  120),
        ("http.request.latency_ms", 200.0, 20.0, 30),
        ("db.query.duration_ms",    50.0,  8.0,  45),
        ("disk.iops",          300.0, 30.0, 90),
    ]

    for i, (name, base, noise, period) in enumerate(metrics):
        print(f"Generating {args.points} points for {name} …")
        points = generate_metric(name, args.points, base, noise, period, seed=i)
        total = ingest_batch(args.api, points, api_key=args.api_key)
        print(f"  → {total} points ingested for {name}")

    print("\nSeed complete.")


if __name__ == "__main__":
    main()
