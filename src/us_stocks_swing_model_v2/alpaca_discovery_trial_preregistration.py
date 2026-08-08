"""Plan-only preregistration template for the caveated Alpaca discovery lane."""

from __future__ import annotations

from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError


REQUIRED_UNSELECTED_FIELDS = (
    "model_family",
    "primary_metric",
    "cost_policy",
    "primary_gate",
    "robustness_policy",
    "trial_family",
)


def build_trial_preregistration_template(feature_wfa_plan: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze known caveated inputs and refuse to create a trial specification.

    A model, metrics, costs, and gates are hypothesis-defining choices.  This
    planner records them as deliberately unselected rather than silently
    choosing a research candidate from already materialized outcomes.
    """

    if type(feature_wfa_plan) is not dict or set(feature_wfa_plan) != {
        "schema_version", "mode", "contract_id", "feature_release",
        "proxy_outcome_release", "features", "wfa", "claims",
        "validation_scope", "required_later_authority", "stop_conditions",
        "feature_wfa_plan_id",
    }:
        raise ContractError("feature/WFA plan fields differ")
    if (
        feature_wfa_plan["schema_version"] != 1
        or feature_wfa_plan["mode"] != "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY"
        or feature_wfa_plan["claims"] != {
            "historical_proxy": True,
            "canonical_target_equivalent": False,
            "trusted_sleeve_eligible": False,
            "alpha_claim": False,
        }
        or feature_wfa_plan["wfa"].get("outer_protocol") != "rolling_origin"
        or feature_wfa_plan["wfa"].get("purge_sessions") != 5
        or feature_wfa_plan["wfa"].get("embargo_sessions") != 5
        or feature_wfa_plan["wfa"].get("state") != "BLOCKED_EXTERNAL_PREREGISTRATION_REQUIRES_ELIGIBLE_RELEASES"
        or feature_wfa_plan["wfa"].get("external_registry_required") is not True
        or feature_wfa_plan["wfa"].get("registration_eligibility") != "INELIGIBLE_LEGACY_CAVEATED_RELEASES"
        or feature_wfa_plan["wfa"].get("real_history_execution_authorized") is not False
    ):
        raise ContractError("feature/WFA plan differs")
    for field in ("contract_id", "feature_wfa_plan_id"):
        require_sha256(feature_wfa_plan[field], field)
    for release_name in ("feature_release", "proxy_outcome_release"):
        release = feature_wfa_plan[release_name]
        if type(release) is not dict:
            raise ContractError("trial input release differs")
        for field in ("release_id", "manifest_sha256"):
            require_sha256(release.get(field), f"{release_name}.{field}")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_DISCOVERY_TRIAL_PREREGISTRATION_TEMPLATE_ONLY",
        "feature_wfa_plan_id": feature_wfa_plan["feature_wfa_plan_id"],
        "data_releases": [
            feature_wfa_plan["feature_release"],
            feature_wfa_plan["proxy_outcome_release"],
        ],
        "fixed_inputs": {
            "feature_names": feature_wfa_plan["features"]["feature_names"],
            "target_semantics": "ALPACA_RAW_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1",
            "outer_protocol": "rolling_origin",
            "purge_sessions": 5,
            "embargo_sessions": 5,
            "fold_local_transforms_required": True,
            "evidence_class": "REGISTERED_HISTORICAL_DISCOVERY",
            "historical_proxy": True,
            "trusted_result_claim": False,
        },
        "unselected_hypothesis_fields": list(REQUIRED_UNSELECTED_FIELDS),
        "registration": {
            "permitted": False,
            "reason": "LEGACY_CAVEATED_RELEASES_INELIGIBLE_FOR_EXTERNAL_REGISTRATION",
            "external_registry_required": True,
            "registration_eligibility": "INELIGIBLE_LEGACY_CAVEATED_RELEASES",
            "trial_written": False,
            "rows_opened": 0,
            "training_or_evaluation": False,
        },
    }
    return {**unsigned, "preregistration_template_id": sha256_bytes(canonical_json_bytes(unsigned))}
