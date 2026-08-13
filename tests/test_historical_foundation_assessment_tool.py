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
    assert assessment.SPENT_PLAN_IDS == {
        "13042ecf4129d52d08b5c2a61653ec5993742c5f4831beb862bfc4f9a9f2d347",
        "2dcba99c2df14eae147994ae2df3c91218886dcf9059c76ab03e8ea823285222",
    }
    for spent in assessment.SPENT_PLAN_IDS:
        with pytest.raises(
            assessment.AssessmentError,
            match="assessment plan invocation is already spent",
        ):
            assessment._require_unspent_plan_id(spent)


def test_successful_recovery_plan_is_content_addressed_and_now_spent() -> None:
    root = assessment.EXPECTED_ROOT.resolve(strict=True)
    plan = json.loads(
        (root / "config" / "historical_foundation_assessment_plan_v1.json").read_text(
            encoding="utf-8"
        )
    )

    assert plan["recovery_authorized"] is True
    assert plan["authorization"]["approval_line"] == "Approve"
    unsigned = {key: value for key, value in plan.items() if key != "plan_id"}
    assert plan["plan_id"] == assessment._sha256_bytes(
        assessment._canonical_bytes(unsigned)
    )
    with pytest.raises(
        assessment.AssessmentError,
        match="assessment plan invocation is already spent",
    ):
        assessment._validate_plan(root, plan)
