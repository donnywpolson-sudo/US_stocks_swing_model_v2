from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from us_stocks_swing_model_v2.alpaca_free_bounded import EvidenceClass, PositionSide
from us_stocks_swing_model_v2.bounded_reporting import (
    ReadinessInputs,
    assess_readiness,
    build_event_status_report,
    validate_readiness_payload,
)
from us_stocks_swing_model_v2.cli.alpaca_free_bounded import main
from us_stocks_swing_model_v2.long_short import resolve_fixed_horizon_outcome


REPO = Path(__file__).resolve().parents[1]
ENTRY = date(2026, 8, 3)
EXIT = date(2026, 8, 7)


def _outcome(side, exit_price, *, nonterminal=False, evidence=EvidenceClass.HISTORICAL_RECONSTRUCTED):
    return resolve_fixed_horizon_outcome(
        side=side,
        evidence_class=evidence,
        entry_session=ENTRY,
        exit_session=EXIT,
        entry_price=100,
        exit_price=exit_price,
        nonterminal_missing=nonterminal,
    )


def test_reporting_reconciles_every_observation_and_separates_required_dimensions() -> None:
    rows = [
        _outcome(PositionSide.LONG, 110),
        _outcome(PositionSide.SHORT, 90),
        _outcome(PositionSide.LONG, None),
        _outcome(PositionSide.SHORT, None),
        _outcome(
            PositionSide.LONG,
            None,
            nonterminal=True,
            evidence=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
        ),
    ]
    report = build_event_status_report(rows)
    assert report["denominator"] == {
        "label": "ALL_ADMITTED_OBSERVATIONS",
        "count": 5,
        "resolved": 2,
        "terminal_unresolved": 2,
        "nonterminal_missing_data": 1,
        "reconciles": True,
    }
    assert report["position_side_counts"] == {"LONG": 3, "SHORT": 2}
    assert report["evidence_class_counts"] == {
        "HISTORICAL_RECONSTRUCTED": 4,
        "PROSPECTIVE_AS_OBSERVED": 1,
    }
    assert report["calendar_year_counts"] == {"2026": 5}
    assert report["stress_scenario_counts"] == {
        "LONG_UNRESOLVED_STRESS_NEGATIVE_100": 1,
        "SHORT_UNRESOLVED_STRESS_2X": 1,
        "SHORT_UNRESOLVED_STRESS_3X": 1,
        "SHORT_UNRESOLVED_STRESS_5X": 1,
    }
    assert report["unresolved_short_has_finite_lower_bound"] is False
    assert report["result_label"] == "GROSS OF STOCK-BORROW AND LOCATE COSTS"


def _readiness(**overrides):
    payload = dict(
        adapters_implemented=True,
        configuration_validated=True,
        credential_redaction_validated=True,
        append_only_receipts_validated=True,
        complete_pagination_validated=True,
        retry_resume_validated=True,
        feed_adjustment_enforced=True,
        evidence_classes_validated=True,
        identity_continuity_validated=True,
        universe_logic_validated=True,
        long_short_outcomes_validated=True,
        stress_reporting_validated=True,
        denominator_reconciliation_validated=True,
        synthetic_tests_passed=True,
    )
    payload.update(overrides)
    return assess_readiness(ReadinessInputs(**payload))


def test_offline_readiness_reaches_infrastructure_only_and_retains_blocks() -> None:
    report = _readiness()
    assert report["data_infrastructure_ready"]
    assert report["historical_universe_status"] == "candidate"
    assert "DATA_INFRASTRUCTURE_READY" in report["states"]
    assert "LIVE_SOURCE_VALIDATION_PENDING" in report["states"]
    assert "HISTORICAL_RESEARCH_READY" not in report["states"]
    assert report["training"] == report["evaluation"] == "BLOCKED"
    validate_readiness_payload(report)


def test_live_readiness_states_remain_distinct_and_training_stays_blocked() -> None:
    limited = _readiness(alpaca_live_validated=True)
    assert "HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS" in limited["states"]
    ready = _readiness(
        alpaca_live_validated=True,
        alpha_vantage_semantics_validated=True,
        prospective_daily_capture_validated=True,
        prospective_short_gate_validated_live=True,
    )
    assert "HISTORICAL_RESEARCH_READY" in ready["states"]
    assert "PROSPECTIVE_CAPTURE_READY" in ready["states"]
    assert "PROSPECTIVE_RESEARCH_READY" in ready["states"]
    assert "TRAINING_BLOCKED" in ready["states"]
    assert "EVALUATION_BLOCKED" in ready["states"]


def test_cli_validate_plan_capture_diagnostics_and_readiness_are_offline(capsys) -> None:
    assert main(["validate-config"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "PASS"
    assert main(["probe-capabilities", "--as-of", "2026-08-10"]) == 0
    probe = json.loads(capsys.readouterr().out)
    assert probe["state"] == "PLAN_ONLY_NO_NETWORK"
    assert probe["credentials_read"] is False
    assert main([
        "plan-backfill", "--symbol", "AAPL", "--start", "2016-01-01",
        "--end", "2016-01-02", "--requested-at", "2026-08-10T20:00:00Z",
    ]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["execution_authorized"] is False
    assert plan["pending_checkpoints"] == 1
    assert main(["capture-premarket", "--as-of", "2026-08-10"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "PLAN_ONLY_NO_NETWORK"
    assert main(["capture-completed-session", "--session", "2026-08-07", "--symbol", "AAPL"]) == 0
    assert len(json.loads(capsys.readouterr().out)["ordered_plans"]) == 2
    assert main(["known-case-diagnostics"]) == 0
    diagnostic = json.loads(capsys.readouterr().out)
    assert len(diagnostic["cases"]) == 5
    assert diagnostic["state"] == "NOT_RUN_NO_ACTION_SPECIFIC_NETWORK_AUTHORIZATION"
    assert main(["check-readiness", "--synthetic-tests-passed"]) == 0
    readiness = json.loads(capsys.readouterr().out)
    assert "DATA_INFRASTRUCTURE_READY" in readiness["states"]
    assert "TRAINING_BLOCKED" in readiness["states"]


def test_cli_resume_preserves_completed_checkpoint(capsys) -> None:
    args = [
        "plan-backfill", "--symbol", "AAPL", "--start", "2016-01-01",
        "--end", "2016-01-02", "--requested-at", "2026-08-10T20:00:00Z", "--full",
    ]
    main(args)
    plan = json.loads(capsys.readouterr().out)
    unit_id = plan["units"][0]["unit_id"]
    main([
        "resume-backfill", "--symbol", "AAPL", "--start", "2016-01-01",
        "--end", "2016-01-02", "--requested-at", "2026-08-10T20:00:00Z",
        "--completed-unit-id", unit_id, "--full",
    ])
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["completed_checkpoints"] == 1
    assert resumed["pending_checkpoints"] == 0


def test_documentation_and_entrypoint_cover_required_user_commands() -> None:
    doc = (REPO / "docs/ALPACA_FREE_BOUNDED_V1.md").read_text(encoding="utf-8")
    for command in (
        "validate-config",
        "probe-capabilities",
        "known-case-diagnostics",
        "plan-backfill",
        "resume-backfill",
        "capture-premarket",
        "capture-completed-session",
        "rebuild-universe",
        "validate-receipts",
        "coverage-report",
        "event-status-report",
        "check-readiness",
    ):
        assert command in doc
    assert "python -m us_stocks_swing_model_v2.cli.alpaca_free_bounded" in doc
    assert "APCA_API_KEY_ID" in doc
    assert "ALPHA_VANTAGE_API_KEY" in doc
    assert "automatic" in doc.lower() and "full backfill" in doc.lower()
