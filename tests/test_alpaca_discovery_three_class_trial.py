from __future__ import annotations

import pytest

from us_stocks_swing_model_v2.alpaca_discovery_three_class_trial import build_three_class_trial_plan, load_three_class_trial_contract
from us_stocks_swing_model_v2.errors import ContractError


def _wfa_plan() -> dict[str, object]:
    return {"mode": "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY", "feature_wfa_plan_id": "a" * 64, "feature_release": {"release_id": "b" * 64}, "proxy_outcome_release": {"release_id": "c" * 64}, "features": {"feature_names": ["d0_raw_intraday_return", "trailing_5_session_raw_return", "trailing_5_session_raw_volatility"]}, "wfa": {"state": "BLOCKED_EXTERNAL_PREREGISTRATION_REQUIRES_ELIGIBLE_RELEASES", "outer_protocol": "rolling_origin", "purge_sessions": 5, "embargo_sessions": 5, "fold_local_transforms_required": True, "external_registry_required": True, "registration_eligibility": "INELIGIBLE_LEGACY_CAVEATED_RELEASES", "real_history_execution_authorized": False, "training_or_evaluation_authorized": False}, "claims": {"historical_proxy": True, "canonical_target_equivalent": False, "trusted_sleeve_eligible": False, "alpha_claim": False}}


def test_three_class_contract_is_fixed_and_non_executable() -> None:
    contract = load_three_class_trial_contract()
    assert contract["target"]["neutral_band"] == 0.005
    assert contract["model"]["hyperparameter_tuning"] is False
    assert contract["registration"]["trial_write_authorized"] is False
    assert contract["registration"]["external_registry_required"] is True
    assert contract["registration"]["registration_eligibility"] == "INELIGIBLE_LEGACY_CAVEATED_RELEASES"


def test_three_class_plan_binds_wfa_without_rows_or_trial_write() -> None:
    plan = build_three_class_trial_plan(_wfa_plan())
    assert len(plan["three_class_trial_plan_id"]) == 64
    assert plan["metrics_and_costs"]["primary_forecast_metric"] == "multiclass_log_loss"
    assert plan["validation_scope"] == {"feature_rows_opened": 0, "outcome_rows_opened": 0, "trial_written": False}


def test_three_class_plan_rejects_weakened_purge() -> None:
    plan = _wfa_plan()
    plan["wfa"] = {**plan["wfa"], "purge_sessions": 0}
    with pytest.raises(ContractError, match="WFA safeguards"):
        build_three_class_trial_plan(plan)
