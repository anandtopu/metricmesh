"""Unit tests for the MM-10.4 detector A/B report assembler (pure, no DB)."""

from __future__ import annotations

from api.routes.anomalies import _assemble_detector_report

_COUNTS = [
    {"metric_name": "cpu.usage", "detector": "isolation_forest",
     "tp": 8, "fp": 2, "unlabeled": 0, "total": 10},
    {"metric_name": "cpu.usage", "detector": "statistical",
     "tp": 3, "fp": 7, "unlabeled": 0, "total": 10},
    {"metric_name": "mem.usage", "detector": "prophet",
     "tp": 0, "fp": 0, "unlabeled": 5, "total": 5},
]
_RECALL = [
    {"metric_name": "cpu.usage", "detector": "isolation_forest",
     "covered_buckets": 9, "total_buckets": 10},
    {"metric_name": "cpu.usage", "detector": "statistical",
     "covered_buckets": 3, "total_buckets": 10},
]


def test_precision_and_comparative_recall():
    report = _assemble_detector_report(_COUNTS, _RECALL, bucket_minutes=5)
    cpu = next(m for m in report["metrics"] if m["metric_name"] == "cpu.usage")
    iso = next(d for d in cpu["detectors"] if d["detector"] == "isolation_forest")
    stat = next(d for d in cpu["detectors"] if d["detector"] == "statistical")

    assert iso["precision"] == 0.8 and iso["comparative_recall"] == 0.9
    assert stat["precision"] == 0.3 and stat["comparative_recall"] == 0.3
    assert cpu["confirmed_tp_events"] == 10


def test_recommended_detector_is_highest_precision():
    report = _assemble_detector_report(_COUNTS, _RECALL, bucket_minutes=5)
    cpu = next(m for m in report["metrics"] if m["metric_name"] == "cpu.usage")
    assert cpu["recommended_detector"] == "isolation_forest"
    # Detectors are ranked best-first.
    assert cpu["detectors"][0]["detector"] == "isolation_forest"


def test_unlabeled_metric_has_no_precision_or_recommendation():
    report = _assemble_detector_report(_COUNTS, _RECALL, bucket_minutes=5)
    mem = next(m for m in report["metrics"] if m["metric_name"] == "mem.usage")
    assert mem["recommended_detector"] is None
    assert mem["confirmed_tp_events"] == 0
    prophet = mem["detectors"][0]
    assert prophet["precision"] is None
    assert prophet["comparative_recall"] is None


def test_metrics_sorted_and_notes_present():
    report = _assemble_detector_report(_COUNTS, _RECALL, bucket_minutes=15)
    assert [m["metric_name"] for m in report["metrics"]] == ["cpu.usage", "mem.usage"]
    assert report["bucket_minutes"] == 15
    # The recall caveat must be stated, not hidden.
    assert "not true recall" in report["notes"]


def test_empty_input_yields_empty_report():
    report = _assemble_detector_report([], [], bucket_minutes=5)
    assert report["metrics"] == []


def test_all_false_positive_detector_is_not_recommended():
    # A detector whose only labeled alerts were false positives (precision 0)
    # must not be "recommended" just because it's the sole labeled one.
    counts = [
        {"metric_name": "db.query", "detector": "statistical",
         "tp": 0, "fp": 4, "unlabeled": 0, "total": 4},
        {"metric_name": "db.query", "detector": "isolation_forest",
         "tp": 0, "fp": 0, "unlabeled": 3, "total": 3},
    ]
    report = _assemble_detector_report(counts, [], bucket_minutes=5)
    db = report["metrics"][0]
    assert db["recommended_detector"] is None
    stat = next(d for d in db["detectors"] if d["detector"] == "statistical")
    assert stat["precision"] == 0.0
