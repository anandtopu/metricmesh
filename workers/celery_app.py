from __future__ import annotations

from celery import Celery
from kombu import Queue

from config import get_settings


def make_celery() -> Celery:
    """
    Celery application factory.
    Python skill: factory function pattern keeps instantiation testable
    (tests can call make_celery() with overridden settings).

    Queue design:
        fast    — statistical detectors (<1s per task)
        slow    — Prophet / Isolation Forest (seconds to minutes)
        alerts  — routing anomalies to sinks (I/O bound, separate pool)

    worker_prefetch_multiplier=1: each worker only takes one task at a time.
    Critical for long-running Prophet tasks — prevents a slow task from
    blocking the entire worker for minutes.
    """
    settings = get_settings()

    app = Celery(
        "metricmesh",
        broker=settings.redis_url,
        backend=settings.celery_result_backend,
        # Import the task module on startup so every worker registers the
        # @shared_task functions. Without this, workers reject tasks with
        # "Received unregistered task ...". workers.deadletter is imported so its
        # task_failure signal handler connects in every worker (MM-4.6).
        include=["workers.tasks", "workers.deadletter"],
    )

    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_track_started=True,
        # Only ack the message AFTER the task succeeds — prevents silent data loss
        task_acks_late=True,
        # Reject tasks on worker shutdown so another worker picks them up
        task_reject_on_worker_lost=True,
        # Prefetch=1: don't let fast workers hoard slow-queue tasks
        worker_prefetch_multiplier=1,
        task_queues=[
            Queue("fast",   routing_key="fast"),
            Queue("slow",   routing_key="slow"),
            Queue("alerts", routing_key="alerts"),
        ],
        task_routes={
            # The beat-scheduled sweep MUST land on a consumed queue. Without an
            # explicit route it goes to the default "celery" queue, which no
            # worker consumes (workers only serve fast/slow/alerts) — so the
            # whole detection pipeline never runs. Route it to the alerts worker.
            "workers.tasks.schedule_detection_sweep": {"queue": "alerts"},
            "workers.tasks.run_statistical":      {"queue": "fast"},
            "workers.tasks.run_prophet":          {"queue": "slow"},
            "workers.tasks.run_isolation_forest": {"queue": "slow"},
            "workers.tasks.route_alert":          {"queue": "alerts"},
            "workers.tasks.aggregate_and_alert":  {"queue": "alerts"},
        },
        beat_schedule={
            "detect-all-metrics-every-minute": {
                "task": "workers.tasks.schedule_detection_sweep",
                "schedule": 60.0,
            },
        },
        # Dead-letter: failed tasks go to a separate queue for inspection
        task_queues_max_priority=10,
    )

    return app


# Module-level app instance (imported by tasks.py via @shared_task)
celery_app = make_celery()
