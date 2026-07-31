"""Synthetic-only mechanics for a legacy-discovery proxy derivative.

The module has two deliberately separate surfaces:

* ``verify_proxy_source_closure`` fully verifies accepted component releases
  and their metadata before any future derivation.
* ``materialize_synthetic_proxy_derivative`` proves row mechanics only for a
  caller-bound synthetic fixture.

There is no real-row reader, publisher, trainer, evaluator, or WFA entrypoint.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import canonical_json_bytes, reject_link, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError
from .releases import ReleaseManifest, verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
EXPECTED_KINDS = ("causal_bars", "feature_inputs", "outcome_inputs")
EXPECTED_EPOCHS = ("hfdl_pitrading_consolidated", "hfdl_iex_only")
DERIVATIVE_SCOPE = "SYNTHETIC_LEGACY_DISCOVERY_PROXY_DERIVATIVE"
TARGET_SEMANTICS = (
    "HFDL_SOURCE_ADJUSTED_NEXT_OPEN_TO_FIFTH_CLOSE_"
    "SIMPLE_PRICE_RETURN_PROXY_V1"
)
_PROVENANCE_FIELDS = {
    "schema_version",
    "build_id",
    "source_epoch",
    "output_kind",
    "source_hfdl_release_id",
    "source_hfdl_set_release_id",
    "calendar_release_id",
    "causal_bar_release_id",
    "contract_id",
    "quality_state",
    "role",
    "evidence_class",
    "point_in_time_safe",
    "point_in_time_state",
    "historical_availability_state",
    "source_adjustment",
    "membership_evidence_status",
    "security_type_evidence_status",
    "action_evidence_status",
    "delisting_evidence_status",
    "source_series_id_is_persistent_asset_identity",
    "epochs_may_be_pooled",
    "model_or_evaluation_inputs_read",
    "real_history_hypothesis_executed",
    "matured_outcomes_emitted",
    "alpha_evidence",
    "candidate_eligible",
}

_CENSUS_FIELDS = {
    "schema_version",
    "source_epoch",
    "output_kind",
    "source_series_count",
    "source_rows",
    "calendar_sessions_in_epoch",
    "calendar_symbol_session_denominator",
    "noncalendar_source_rows",
    "output_rows",
    "status_counts",
    "missing_status_rows",
    "evidence_denominator_rows",
    "membership_evidence_available_rows",
    "membership_evidence_unknown_rows",
    "security_type_evidence_available_rows",
    "security_type_evidence_unknown_rows",
    "action_evidence_available_rows",
    "action_evidence_unavailable_rows",
    "delisting_evidence_available_rows",
    "delisting_evidence_unavailable_rows",
    "outcome_evaluable_rows",
    "matured_outcome_rows",
    "historical_evidence_scope",
}

_CAVEAT_FIELDS = {
    "source_epoch",
    "source_adjustment",
    "evidence_class",
    "point_in_time_state",
    "historical_availability_state",
    "calendar_release_id",
    "membership_evidence_status",
    "security_type_evidence_status",
    "action_evidence_status",
    "delisting_evidence_status",
}

_FEATURE_FIELDS = {
    "source_series_id",
    "symbol",
    "decision_session",
    "decision_at",
    "feature_status",
    "close_to_close_return_1",
    "intraday_return",
    "range_fraction",
    "log1p_volume",
    *_CAVEAT_FIELDS,
}

_OUTCOME_FIELDS = {
    "source_series_id",
    "symbol",
    "decision_session",
    "entry_session",
    "exit_session",
    "entry_open",
    "exit_close",
    "split_normalized_price_return",
    "outcome_input_status",
    *_CAVEAT_FIELDS,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _strict_mapping(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        raise ContractError(f"{name} fields differ from the exact contract")
    return value


def load_legacy_discovery_derivative_contract(
    repository_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    root = Path(repository_root) if repository_root is not None else _repo_root()
    path = root / "config" / "legacy_discovery_derivative_contract.json"
    reject_link(path)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("legacy-discovery derivative contract is unreadable") from exc
    expected = {
        "schema_version",
        "contract_version",
        "project",
        "contract_type",
        "bridge_contract_id",
        "source_closure",
        "proxy_derivative",
        "preregistered_adapter",
        "synthetic_mechanics",
        "authorities",
        "stop_conditions",
        "contract_id",
    }
    contract = _strict_mapping(payload, expected, "derivative contract")
    unsigned = {key: value for key, value in contract.items() if key != "contract_id"}
    observed_id = sha256_bytes(canonical_json_bytes(unsigned))
    if contract["contract_id"] != observed_id:
        raise IntegrityError("derivative contract ID differs from canonical content")
    source = _strict_mapping(
        contract["source_closure"],
        {
            "required_output_kinds",
            "role",
            "quality_state",
            "evidence_class",
            "historical_evidence_scope",
            "source_adjustment",
            "point_in_time_safe",
            "epochs_may_be_pooled",
            "required_source_epochs",
            "full_foundation_set_verification_required",
            "component_payload_hash_verification_required",
            "component_provenance_verification_required",
        },
        "derivative source closure",
    )
    proxy = _strict_mapping(
        contract["proxy_derivative"],
        {
            "sample_key",
            "feature_names",
            "feature_ready_status",
            "outcome_ready_input_status",
            "target_semantics",
            "target_formula",
            "canonical_split_normalized_target_equivalent",
            "membership_evidence_status",
            "security_type_evidence_status",
            "action_evidence_status",
            "delisting_evidence_status",
            "source_series_id_is_persistent_asset_identity",
            "preserve_every_input_row",
            "trusted_sleeves",
            "diagnostic_sleeves",
        },
        "proxy derivative",
    )
    preregistered = _strict_mapping(
        contract["preregistered_adapter"],
        {
            "mode",
            "evidence_class",
            "required_bindings",
            "accepted_derivative_release_required_for_real_use",
            "epochs_registered_separately",
            "executor_entrypoint",
            "real_history_execution_authorized",
        },
        "preregistered adapter",
    )
    synthetic = _strict_mapping(
        contract["synthetic_mechanics"],
        {
            "derivative_permit_scope",
            "adapter_permit_scope",
            "fixture_root_required",
            "generated_evidence_eligible",
            "alpha_evidence",
            "candidate_eligible",
        },
        "synthetic mechanics",
    )
    authorities = _strict_mapping(
        contract["authorities"],
        {
            "real_row_access",
            "proxy_outcome_computation_on_real_rows",
            "generated_evidence_write",
            "derivative_publication",
            "training",
            "evaluation",
            "real_history_wfa",
            "candidate_sealing",
            "trusted_readiness_claim",
            "alpha_claim",
        },
        "derivative authorities",
    )
    if (
        contract["schema_version"] != 1
        or contract["contract_version"] != "1.0.0"
        or contract["project"] != PROJECT
        or contract["contract_type"]
        != "LEGACY_DISCOVERY_PROXY_DERIVATIVE_SYNTHETIC_MECHANICS"
        or contract["bridge_contract_id"]
        != "e4ee1cccd52cb5a0af5f574eea07c46793ef522f928a6bb08ed8e017b0cfdfb3"
        or source
        != {
            "required_output_kinds": list(EXPECTED_KINDS),
            "role": "legacy_discovery_only",
            "quality_state": "LEGACY_CAVEATED",
            "evidence_class": "LEGACY_DISCOVERY",
            "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
            "source_adjustment": "hfdl_clean_source_adjusted",
            "point_in_time_safe": False,
            "epochs_may_be_pooled": False,
            "required_source_epochs": list(EXPECTED_EPOCHS),
            "full_foundation_set_verification_required": True,
            "component_payload_hash_verification_required": True,
            "component_provenance_verification_required": True,
        }
        or proxy["sample_key"]
        != ["source_epoch", "source_series_id", "decision_session"]
        or proxy["feature_names"]
        != [
            "close_to_close_return_1",
            "intraday_return",
            "range_fraction",
            "log1p_volume",
        ]
        or proxy["feature_ready_status"] != "PRICE_INPUT_READY_PIT_UNRESOLVED"
        or proxy["outcome_ready_input_status"]
        != "BLOCKED_ACTION_AND_DELISTING_EVIDENCE"
        or proxy["target_semantics"] != TARGET_SEMANTICS
        or proxy["target_formula"] != "(exit_close / entry_open) - 1"
        or proxy["canonical_split_normalized_target_equivalent"] is not False
        or proxy["membership_evidence_status"] != "UNKNOWN_NOT_AS_RECEIVED"
        or proxy["security_type_evidence_status"] != "UNKNOWN_NOT_AS_RECEIVED"
        or proxy["action_evidence_status"] != "UNAVAILABLE_NOT_AS_RECEIVED"
        or proxy["delisting_evidence_status"] != "UNAVAILABLE_NOT_AS_RECEIVED"
        or proxy["source_series_id_is_persistent_asset_identity"] is not False
        or proxy["preserve_every_input_row"] is not True
        or proxy["trusted_sleeves"] != []
        or proxy["diagnostic_sleeves"]
        != ["proxy_unknown_long", "proxy_unknown_short"]
        or preregistered["mode"] != "PREREGISTERED_INPUT_ADAPTER_ONLY"
        or preregistered["evidence_class"] != "REGISTERED_HISTORICAL_DISCOVERY"
        or preregistered["required_bindings"]
        != [
            "trial_declaration_id",
            "trial_registry_binding_id",
            "trial_ledger_head_id",
            "charter_id",
            "feature_spec_id",
            "proxy_label_spec_id",
            "split_spec_id",
            "cost_spec_id",
            "robustness_policy_id",
            "code_commit",
            "code_hash",
            "environment_id",
        ]
        or preregistered["accepted_derivative_release_required_for_real_use"]
        is not True
        or preregistered["epochs_registered_separately"] is not True
        or preregistered["executor_entrypoint"] is not None
        or preregistered["real_history_execution_authorized"] is not False
        or synthetic
        != {
            "derivative_permit_scope": DERIVATIVE_SCOPE,
            "adapter_permit_scope":
                "SYNTHETIC_LEGACY_DISCOVERY_PREREGISTERED_ADAPTER",
            "fixture_root_required": True,
            "generated_evidence_eligible": False,
            "alpha_evidence": False,
            "candidate_eligible": False,
        }
        or any(type(value) is not bool or value for value in authorities.values())
        or type(contract["stop_conditions"]) is not list
        or not contract["stop_conditions"]
        or any(
            type(value) is not str or not value
            for value in contract["stop_conditions"]
        )
    ):
        raise ContractError("derivative contract weakens the discovery-only boundary")
    return contract, observed_id


def _read_exact_metadata(
    release_directory: Path,
    manifest: ReleaseManifest,
    name: str,
    fields: set[str],
) -> dict[str, Any]:
    declared = {entry.path for entry in manifest.files}
    if name not in declared:
        raise IntegrityError(f"verified component release omits {name}")
    path = release_directory / name
    reject_link(path)
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"verified component {name} is unreadable") from exc
    if raw != canonical_json_bytes(payload):
        raise IntegrityError(f"verified component {name} is not canonical JSON")
    return _strict_mapping(payload, fields, f"component {name}")


def _require_exact_nonnegative_int(value: object, name: str) -> int:
    if type(value) is not int or value < 0:
        raise IntegrityError(f"{name} must be a nonnegative exact integer")
    return value


def _validate_census(
    census: Mapping[str, Any],
    *,
    manifest: ReleaseManifest,
    epoch: str,
    kind: str,
) -> None:
    if (
        census["schema_version"] != 1
        or census["source_epoch"] != epoch
        or census["output_kind"] != kind
        or census["historical_evidence_scope"]
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    ):
        raise IntegrityError("component census identity differs")
    numeric = {
        name: _require_exact_nonnegative_int(census[name], f"census.{name}")
        for name in _CENSUS_FIELDS
        if name
        not in {
            "schema_version",
            "source_epoch",
            "output_kind",
            "status_counts",
            "historical_evidence_scope",
        }
    }
    statuses = census["status_counts"]
    if (
        type(statuses) is not dict
        or not statuses
        or any(type(key) is not str or not key for key in statuses)
        or any(type(value) is not int or value < 0 for value in statuses.values())
        or sum(statuses.values()) != numeric["output_rows"]
        or numeric["output_rows"] != manifest.row_count
        or numeric["source_series_count"] == 0
        or numeric["calendar_symbol_session_denominator"]
        != numeric["source_series_count"] * numeric["calendar_sessions_in_epoch"]
        or numeric["output_rows"]
        != numeric["calendar_symbol_session_denominator"]
        + numeric["noncalendar_source_rows"]
        or numeric["missing_status_rows"]
        != sum(
            count
            for status, count in statuses.items()
            if status.startswith("MISSING_")
        )
        or numeric["evidence_denominator_rows"] != numeric["output_rows"]
        or numeric["membership_evidence_available_rows"] != 0
        or numeric["membership_evidence_unknown_rows"] != numeric["output_rows"]
        or numeric["security_type_evidence_available_rows"] != 0
        or numeric["security_type_evidence_unknown_rows"] != numeric["output_rows"]
        or numeric["action_evidence_available_rows"] != 0
        or numeric["action_evidence_unavailable_rows"] != numeric["output_rows"]
        or numeric["delisting_evidence_available_rows"] != 0
        or numeric["delisting_evidence_unavailable_rows"] != numeric["output_rows"]
        or numeric["outcome_evaluable_rows"] != 0
        or numeric["matured_outcome_rows"] != 0
    ):
        raise IntegrityError("component census weakens or misstates evidence coverage")


def _validate_provenance(
    provenance: Mapping[str, Any],
    *,
    manifest: ReleaseManifest,
    epoch: str,
    kind: str,
) -> None:
    sha_fields = (
        "build_id",
        "source_hfdl_release_id",
        "source_hfdl_set_release_id",
        "calendar_release_id",
        "contract_id",
    )
    for name in sha_fields:
        require_sha256(provenance[name], f"provenance.{name}")
    if provenance["causal_bar_release_id"] is not None:
        require_sha256(
            provenance["causal_bar_release_id"],
            "provenance.causal_bar_release_id",
        )
    required = {
        "schema_version": 1,
        "source_epoch": epoch,
        "output_kind": kind,
        "quality_state": "LEGACY_CAVEATED",
        "role": "legacy_discovery_only",
        "evidence_class": "LEGACY_DISCOVERY",
        "point_in_time_safe": False,
        "point_in_time_state": "UNRESOLVED_NOT_AS_RECEIVED",
        "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
        "source_adjustment": "hfdl_clean_source_adjusted",
        "membership_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "security_type_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "action_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "delisting_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "source_series_id_is_persistent_asset_identity": False,
        "epochs_may_be_pooled": False,
        "model_or_evaluation_inputs_read": False,
        "real_history_hypothesis_executed": False,
        "matured_outcomes_emitted": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }
    if any(provenance[name] != value for name, value in required.items()):
        raise IntegrityError("component provenance weakens the discovery-only boundary")
    if (
        manifest.project != PROJECT
        or manifest.dataset != f"{epoch}_{kind}"
        or manifest.source_epoch != epoch
        or manifest.role != "legacy_discovery_only"
        or manifest.quality_state != "LEGACY_CAVEATED"
    ):
        raise IntegrityError("component release manifest identity differs")


@dataclass(frozen=True)
class VerifiedProxyComponent:
    kind: str
    release_id: str
    dataset: str
    row_count: int
    event_start: str
    event_end: str
    build_id: str
    source_hfdl_release_id: str
    source_hfdl_set_release_id: str
    calendar_release_id: str
    foundation_contract_id: str
    causal_bar_release_id: str | None
    manifest_sha256: str
    census_sha256: str
    provenance_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProxySourceClosure:
    schema_version: int
    project: str
    source_epoch: str
    derivative_contract_id: str
    foundation_set_release_id: str
    foundation_set_payload_sha256: str
    components: tuple[VerifiedProxyComponent, ...]
    source_closure_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "source_epoch": self.source_epoch,
            "derivative_contract_id": self.derivative_contract_id,
            "foundation_set_release_id": self.foundation_set_release_id,
            "foundation_set_payload_sha256": self.foundation_set_payload_sha256,
            "components": [item.as_dict() for item in self.components],
        }

    def validate(self) -> None:
        if (
            self.schema_version != 1
            or self.project != PROJECT
            or tuple(item.kind for item in self.components) != EXPECTED_KINDS
            or any(item.row_count < 0 for item in self.components)
        ):
            raise IntegrityError("proxy source closure shape differs")
        require_sha256(self.derivative_contract_id, "derivative_contract_id")
        require_sha256(self.foundation_set_release_id, "foundation_set_release_id")
        require_sha256(
            self.foundation_set_payload_sha256,
            "foundation_set_payload_sha256",
        )
        require_sha256(self.source_closure_id, "source_closure_id")
        if self.source_closure_id != sha256_bytes(
            canonical_json_bytes(self.unsigned_dict())
        ):
            raise IntegrityError("proxy source closure ID differs from its content")


def _verify_foundation_binding(
    foundation_set_directory: Path,
    *,
    accepted_root: Path,
    expected_epoch: str,
    components: Sequence[VerifiedProxyComponent],
) -> tuple[str, str]:
    manifest = verify_accepted_release(
        foundation_set_directory,
        accepted_root=accepted_root,
    )
    if (
        manifest.project != PROJECT
        or manifest.dataset != "stock_historical_foundation_set"
        or manifest.source_epoch != "hfdl_two_epoch_legacy_discovery_no_pooling"
        or manifest.role != "legacy_discovery_only"
        or manifest.quality_state != "LEGACY_CAVEATED"
    ):
        raise IntegrityError("foundation-set release identity differs")
    declared = {entry.path for entry in manifest.files}
    if "foundation_set.json" not in declared:
        raise IntegrityError("foundation-set release omits foundation_set.json")
    payload_path = foundation_set_directory / "foundation_set.json"
    reject_link(payload_path)
    try:
        raw = payload_path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("foundation-set payload is unreadable") from exc
    if raw != canonical_json_bytes(payload):
        raise IntegrityError("foundation-set payload is not canonical JSON")
    expected_top = {
        "historical_evidence_scope",
        "point_in_time_safe",
        "epochs_may_be_pooled",
        "labels_emitted",
        "matured_outcomes_emitted",
        "model_or_evaluation_inputs_read",
        "wfa_executed",
        "real_history_hypothesis_executed",
        "alpha_evidence",
        "candidate_eligible",
        "contract_id",
        "contract",
        "historical_foundation",
        "calendar",
    }
    foundation = _strict_mapping(payload, expected_top, "foundation-set payload")
    if (
        foundation["historical_evidence_scope"]
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or foundation["point_in_time_safe"] is not False
        or foundation["epochs_may_be_pooled"] is not False
        or any(
            foundation[name] is not False
            for name in (
                "labels_emitted",
                "matured_outcomes_emitted",
                "model_or_evaluation_inputs_read",
                "wfa_executed",
                "real_history_hypothesis_executed",
                "alpha_evidence",
                "candidate_eligible",
            )
        )
    ):
        raise IntegrityError("foundation-set payload weakens its evidence boundary")
    require_sha256(foundation["contract_id"], "foundation_set.contract_id")
    historical = foundation["historical_foundation"]
    if (
        type(historical) is not dict
        or set(historical) != {"bridge_set", "build_id", "epochs"}
        or type(historical["epochs"]) is not dict
        or set(historical["epochs"]) != set(EXPECTED_EPOCHS)
        or expected_epoch not in historical["epochs"]
    ):
        raise IntegrityError("foundation-set epoch census differs")
    require_sha256(historical["build_id"], "foundation_set.build_id")
    epoch_bindings = historical["epochs"][expected_epoch]
    if type(epoch_bindings) is not dict or tuple(epoch_bindings) != EXPECTED_KINDS:
        raise IntegrityError("foundation-set component census differs")
    expected_binding_fields = {
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
    by_kind = {component.kind: component for component in components}
    for kind in EXPECTED_KINDS:
        binding = _strict_mapping(
            epoch_bindings[kind],
            expected_binding_fields,
            f"foundation-set {kind} binding",
        )
        component = by_kind[kind]
        require_sha256(binding["manifest_sha256"], f"foundation_set.{kind}.manifest")
        if (
            binding["dataset"] != component.dataset
            or binding["epoch"] != expected_epoch
            or binding["source_epoch"] != expected_epoch
            or binding["kind"] != kind
            or binding["release_id"] != component.release_id
            or binding["manifest_sha256"] != component.manifest_sha256
            or binding["row_count"] != component.row_count
            or binding["event_start"] != component.event_start
            or binding["event_end"] != component.event_end
            or binding["role"] != "legacy_discovery_only"
            or binding["quality_state"] != "LEGACY_CAVEATED"
            or binding["phase"] != "bridge"
        ):
            raise IntegrityError("foundation-set component binding differs")
    calendar = foundation["calendar"]
    if (
        type(calendar) is not dict
        or type(calendar.get("release")) is not dict
        or calendar["release"].get("release_id") != components[0].calendar_release_id
        or historical["build_id"] != components[0].build_id
        or foundation["contract_id"] != components[0].foundation_contract_id
    ):
        raise IntegrityError("foundation-set build, calendar, or contract binding differs")
    return (
        manifest.release_id,
        sha256_bytes(raw),
    )


def verify_proxy_source_closure(
    release_directories: Mapping[str, Path],
    *,
    foundation_set_directory: Path,
    accepted_root: Path,
    expected_epoch: str,
    repository_root: Path | None = None,
) -> ProxySourceClosure:
    """Fully verify one epoch's three accepted releases and metadata closure."""

    if (
        type(release_directories) is not dict
        or tuple(release_directories) != EXPECTED_KINDS
        or type(expected_epoch) is not str
        or not expected_epoch
    ):
        raise ContractError("component directories must use exact output-kind order")
    if expected_epoch not in EXPECTED_EPOCHS:
        raise ContractError("proxy source closure requires one declared HFDL epoch")
    _contract, contract_id = load_legacy_discovery_derivative_contract(repository_root)
    components: list[VerifiedProxyComponent] = []
    provenances: dict[str, dict[str, Any]] = {}
    for kind in EXPECTED_KINDS:
        directory = Path(release_directories[kind])
        manifest = verify_accepted_release(directory, accepted_root=Path(accepted_root))
        provenance = _read_exact_metadata(
            directory, manifest, "provenance.json", _PROVENANCE_FIELDS
        )
        census = _read_exact_metadata(
            directory, manifest, "census.json", _CENSUS_FIELDS
        )
        _validate_provenance(
            provenance, manifest=manifest, epoch=expected_epoch, kind=kind
        )
        _validate_census(census, manifest=manifest, epoch=expected_epoch, kind=kind)
        if manifest.event_start is None or manifest.event_end is None:
            raise IntegrityError("component release omits event bounds")
        provenances[kind] = provenance
        components.append(
            VerifiedProxyComponent(
                kind=kind,
                release_id=manifest.release_id,
                dataset=manifest.dataset,
                row_count=manifest.row_count,
                event_start=manifest.event_start,
                event_end=manifest.event_end,
                build_id=provenance["build_id"],
                source_hfdl_release_id=provenance["source_hfdl_release_id"],
                source_hfdl_set_release_id=provenance["source_hfdl_set_release_id"],
                calendar_release_id=provenance["calendar_release_id"],
                foundation_contract_id=provenance["contract_id"],
                causal_bar_release_id=provenance["causal_bar_release_id"],
                manifest_sha256=sha256_bytes(
                    (directory / "release_manifest.json").read_bytes()
                ),
                census_sha256=sha256_bytes(canonical_json_bytes(census)),
                provenance_sha256=sha256_bytes(canonical_json_bytes(provenance)),
            )
        )
    common_fields = (
        "build_id",
        "source_hfdl_release_id",
        "source_hfdl_set_release_id",
        "calendar_release_id",
        "contract_id",
    )
    if any(
        len({provenances[kind][field] for kind in EXPECTED_KINDS}) != 1
        for field in common_fields
    ):
        raise IntegrityError("component provenance closure differs across output kinds")
    causal_release_id = components[0].release_id
    if (
        provenances["causal_bars"]["causal_bar_release_id"] is not None
        or provenances["feature_inputs"]["causal_bar_release_id"] != causal_release_id
        or provenances["outcome_inputs"]["causal_bar_release_id"] != causal_release_id
        or len({(item.event_start, item.event_end) for item in components}) != 1
        or components[1].row_count != components[2].row_count
    ):
        raise IntegrityError("component causal, bounds, or row-census binding differs")
    foundation_set_release_id, foundation_set_payload_sha256 = (
        _verify_foundation_binding(
            Path(foundation_set_directory),
            accepted_root=Path(accepted_root),
            expected_epoch=expected_epoch,
            components=components,
        )
    )
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "source_epoch": expected_epoch,
        "derivative_contract_id": contract_id,
        "foundation_set_release_id": foundation_set_release_id,
        "foundation_set_payload_sha256": foundation_set_payload_sha256,
        "components": [item.as_dict() for item in components],
    }
    closure = ProxySourceClosure(
        schema_version=1,
        project=PROJECT,
        source_epoch=expected_epoch,
        derivative_contract_id=contract_id,
        foundation_set_release_id=foundation_set_release_id,
        foundation_set_payload_sha256=foundation_set_payload_sha256,
        components=tuple(components),
        source_closure_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    closure.validate()
    return closure


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
            raise ContractError("synthetic datetime must be timezone-aware UTC")
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if type(value) in {str, int, float, bool} or value is None:
        if type(value) is float and not math.isfinite(value):
            raise ContractError("synthetic fixture contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise ContractError("synthetic fixture contains an unsupported value type")


def synthetic_derivative_fixture_id(
    closure: ProxySourceClosure,
    feature_rows: Sequence[Mapping[str, object]],
    outcome_rows: Sequence[Mapping[str, object]],
) -> str:
    closure.validate()
    return sha256_bytes(
        canonical_json_bytes(
            {
                "source_closure_id": closure.source_closure_id,
                "feature_rows": _canonical_value(feature_rows),
                "outcome_rows": _canonical_value(outcome_rows),
            }
        )
    )


def _require_row_caveats(
    row: Mapping[str, object],
    *,
    closure: ProxySourceClosure,
) -> None:
    expected = {
        "source_epoch": closure.source_epoch,
        "source_adjustment": "hfdl_clean_source_adjusted",
        "evidence_class": "LEGACY_DISCOVERY",
        "point_in_time_state": "UNRESOLVED_NOT_AS_RECEIVED",
        "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
        "calendar_release_id": closure.components[0].calendar_release_id,
        "membership_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "security_type_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "action_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "delisting_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
    }
    if any(row[name] != value for name, value in expected.items()):
        raise ContractError("synthetic row caveats differ from verified source closure")


def _sample_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    source_series_id = row["source_series_id"]
    session = row["decision_session"]
    if type(source_series_id) is not str:
        raise ContractError("source_series_id must be an exact string")
    require_sha256(source_series_id, "source_series_id")
    if type(session) is not date:
        raise ContractError("decision_session must be an exact date")
    return (str(row["source_epoch"]), source_series_id, session.isoformat())


def _require_sorted_unique_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    fields: set[str],
    name: str,
    closure: ProxySourceClosure,
) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(rows, (list, tuple)):
        raise ContractError(f"{name} rows must be an ordered sequence")
    keys: list[tuple[str, str, str]] = []
    for row in rows:
        _strict_mapping(row, fields, f"{name} row")
        _require_row_caveats(row, closure=closure)
        keys.append(_sample_key(row))
    if keys != sorted(set(keys)):
        raise ContractError(f"{name} rows must have sorted unique sample keys")
    return tuple(keys)


def _finite_optional(value: object, name: str) -> float | None:
    if value is None:
        return None
    if type(value) not in {int, float} or type(value) is bool:
        raise ContractError(f"{name} must be an explicit number or null")
    result = float(value)
    if not math.isfinite(result):
        raise ContractError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class ProxyEligibilityRow:
    sample_key: tuple[str, str, str]
    symbol: str
    historical_proxy: bool
    persistent_asset_identity: bool
    trusted_sleeve: None
    eligibility_status: str


@dataclass(frozen=True)
class ProxyFeatureRow:
    sample_key: tuple[str, str, str]
    feature_status: str
    values: tuple[float | None, float | None, float | None, float | None]


@dataclass(frozen=True)
class ProxyOutcomeRow:
    sample_key: tuple[str, str, str]
    input_status: str
    proxy_status: str
    target_semantics: str
    proxy_return: float | None
    canonical_split_normalized_target_equivalent: bool


@dataclass(frozen=True)
class ProxyWfaSample:
    sample_key: tuple[str, str, str]
    feature_status: str
    proxy_status: str
    feature_values: tuple[float | None, float | None, float | None, float | None]
    proxy_return: float | None
    mechanically_complete: bool
    trusted_gate_eligible: bool
    real_history_execution_authorized: bool


@dataclass(frozen=True)
class InMemoryProxyDerivative:
    schema_version: int
    source_epoch: str
    source_closure_id: str
    derivative_contract_id: str
    fixture_id: str
    synthetic_permit_id: str
    eligibility_rows: tuple[ProxyEligibilityRow, ...]
    feature_rows: tuple[ProxyFeatureRow, ...]
    outcome_rows: tuple[ProxyOutcomeRow, ...]
    wfa_samples: tuple[ProxyWfaSample, ...]
    trusted_sleeves: tuple[()]
    diagnostic_sleeves: tuple[str, ...]
    real_history_execution_authorized: bool
    candidate_eligible: bool
    alpha_evidence: bool
    derivative_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_epoch": self.source_epoch,
            "source_closure_id": self.source_closure_id,
            "derivative_contract_id": self.derivative_contract_id,
            "fixture_id": self.fixture_id,
            "synthetic_permit_id": self.synthetic_permit_id,
            "eligibility_rows": [_canonical_value(asdict(row)) for row in self.eligibility_rows],
            "feature_rows": [_canonical_value(asdict(row)) for row in self.feature_rows],
            "outcome_rows": [_canonical_value(asdict(row)) for row in self.outcome_rows],
            "wfa_samples": [_canonical_value(asdict(row)) for row in self.wfa_samples],
            "trusted_sleeves": list(self.trusted_sleeves),
            "diagnostic_sleeves": list(self.diagnostic_sleeves),
            "real_history_execution_authorized": self.real_history_execution_authorized,
            "candidate_eligible": self.candidate_eligible,
            "alpha_evidence": self.alpha_evidence,
        }

    def validate(self) -> None:
        require_sha256(self.derivative_id, "derivative_id")
        if (
            self.schema_version != 1
            or self.trusted_sleeves != ()
            or self.diagnostic_sleeves != ("proxy_unknown_long", "proxy_unknown_short")
            or self.real_history_execution_authorized
            or self.candidate_eligible
            or self.alpha_evidence
            or not self.wfa_samples
            or not (
                len(self.eligibility_rows)
                == len(self.feature_rows)
                == len(self.outcome_rows)
                == len(self.wfa_samples)
            )
        ):
            raise ContractError("in-memory proxy derivative weakens its evidence boundary")
        if self.derivative_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("in-memory proxy derivative ID differs from content")


def materialize_synthetic_proxy_derivative(
    closure: ProxySourceClosure,
    feature_rows: Sequence[Mapping[str, object]],
    outcome_rows: Sequence[Mapping[str, object]],
    *,
    permit: SyntheticOnlyPermit | None,
) -> InMemoryProxyDerivative:
    """Derive proxy rows in memory only from one exactly bound synthetic fixture."""

    closure.validate()
    synthetic = require_synthetic_permit(permit, scope=DERIVATIVE_SCOPE)
    fixture_id = synthetic_derivative_fixture_id(closure, feature_rows, outcome_rows)
    if synthetic.fixture_id != fixture_id:
        raise ContractError("synthetic permit does not bind the exact derivative fixture")
    feature_keys = _require_sorted_unique_rows(
        feature_rows,
        fields=_FEATURE_FIELDS,
        name="feature",
        closure=closure,
    )
    outcome_keys = _require_sorted_unique_rows(
        outcome_rows,
        fields=_OUTCOME_FIELDS,
        name="outcome",
        closure=closure,
    )
    if (
        feature_keys != outcome_keys
        or len(feature_rows) != closure.components[1].row_count
        or len(outcome_rows) != closure.components[2].row_count
    ):
        raise ContractError("feature/outcome sample census differs from verified closure")

    eligibility: list[ProxyEligibilityRow] = []
    features: list[ProxyFeatureRow] = []
    outcomes: list[ProxyOutcomeRow] = []
    samples: list[ProxyWfaSample] = []
    names = (
        "close_to_close_return_1",
        "intraday_return",
        "range_fraction",
        "log1p_volume",
    )
    for key, feature, outcome in zip(feature_keys, feature_rows, outcome_rows, strict=True):
        if (
            feature["symbol"] != outcome["symbol"]
            or feature["decision_session"] != outcome["decision_session"]
            or type(feature["symbol"]) is not str
            or not feature["symbol"]
        ):
            raise ContractError("feature/outcome symbol or decision binding differs")
        if outcome["split_normalized_price_return"] is not None:
            raise ContractError("foundation outcome unexpectedly contains a matured target")
        values = tuple(_finite_optional(feature[name], name) for name in names)
        feature_ready = feature["feature_status"] == "PRICE_INPUT_READY_PIT_UNRESOLVED"
        if (feature_ready and any(value is None for value in values)) or (
            not feature_ready and any(value is not None for value in values)
        ):
            raise ContractError("feature readiness and values disagree")
        entry = _finite_optional(outcome["entry_open"], "entry_open")
        exit_close = _finite_optional(outcome["exit_close"], "exit_close")
        outcome_ready = (
            outcome["outcome_input_status"]
            == "BLOCKED_ACTION_AND_DELISTING_EVIDENCE"
        )
        if outcome_ready:
            if entry is None or exit_close is None or entry <= 0 or exit_close <= 0:
                raise ContractError("proxy-ready outcome requires positive entry and exit prices")
            proxy_return = exit_close / entry - 1.0
            proxy_status = "PROXY_SOURCE_ADJUSTED_RETURN_READY_UNTRUSTED"
        else:
            proxy_return = None
            proxy_status = f"UNRESOLVED_{outcome['outcome_input_status']}"
        mechanically_complete = feature_ready and outcome_ready
        eligibility.append(
            ProxyEligibilityRow(
                sample_key=key,
                symbol=feature["symbol"],
                historical_proxy=True,
                persistent_asset_identity=False,
                trusted_sleeve=None,
                eligibility_status=(
                    "PROXY_DIAGNOSTIC_COMPLETE_UNTRUSTED"
                    if mechanically_complete
                    else "PROXY_DIAGNOSTIC_INCOMPLETE_UNTRUSTED"
                ),
            )
        )
        feature_row = ProxyFeatureRow(
            sample_key=key,
            feature_status=str(feature["feature_status"]),
            values=values,
        )
        outcome_row = ProxyOutcomeRow(
            sample_key=key,
            input_status=str(outcome["outcome_input_status"]),
            proxy_status=proxy_status,
            target_semantics=TARGET_SEMANTICS,
            proxy_return=proxy_return,
            canonical_split_normalized_target_equivalent=False,
        )
        features.append(feature_row)
        outcomes.append(outcome_row)
        samples.append(
            ProxyWfaSample(
                sample_key=key,
                feature_status=feature_row.feature_status,
                proxy_status=outcome_row.proxy_status,
                feature_values=values,
                proxy_return=proxy_return,
                mechanically_complete=mechanically_complete,
                trusted_gate_eligible=False,
                real_history_execution_authorized=False,
            )
        )
    unsigned = {
        "schema_version": 1,
        "source_epoch": closure.source_epoch,
        "source_closure_id": closure.source_closure_id,
        "derivative_contract_id": closure.derivative_contract_id,
        "fixture_id": fixture_id,
        "synthetic_permit_id": synthetic.permit_id,
        "eligibility_rows": [_canonical_value(asdict(row)) for row in eligibility],
        "feature_rows": [_canonical_value(asdict(row)) for row in features],
        "outcome_rows": [_canonical_value(asdict(row)) for row in outcomes],
        "wfa_samples": [_canonical_value(asdict(row)) for row in samples],
        "trusted_sleeves": [],
        "diagnostic_sleeves": ["proxy_unknown_long", "proxy_unknown_short"],
        "real_history_execution_authorized": False,
        "candidate_eligible": False,
        "alpha_evidence": False,
    }
    derivative = InMemoryProxyDerivative(
        schema_version=1,
        source_epoch=closure.source_epoch,
        source_closure_id=closure.source_closure_id,
        derivative_contract_id=closure.derivative_contract_id,
        fixture_id=fixture_id,
        synthetic_permit_id=synthetic.permit_id,
        eligibility_rows=tuple(eligibility),
        feature_rows=tuple(features),
        outcome_rows=tuple(outcomes),
        wfa_samples=tuple(samples),
        trusted_sleeves=(),
        diagnostic_sleeves=("proxy_unknown_long", "proxy_unknown_short"),
        real_history_execution_authorized=False,
        candidate_eligible=False,
        alpha_evidence=False,
        derivative_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    derivative.validate()
    return derivative
