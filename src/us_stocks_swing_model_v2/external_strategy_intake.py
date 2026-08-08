"""Fail-closed intake for external DISCOVERY_ONLY strategy specifications.

This module never imports the external scout, reads Yahoo artifacts, writes a
release, registers a trial, or executes prospective inference. It accepts only
one bounded strategy-definition JSON and produces content-addressed plans.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError
from .external_strategy_census import HistoricalTrialCensusAssessment
from .gates import IndependentGatePolicy
from .governance import ReleaseBinding, verify_release_bindings
from .monitoring_policy import frozen_monitoring_policy_hash
from .s3_object_lock_trial_registry import S3ObjectLockRegistryPolicy
from .trials import (
    TrialSpec,
    repository_trial_identity,
    require_trial_gate_policy,
)


MAX_SPEC_BYTES = 256 * 1024
ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
EXACT_FEATURE_NAMES = (
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
)
REQUIRED_RELEASE_DATASETS = {
    "identity",
    "alpaca_daily_bars",
    "corporate_actions",
    "xnys_sessions",
    "eligible_universe",
    "features",
    "outcomes",
}
COMPATIBLE_ADAPTER_ID = "v2_price_linear_distribution_v1"
COMPATIBLE_ADAPTER_PARAMETERS = {
    "ridge_alpha": 1.0,
    "neutral_band": 0.005,
    "initial_training_sessions": 1008,
    "outer_folds": 8,
    "outer_test_sessions": 126,
    "purge_sessions": 5,
    "embargo_sessions": 5,
    "holding_sessions": 5,
}
REQUIRED_RELEASE_CONTRACTS = {
    "identity": ("prospective_as_received", "PASS"),
    "alpaca_daily_bars": ("active_historical", "PASS"),
    "corporate_actions": ("prospective_as_received", "PASS"),
    "xnys_sessions": ("derived_causal", "PASS"),
    "eligible_universe": ("derived_causal", "PASS"),
    "features": ("feature_only", "PASS"),
    "outcomes": ("outcome_only", "PASS"),
}
REQUIRED_SOURCE_EPOCHS = {
    "identity": "nasdaq_alpaca_active_us_equity_v1",
    "alpaca_daily_bars": "alpaca_basic_sip_raw_v1",
    "corporate_actions": "alpaca_corporate_actions_v1",
}
FROZEN_SPEC_FIELDS = {
    "schema_version",
    "mode",
    "target_project",
    "hypothesis",
    "timing",
    "universe",
    "cost_policy",
    "trial_family",
    "discovery_lineage",
    "claims",
    "spec_id",
}


def _exact_dict(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ContractError(f"{name} fields differ from the exact contract")
    return value


def _exact_string(value: object, name: str) -> str:
    if type(value) is not str or not value or not value.isascii():
        raise ContractError(f"{name} must be nonempty ASCII text")
    if any(token in value for token in ("/", "\\", ":", ".py")):
        raise ContractError(f"{name} cannot contain a filesystem or module path")
    return value


def _exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ContractError(f"{name} must be an exact integer >= {minimum}")
    return value


def _require_finite_tree(value: object) -> None:
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ContractError("strategy spec keys must be exact text")
            _require_finite_tree(item)
    elif type(value) is list:
        for item in value:
            _require_finite_tree(item)
    elif isinstance(value, float) and not math.isfinite(value):
        raise ContractError("strategy spec contains NaN or infinity")


@dataclass(frozen=True)
class ExternalStrategySpec:
    payload: Mapping[str, Any]

    @property
    def spec_id(self) -> str:
        return str(self.payload["spec_id"])

    @property
    def hypothesis(self) -> Mapping[str, Any]:
        return self.payload["hypothesis"]

    @property
    def timing(self) -> Mapping[str, Any]:
        return self.payload["timing"]

    @property
    def trial_family(self) -> Mapping[str, Any]:
        return self.payload["trial_family"]

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ExternalStrategySpec":
        if not raw or len(raw) > MAX_SPEC_BYTES:
            raise ContractError("external strategy spec exceeds its bounded size")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("external strategy spec is not valid UTF-8 JSON") from exc
        root = _exact_dict(value, FROZEN_SPEC_FIELDS, "external_strategy_spec")
        if (
            root["schema_version"] != 1
            or root["mode"] != "DISCOVERY_ONLY_FROZEN_STRATEGY_SPEC"
            or root["target_project"] != "US_stocks_swing_model_v2"
        ):
            raise ContractError("external strategy spec identity differs")
        hypothesis = _exact_dict(
            root["hypothesis"],
            {
                "adapter_id",
                "parameters",
                "requested_model_family",
                "primary_metric",
                "required_feature_schema_id",
                "required_feature_names",
                "outcome_semantics",
            },
            "hypothesis",
        )
        adapter_id = _exact_string(hypothesis["adapter_id"], "hypothesis.adapter_id")
        if ADAPTER_ID_RE.fullmatch(adapter_id) is None:
            raise ContractError("strategy adapter ID is not a safe registered identifier")
        if type(hypothesis["parameters"]) is not dict:
            raise ContractError("strategy parameters must be an exact object")
        for key, item in hypothesis["parameters"].items():
            _exact_string(key, "strategy parameter name")
            if isinstance(item, bool) or not isinstance(item, (int, float, str)):
                raise ContractError("strategy parameters allow only explicit scalars")
            if type(item) is str:
                _exact_string(item, f"strategy parameter {key}")
        _exact_string(hypothesis["requested_model_family"], "requested_model_family")
        _exact_string(hypothesis["primary_metric"], "primary_metric")
        _exact_string(hypothesis["required_feature_schema_id"], "required_feature_schema_id")
        _exact_string(hypothesis["outcome_semantics"], "outcome_semantics")
        if type(hypothesis["required_feature_names"]) is not list:
            raise ContractError("required feature names must be an exact array")
        for name in hypothesis["required_feature_names"]:
            _exact_string(name, "required feature name")
        timing = _exact_dict(root["timing"], {"decision", "entry", "exit", "holding_sessions"}, "timing")
        for name in ("decision", "entry", "exit"):
            _exact_string(timing[name], f"timing.{name}")
        _exact_int(timing["holding_sessions"], "holding_sessions", minimum=1)
        universe = _exact_dict(root["universe"], {"project_eligible_universe_required", "scout_universe_hash"}, "universe")
        if universe["project_eligible_universe_required"] is not True:
            raise ContractError("project eligible universe must be required")
        require_sha256(universe["scout_universe_hash"], "scout_universe_hash")
        costs = _exact_dict(root["cost_policy"], {"one_way_bps", "binding_bps"}, "cost_policy")
        if costs != {"one_way_bps": [0, 10, 25, 50], "binding_bps": 25}:
            raise ContractError("external cost policy differs from Stocks V2")
        family = _exact_dict(
            root["trial_family"],
            {
                "trial_family_id",
                "run_configuration_count",
                "evaluated_cost_curve_count",
                "prior_exposure_floor",
                "exact_census_complete",
            },
            "trial_family",
        )
        _exact_string(family["trial_family_id"], "trial_family_id")
        _exact_int(family["run_configuration_count"], "run_configuration_count", minimum=1)
        _exact_int(family["evaluated_cost_curve_count"], "evaluated_cost_curve_count", minimum=1)
        _exact_int(family["prior_exposure_floor"], "prior_exposure_floor")
        if type(family["exact_census_complete"]) is not bool:
            raise ContractError("exact census state must be boolean")
        lineage = _exact_dict(root["discovery_lineage"], {"screening_run_id", "gate_receipt_id"}, "discovery_lineage")
        require_sha256(lineage["screening_run_id"], "screening_run_id")
        require_sha256(lineage["gate_receipt_id"], "gate_receipt_id")
        claims = _exact_dict(
            root["claims"],
            {
                "discovery_only",
                "contains_market_data",
                "contains_performance",
                "alpha_claim",
                "registration_authority",
                "prospective_authority",
            },
            "claims",
        )
        if claims != {
            "discovery_only": True,
            "contains_market_data": False,
            "contains_performance": False,
            "alpha_claim": False,
            "registration_authority": False,
            "prospective_authority": False,
        }:
            raise ContractError("external strategy claims differ")
        _require_finite_tree(root)
        require_sha256(root["spec_id"], "spec_id")
        unsigned = {key: item for key, item in root.items() if key != "spec_id"}
        if root["spec_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
            raise ContractError("external strategy spec ID differs from content")
        forbidden_keys = {
            "bars",
            "prices",
            "returns",
            "metrics",
            "cagr",
            "sharpe",
            "drawdown",
            "symbols",
            "path",
            "module",
            "class",
        }
        def walk(item: object) -> None:
            if type(item) is dict:
                for key, nested in item.items():
                    if key.lower() in forbidden_keys:
                        raise ContractError("external strategy spec embeds forbidden data, results, or code references")
                    walk(nested)
            elif type(item) is list:
                for nested in item:
                    walk(nested)
        walk(root)
        return cls(payload=dict(root))


@dataclass(frozen=True)
class ProjectTrialBindings:
    census_anchor_id: str
    trial_family_anchor_id: str
    evaluator_closure_hash: str
    governance_contract_hash: str
    code_hash: str
    config_hash: str
    environment_hash: str

    @classmethod
    def from_repository(
        cls,
        *,
        repository_root: Path,
        census_anchor_id: str,
        trial_family_anchor_id: str,
    ) -> "ProjectTrialBindings":
        identity = repository_trial_identity(repository_root)
        return cls(
            census_anchor_id=census_anchor_id,
            trial_family_anchor_id=trial_family_anchor_id,
            evaluator_closure_hash=identity.evaluator_closure_hash,
            governance_contract_hash=identity.governance_contract_hash,
            code_hash=identity.code_hash,
            config_hash=identity.config_hash,
            environment_hash=identity.environment_hash,
        )

    def validate(self, *, repository_root: Path | None = None) -> None:
        for name in self.__dataclass_fields__:
            require_sha256(getattr(self, name), name)
        if repository_root is not None:
            identity = repository_trial_identity(repository_root)
            expected = {
                "evaluator_closure_hash": identity.evaluator_closure_hash,
                "governance_contract_hash": identity.governance_contract_hash,
                "code_hash": identity.code_hash,
                "config_hash": identity.config_hash,
                "environment_hash": identity.environment_hash,
            }
            if any(getattr(self, name) != value for name, value in expected.items()):
                raise ContractError(
                    "project trial bindings differ from the live repository execution identity"
                )


@dataclass(frozen=True)
class ProspectiveCandidateBindings:
    trial_id: str
    registration_hash: str
    trial_registry_binding_id: str
    candidate_id: str
    bundle_id: str
    monitoring_policy_hash: str
    accepted_release_ids: tuple[str, ...]

    def validate(self) -> None:
        for name in (
            "trial_id",
            "registration_hash",
            "trial_registry_binding_id",
            "candidate_id",
            "bundle_id",
            "monitoring_policy_hash",
        ):
            require_sha256(getattr(self, name), name)
        if self.monitoring_policy_hash != frozen_monitoring_policy_hash():
            raise ContractError("prospective binding monitoring policy differs")
        if list(self.accepted_release_ids) != sorted(set(self.accepted_release_ids)) or not self.accepted_release_ids:
            raise ContractError("prospective accepted release IDs must be sorted and unique")
        for release_id in self.accepted_release_ids:
            require_sha256(release_id, "accepted_release_id")


def _compatibility_status(spec: ExternalStrategySpec) -> str | None:
    if spec.timing != {
        "decision": "AFTER_D0_CLOSE",
        "entry": "D1_OPEN",
        "exit": "D5_CLOSE",
        "holding_sessions": 5,
    }:
        return "REJECTED_NON_D5"
    if (
        spec.hypothesis["adapter_id"] != COMPATIBLE_ADAPTER_ID
        or spec.hypothesis["parameters"] != COMPATIBLE_ADAPTER_PARAMETERS
        or spec.hypothesis["requested_model_family"] != "linear_distribution_v1"
    ):
        return "SCOUT_ONLY_INCOMPATIBLE_MODEL"
    if (
        spec.hypothesis["required_feature_schema_id"] != "prospective_price_only_v1"
        or tuple(spec.hypothesis["required_feature_names"]) != EXACT_FEATURE_NAMES
    ):
        return "SCOUT_ONLY_INCOMPATIBLE_FEATURE_SCHEMA"
    if spec.hypothesis["primary_metric"] != "multiclass_log_loss":
        return "SCOUT_ONLY_INCOMPATIBLE_MODEL"
    return None


def _require_census_binding(
    spec: ExternalStrategySpec,
    census: HistoricalTrialCensusAssessment | None,
) -> bool:
    if census is None or census.status != "COMPLETE":
        return False
    if (
        census.payload["candidate_spec_id"] != spec.spec_id
        or census.payload["trial_family_id"] != spec.trial_family["trial_family_id"]
        or census.payload["counts"]["documented_prior_floor"] != spec.trial_family["prior_exposure_floor"]
        or census.exact_global_count is None
        or census.exact_family_count is None
        or census.exact_global_count < spec.trial_family["prior_exposure_floor"]
        or census.exact_family_count < spec.trial_family["run_configuration_count"]
    ):
        raise ContractError("project census assessment does not bind the external strategy")
    return True


def _release_contracts_are_complete(bindings: tuple[ReleaseBinding, ...]) -> bool:
    if len(bindings) != len(REQUIRED_RELEASE_DATASETS):
        return False
    by_dataset = {item.dataset: item for item in bindings}
    if set(by_dataset) != REQUIRED_RELEASE_DATASETS:
        return False
    for dataset, (role, quality) in REQUIRED_RELEASE_CONTRACTS.items():
        item = by_dataset[dataset]
        if item.role != role or item.quality_state != quality:
            return False
        if any(token in item.source_epoch.lower() for token in ("legacy", "proxy", "synthetic")):
            return False
    return all(by_dataset[name].source_epoch == epoch for name, epoch in REQUIRED_SOURCE_EPOCHS.items())


def _release_readiness(bindings: tuple[ReleaseBinding, ...]) -> dict[str, object]:
    counts = {name: sum(item.dataset == name for item in bindings) for name in REQUIRED_RELEASE_DATASETS}
    by_dataset = {item.dataset: item for item in bindings if item.dataset in REQUIRED_RELEASE_DATASETS}
    missing = sorted(name for name, count in counts.items() if count == 0)
    invalid: list[str] = []
    for dataset, count in counts.items():
        if count > 1:
            invalid.append(f"{dataset}:duplicate")
        if count != 1:
            continue
        item = by_dataset[dataset]
        role, quality = REQUIRED_RELEASE_CONTRACTS[dataset]
        if item.role != role:
            invalid.append(f"{dataset}:role")
        if item.quality_state != quality:
            invalid.append(f"{dataset}:quality_state")
        if any(token in item.source_epoch.lower() for token in ("legacy", "proxy", "synthetic")):
            invalid.append(f"{dataset}:ineligible_source_epoch")
        if dataset in REQUIRED_SOURCE_EPOCHS and item.source_epoch != REQUIRED_SOURCE_EPOCHS[dataset]:
            invalid.append(f"{dataset}:source_epoch")
    complete = not missing and not invalid and len(bindings) == len(REQUIRED_RELEASE_DATASETS)
    return {
        "status": "COMPLETE" if complete else "WAITING_PROSPECTIVE_EVIDENCE_HORIZON",
        "minimum_total_sessions": 2268,
        "missing_datasets": missing,
        "invalid_contracts": sorted(invalid),
        "legacy_or_proxy_substitution_allowed": False,
        "aapl_spy_smoke_satisfies_horizon": False,
    }


def build_external_strategy_intake_plan(
    spec: ExternalStrategySpec,
    *,
    repository_root: Path,
    census_assessment: HistoricalTrialCensusAssessment | None = None,
    accepted_release_directories: Iterable[Path] = (),
    accepted_release_root: Path | None = None,
) -> dict[str, object]:
    if type(spec) is not ExternalStrategySpec:
        raise ContractError("external strategy intake requires a validated specification")
    root = Path(repository_root).resolve(strict=True)
    compatibility = _compatibility_status(spec)
    bindings: tuple[ReleaseBinding, ...] = ()
    status = compatibility
    if status is None and not _require_census_binding(spec, census_assessment):
        status = "BLOCKED_EXACT_CENSUS"
    release_directories = tuple(Path(item) for item in accepted_release_directories)
    if status is None:
        if not release_directories or accepted_release_root is None:
            status = "BLOCKED_ACCEPTED_RELEASES"
        else:
            bindings = verify_release_bindings(
                release_directories,
                accepted_release_root=Path(accepted_release_root),
                expected_project="US_stocks_swing_model_v2",
            )
            if not _release_contracts_are_complete(bindings):
                status = "BLOCKED_ACCEPTED_RELEASES"
    release_readiness = _release_readiness(bindings)
    registry = S3ObjectLockRegistryPolicy.load(
        root / "config/trial_registry_s3_object_lock_policy.json",
        repository_root=root,
    )
    if status is None and registry.status != "CONFIGURED":
        status = "BLOCKED_EXTERNAL_REGISTRY"
    if status is None:
        status = "READY_FOR_SEPARATELY_AUTHORIZED_REGISTRATION"
    unsigned = {
        "schema_version": 1,
        "mode": "STOCKS_V2_EXTERNAL_STRATEGY_INTAKE_PLAN_ONLY",
        "spec_id": spec.spec_id,
        "census_assessment_id": None if census_assessment is None else census_assessment.assessment_id,
        "status": status,
        "accepted_release_ids": [binding.release_id for binding in bindings],
        "release_readiness": release_readiness,
        "registry_policy_id": registry.policy_id,
        "project_contract": {
            "model_family": "linear_distribution_v1",
            "feature_schema_id": "prospective_price_only_v1",
            "feature_names": list(EXACT_FEATURE_NAMES),
            "outcome_schema_id": "d1_open_to_d5_close_split_normalized_v1",
            "primary_metric": "multiclass_log_loss",
        },
        "authorities": {
            "registration": False,
            "outcome_access": False,
            "training": False,
            "evaluation": False,
            "candidate_sealing": False,
            "prospective_execution": False,
        },
    }
    return {**unsigned, "intake_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_trial_spec_from_intake(
    spec: ExternalStrategySpec,
    *,
    release_bindings: Iterable[ReleaseBinding],
    census_assessment: HistoricalTrialCensusAssessment,
    project_bindings: ProjectTrialBindings,
    gate_policy: IndependentGatePolicy,
    repository_root: Path,
) -> TrialSpec:
    if _compatibility_status(spec) is not None or not _require_census_binding(spec, census_assessment):
        raise ContractError("external strategy is not compatible and project-census-complete")
    bindings = tuple(sorted(release_bindings, key=lambda item: item.release_id))
    if not _release_contracts_are_complete(bindings):
        raise ContractError("trial spec lacks required accepted release roles")
    for binding in bindings:
        binding.validate()
    root = Path(repository_root).resolve(strict=True)
    project_bindings.validate(repository_root=root)
    if (
        project_bindings.census_anchor_id != census_assessment.census_anchor_id
        or project_bindings.trial_family_anchor_id != census_assessment.trial_family_anchor_id
    ):
        raise ContractError("trial bindings differ from the project census anchors")
    readiness = json.loads((root / "config/research_readiness_contract.json").read_text(encoding="utf-8"))
    if type(gate_policy) is not IndependentGatePolicy:
        raise ContractError("trial specification requires the exact independent gate policy")
    gate_policy.validate()
    governance_hash = sha256_bytes(canonical_json_bytes(readiness))
    if governance_hash != project_bindings.governance_contract_hash:
        raise ContractError("project governance hash does not bind the readiness contract")
    trial = TrialSpec(
        hypothesis_id=spec.spec_id,
        evidence_class="PROSPECTIVE_FINAL",
        data_release_ids=tuple(item.release_id for item in bindings),
        release_bindings=bindings,
        feature_schema_id="prospective_price_only_v1",
        outcome_schema_id="d1_open_to_d5_close_split_normalized_v1",
        split_plan_id=sha256_bytes(canonical_json_bytes(readiness["nested_wfa"])),
        model_family="linear_distribution_v1",
        primary_metric="multiclass_log_loss",
        primary_gate_id=sha256_bytes(canonical_json_bytes(gate_policy.as_dict())),
        robustness_policy_id=sha256_bytes(canonical_json_bytes(readiness["robustness"])),
        cost_policy_id=sha256_bytes(canonical_json_bytes(readiness["economic_translation"])),
        trial_family_id=str(spec.trial_family["trial_family_id"]),
        census_anchor_id=project_bindings.census_anchor_id,
        trial_family_anchor_id=project_bindings.trial_family_anchor_id,
        evaluator_closure_hash=project_bindings.evaluator_closure_hash,
        governance_contract_hash=project_bindings.governance_contract_hash,
        code_hash=project_bindings.code_hash,
        config_hash=project_bindings.config_hash,
        environment_hash=project_bindings.environment_hash,
    )
    trial.validate()
    repository_trial_identity(root).require_spec(trial)
    require_trial_gate_policy(trial, gate_policy, repository_root=root)
    return trial


def build_prospective_paper_test_plan(
    spec: ExternalStrategySpec,
    *,
    intake_plan: Mapping[str, Any],
    trial_spec: TrialSpec,
    bindings: ProspectiveCandidateBindings,
) -> dict[str, object]:
    if _compatibility_status(spec) is not None:
        raise ContractError("prospective plan requires a compatible census-complete spec")
    if (
        type(intake_plan) is not dict
        or intake_plan.get("mode") != "STOCKS_V2_EXTERNAL_STRATEGY_INTAKE_PLAN_ONLY"
        or intake_plan.get("spec_id") != spec.spec_id
        or intake_plan.get("status") != "READY_FOR_SEPARATELY_AUTHORIZED_REGISTRATION"
    ):
        raise ContractError("prospective plan requires the matching ready intake plan")
    if set(intake_plan) != {
        "schema_version",
        "mode",
        "spec_id",
        "census_assessment_id",
        "status",
        "accepted_release_ids",
        "release_readiness",
        "registry_policy_id",
        "project_contract",
        "authorities",
        "intake_plan_id",
    }:
        raise ContractError("prospective intake plan fields differ")
    unsigned_intake = {key: item for key, item in intake_plan.items() if key != "intake_plan_id"}
    require_sha256(intake_plan["intake_plan_id"], "intake_plan_id")
    if intake_plan["intake_plan_id"] != sha256_bytes(canonical_json_bytes(unsigned_intake)):
        raise ContractError("prospective intake plan ID differs from content")
    require_sha256(intake_plan["census_assessment_id"], "census_assessment_id")
    if type(trial_spec) is not TrialSpec:
        raise ContractError("prospective plan requires the exact project TrialSpec")
    trial_spec.validate()
    bindings.validate()
    if trial_spec.hypothesis_id != spec.spec_id or bindings.trial_id != trial_spec.trial_id:
        raise ContractError("prospective trial differs from the frozen strategy and bindings")
    if tuple(bindings.accepted_release_ids) != trial_spec.data_release_ids:
        raise ContractError("prospective releases differ from the registered trial")
    if list(bindings.accepted_release_ids) != intake_plan.get("accepted_release_ids"):
        raise ContractError("prospective release bindings differ from intake")
    unsigned = {
        "schema_version": 1,
        "mode": "PROSPECTIVE_CANDIDATE_PAPER_TEST_PLAN_ONLY",
        "status": "BLOCKED_PENDING_SEPARATE_AUTHORITIES",
        "spec_id": spec.spec_id,
        "trial_id": bindings.trial_id,
        "registration_hash": bindings.registration_hash,
        "trial_registry_binding_id": bindings.trial_registry_binding_id,
        "candidate_id": bindings.candidate_id,
        "bundle_id": bindings.bundle_id,
        "monitoring_policy_hash": bindings.monitoring_policy_hash,
        "accepted_release_ids": list(bindings.accepted_release_ids),
        "required_existing_lanes": [
            "accepted_prospective_releases",
            "external_immutable_trial_registration",
            "sealed_linear_distribution_bundle",
            "fit_free_inference",
            "append_only_prediction_ledger",
            "frozen_prospective_monitoring",
        ],
        "authorities": {
            "provider_capture": False,
            "source_activation": False,
            "inference": False,
            "prediction_append": False,
            "monitoring_append": False,
            "paper_test_execution": False,
            "trading": False,
        },
    }
    return {**unsigned, "prospective_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
