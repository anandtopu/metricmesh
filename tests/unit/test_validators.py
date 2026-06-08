"""
Unit tests for ingestion validators.
Python skill: pytest parametrize, testing Pydantic strict mode, exception assertions.
"""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from ingestion.validators import MetricBatch, MetricPoint


class TestMetricPoint:
    def test_valid_point(self):
        p = MetricPoint(metric_name="app.latency", value=123.4, source="api")
        assert p.metric_name == "app.latency"
        assert p.value == pytest.approx(123.4)

    @pytest.mark.parametrize("bad_name", [
        "",          # empty
        "A.b",      # uppercase
        "1start",   # starts with digit
        "a" * 129,  # too long
        "has space",
        "has-dash",
    ])
    def test_invalid_metric_names(self, bad_name: str):
        with pytest.raises(ValidationError):
            MetricPoint(metric_name=bad_name, value=1.0)

    @pytest.mark.parametrize("bad_value", [math.inf, -math.inf, math.nan])
    def test_non_finite_values(self, bad_value: float):
        with pytest.raises(ValidationError):
            MetricPoint(metric_name="test.metric", value=bad_value)

    def test_too_many_labels(self):
        with pytest.raises(ValidationError):
            MetricPoint(
                metric_name="test.metric",
                value=1.0,
                labels={f"k{i}": "v" for i in range(21)},
            )

    def test_frozen_immutable(self):
        p = MetricPoint(metric_name="test.metric", value=1.0)
        with pytest.raises(ValueError):   # pydantic ValidationError subclasses it
            p.value = 2.0  # type: ignore[misc]

    def test_strict_mode_rejects_string_value(self):
        with pytest.raises(ValidationError):
            MetricPoint(metric_name="test.metric", value="not_a_float")  # type: ignore[arg-type]


class TestMetricBatch:
    def test_empty_batch_rejected(self):
        with pytest.raises(ValidationError):
            MetricBatch(points=[], source_id="test")

    def test_valid_batch(self):
        points = [MetricPoint(metric_name="a.b", value=float(i)) for i in range(5)]
        batch = MetricBatch(points=points, source_id="test-src")
        assert len(batch.points) == 5
