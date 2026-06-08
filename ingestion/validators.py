from __future__ import annotations

import math
import time
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


class MetricPoint(BaseModel):
    """
    A single time-series data point.
    strict=True prevents silent coercion (e.g. "3" -> 3.0).
    frozen=True makes instances hashable and safe to cache.

    Python skill: Pydantic v2 strict mode, field_validator, model_validator,
    Annotated types with Field constraints.
    """
    model_config = {"strict": True, "frozen": True}

    metric_name: Annotated[
        str,
        Field(
            min_length=1,
            max_length=128,
            pattern=r"^[a-z_][a-z0-9_.]*$",
            description="Dot-separated lowercase metric name, e.g. app.request.latency",
        ),
    ]
    value: float
    timestamp: float = Field(default_factory=time.time)
    labels: dict[str, str] = Field(default_factory=dict)
    source: str = Field(default="api", max_length=64)

    @field_validator("value")
    @classmethod
    def value_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"value must be finite, got {v!r}")
        return v

    @model_validator(mode="after")
    def validate_label_constraints(self) -> MetricPoint:
        if len(self.labels) > 20:
            raise ValueError("max 20 labels per metric point")
        for k, v in self.labels.items():
            if len(k) > 64:
                raise ValueError(f"label key too long: {k!r}")
            if len(v) > 256:
                raise ValueError(f"label value too long for key {k!r}")
        return self


class MetricBatch(BaseModel):
    """Batch ingest — validates all points atomically. Rejects whole batch on any error."""
    points: list[MetricPoint] = Field(min_length=1, max_length=10_000)
    source_id: str = Field(max_length=128)


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    errors: list[str] = Field(default_factory=list)
