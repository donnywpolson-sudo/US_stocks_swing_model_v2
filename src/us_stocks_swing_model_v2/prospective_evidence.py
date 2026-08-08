"""Fail-closed planning for the prospective evidence collection lane.

This module deliberately validates only immutable release metadata.  It never
opens payload rows, captures a provider response, publishes a release, or
authorizes training.  Those actions retain their independent, bounded gates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError
from .releases import ReleaseManifest, verify_accepted_release
from .git_trial_registry import GitTrialRegistryPolicy


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/prospective_evidence_pipeline_policy.json"
_RELEASE_KEYS = ("identity", "bars", "actions", "calendar")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"prospective evidence policy is unreadable: {path}") from exc
    if type(payload) is not dict:
        raise ContractError("prospective evidence policy must be an object")
    return payload


def load_prospective_evidence_policy(repository_root: Path) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    policy = _load_json(root / POLICY_PATH)
    required = {
        "schema_version", "project", "mode", "source_epoch", "required_releases",
        "daily_order", "downstream_release_order", "feature_contract", "outcome_policy",
        "wfa_evidence_horizon", "external_registry", "prohibitions",
    }
    if set(policy) != required:
        raise ContractError("prospective evidence policy fields differ")
    if (
        policy["schema_version"] != 1
        or policy["project"] != PROJECT
        or policy["mode"] != "PROSPECTIVE_EVIDENCE_PIPELINE_PLAN_ONLY"
        or type(policy["source_epoch"]) is not str
        or set(policy["required_releases"]) != set(_RELEASE_KEYS)
        or policy["daily_order"] != ["identity", "bars", "actions"]
        or policy["downstream_release_order"] != ["eligible_universe", "features", "outcomes"]
    ):
        raise ContractError("prospective evidence policy contract differs")
    horizon = policy["wfa_evidence_horizon"]
    if (
        type(horizon) is not dict
        or horizon != {
            "initial_training_sessions": 1008,
            "outer_folds": 8,
            "outer_test_sessions": 126,
            "final_holdout_sessions": 252,
            "minimum_total_sessions": 2268,
        }
    ):
        raise ContractError("prospective evidence WFA horizon differs")
    outcome = policy["outcome_policy"]
    if (
        type(outcome) is not dict
        or outcome.get("holding_sessions") != 5
        or outcome.get("require_complete_action_and_delisting_coverage") is not True
        or outcome.get("unresolved_rows_remain_in_coverage_denominator") is not True
        or outcome.get("imputation_or_drop_allowed") is not False
    ):
        raise ContractError("prospective evidence outcome policy differs")
    features = policy["feature_contract"]
    if (
        type(features) is not dict
        or features.get("schema_name") != "prospective_price_only_v1"
        or features.get("feature_names") != [
            "d0_raw_intraday_return",
            "trailing_5_session_raw_return",
            "trailing_5_session_raw_volatility",
        ]
        or features.get("lookback_sessions") != 5
        or features.get("formulas") != {
            "d0_raw_intraday_return": "close[D0] / open[D0] - 1",
            "trailing_5_session_raw_return": "close[D0] / close[D-5] - 1",
            "trailing_5_session_raw_volatility": "population_stddev(close_to_close_returns[D-5_to_D0])",
        }
        or features.get("requires_complete_action_and_delisting_coverage") is not True
        or features.get("forbid_action_event_in_lookback") is not True
        or features.get("global_imputation_or_cross_sectional_transform") is not False
        or features.get("unresolved_status") != "ABSTAIN_UNRESOLVED_CAUSAL_LOOKBACK"
    ):
        raise ContractError("prospective evidence feature contract differs")
    registry = policy["external_registry"]
    if (
        type(registry) is not dict
        or registry.get("policy_path") != "config/trial_registry_git_policy.json"
        or registry.get("required_before_real_trial") is not True
        or registry.get("configured_status") != "CONFIGURED_LOCAL_GIT"
        or registry.get("requires_remote_backup") is not True
        or registry.get("owner_controlled") is not True
        or registry.get("independent_immutability") is not False
    ):
        raise ContractError("prospective evidence registry policy differs")
    if not isinstance(policy["prohibitions"], list) or set(policy["prohibitions"]) != {
        "legacy_discovery_only_input", "proxy_input", "training_or_evaluation",
        "candidate_sealing", "source_activation", "network_or_generated_write_execution",
    }:
        raise ContractError("prospective evidence prohibitions differ")
    return policy


def _require_manifest(name: str, manifest: ReleaseManifest, expected: Mapping[str, object]) -> None:
    if manifest.project != PROJECT:
        raise ContractError(f"prospective {name} release project differs")
    for field in ("dataset", "role", "quality_state"):
        if getattr(manifest, field) != expected[field]:
            raise ContractError(f"prospective {name} release {field} differs")
    if "source_epoch" in expected and manifest.source_epoch != expected["source_epoch"]:
        raise ContractError(f"prospective {name} release source_epoch differs")
    if manifest.role == "legacy_discovery_only" or manifest.dataset.startswith("alpaca_discovery_"):
        raise ContractError(f"prospective {name} release is a prohibited legacy proxy input")


def _release_binding(manifest: ReleaseManifest) -> dict[str, object]:
    return {
        "release_id": manifest.release_id,
        "dataset": manifest.dataset,
        "source_epoch": manifest.source_epoch,
        "role": manifest.role,
        "quality_state": manifest.quality_state,
        "row_count": manifest.row_count,
        "event_start": manifest.event_start,
        "event_end": manifest.event_end,
        "manifest_sha256": sha256_bytes(canonical_json_bytes(manifest.as_dict())),
    }


def build_prospective_epoch_plan(
    *,
    identity_release_directory: Path,
    bars_release_directory: Path,
    actions_release_directory: Path,
    calendar_release_directory: Path,
    accepted_root: Path,
    repository_root: Path,
) -> dict[str, object]:
    """Build a no-row-read/no-write plan for one future prospective epoch."""

    root = Path(repository_root).resolve(strict=True)
    policy = load_prospective_evidence_policy(root)
    accepted = Path(accepted_root).resolve(strict=True)
    directories = {
        "identity": Path(identity_release_directory),
        "bars": Path(bars_release_directory),
        "actions": Path(actions_release_directory),
        "calendar": Path(calendar_release_directory),
    }
    manifests = {
        name: verify_accepted_release(directory, accepted_root=accepted)
        for name, directory in directories.items()
    }
    for name in _RELEASE_KEYS:
        _require_manifest(name, manifests[name], policy["required_releases"][name])
    release_ids = {manifest.release_id for manifest in manifests.values()}
    if len(release_ids) != len(manifests):
        raise ContractError("prospective epoch releases must be distinct")
    if not {manifests["identity"].release_id, manifests["calendar"].release_id}.issubset(
        manifests["bars"].upstream_release_ids
    ):
        raise ContractError("prospective bars release does not bind identity and calendar releases")
    registry_policy = GitTrialRegistryPolicy.load(
        root / policy["external_registry"]["policy_path"], repository_root=root
    )
    unsigned = {
        "schema_version": 1,
        "mode": "PROSPECTIVE_EVIDENCE_EPOCH_PLAN_ONLY",
        "policy_sha256": sha256_bytes(canonical_json_bytes(policy)),
        "source_epoch": policy["source_epoch"],
        "release_bindings": {name: _release_binding(manifests[name]) for name in _RELEASE_KEYS},
        "daily_order": policy["daily_order"],
        "downstream_release_order": policy["downstream_release_order"],
        "outcome_policy": policy["outcome_policy"],
        "feature_contract": policy["feature_contract"],
        "wfa_evidence_horizon": policy["wfa_evidence_horizon"],
        "external_registry": {
            "policy_id": registry_policy.policy_id,
            "status": registry_policy.status,
            "required_before_real_trial": True,
            "configured_for_real_trial": registry_policy.status == "CONFIGURED_LOCAL_GIT",
            "owner_controlled": registry_policy.owner_controlled,
            "independent_immutability": registry_policy.independent_immutability,
        },
        "authorities": {
            "network": False,
            "credential_access": False,
            "release_publication": False,
            "eligible_universe_build": False,
            "feature_build": False,
            "outcome_build": False,
            "training": False,
            "evaluation": False,
            "candidate_sealing": False,
        },
        "stop_conditions": [
            "legacy_or_proxy_release_input",
            "identity_calendar_or_bar_lineage_drift",
            "incomplete_corporate_action_or_delisting_coverage",
            "attempt_to_impute_or_drop_unresolved_outcomes",
            "git_registration_not_committed_and_backed_up_before_real_trial",
        ],
    }
    return {**unsigned, "prospective_epoch_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
