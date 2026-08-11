from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .alpaca_free_bounded import EvidenceClass, PositionSide
from .errors import ContractError, IntegrityError
from .long_short import (
    BuyToCoverInstruction,
    ProfileOutcomeStatus,
    PositionOutcome,
    ShortEligibilityDecision,
    unresolved_stress,
)


@dataclass(frozen=True)
class AcquisitionCoverage:
    provider: str
    requested_start: str
    requested_end: str
    actual_first_available: str | None
    actual_last_available: str | None
    symbols_requested: int
    symbols_returned: int
    pages_expected_or_bounded: int
    pages_received: int
    terminal_page_observed: bool
    missing_sessions: int
    quarantined_records: int
    unknown_security_types: int
    later_delisted_known_cases_present: int
    corporate_action_records: int
    borrow_status_observed: int
    evidence_class_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, object]:
        return dict(self.__dict__)


def build_event_status_report(
    outcomes: Iterable[PositionOutcome],
    *,
    acquisition_coverage: Iterable[AcquisitionCoverage] = (),
    short_eligibility_decisions: Iterable[ShortEligibilityDecision] = (),
    buy_to_cover_instructions: Iterable[BuyToCoverInstruction] = (),
    order_rejection_count: int = 0,
) -> dict[str, object]:
    rows = tuple(outcomes)
    for row in rows:
        row.validate()
    by_side = Counter(row.side.value for row in rows)
    by_evidence = Counter(row.evidence_class.value for row in rows)
    by_status = Counter(row.status.value for row in rows)
    by_year = Counter(str(row.entry_session.year) for row in rows)
    by_layer = Counter(row.layer for row in rows)
    resolved_statuses = {
        ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME,
        ProfileOutcomeStatus.RESOLVED_CASH_MERGER,
        ProfileOutcomeStatus.RESOLVED_STOCK_MERGER,
        ProfileOutcomeStatus.RESOLVED_STOCK_AND_CASH_MERGER,
        ProfileOutcomeStatus.RESOLVED_REDEMPTION,
        ProfileOutcomeStatus.RESOLVED_WORTHLESS_REMOVAL,
    }
    resolved = sum(row.status in resolved_statuses for row in rows)
    terminal_unresolved = sum(
        row.status is ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED for row in rows
    )
    nonterminal_missing = sum(
        row.status is ProfileOutcomeStatus.DATA_MISSING_NONTERMINAL for row in rows
    )
    if resolved + terminal_unresolved + nonterminal_missing != len(rows):
        raise IntegrityError("outcome denominator does not reconcile")
    stresses = [scenario for row in rows for scenario in unresolved_stress(row)]
    stress_counts = Counter(str(item["scenario"]) for item in stresses)
    short_decisions = tuple(short_eligibility_decisions)
    short_exclusions = Counter(
        reason for decision in short_decisions for reason in decision.reason_codes
    )
    degradation_instructions = tuple(buy_to_cover_instructions)
    if type(order_rejection_count) is not int or order_rejection_count < 0:
        raise ContractError("order rejection count must be a nonnegative integer")
    historical_shortability_unverified = sum(
        row.side is PositionSide.SHORT
        and row.evidence_class is EvidenceClass.HISTORICAL_RECONSTRUCTED
        and "SHORTABILITY_UNVERIFIED_HISTORICAL" in row.labels
        for row in rows
    )
    report = {
        "denominator": {
            "label": "ALL_ADMITTED_OBSERVATIONS",
            "count": len(rows),
            "resolved": resolved,
            "terminal_unresolved": terminal_unresolved,
            "nonterminal_missing_data": nonterminal_missing,
            "reconciles": True,
        },
        "position_side_counts": dict(sorted(by_side.items())),
        "evidence_class_counts": dict(sorted(by_evidence.items())),
        "evidence_side_counts": dict(sorted(Counter(
            f"{row.evidence_class.value}:{row.side.value}" for row in rows
        ).items())),
        "event_status_counts": dict(sorted(by_status.items())),
        "calendar_year_counts": dict(sorted(by_year.items())),
        "result_layer_counts": dict(sorted(by_layer.items())),
        "historical_shortability_unverified_count": historical_shortability_unverified,
        "prospective_easy_to_borrow_count": sum(decision.eligible for decision in short_decisions),
        "short_exclusion_reason_counts": dict(sorted(short_exclusions.items())),
        "hard_to_borrow_exclusion_count": short_exclusions["SHORT_INELIGIBLE_HTB"],
        "unknown_borrow_exclusion_count": short_exclusions["SHORT_INELIGIBLE_UNKNOWN_BORROW"],
        "stale_borrow_snapshot_exclusion_count": short_exclusions["SHORT_INELIGIBLE_STALE_SNAPSHOT"],
        "whole_share_sizing_exclusion_count": short_exclusions["SHORT_INELIGIBLE_FRACTIONAL_ONLY"],
        "borrow_status_degradation_count": len(degradation_instructions),
        "order_rejection_count": order_rejection_count,
        "stress_scenario_counts": dict(sorted(stress_counts.items())),
        "unresolved_short_has_finite_lower_bound": False,
        "result_label": "GROSS OF STOCK-BORROW AND LOCATE COSTS",
        "stock_borrow_fee": 0.0,
        "locate_fee": 0.0,
        "configured_execution_costs_total": sum(row.configured_execution_costs for row in rows),
        "not_complete_total_return": True,
        "provider_acquisition_coverage": [item.as_dict() for item in acquisition_coverage],
    }
    return report


@dataclass(frozen=True)
class ReadinessInputs:
    adapters_implemented: bool
    configuration_validated: bool
    credential_redaction_validated: bool
    append_only_receipts_validated: bool
    complete_pagination_validated: bool
    retry_resume_validated: bool
    feed_adjustment_enforced: bool
    evidence_classes_validated: bool
    identity_continuity_validated: bool
    universe_logic_validated: bool
    long_short_outcomes_validated: bool
    stress_reporting_validated: bool
    denominator_reconciliation_validated: bool
    synthetic_tests_passed: bool
    alpaca_live_validated: bool = False
    alpha_vantage_semantics_validated: bool = False
    prospective_daily_capture_validated: bool = False
    prospective_short_gate_validated_live: bool = False

    def infrastructure_requirements(self) -> dict[str, bool]:
        return {
            key: value
            for key, value in self.__dict__.items()
            if key
            not in {
                "alpaca_live_validated",
                "alpha_vantage_semantics_validated",
                "prospective_daily_capture_validated",
                "prospective_short_gate_validated_live",
            }
        }


def assess_readiness(inputs: ReadinessInputs) -> dict[str, object]:
    requirements = inputs.infrastructure_requirements()
    infrastructure_ready = all(requirements.values())
    states: list[str] = []
    if infrastructure_ready:
        states.append("DATA_INFRASTRUCTURE_READY")
    if not inputs.alpaca_live_validated or not inputs.alpha_vantage_semantics_validated:
        states.append("LIVE_SOURCE_VALIDATION_PENDING")
    historical_universe_status = (
        "established" if inputs.alpha_vantage_semantics_validated else "candidate"
    )
    if inputs.alpaca_live_validated and not inputs.alpha_vantage_semantics_validated:
        states.append("HISTORICAL_RECONSTRUCTED_WITH_LIMITATIONS")
    historical_research_ready = (
        infrastructure_ready
        and inputs.alpaca_live_validated
        and inputs.alpha_vantage_semantics_validated
    )
    if historical_research_ready:
        states.append("HISTORICAL_RESEARCH_READY")
    if inputs.prospective_daily_capture_validated:
        states.append("PROSPECTIVE_CAPTURE_READY")
    if inputs.prospective_daily_capture_validated and inputs.prospective_short_gate_validated_live:
        states.append("PROSPECTIVE_RESEARCH_READY")
    states.extend(("TRAINING_BLOCKED", "EVALUATION_BLOCKED"))
    return {
        "states": states,
        "data_infrastructure_ready": infrastructure_ready,
        "infrastructure_requirements": requirements,
        "live_source_validation_pending": "LIVE_SOURCE_VALIDATION_PENDING" in states,
        "historical_universe_status": historical_universe_status,
        "historical_research_ready": historical_research_ready,
        "prospective_capture_ready": "PROSPECTIVE_CAPTURE_READY" in states,
        "prospective_research_ready": "PROSPECTIVE_RESEARCH_READY" in states,
        "training": "BLOCKED",
        "evaluation": "BLOCKED",
        "implementation_does_not_authorize_research": True,
    }


def validate_readiness_payload(payload: Mapping[str, object]) -> None:
    states = payload.get("states")
    if not isinstance(states, list) or "TRAINING_BLOCKED" not in states or "EVALUATION_BLOCKED" not in states:
        raise ContractError("readiness must retain training and evaluation blocks")
