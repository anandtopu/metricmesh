"""Unit tests for the MM-4.6 dead-letter signal handler."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from workers.deadletter import record_dead_letter


def _sender(name="workers.tasks.route_alert", retries=3, queue="alerts"):
    return SimpleNamespace(
        name=name,
        request=SimpleNamespace(retries=retries, delivery_info={"routing_key": queue}),
    )


def test_records_dead_letter_with_expected_fields():
    with patch("storage.timescale.persist_dead_letter_sync") as persist:
        record_dead_letter(
            sender=_sender(),
            task_id="abc-123",
            exception=RuntimeError("boom"),
            args=["cpu.usage"],
            kwargs={"method": "zscore"},
            einfo="TRACEBACK-TEXT",
        )
    persist.assert_called_once()
    kw = persist.call_args.kwargs
    assert kw["task_name"] == "workers.tasks.route_alert"
    assert kw["task_id"] == "abc-123"
    assert kw["queue"] == "alerts"
    assert kw["retries"] == 3
    assert kw["exception"] == "RuntimeError: boom"
    assert kw["args"] == ["cpu.usage"]
    assert kw["kwargs"] == {"method": "zscore"}
    assert kw["traceback"] == "TRACEBACK-TEXT"


def test_persist_failure_is_swallowed():
    # A DB error in the handler must NOT propagate and mask the task failure.
    with patch(
        "storage.timescale.persist_dead_letter_sync", side_effect=RuntimeError("db down")
    ):
        record_dead_letter(sender=_sender(), task_id="x", exception=ValueError("v"))


def test_handles_missing_sender_metadata_gracefully():
    # Defensive: no sender / no exception → still records something, no crash.
    with patch("storage.timescale.persist_dead_letter_sync") as persist:
        record_dead_letter(sender=None, task_id=None, exception=None)
    kw = persist.call_args.kwargs
    assert kw["task_name"] == "unknown"
    assert kw["retries"] == 0
    assert kw["exception"] == "unknown"
    assert kw["args"] == [] and kw["kwargs"] == {}
