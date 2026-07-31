from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, sha256_bytes
from .errors import ContractError


POLICY_PATH = Path("config/hfdl_retirement_policy.json")
RETIRED_STATE = "RETIRED_EXCLUDED_PRESERVED_EVIDENCE_ONLY"


def _strict_mapping(
    value: object, expected_keys: set[str], field: str
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ContractError(f"{field} schema differs from the retired HFDL contract")
    return value


def load_hfdl_retirement_policy(
    repository_root: Path,
) -> tuple[dict[str, Any], str]:
    root = Path(repository_root).resolve(strict=True)
    path = root / POLICY_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("HFDL retirement policy is unreadable") from exc
    policy = _strict_mapping(
        payload,
        {
            "schema_version",
            "policy_version",
            "project",
            "source_id",
            "state",
            "replacement_source",
            "blocked_work",
            "preservation",
            "cleanup",
            "authorities",
            "stop_conditions",
        },
        "HFDL retirement policy",
    )
    if (
        policy["schema_version"] != 1
        or policy["policy_version"] != "1.0.0"
        or policy["project"] != "US_stocks_swing_model_v2"
        or policy["source_id"] != "hfdl_legacy_discovery"
        or policy["state"] != RETIRED_STATE
        or policy["replacement_source"] != "alpaca_basic_delayed_sip"
    ):
        raise ContractError("HFDL retirement identity differs")
    blocked = policy["blocked_work"]
    if type(blocked) is not list or set(blocked) != {
        "new_hfdl_bridge_planning",
        "new_hfdl_derivative_work",
        "new_hfdl_research",
        "new_hfdl_training",
        "new_hfdl_evaluation",
        "new_hfdl_wfa",
        "hfdl_alpaca_pooling",
    }:
        raise ContractError("HFDL blocked-work census differs")
    preservation = _strict_mapping(
        policy["preservation"],
        {
            "existing_tracked_machinery_untouched_until_cleanup_gate",
            "existing_generated_releases_untouched_until_cleanup_gate",
            "existing_legacy_files_untouched",
            "historical_audit_and_trial_census_evidence_retained",
        },
        "HFDL preservation",
    )
    cleanup = _strict_mapping(
        policy["cleanup"],
        {
            "authorized_now",
            "tracked_removal_requires_separate_exact_gate",
            "generated_release_removal_requires_separate_destructive_gate",
            "alpaca_replacement_must_verify_first",
        },
        "HFDL cleanup",
    )
    authorities = _strict_mapping(
        policy["authorities"],
        {
            "provider_access",
            "legacy_row_access",
            "derivative_planning",
            "derivative_publication",
            "research",
            "training",
            "evaluation",
            "wfa",
            "source_activation",
            "cleanup",
        },
        "HFDL authorities",
    )
    if (
        any(value is not True for value in preservation.values())
        or cleanup["authorized_now"] is not False
        or any(
            cleanup[name] is not True
            for name in cleanup
            if name != "authorized_now"
        )
        or any(value is not False for value in authorities.values())
        or type(policy["stop_conditions"]) is not list
        or not policy["stop_conditions"]
    ):
        raise ContractError("HFDL retirement boundary is weakened")
    policy_id = sha256_bytes(canonical_json_bytes(policy))
    return policy, policy_id


def reject_hfdl_work(
    repository_root: Path,
    *,
    requested_action: str,
) -> None:
    if type(requested_action) is not str or not requested_action:
        raise ContractError("requested HFDL action must be exact nonempty text")
    _policy, policy_id = load_hfdl_retirement_policy(repository_root)
    raise ContractError(
        "HFDL is retired and excluded from future work; "
        f"blocked_action={requested_action}; retirement_policy_id={policy_id}"
    )
