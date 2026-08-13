from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import inspect_historical_foundation as assessment


def test_receipt_census_counts_nullable_field_presence(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "first.json").write_text(
        json.dumps(
            {
                "source": "candidate_source",
                "receipt_time": "2026-01-02T12:00:00Z",
                "nullable_field": None,
            }
        ),
        encoding="utf-8",
    )
    (receipts / "second.json").write_text(
        json.dumps(
            {
                "source": "candidate_source",
                "receipt_time": "2026-01-03T12:00:00Z",
                "nullable_field": None,
            }
        ),
        encoding="utf-8",
    )

    result = assessment._receipt_census(
        tmp_path,
        {
            "directory": "receipts",
            "maximum_files": 2,
            "maximum_file_bytes": 1024,
            "time_fields": ["receipt_time"],
        },
        assessment.ReadBudget(2048),
    )

    assert result["json_file_count"] == 2
    assert result["field_presence"] == {
        "nullable_field": 2,
        "receipt_time": 2,
        "source": 2,
    }
    assert result["source_counts"] == {"candidate_source": 2}
    assert result["time_ranges"] == {
        "receipt_time": {
            "minimum": "2026-01-02T12:00:00Z",
            "maximum": "2026-01-03T12:00:00Z",
        }
    }


def test_spent_assessment_plan_id_is_fail_closed() -> None:
    spent = next(iter(assessment.SPENT_PLAN_IDS))

    with pytest.raises(
        assessment.AssessmentError,
        match="assessment plan invocation is already spent",
    ):
        assessment._require_unspent_plan_id(spent)


def test_recovery_plan_binds_explicit_authorization_and_is_valid() -> None:
    root = assessment.EXPECTED_ROOT.resolve(strict=True)
    plan = json.loads(
        (root / "config" / "historical_foundation_assessment_plan_v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert plan["recovery_authorized"] is True
    assert plan["authorization"]["approval_line"] == "Approve"
    assert assessment._validate_plan(root, plan) == plan
