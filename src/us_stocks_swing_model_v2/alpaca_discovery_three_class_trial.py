"""Fixed, non-executable three-class discovery trial contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .common import canonical_json_bytes, reject_link, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_discovery_three_class_trial_contract.json"
READINESS_POLICY_PATH = "config/research_readiness_contract.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_three_class_trial_contract(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ContractError("three-class trial contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise IntegrityError("three-class trial contract ID differs")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "ALPACA_DISCOVERY_THREE_CLASS_TRIAL_CONTRACT_PLAN_ONLY"
        or payload.get("hypothesis_id") != "alpaca_raw_price_proxy_three_class_fixed_ridge_v1"
        or payload.get("model") != {"family": "linear_distribution_v1_fixed_ridge", "ridge_alpha": 1.0, "hyperparameter_tuning": False, "feature_selection": False}
        or payload.get("target") != {"semantics": "ALPACA_RAW_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1", "classes": ["down", "neutral", "up"], "neutral_band": 0.005}
        or payload.get("metrics_and_costs") != {"primary_forecast_metric": "multiclass_log_loss", "stress_cost_basis_points_one_way": 25, "policy_path": READINESS_POLICY_PATH}
        or payload.get("wfa") != {"outer_protocol": "rolling_origin", "purge_sessions": 5, "embargo_sessions": 5, "fold_local_transforms_required": True}
        or payload.get("claims") != {"historical_proxy": True, "trusted_result_claim": False, "alpha_claim": False, "candidate_sealing": False, "training_or_evaluation_authorized": False}
        or payload.get("registration") != {"trial_write_authorized": False, "real_history_execution_authorized": False, "required_evidence_class": "UNREGISTERED_HISTORICAL_DISCOVERY", "external_registry_required": False}
    ):
        raise ContractError("three-class trial contract differs")
    return {**payload, "contract_id": contract_id}


def build_three_class_trial_plan(feature_wfa_plan: Mapping[str, Any], *, repo_root: Path | None = None) -> dict[str, Any]:
    """Bind the fixed hypothesis to the prior feature/outcome WFA plan only."""

    contract = load_three_class_trial_contract(repo_root)
    if type(feature_wfa_plan) is not dict or feature_wfa_plan.get("mode") != "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY":
        raise ContractError("three-class trial WFA input differs")
    if feature_wfa_plan.get("claims") != {"historical_proxy": True, "canonical_target_equivalent": False, "trusted_sleeve_eligible": False, "alpha_claim": False}:
        raise ContractError("three-class trial caveats differ")
    if feature_wfa_plan.get("features", {}).get("feature_names") != ["d0_raw_intraday_return", "trailing_5_session_raw_return", "trailing_5_session_raw_volatility"]:
        raise ContractError("three-class trial features differ")
    if feature_wfa_plan.get("wfa") != {"state": "CHRONOLOGICAL_PROXY_DISCOVERY_EXECUTION_PLANNED_UNREGISTERED", "outer_protocol": "rolling_origin", "purge_sessions": 5, "embargo_sessions": 5, "fold_local_transforms_required": True, "external_registry_required": False, "real_history_execution_authorized": False, "training_or_evaluation_authorized": False}:
        raise ContractError("three-class trial WFA safeguards differ")
    for field in ("feature_wfa_plan_id",):
        value = feature_wfa_plan.get(field)
        if type(value) is not str or len(value) != 64:
            raise ContractError("three-class trial WFA identity differs")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    unsigned = {
        "schema_version": 1,
        "mode": contract["mode"],
        "contract_id": contract["contract_id"],
        "feature_wfa_plan_id": feature_wfa_plan["feature_wfa_plan_id"],
        "feature_release": feature_wfa_plan["feature_release"],
        "proxy_outcome_release": feature_wfa_plan["proxy_outcome_release"],
        "hypothesis_id": contract["hypothesis_id"],
        "model": contract["model"],
        "target": contract["target"],
        "metrics_and_costs": {**contract["metrics_and_costs"], "policy_sha256": sha256_file(root / READINESS_POLICY_PATH)},
        "wfa": contract["wfa"],
        "claims": contract["claims"],
        "registration": contract["registration"],
        "validation_scope": {"feature_rows_opened": 0, "outcome_rows_opened": 0, "trial_written": False},
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "three_class_trial_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
