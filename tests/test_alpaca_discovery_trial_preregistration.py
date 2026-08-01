from __future__ import annotations

import pytest

from us_stocks_swing_model_v2.alpaca_discovery_trial_preregistration import build_trial_preregistration_template
from us_stocks_swing_model_v2.errors import ContractError


def _wfa_plan() -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY",
        "contract_id": "a" * 64,
        "feature_release": {"release_id": "b" * 64, "manifest_sha256": "c" * 64, "row_count": 1, "event_start": "2020-01-06", "event_end": "2020-01-08"},
        "proxy_outcome_release": {"release_id": "d" * 64, "manifest_sha256": "e" * 64, "row_count": 1, "event_start": "2020-01-02", "event_end": "2020-01-08"},
        "features": {"feature_names": ["d0_raw_intraday_return", "trailing_5_session_raw_return", "trailing_5_session_raw_volatility"]},
        "wfa": {"outer_protocol": "rolling_origin", "purge_sessions": 5, "embargo_sessions": 5},
        "claims": {"historical_proxy": True, "canonical_target_equivalent": False, "trusted_sleeve_eligible": False, "alpha_claim": False},
        "validation_scope": {}, "required_later_authority": {}, "stop_conditions": [],
        "feature_wfa_plan_id": "f" * 64,
    }


def test_template_binds_known_inputs_but_cannot_register_or_evaluate() -> None:
    template = build_trial_preregistration_template(_wfa_plan())
    assert len(template["preregistration_template_id"]) == 64
    assert template["registration"]["permitted"] is False
    assert template["registration"]["rows_opened"] == 0
    assert template["fixed_inputs"]["evidence_class"] == "UNREGISTERED_HISTORICAL_DISCOVERY"
    assert template["unselected_hypothesis_fields"] == [
        "model_family", "primary_metric", "cost_policy", "primary_gate", "robustness_policy", "trial_family"
    ]


def test_template_rejects_a_weakened_wfa_plan() -> None:
    plan = _wfa_plan()
    plan["wfa"] = {"outer_protocol": "rolling_origin", "purge_sessions": 0, "embargo_sessions": 5}
    with pytest.raises(ContractError, match="feature/WFA"):
        build_trial_preregistration_template(plan)
