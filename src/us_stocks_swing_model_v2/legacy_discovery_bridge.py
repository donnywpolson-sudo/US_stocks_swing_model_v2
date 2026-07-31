"""Plan-only bridge from accepted HFDL metadata to proxy discovery contracts.

This module reads only release metadata.  It never reads Parquet rows, computes
an outcome, fits a model, writes an artifact, or grants research authority.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .common import (
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .hfdl_retirement import reject_hfdl_work
from .releases import ReleaseManifest


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = Path("config/legacy_discovery_bridge_contract.json")
CONTRACT_TYPE = "LEGACY_DISCOVERY_PROXY_BRIDGE_PLAN_ONLY"
PLAN_MODE = "LEGACY_DISCOVERY_PROXY_BRIDGE_PLAN_ONLY_NO_WRITES"
FOUNDATION_DATASET = "stock_historical_foundation_set"
FOUNDATION_PAYLOAD = "foundation_set.json"
EXPECTED_EPOCHS = (
    "hfdl_pitrading_consolidated",
    "hfdl_iex_only",
)
EXPECTED_KINDS = ("causal_bars", "feature_inputs", "outcome_inputs")
GIT_OBJECT = re.compile(r"^[0-9a-f]{40}$")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_json_object(
    path: Path,
    *,
    label: str,
    require_canonical: bool,
) -> tuple[dict[str, Any], bytes]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(payload) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    if require_canonical and raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} must use canonical JSON encoding")
    return payload, raw


def _validate_contract(contract: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "contract_version",
        "project",
        "contract_type",
        "evidence_class",
        "source_foundation",
        "proxy_eligibility",
        "feature_adapter",
        "outcome_adapter",
        "wfa_input_adapter",
        "future_derivative_release",
        "authorities",
        "stop_conditions",
        "contract_id",
    }
    if set(contract) != expected_fields:
        raise ContractError("legacy-discovery bridge contract fields differ")
    if (
        type(contract["schema_version"]) is not int
        or contract["schema_version"] != 1
        or contract["contract_version"] != "1.0.0"
        or contract["project"] != PROJECT
        or contract["contract_type"] != CONTRACT_TYPE
        or contract["evidence_class"] != "LEGACY_DISCOVERY"
    ):
        raise ContractError("legacy-discovery bridge contract identity differs")

    source = contract["source_foundation"]
    if (
        type(source) is not dict
        or source.get("dataset") != FOUNDATION_DATASET
        or source.get("source_epoch")
        != "hfdl_two_epoch_legacy_discovery_no_pooling"
        or source.get("role") != "legacy_discovery_only"
        or source.get("quality_state") != "LEGACY_CAVEATED"
        or source.get("historical_evidence_scope")
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or source.get("point_in_time_safe") is not False
        or source.get("direct_model_or_evaluation_inputs_allowed") is not False
        or source.get("required_output_kinds") != list(EXPECTED_KINDS)
        or source.get("required_source_adjustment")
        != "hfdl_clean_source_adjusted"
    ):
        raise ContractError("legacy-discovery source-foundation contract differs")
    epoch_specs = source.get("required_epochs")
    if (
        type(epoch_specs) is not list
        or [value.get("epoch_id") for value in epoch_specs] != list(EXPECTED_EPOCHS)
        or epoch_specs[0].get("event_start") != "2010-01-04"
        or epoch_specs[0].get("event_end") != "2022-03-03"
        or epoch_specs[1].get("event_start") != "2022-03-04"
        or epoch_specs[1].get("event_end") != "2026-06-26"
    ):
        raise ContractError("legacy-discovery epoch contract differs")

    eligibility = contract["proxy_eligibility"]
    if (
        type(eligibility) is not dict
        or eligibility.get("identity_key") != "source_series_id"
        or eligibility.get("symbol_is_persistent_identity") is not False
        or eligibility.get("membership_evidence_status")
        != "UNKNOWN_NOT_AS_RECEIVED"
        or eligibility.get("security_type_evidence_status")
        != "UNKNOWN_NOT_AS_RECEIVED"
        or eligibility.get("historical_proxy") is not True
        or eligibility.get("trusted_sleeves") != []
        or eligibility.get("diagnostic_sleeves")
        != ["proxy_unknown_long", "proxy_unknown_short"]
        or eligibility.get("preserve_missing_rows_in_denominator") is not True
    ):
        raise ContractError("legacy-discovery proxy-eligibility contract differs")

    features = contract["feature_adapter"]
    if (
        type(features) is not dict
        or features.get("source_kind") != "feature_inputs"
        or features.get("features")
        != [
            "close_to_close_return_1",
            "intraday_return",
            "range_fraction",
            "log1p_volume",
        ]
        or features.get("ready_status") != "PRICE_INPUT_READY_PIT_UNRESOLVED"
        or features.get("preserve_nonready_rows") is not True
        or features.get("may_read_outcomes") is not False
    ):
        raise ContractError("legacy-discovery feature-adapter contract differs")

    outcome = contract["outcome_adapter"]
    if (
        type(outcome) is not dict
        or outcome.get("source_kind") != "outcome_inputs"
        or outcome.get("target_semantics")
        != "HFDL_SOURCE_ADJUSTED_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1"
        or outcome.get("formula") != "(exit_close / entry_open) - 1"
        or outcome.get("source_adjustment") != "hfdl_clean_source_adjusted"
        or outcome.get("canonical_split_normalized_target_equivalent") is not False
        or outcome.get("ready_input_status")
        != "BLOCKED_ACTION_AND_DELISTING_EVIDENCE"
        or outcome.get("action_evidence_status") != "UNAVAILABLE_NOT_AS_RECEIVED"
        or outcome.get("delisting_evidence_status")
        != "UNAVAILABLE_NOT_AS_RECEIVED"
        or outcome.get("preserve_unresolved_rows") is not True
        or outcome.get("compute_during_planning") is not False
    ):
        raise ContractError("legacy-discovery proxy-outcome contract differs")

    wfa = contract["wfa_input_adapter"]
    if (
        type(wfa) is not dict
        or wfa.get("mode") != "PREREGISTRATION_SCHEMA_PLAN_ONLY"
        or wfa.get("sample_key")
        != ["source_epoch", "source_series_id", "decision_session"]
        or wfa.get("decision_contract") != "DECISION_AFTER_D0_CLOSE"
        or wfa.get("entry_contract") != "D1_OPEN"
        or wfa.get("exit_contract") != "D5_CLOSE"
        or wfa.get("epochs_planned_separately") is not True
        or wfa.get("cross_epoch_pooling_allowed") is not False
        or wfa.get("training_allowed_during_planning") is not False
        or wfa.get("evaluation_allowed_during_planning") is not False
    ):
        raise ContractError("legacy-discovery WFA-input contract differs")

    derivative = contract["future_derivative_release"]
    if type(derivative) is not dict or any(
        derivative.get(name) is not True
        for name in (
            "required",
            "accepted_foundation_remains_immutable",
            "must_use_new_dataset_identity",
            "must_preserve_source_epoch",
            "full_foundation_release_verification_required_before_derivation",
            "component_provenance_verification_required_before_derivation",
            "publication_requires_separate_authorization",
            "real_history_outcome_access_requires_separate_authorization",
        )
    ):
        raise ContractError("legacy-discovery derivative-release boundary differs")
    authorities = contract["authorities"]
    if (
        type(authorities) is not dict
        or not authorities
        or any(value is not False for value in authorities.values())
    ):
        raise ContractError("legacy-discovery planning contract grants authority")
    if (
        type(contract["stop_conditions"]) is not list
        or not contract["stop_conditions"]
        or any(type(value) is not str or not value for value in contract["stop_conditions"])
    ):
        raise ContractError("legacy-discovery stop conditions are invalid")
    require_sha256(contract["contract_id"], "legacy_discovery.contract_id")


def load_legacy_discovery_bridge_contract(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = require_contained_path(root / CONTRACT_PATH, root)
    contract, _ = _read_json_object(
        path,
        label="legacy-discovery bridge contract",
        require_canonical=False,
    )
    _validate_contract(contract)
    unsigned = {name: value for name, value in contract.items() if name != "contract_id"}
    if contract["contract_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("legacy-discovery bridge contract ID differs")
    return contract


def _release_binding(value: object, *, epoch: str, kind: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"legacy-discovery {epoch}/{kind} binding must be an object")
    required = {
        "dataset",
        "epoch",
        "event_end",
        "event_start",
        "kind",
        "manifest_sha256",
        "phase",
        "quality_state",
        "relative_directory",
        "release_id",
        "role",
        "row_count",
        "source_epoch",
    }
    if (
        set(value) != required
        or value["epoch"] != epoch
        or value["source_epoch"] != epoch
        or value["kind"] != kind
        or value["phase"] != "bridge"
        or value["quality_state"] != "LEGACY_CAVEATED"
        or value["role"] != "legacy_discovery_only"
        or type(value["row_count"]) is not int
        or value["row_count"] <= 0
    ):
        raise ContractError(f"legacy-discovery {epoch}/{kind} binding differs")
    require_sha256(value["manifest_sha256"], f"{epoch}.{kind}.manifest_sha256")
    require_sha256(value["release_id"], f"{epoch}.{kind}.release_id")
    return {
        "dataset": value["dataset"],
        "release_id": value["release_id"],
        "manifest_sha256": value["manifest_sha256"],
        "row_count": value["row_count"],
        "event_start": value["event_start"],
        "event_end": value["event_end"],
        "source_epoch": epoch,
    }


def foundation_context_from_payload(
    payload: Mapping[str, Any],
    *,
    manifest: ReleaseManifest,
    manifest_sha256: str,
    payload_sha256: str,
) -> dict[str, Any]:
    """Validate foundation metadata without reading any historical row payload."""

    manifest.validate()
    require_sha256(manifest_sha256, "foundation.manifest_sha256")
    require_sha256(payload_sha256, "foundation.payload_sha256")
    if (
        manifest.project != PROJECT
        or manifest.dataset != FOUNDATION_DATASET
        or manifest.source_epoch != "hfdl_two_epoch_legacy_discovery_no_pooling"
        or manifest.role != "legacy_discovery_only"
        or manifest.quality_state != "LEGACY_CAVEATED"
        or manifest.row_count != 11
    ):
        raise ContractError("legacy-discovery foundation manifest identity differs")
    required_flags = {
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
        "point_in_time_safe": False,
        "epochs_may_be_pooled": False,
        "labels_emitted": False,
        "matured_outcomes_emitted": False,
        "model_or_evaluation_inputs_read": False,
        "wfa_executed": False,
        "real_history_hypothesis_executed": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }
    if any(payload.get(name) != expected for name, expected in required_flags.items()):
        raise ContractError("legacy-discovery foundation safety flags differ")
    source_contract = payload.get("contract")
    if (
        type(source_contract) is not dict
        or source_contract.get("project") != PROJECT
        or source_contract.get("historical_evidence_scope")
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or source_contract.get("physical_hfdl_epochs") != list(EXPECTED_EPOCHS)
        or source_contract.get("historical_release_kinds") != list(EXPECTED_KINDS)
        or source_contract.get("epochs_may_be_pooled") is not False
        or source_contract.get("labels_allowed") is not False
        or source_contract.get("models_allowed") is not False
        or source_contract.get("wfa_allowed") is not False
    ):
        raise ContractError("legacy-discovery foundation source contract differs")
    historical = payload.get("historical_foundation")
    if type(historical) is not dict or set(historical) != {"bridge_set", "build_id", "epochs"}:
        raise ContractError("legacy-discovery historical-foundation binding differs")
    require_sha256(historical["build_id"], "foundation.build_id")
    epochs = historical["epochs"]
    if type(epochs) is not dict or set(epochs) != set(EXPECTED_EPOCHS):
        raise ContractError("legacy-discovery foundation epoch census differs")
    expected_bounds = {
        "hfdl_pitrading_consolidated": ("2010-01-04", "2022-03-03"),
        "hfdl_iex_only": ("2022-03-04", "2026-06-26"),
    }
    epoch_contexts: list[dict[str, Any]] = []
    for epoch in EXPECTED_EPOCHS:
        bindings = epochs[epoch]
        if type(bindings) is not dict or set(bindings) != set(EXPECTED_KINDS):
            raise ContractError(f"legacy-discovery {epoch} component census differs")
        components = {
            kind: _release_binding(bindings[kind], epoch=epoch, kind=kind)
            for kind in EXPECTED_KINDS
        }
        starts = {value["event_start"] for value in components.values()}
        ends = {value["event_end"] for value in components.values()}
        counts = {value["row_count"] for value in components.values()}
        if (
            starts != {expected_bounds[epoch][0]}
            or ends != {expected_bounds[epoch][1]}
            or len(counts) != 1
        ):
            raise ContractError(f"legacy-discovery {epoch} bounds/row census differs")
        epoch_contexts.append(
            {
                "epoch_id": epoch,
                "event_start": expected_bounds[epoch][0],
                "event_end": expected_bounds[epoch][1],
                "row_count": next(iter(counts)),
                "components": components,
            }
        )
    calendar = payload.get("calendar")
    if (
        type(calendar) is not dict
        or type(calendar.get("release")) is not dict
        or calendar["release"].get("dataset") != "xnys_sessions"
        or calendar["release"].get("role") != "derived_causal"
        or calendar["release"].get("quality_state") != "PASS"
    ):
        raise ContractError("legacy-discovery foundation calendar binding differs")
    calendar_release_id = require_sha256(
        calendar["release"].get("release_id"),
        "foundation.calendar_release_id",
    )
    contract_id = require_sha256(payload.get("contract_id"), "foundation.contract_id")
    return {
        "foundation_release_id": manifest.release_id,
        "foundation_manifest_sha256": manifest_sha256,
        "foundation_payload_sha256": payload_sha256,
        "foundation_contract_id": contract_id,
        "foundation_build_id": historical["build_id"],
        "calendar_release_id": calendar_release_id,
        "historical_evidence_scope": required_flags["historical_evidence_scope"],
        "point_in_time_safe": False,
        "direct_model_or_evaluation_inputs_allowed": False,
        "epochs_may_be_pooled": False,
        "epochs": epoch_contexts,
    }


def load_foundation_plan_context(
    set_directory: Path,
    *,
    accepted_root: Path,
) -> dict[str, Any]:
    """Load only the set manifest and set JSON; component rows stay unopened."""

    accepted = Path(accepted_root).resolve(strict=True)
    directory = require_contained_path(Path(set_directory), accepted)
    reject_link(directory)
    if not directory.is_dir() or directory.name != directory.name.lower():
        raise ContractError("legacy-discovery foundation directory is invalid")
    require_sha256(directory.name, "foundation directory release_id")
    manifest_path = require_contained_path(directory / "release_manifest.json", accepted)
    payload_path = require_contained_path(directory / FOUNDATION_PAYLOAD, accepted)
    manifest_payload, _ = _read_json_object(
        manifest_path,
        label="legacy-discovery foundation manifest",
        require_canonical=True,
    )
    manifest = ReleaseManifest.from_dict(manifest_payload)
    if manifest.release_id != directory.name:
        raise IntegrityError("legacy-discovery foundation directory ID differs")
    payload_entry = next(
        (entry for entry in manifest.files if entry.path == FOUNDATION_PAYLOAD),
        None,
    )
    if payload_entry is None:
        raise IntegrityError("legacy-discovery foundation payload is not manifested")
    payload, raw = _read_json_object(
        payload_path,
        label="legacy-discovery foundation set payload",
        require_canonical=True,
    )
    if len(raw) != payload_entry.size or sha256_bytes(raw) != payload_entry.sha256:
        raise IntegrityError("legacy-discovery foundation set payload identity differs")
    return foundation_context_from_payload(
        payload,
        manifest=manifest,
        manifest_sha256=sha256_file(manifest_path),
        payload_sha256=sha256_bytes(raw),
    )


def _repository_binding(root: Path) -> dict[str, str]:
    commands = {
        "root": ["git", "rev-parse", "--show-toplevel"],
        "branch": ["git", "branch", "--show-current"],
        "head": ["git", "rev-parse", "HEAD"],
        "tree": ["git", "rev-parse", "HEAD^{tree}"],
        "status": ["git", "status", "--porcelain"],
    }
    values = {
        name: subprocess.run(
            argv,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        for name, argv in commands.items()
    }
    if Path(values["root"]).resolve() != root or values["branch"] != "main":
        raise ContractError("legacy-discovery planner repository identity differs")
    if values["status"]:
        raise ContractError("legacy-discovery planner requires a clean repository")
    if GIT_OBJECT.fullmatch(values["head"]) is None or GIT_OBJECT.fullmatch(values["tree"]) is None:
        raise ContractError("legacy-discovery planner Git object identity differs")
    return {"head": values["head"], "tree": values["tree"]}


def plan_from_context(
    *,
    contract: Mapping[str, Any],
    foundation: Mapping[str, Any],
    repository: Mapping[str, str],
) -> dict[str, Any]:
    _validate_contract(contract)
    for name in (
        "foundation_release_id",
        "foundation_manifest_sha256",
        "foundation_payload_sha256",
        "foundation_contract_id",
        "foundation_build_id",
        "calendar_release_id",
    ):
        require_sha256(foundation.get(name), f"legacy_discovery.foundation.{name}")
    if (
        foundation.get("historical_evidence_scope")
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or foundation.get("point_in_time_safe") is not False
        or foundation.get("direct_model_or_evaluation_inputs_allowed") is not False
        or foundation.get("epochs_may_be_pooled") is not False
    ):
        raise ContractError("legacy-discovery foundation safety boundary differs")
    epochs = foundation.get("epochs")
    if (
        type(epochs) is not list
        or [value.get("epoch_id") for value in epochs] != list(EXPECTED_EPOCHS)
    ):
        raise ContractError("legacy-discovery plan epoch order/census differs")
    if (
        type(repository) is not dict
        or set(repository) != {"head", "tree"}
        or GIT_OBJECT.fullmatch(repository["head"]) is None
        or GIT_OBJECT.fullmatch(repository["tree"]) is None
    ):
        raise ContractError("legacy-discovery plan repository binding differs")

    epoch_plans = []
    for epoch in epochs:
        components = epoch.get("components")
        if type(components) is not dict or set(components) != set(EXPECTED_KINDS):
            raise ContractError("legacy-discovery plan component census differs")
        epoch_plans.append(
            {
                "epoch_id": epoch["epoch_id"],
                "event_start": epoch["event_start"],
                "event_end": epoch["event_end"],
                "row_count": epoch["row_count"],
                "source_components": {
                    kind: components[kind] for kind in EXPECTED_KINDS
                },
                "proxy_eligibility_adapter": contract["proxy_eligibility"],
                "feature_adapter": contract["feature_adapter"],
                "proxy_outcome_adapter": contract["outcome_adapter"],
                "wfa_input_adapter": contract["wfa_input_adapter"],
                "execution_state": "PLANNED_NOT_AUTHORIZED",
            }
        )
    unsigned = {
        "schema_version": 1,
        "mode": PLAN_MODE,
        "contract_id": contract["contract_id"],
        "repository": dict(repository),
        "foundation": {
            name: foundation[name]
            for name in (
                "foundation_release_id",
                "foundation_manifest_sha256",
                "foundation_payload_sha256",
                "foundation_contract_id",
                "foundation_build_id",
                "calendar_release_id",
                "historical_evidence_scope",
                "point_in_time_safe",
                "direct_model_or_evaluation_inputs_allowed",
                "epochs_may_be_pooled",
            )
        },
        "metadata_validation_scope": {
            "release_manifest_validated": True,
            "foundation_set_payload_hash_validated": True,
            "component_row_payloads_opened": False,
            "complete_accepted_release_verification_performed": False,
            "complete_verification_required_before_derivation": True,
        },
        "epoch_plans": epoch_plans,
        "cross_epoch_disposition": {
            "pooling_allowed": False,
            "relabeling_allowed": False,
            "separate_derivative_releases_required": True,
            "separate_wfa_plans_required": True,
            "cross_epoch_comparison": "DIAGNOSTIC_ONLY",
        },
        "future_derivative_release": contract["future_derivative_release"],
        "preregistration_gate": {
            "state": "REQUIRED_BEFORE_ANY_REAL_HISTORY_OUTCOME_ACCESS",
            "required_bindings": contract["wfa_input_adapter"][
                "required_preregistration_bindings"
            ],
            "registered_real_history_executor": None,
            "trial_counted": True,
        },
        "output_disposition": {
            "conversation_json_only": True,
            "files_written": 0,
            "generated_evidence_mutated": False,
            "historical_rows_read": 0,
            "outcomes_computed": 0,
            "models_fit": 0,
        },
        "authorities": contract["authorities"],
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_legacy_discovery_bridge_plan(
    set_directory: Path,
    *,
    accepted_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    reject_hfdl_work(
        root,
        requested_action="new_hfdl_bridge_planning",
    )
    contract = load_legacy_discovery_bridge_contract(root)
    foundation = load_foundation_plan_context(
        set_directory,
        accepted_root=accepted_root,
    )
    repository = _repository_binding(root)
    return plan_from_context(
        contract=contract,
        foundation=foundation,
        repository=repository,
    )
