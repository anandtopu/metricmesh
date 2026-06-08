"""Unit tests for per-metric threshold overrides (MM-10.2)."""
from __future__ import annotations

import pytest

import config
from detection.thresholds import _parse, resolve_threshold


@pytest.fixture(autouse=True)
def _clear_caches():
    """Keep the settings + parse caches from leaking between tests / suites."""
    config.get_settings.cache_clear()
    _parse.cache_clear()
    yield
    config.get_settings.cache_clear()
    _parse.cache_clear()


def _with_thresholds(monkeypatch, raw: str) -> None:
    """Point get_settings() at a Settings instance carrying the given JSON."""
    config.get_settings.cache_clear()
    monkeypatch.setenv("METRIC_THRESHOLDS", raw)
    # Warm the cache so resolve_threshold() reads our value.
    config.get_settings()


def test_empty_config_returns_default(monkeypatch):
    _with_thresholds(monkeypatch, "")
    assert resolve_threshold("cpu.usage", "zscore", 0.8) == 0.8


def test_exact_metric_override(monkeypatch):
    _with_thresholds(monkeypatch, '{"cpu.usage": {"zscore": 0.95}}')
    assert resolve_threshold("cpu.usage", "zscore", 0.8) == 0.95


def test_override_only_applies_to_named_detector(monkeypatch):
    _with_thresholds(monkeypatch, '{"cpu.usage": {"zscore": 0.95}}')
    # isolation_forest has no override for this metric → global default.
    assert resolve_threshold("cpu.usage", "isolation_forest", 0.75) == 0.75


def test_glob_match(monkeypatch):
    _with_thresholds(monkeypatch, '{"db.*": {"prophet": 0.6}}')
    assert resolve_threshold("db.query.duration_ms", "prophet", 0.5) == 0.6
    assert resolve_threshold("cpu.usage", "prophet", 0.5) == 0.5


def test_first_matching_glob_wins(monkeypatch):
    _with_thresholds(
        monkeypatch,
        '{"db.query.*": {"zscore": 0.9}, "db.*": {"zscore": 0.7}}',
    )
    assert resolve_threshold("db.query.duration_ms", "zscore", 0.8) == 0.9


def test_unmatched_metric_returns_default(monkeypatch):
    _with_thresholds(monkeypatch, '{"db.*": {"zscore": 0.9}}')
    assert resolve_threshold("http.latency", "zscore", 0.8) == 0.8


def test_invalid_json_fails_safe_to_default(monkeypatch):
    _with_thresholds(monkeypatch, "{not valid json")
    assert resolve_threshold("cpu.usage", "zscore", 0.8) == 0.8


def test_non_object_json_fails_safe(monkeypatch):
    _with_thresholds(monkeypatch, "[1, 2, 3]")
    assert resolve_threshold("cpu.usage", "zscore", 0.8) == 0.8


def test_bad_value_skipped(monkeypatch):
    # A non-numeric threshold for one detector is dropped, not fatal.
    _parse.cache_clear()
    pairs = _parse('{"cpu.usage": {"zscore": "high"}}')
    assert pairs == ()


def test_parse_coerces_ints_to_float():
    _parse.cache_clear()
    pairs = _parse('{"cpu.usage": {"zscore": 1}}')
    assert pairs == (("cpu.usage", {"zscore": 1.0}),)
