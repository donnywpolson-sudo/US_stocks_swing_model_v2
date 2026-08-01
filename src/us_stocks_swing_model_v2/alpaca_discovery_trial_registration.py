"""Fail-closed production registration preflight for the discovery trial."""

from __future__ import annotations

from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError


def build_trial_registration_preflight(three_class_trial_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Report the exact production-registry blocker without writing a trial.

    `TrialRegistry` is intentionally synthetic-only.  A real historical trial
    cannot be represented by a local hash-chain file as if it were immutable
    external registration evidence.
    """

    if type(three_class_trial_plan) is not dict or three_class_trial_plan.get("mode") != "ALPACA_DISCOVERY_THREE_CLASS_TRIAL_CONTRACT_PLAN_ONLY":
        raise ContractError("three-class trial plan differs")
    if (
        three_class_trial_plan.get("registration") != {
            "trial_write_authorized": False,
            "real_history_execution_authorized": False,
            "required_evidence_class": "REGISTERED_HISTORICAL_DISCOVERY",
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
        "registration_state": "BLOCKED_NO_PRODUCTION_IMMUTABLE_REGISTRY",
        "production_registry_support": False,
        "synthetic_registry_support": True,
        "required_capability": "external_immutable_trial_registry_loader",
        "forbidden_substitutes": ["local_hash_chain_registry", "synthetic_registry_permit", "generated_trial_receipt"],
        "writes": {"trial_registry": False, "ledger": False, "evaluation": False},
        "rows_opened": 0,
        "stop_conditions": ["production registry remains synthetic-only", "attempt to treat local evidence as external immutable registration", "trial write or evaluation request before capability exists"],
    }
    return {**unsigned, "registration_preflight_id": sha256_bytes(canonical_json_bytes(unsigned))}
