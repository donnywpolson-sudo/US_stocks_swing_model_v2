from __future__ import annotations

import pytest

from us_stocks_swing_model_v2.alpaca_discovery_trial_registration import build_trial_registration_preflight
from us_stocks_swing_model_v2.errors import ContractError


def _trial_plan() -> dict[str, object]:
    return {
        "mode": "ALPACA_DISCOVERY_THREE_CLASS_TRIAL_CONTRACT_PLAN_ONLY",
        "three_class_trial_plan_id": "a" * 64,
        "registration": {"trial_write_authorized": False, "real_history_execution_authorized": False, "required_evidence_class": "REGISTERED_HISTORICAL_DISCOVERY", "external_registry_required": True, "registration_eligibility": "INELIGIBLE_LEGACY_CAVEATED_RELEASES"},
        "claims": {"historical_proxy": True, "trusted_result_claim": False},
    }


def test_registration_preflight_blocks_ineligible_releases_before_external_registration() -> None:
    preflight = build_trial_registration_preflight(_trial_plan())
    assert preflight["registration_state"] == "BLOCKED_LEGACY_CAVEATED_RELEASES_INELIGIBLE_FOR_EXTERNAL_PREREGISTRATION"
    assert preflight["external_registry_required"] is True
    assert preflight["required_capability"] == "external_preregistration_with_registration_eligible_releases"
    assert preflight["trusted_registry_support"] is False
    assert preflight["writes"] == {"trial_registry": False, "ledger": False, "evaluation": False}
    assert len(preflight["registration_preflight_id"]) == 64


def test_registration_preflight_rejects_a_write_enabled_trial() -> None:
    plan = _trial_plan()
    plan["registration"] = {"trial_write_authorized": True, "real_history_execution_authorized": False, "required_evidence_class": "REGISTERED_HISTORICAL_DISCOVERY"}
    with pytest.raises(ContractError, match="safeguards"):
        build_trial_registration_preflight(plan)
