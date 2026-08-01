"""Fail-closed unregistered-discovery execution preflight."""

from __future__ import annotations

from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError


def build_trial_registration_preflight(three_class_trial_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Keep external registration out of the discovery-only execution lane."""

    if type(three_class_trial_plan) is not dict or three_class_trial_plan.get("mode") != "ALPACA_DISCOVERY_THREE_CLASS_TRIAL_CONTRACT_PLAN_ONLY":
        raise ContractError("three-class trial plan differs")
    if (
        three_class_trial_plan.get("registration") != {
            "trial_write_authorized": False,
            "real_history_execution_authorized": False,
            "required_evidence_class": "UNREGISTERED_HISTORICAL_DISCOVERY",
            "external_registry_required": False,
        }
        or three_class_trial_plan.get("claims", {}).get("historical_proxy") is not True
        or three_class_trial_plan.get("claims", {}).get("trusted_result_claim") is not False
    ):
        raise ContractError("three-class trial registration safeguards differ")
    plan_id = three_class_trial_plan.get("three_class_trial_plan_id")
    require_sha256(plan_id, "three_class_trial_plan_id")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_DISCOVERY_TRIAL_REGISTRATION_PREFLIGHT_ONLY",
        "three_class_trial_plan_id": plan_id,
        "registration_state": "UNREGISTERED_DISCOVERY_EXECUTION_REQUIRES_SEPARATE_AUTHORIZATION",
        "external_registry_required": False,
        "trusted_registry_support": False,
        "required_capability": "separately_authorized_unregistered_discovery_execution",
        "forbidden_claims": ["registered_historical_discovery", "trusted_result", "alpha", "candidate_sealing"],
        "writes": {"trial_registry": False, "ledger": False, "evaluation": False},
        "rows_opened": 0,
        "stop_conditions": ["trial write request", "real-history discovery execution without separate authorization", "attempt to claim a registered, trusted, alpha, or candidate result"],
    }
    return {**unsigned, "registration_preflight_id": sha256_bytes(canonical_json_bytes(unsigned))}
