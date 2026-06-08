"""Unit tests for the MM-9.5 audit helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from api.auth import principal_for
from storage.timescale import record_audit_async, record_audit_sync


def test_principal_is_deterministic_and_non_reversible():
    a = principal_for("super-secret-key")
    b = principal_for("super-secret-key")
    assert a == b
    assert a.startswith("key:")
    # The raw secret must never appear in the principal.
    assert "super-secret-key" not in a
    assert principal_for("other-key") != a


def test_record_audit_sync_noop_when_disabled():
    with (
        patch(
            "storage.timescale.get_settings",
            return_value=SimpleNamespace(audit_enabled=False, database_url_sync="x"),
        ),
        patch("sqlalchemy.create_engine") as create_engine,
    ):
        record_audit_sync("auth.denied", principal="key:abc")
    create_engine.assert_not_called()


def test_record_audit_sync_is_best_effort():
    # A DB error must be swallowed — auditing never breaks the audited op.
    with (
        patch(
            "storage.timescale.get_settings",
            return_value=SimpleNamespace(audit_enabled=True, database_url_sync="x"),
        ),
        patch("sqlalchemy.create_engine", side_effect=RuntimeError("db down")),
    ):
        record_audit_sync("alert.routed")  # must not raise


async def test_record_audit_async_best_effort_without_engine():
    # _engine is None in the unit environment → the insert is swallowed.
    with patch(
        "storage.timescale.get_settings",
        return_value=SimpleNamespace(audit_enabled=True),
    ):
        await record_audit_async("feedback.submitted", principal="anonymous")


async def test_record_audit_async_noop_when_disabled():
    with patch(
        "storage.timescale.get_settings",
        return_value=SimpleNamespace(audit_enabled=False),
    ):
        await record_audit_async("auth.denied")  # returns immediately, no error
