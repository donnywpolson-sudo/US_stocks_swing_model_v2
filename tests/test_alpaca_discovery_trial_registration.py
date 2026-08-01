from __future__ import annotations

import pytest

from us_stocks_swing_model_v2.alpaca_discovery_trial_registration import build_trial_registration_preflight
from us_stocks_swing_model_v2.errors import ContractError


def _trial_plan() -> dict[str, object]:
    return {
        "mode": "ALPACA_DISCOVERY_THREE_CLASS_TRIAL_CONTRACT_PLAN_ONLY",
        "three_class_trial_plan_id": "a" * 64,
        "registration": {"trial_write_authorized": False, "real_history_execution_authorized": False, "required_evidence_class": "REGISTERED_HISTORICAL_DISCOVERY"},
        "claims": {"historical_proxy": True, "trusted_result_claim": False},
    }


def test_registration_preflight_reports_the_production_registry_blocker() -> None:
    preflight = build_trial_registration_preflight(_trial_plan())
    assert preflight["registration_state"] == "BLOCKED_NO_PRODUCTION_IMMUTABLE_REGISTRY"
    assert preflight["writes"] == {"trial_registry": False, "ledger": False, "evaluation": False}
    assert len(preflight["registration_preflight_id"]) == 64


def test_registration_preflight_rejects_a_write_enabled_trial() -> None:
    plan = _trial_plan()
    plan["registration"] = {"trial_write_authorized": True, "real_history_execution_authorized": False, "required_evidence_class": "REGISTERED_HISTORICAL_DISCOVERY"}
    with pytest.raises(ContractError, match="safeguards"):
        build_trial_registration_preflight(plan)
