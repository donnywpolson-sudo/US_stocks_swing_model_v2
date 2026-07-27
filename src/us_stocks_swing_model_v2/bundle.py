from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError, IntegrityError
from .gates import GateReceipt, GateState
from .monitoring_policy import frozen_monitoring_policy_hash
from .governance import (
    LocalIntegrityRecord,
    ReleaseBinding,
    release_bindings_hash,
    verify_release_bindings,
)


BUNDLE_MANIFEST = "sealed_bundle.json"
BUNDLE_SEALING_MAX_AGE = timedelta(minutes=15)
BLOCKED_READINESS_RECEIPT_ID = sha256_bytes(
    canonical_json_bytes({"state": "NOT_ISSUED_BLOCKS_PRODUCTION", "schema": 1})
)
BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID = sha256_bytes(
    canonical_json_bytes({"state": "NOT_CONFIGURED_BLOCKS_PRODUCTION", "schema": 1})
)
TRUST_ELIGIBLE_ROLES = {
    "active_historical",
    "prospective_as_received",
    "derived_causal",
    "feature_only",
    "outcome_only",
}


def _require_reachable_sealing_time(sealed_at: str, observed_at: datetime) -> None:
    """Require a recent, non-future candidate time without instant equality."""

    sealed = parse_utc_z(sealed_at, "sealed_at")
    if sealed > observed_at:
        raise ContractError("bundle sealed_at cannot be in the future")
    if observed_at - sealed > BUNDLE_SEALING_MAX_AGE:
        raise ContractError("bundle sealed_at is outside the bounded sealing window")


@dataclass(frozen=True)
class BundleArtifact:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


@dataclass(frozen=True)
class SealedBundleMetadata:
    schema_version: int
    model_kind: str
    feature_schema_id: str
    feature_names: tuple[str, ...]
    feature_types: Mapping[str, str]
    training_cutoff: str
    sealed_at: str
    data_release_ids: tuple[str, ...]
    release_bindings: tuple[ReleaseBinding, ...]
    training_release_ids: tuple[str, ...]
    allowed_feature_release_ids: tuple[str, ...]
    allowed_identity_release_ids: tuple[str, ...]
    allowed_security_type_evidence_ids: tuple[str, ...]
    allowed_source_epochs: tuple[str, ...]
    calendar_release_id: str
    action_release_id: str
    trial_registry_binding_id: str
    trial_id: str
    registration_hash: str
    evaluation_permit_id: str
    census_anchor_id: str
    trial_family_anchor_id: str
    holdout_receipt_id: str
    readiness_receipt_id: str
    eligibility_census_contract_id: str
    external_anchor_receipt_id: str
    production_readiness_state: str
    governance_contract_hash: str
    primary_gate_id: str
    robustness_policy_hash: str
    robustness_evidence_hash: str
    monitoring_policy_hash: str
    monitoring_reference_hash: str
    gate_receipt: GateReceipt
    sealing_authorization: LocalIntegrityRecord
    code_hash: str
    config_hash: str
    environment_hash: str
    neutral_band: float
    maximum_uncertainty: float
    maximum_feature_age_minutes: int
    maximum_identity_age_minutes: int
    maximum_inference_latency_minutes: int
    artifacts: tuple[BundleArtifact, ...]
    candidate_id: str
    bundle_id: str

    def candidate_dict(self) -> dict[str, Any]:
        """All candidate evidence except the later keyed sealing authorization."""
        return {
            "schema_version": self.schema_version,
            "model_kind": self.model_kind,
            "feature_schema_id": self.feature_schema_id,
            "feature_names": list(self.feature_names),
            "feature_types": dict(sorted(self.feature_types.items())),
            "training_cutoff": self.training_cutoff,
            "sealed_at": self.sealed_at,
            "data_release_ids": list(self.data_release_ids),
            "release_bindings": [binding.as_dict() for binding in self.release_bindings],
            "training_release_ids": list(self.training_release_ids),
            "allowed_feature_release_ids": list(self.allowed_feature_release_ids),
            "allowed_identity_release_ids": list(self.allowed_identity_release_ids),
            "allowed_security_type_evidence_ids": list(self.allowed_security_type_evidence_ids),
            "allowed_source_epochs": list(self.allowed_source_epochs),
            "calendar_release_id": self.calendar_release_id,
            "action_release_id": self.action_release_id,
            "trial_registry_binding_id": self.trial_registry_binding_id,
            "trial_id": self.trial_id,
            "registration_hash": self.registration_hash,
            "evaluation_permit_id": self.evaluation_permit_id,
            "census_anchor_id": self.census_anchor_id,
            "trial_family_anchor_id": self.trial_family_anchor_id,
            "holdout_receipt_id": self.holdout_receipt_id,
            "readiness_receipt_id": self.readiness_receipt_id,
            "eligibility_census_contract_id": self.eligibility_census_contract_id,
            "external_anchor_receipt_id": self.external_anchor_receipt_id,
            "production_readiness_state": self.production_readiness_state,
            "governance_contract_hash": self.governance_contract_hash,
            "primary_gate_id": self.primary_gate_id,
            "robustness_policy_hash": self.robustness_policy_hash,
            "robustness_evidence_hash": self.robustness_evidence_hash,
            "monitoring_policy_hash": self.monitoring_policy_hash,
            "monitoring_reference_hash": self.monitoring_reference_hash,
            "gate_receipt": self.gate_receipt.as_dict(),
            "code_hash": self.code_hash,
            "config_hash": self.config_hash,
            "environment_hash": self.environment_hash,
            "neutral_band": self.neutral_band,
            "maximum_uncertainty": self.maximum_uncertainty,
            "maximum_feature_age_minutes": self.maximum_feature_age_minutes,
            "maximum_identity_age_minutes": self.maximum_identity_age_minutes,
            "maximum_inference_latency_minutes": self.maximum_inference_latency_minutes,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
        }

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            **self.candidate_dict(),
            "candidate_id": self.candidate_id,
            "sealing_authorization": self.sealing_authorization.as_dict(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "bundle_id": self.bundle_id}

    def sealing_bindings(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "trial_id": self.trial_id,
            "trial_registry_binding_id": self.trial_registry_binding_id,
            "registration_hash": self.registration_hash,
            "evaluation_permit_id": self.evaluation_permit_id,
            "census_anchor_id": self.census_anchor_id,
            "trial_family_anchor_id": self.trial_family_anchor_id,
            "holdout_receipt_id": self.holdout_receipt_id,
            "readiness_receipt_id": self.readiness_receipt_id,
            "eligibility_census_contract_id": self.eligibility_census_contract_id,
            "external_anchor_receipt_id": self.external_anchor_receipt_id,
            "gate_receipt_id": self.gate_receipt.receipt_id,
            "release_bindings_hash": release_bindings_hash(self.release_bindings),
            "governance_contract_hash": self.governance_contract_hash,
            "primary_gate_id": self.primary_gate_id,
            "robustness_policy_hash": self.robustness_policy_hash,
            "robustness_evidence_hash": self.robustness_evidence_hash,
        }

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 3 or self.model_kind != "linear_distribution_v1":
            raise ContractError("unsupported sealed bundle contract")
        cutoff = parse_utc_z(self.training_cutoff, "training_cutoff")
        sealed = parse_utc_z(self.sealed_at, "sealed_at")
        if cutoff > sealed:
            raise ContractError("training cutoff cannot follow bundle sealing time")
        if not self.feature_names or list(self.feature_names) != list(dict.fromkeys(self.feature_names)):
            raise ContractError("feature names must be nonempty, ordered, and unique")
        if set(self.feature_types) != set(self.feature_names):
            raise ContractError("feature type registry must exactly match feature names")
        if any(self.feature_types[name] != "float64" for name in self.feature_names):
            raise ContractError("feature types must explicitly pin every model input to float64")
        if list(self.data_release_ids) != sorted(set(self.data_release_ids)) or not self.data_release_ids:
            raise ContractError("bundle data releases must be nonempty, sorted, and unique")
        binding_ids = tuple(binding.release_id for binding in self.release_bindings)
        if binding_ids != self.data_release_ids:
            raise ContractError("bundle release bindings must exactly match data release IDs")
        for binding in self.release_bindings:
            binding.validate()
            if binding.role not in TRUST_ELIGIBLE_ROLES or binding.quality_state != "PASS":
                raise ContractError("bundle contains a non-trust-eligible release")
        exact_allowlists = {
            "training_release_ids": self.training_release_ids,
            "allowed_feature_release_ids": self.allowed_feature_release_ids,
            "allowed_identity_release_ids": self.allowed_identity_release_ids,
            "allowed_security_type_evidence_ids": self.allowed_security_type_evidence_ids,
            "allowed_source_epochs": self.allowed_source_epochs,
        }
        for name, values in exact_allowlists.items():
            if not values or list(values) != sorted(set(values)):
                raise ContractError(f"{name} must be nonempty, sorted, and unique")
        referenced = {
            *self.allowed_feature_release_ids,
            *self.allowed_identity_release_ids,
            *self.allowed_security_type_evidence_ids,
            self.calendar_release_id,
            self.action_release_id,
        }
        referenced.update(self.training_release_ids)
        if referenced != set(self.data_release_ids):
            raise ContractError("bundle slots must exactly cover the verified release set")
        by_id = {binding.release_id: binding for binding in self.release_bindings}
        slot_contracts = (
            (self.allowed_feature_release_ids, "features", "feature_only", "feature"),
            (self.allowed_identity_release_ids, "identity", "prospective_as_received", "identity"),
            (
                self.allowed_security_type_evidence_ids,
                "security_types",
                "prospective_as_received",
                "security-type",
            ),
            ((self.calendar_release_id,), "xnys_sessions", "derived_causal", "calendar"),
            ((self.action_release_id,), "corporate_actions", "prospective_as_received", "corporate-action"),
        )
        for release_ids, expected_dataset, expected_role, slot_name in slot_contracts:
            for release_id in release_ids:
                binding = by_id[release_id]
                if binding.dataset != expected_dataset or binding.role != expected_role:
                    raise ContractError(f"bundle {slot_name} slot has the wrong release dataset/role")
        training_roles = {"active_historical", "feature_only", "outcome_only"}
        if not {by_id[item].role for item in self.training_release_ids} >= {
            "active_historical",
            "feature_only",
            "outcome_only",
        }:
            raise ContractError("training releases require bars, features, and outcomes")
        for release_id in self.training_release_ids:
            binding = by_id[release_id]
            if binding.role not in training_roles:
                raise ContractError("training slot contains a non-training release role")
            if binding.event_end is None:
                raise ContractError("training release lacks a bounded event_end")
            try:
                event_end_date = date.fromisoformat(binding.event_end)
            except ValueError:
                if parse_utc_z(binding.event_end, "training.event_end") > cutoff:
                    raise ContractError("training release contains events after the training cutoff")
            else:
                if event_end_date > cutoff.date():
                    raise ContractError("training release contains events after the training cutoff")
        for binding in self.release_bindings:
            if parse_utc_z(binding.created_at, "release_binding.created_at") > sealed:
                raise ContractError("bundle contains a release created after sealing")
        bound_epochs = {binding.source_epoch for binding in self.release_bindings}
        if set(self.allowed_source_epochs) != bound_epochs:
            raise ContractError("bundle source epochs must exactly match verified releases")
        if (
            not math.isfinite(self.neutral_band)
            or not math.isfinite(self.maximum_uncertainty)
            or self.neutral_band < 0
            or self.maximum_uncertainty <= 0
            or self.maximum_feature_age_minutes <= 0
            or self.maximum_identity_age_minutes <= 0
            or self.maximum_inference_latency_minutes <= 0
        ):
            raise ContractError("bundle thresholds must be valid and positive")
        paths = [artifact.path for artifact in self.artifacts]
        if paths != sorted(set(paths)) or not paths:
            raise ContractError("bundle artifacts must be nonempty, sorted, and unique")
        for artifact in self.artifacts:
            safe_relative_path(artifact.path)
            if artifact.path == BUNDLE_MANIFEST or type(artifact.size) is not int or artifact.size < 0:
                raise ContractError("invalid bundle artifact metadata")
            require_sha256(artifact.sha256, f"bundle.artifact.{artifact.path}.sha256")
        for field_name in (
            "trial_registry_binding_id",
            "trial_id",
            "registration_hash",
            "evaluation_permit_id",
            "census_anchor_id",
            "trial_family_anchor_id",
            "holdout_receipt_id",
            "readiness_receipt_id",
            "eligibility_census_contract_id",
            "external_anchor_receipt_id",
            "governance_contract_hash",
            "primary_gate_id",
            "robustness_policy_hash",
            "robustness_evidence_hash",
            "monitoring_policy_hash",
            "monitoring_reference_hash",
            "code_hash",
            "config_hash",
            "environment_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)
        if self.monitoring_policy_hash != frozen_monitoring_policy_hash():
            raise ContractError(
                "bundle monitoring policy hash differs from the frozen policy"
            )
        if self.readiness_receipt_id != BLOCKED_READINESS_RECEIPT_ID:
            raise ContractError("readiness receipt must be the exact not-issued blocker")
        if self.external_anchor_receipt_id != BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID:
            raise ContractError("external anchor receipt must be the exact not-configured blocker")
        if self.production_readiness_state != "NOT_CONFIGURED_BLOCKS_PRODUCTION":
            raise ContractError(
                "production readiness verification is not implemented; candidate must remain blocked"
            )
        self.gate_receipt.validate()
        if (
            self.gate_receipt.state
            != GateState.PASS_HISTORICAL_DISCOVERY_SCREEN.value
            or self.gate_receipt.trial_id != self.trial_id
            or self.gate_receipt.trial_registry_binding_id != self.trial_registry_binding_id
            or self.gate_receipt.evaluation_permit_id != self.evaluation_permit_id
            or self.gate_receipt.registration_hash != self.registration_hash
            or self.gate_receipt.census_anchor_id != self.census_anchor_id
            or self.gate_receipt.trial_family_anchor_id != self.trial_family_anchor_id
            or self.gate_receipt.holdout_receipt_id != self.holdout_receipt_id
            or self.gate_receipt.governance_contract_hash != self.governance_contract_hash
            or self.gate_receipt.release_bindings_hash
            != release_bindings_hash(self.release_bindings)
            or self.gate_receipt.policy_hash != self.primary_gate_id
            or self.gate_receipt.robustness_policy_hash
            != self.robustness_policy_hash
            or self.gate_receipt.robustness_evidence_hash
            != self.robustness_evidence_hash
            or parse_utc_z(self.gate_receipt.evaluated_at, "gate.evaluated_at") > sealed
        ):
            raise ContractError("bundle requires the matching frozen-policy PASS gate receipt")
        self.sealing_authorization.validate_content()
        if self.candidate_id != sha256_bytes(canonical_json_bytes(self.candidate_dict())):
            raise IntegrityError("candidate_id does not match governed bundle evidence")
        if (
            self.sealing_authorization.scope != "AUTHORIZE_CANDIDATE_SEALING"
            or self.sealing_authorization.subject_id != self.candidate_id
            or dict(self.sealing_authorization.bindings) != self.sealing_bindings()
        ):
            raise ContractError("bundle sealing authorization is not bound to this candidate")
        if self.bundle_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("bundle_id does not match sealed metadata")

    @property
    def trust_eligible(self) -> bool:
        """Schema v3 bundles are mechanically sealable but production blocked.

        A future production-ready contract requires a separately reviewed
        schema transition that verifies real readiness and external-anchor
        receipts.  Do not encode an unreachable ``VERIFIED_READY`` branch in
        the currently validated blocked-state schema.
        """

        return False

    @classmethod
    def _validate_json_types(
        cls,
        value: Mapping[str, Any],
        *,
        candidate_only: bool,
    ) -> None:
        expected_fields = set(cls.__dataclass_fields__)
        if candidate_only:
            expected_fields -= {
                "candidate_id",
                "sealing_authorization",
                "bundle_id",
            }
        if type(value) is not dict or set(value) != expected_fields:
            label = "candidate" if candidate_only else "metadata"
            raise ContractError(
                f"sealed bundle {label} fields differ from the exact contract"
            )
        if type(value["schema_version"]) is not int:
            raise ContractError("sealed bundle schema_version must be an exact JSON integer")
        text_fields = [
            "model_kind",
            "feature_schema_id",
            "training_cutoff",
            "sealed_at",
            "calendar_release_id",
            "action_release_id",
            "trial_registry_binding_id",
            "trial_id",
            "registration_hash",
            "evaluation_permit_id",
            "census_anchor_id",
            "trial_family_anchor_id",
            "holdout_receipt_id",
            "readiness_receipt_id",
            "eligibility_census_contract_id",
            "external_anchor_receipt_id",
            "production_readiness_state",
            "governance_contract_hash",
            "primary_gate_id",
            "robustness_policy_hash",
            "robustness_evidence_hash",
            "monitoring_policy_hash",
            "monitoring_reference_hash",
            "code_hash",
            "config_hash",
            "environment_hash",
        ]
        if not candidate_only:
            text_fields.extend(("candidate_id", "bundle_id"))
        if any(type(value[name]) is not str for name in text_fields):
            raise ContractError(
                "sealed bundle identity/time/hash fields must be exact JSON strings"
            )
        string_array_fields = (
            "feature_names",
            "data_release_ids",
            "training_release_ids",
            "allowed_feature_release_ids",
            "allowed_identity_release_ids",
            "allowed_security_type_evidence_ids",
            "allowed_source_epochs",
        )
        if any(
            type(value[name]) is not list
            or any(type(item) is not str for item in value[name])
            for name in string_array_fields
        ):
            raise ContractError(
                "sealed bundle string censuses must be exact JSON arrays"
            )
        if (
            type(value["feature_types"]) is not dict
            or any(
                type(key) is not str or type(item) is not str
                for key, item in value["feature_types"].items()
            )
        ):
            raise ContractError(
                "sealed bundle feature types must be exact JSON strings"
            )
        if (
            type(value["release_bindings"]) is not list
            or any(
                type(entry) is not dict
                or set(entry) != set(ReleaseBinding.__dataclass_fields__)
                or any(
                    type(entry[name]) is not str
                    for name in (
                        "release_id",
                        "project",
                        "dataset",
                        "source_epoch",
                        "role",
                        "quality_state",
                        "created_at",
                    )
                )
                or any(
                    entry[name] is not None
                    and type(entry[name]) is not str
                    for name in ("event_start", "event_end")
                )
                for entry in value["release_bindings"]
            )
        ):
            raise ContractError(
                "sealed bundle release bindings have invalid exact JSON types"
            )
        if (
            type(value["artifacts"]) is not list
            or any(
                type(entry) is not dict
                or set(entry) != set(BundleArtifact.__dataclass_fields__)
                or type(entry["path"]) is not str
                or type(entry["sha256"]) is not str
                or type(entry["size"]) is not int
                for entry in value["artifacts"]
            )
            or type(value["gate_receipt"]) is not dict
            or (
                not candidate_only
                and type(value["sealing_authorization"]) is not dict
            )
        ):
            raise ContractError(
                "sealed bundle nested evidence has invalid exact JSON types"
            )
        for name in ("neutral_band", "maximum_uncertainty"):
            if isinstance(value[name], bool) or not isinstance(value[name], (int, float)):
                raise ContractError(f"sealed bundle {name} must be an explicit JSON number")
        for name in (
            "maximum_feature_age_minutes",
            "maximum_identity_age_minutes",
            "maximum_inference_latency_minutes",
        ):
            if type(value[name]) is not int:
                raise ContractError(f"sealed bundle {name} must be an exact JSON integer")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SealedBundleMetadata":
        cls._validate_json_types(value, candidate_only=False)
        metadata = cls(
            schema_version=value["schema_version"],
            model_kind=value["model_kind"],
            feature_schema_id=value["feature_schema_id"],
            feature_names=tuple(value["feature_names"]),
            feature_types=dict(value["feature_types"]),
            training_cutoff=value["training_cutoff"],
            sealed_at=value["sealed_at"],
            data_release_ids=tuple(value["data_release_ids"]),
            release_bindings=tuple(ReleaseBinding(**entry) for entry in value["release_bindings"]),
            training_release_ids=tuple(value["training_release_ids"]),
            allowed_feature_release_ids=tuple(value["allowed_feature_release_ids"]),
            allowed_identity_release_ids=tuple(value["allowed_identity_release_ids"]),
            allowed_security_type_evidence_ids=tuple(value["allowed_security_type_evidence_ids"]),
            allowed_source_epochs=tuple(value["allowed_source_epochs"]),
            calendar_release_id=value["calendar_release_id"],
            action_release_id=value["action_release_id"],
            trial_registry_binding_id=value["trial_registry_binding_id"],
            trial_id=value["trial_id"],
            registration_hash=value["registration_hash"],
            evaluation_permit_id=value["evaluation_permit_id"],
            census_anchor_id=value["census_anchor_id"],
            trial_family_anchor_id=value["trial_family_anchor_id"],
            holdout_receipt_id=value["holdout_receipt_id"],
            readiness_receipt_id=value["readiness_receipt_id"],
            eligibility_census_contract_id=value["eligibility_census_contract_id"],
            external_anchor_receipt_id=value["external_anchor_receipt_id"],
            production_readiness_state=value["production_readiness_state"],
            governance_contract_hash=value["governance_contract_hash"],
            primary_gate_id=value["primary_gate_id"],
            robustness_policy_hash=value["robustness_policy_hash"],
            robustness_evidence_hash=value["robustness_evidence_hash"],
            monitoring_policy_hash=value["monitoring_policy_hash"],
            monitoring_reference_hash=value["monitoring_reference_hash"],
            gate_receipt=GateReceipt.from_dict(value["gate_receipt"]),
            sealing_authorization=LocalIntegrityRecord.from_dict(value["sealing_authorization"]),
            code_hash=value["code_hash"],
            config_hash=value["config_hash"],
            environment_hash=value["environment_hash"],
            neutral_band=float(value["neutral_band"]),
            maximum_uncertainty=float(value["maximum_uncertainty"]),
            maximum_feature_age_minutes=value["maximum_feature_age_minutes"],
            maximum_identity_age_minutes=value["maximum_identity_age_minutes"],
            maximum_inference_latency_minutes=value["maximum_inference_latency_minutes"],
            artifacts=tuple(BundleArtifact(**entry) for entry in value["artifacts"]),
            candidate_id=value["candidate_id"],
            bundle_id=value["bundle_id"],
        )
        metadata.validate()
        return metadata


@dataclass(frozen=True)
class PreparedBundleCandidate:
    bundle_dir: Path
    candidate_json: bytes
    candidate_id: str

    def candidate_dict(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.candidate_json)
        except json.JSONDecodeError as exc:
            raise IntegrityError("prepared candidate JSON is invalid") from exc
        if not isinstance(payload, dict) or self.candidate_json != canonical_json_bytes(payload):
            raise IntegrityError("prepared candidate is not canonical JSON")
        if self.candidate_id != sha256_bytes(self.candidate_json):
            raise IntegrityError("prepared candidate ID differs from its canonical evidence")
        SealedBundleMetadata._validate_json_types(payload, candidate_only=True)
        GateReceipt.from_dict(payload["gate_receipt"])
        release_bindings_hash(
            tuple(ReleaseBinding(**entry) for entry in payload["release_bindings"])
        )
        return payload

    def sealing_bindings(self) -> dict[str, str]:
        payload = self.candidate_dict()
        bindings = tuple(ReleaseBinding(**entry) for entry in payload["release_bindings"])
        gate = GateReceipt.from_dict(payload["gate_receipt"])
        return {
            "candidate_id": self.candidate_id,
            "trial_id": payload["trial_id"],
            "trial_registry_binding_id": payload["trial_registry_binding_id"],
            "registration_hash": payload["registration_hash"],
            "evaluation_permit_id": payload["evaluation_permit_id"],
            "census_anchor_id": payload["census_anchor_id"],
            "trial_family_anchor_id": payload["trial_family_anchor_id"],
            "holdout_receipt_id": payload["holdout_receipt_id"],
            "readiness_receipt_id": payload["readiness_receipt_id"],
            "eligibility_census_contract_id": payload["eligibility_census_contract_id"],
            "external_anchor_receipt_id": payload["external_anchor_receipt_id"],
            "gate_receipt_id": gate.receipt_id,
            "release_bindings_hash": release_bindings_hash(bindings),
            "governance_contract_hash": payload["governance_contract_hash"],
            "primary_gate_id": payload["primary_gate_id"],
            "robustness_policy_hash": payload["robustness_policy_hash"],
            "robustness_evidence_hash": payload["robustness_evidence_hash"],
        }


def prepare_bundle_candidate(
    bundle_dir: Path,
    artifact_paths: Iterable[str],
    *,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    expected_project: str,
    gate_receipt: GateReceipt,
    trial_registry: Any,
    trial_permit: Any,
    **fields: Any,
) -> PreparedBundleCandidate:
    from .trials import TrialPermit, TrialRegistry

    normalized_artifact_paths = tuple(
        safe_relative_path(raw_relative).as_posix()
        for raw_relative in artifact_paths
    )
    if len(normalized_artifact_paths) != len(set(normalized_artifact_paths)):
        raise ContractError("bundle artifact declarations must be unique")
    validated_artifact_paths = tuple(sorted(normalized_artifact_paths))
    if type(trial_registry) is not TrialRegistry or type(trial_permit) is not TrialPermit:
        raise ContractError("bundle requires concrete registry and permit contract types")
    issued_permit = trial_registry.verify_issued_permit(trial_permit)
    if issued_permit != trial_permit.as_dict():
        raise ContractError("bundle requires the exact registry-issued trial permit")
    gate_receipt.validate()
    permit_gate_bindings = {
        "trial_registry_binding_id": trial_permit.trial_registry_binding_id,
        "trial_id": trial_permit.trial_id,
        "evaluation_permit_id": trial_permit.permit_id,
        "registration_hash": trial_permit.registration_hash,
        "census_anchor_id": trial_permit.census_anchor_id,
        "trial_family_anchor_id": trial_permit.trial_family_anchor_id,
        "holdout_receipt_id": trial_permit.holdout_receipt_id,
        "governance_contract_hash": trial_permit.governance_contract_hash,
        "primary_gate_id": trial_permit.primary_gate_id,
        "robustness_policy_hash": trial_permit.robustness_policy_id,
    }
    for name, expected in permit_gate_bindings.items():
        supplied = fields.pop(name, expected)
        if supplied != expected:
            raise ContractError(f"bundle {name} differs from the issued permit")
        fields[name] = expected
    supplied_robustness_evidence = fields.pop(
        "robustness_evidence_hash",
        gate_receipt.robustness_evidence_hash,
    )
    if supplied_robustness_evidence != gate_receipt.robustness_evidence_hash:
        raise ContractError(
            "bundle robustness evidence differs from the gate receipt"
        )
    fields["robustness_evidence_hash"] = gate_receipt.robustness_evidence_hash
    for name, expected in (
        ("readiness_receipt_id", BLOCKED_READINESS_RECEIPT_ID),
        ("external_anchor_receipt_id", BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID),
    ):
        supplied = fields.pop(name, expected)
        if supplied != expected:
            raise ContractError(f"bundle {name} differs from the fail-closed blocker")
        fields[name] = expected
    if (
        gate_receipt.trial_registry_binding_id != trial_permit.trial_registry_binding_id
        or gate_receipt.trial_id != trial_permit.trial_id
        or gate_receipt.evaluation_permit_id != trial_permit.permit_id
        or gate_receipt.permit_payload_hash
        != sha256_bytes(canonical_json_bytes(trial_permit.as_dict()))
        or gate_receipt.primary_gate_id != trial_permit.primary_gate_id
        or gate_receipt.robustness_policy_hash
        != trial_permit.robustness_policy_id
    ):
        raise ContractError("bundle gate receipt differs from the registry-issued permit")
    root = Path(bundle_dir)
    resolved_root = root.resolve(strict=True)
    built: list[BundleArtifact] = []
    for raw_relative in validated_artifact_paths:
        relative = safe_relative_path(raw_relative)
        candidate = root.joinpath(*relative.parts)
        reject_link(candidate)
        resolved = candidate.resolve(strict=True)
        if not candidate.is_file() or (resolved.parent != resolved_root and resolved_root not in resolved.parents):
            raise ContractError(f"bundle artifact is not a contained plain file: {raw_relative}")
        if candidate.stat().st_nlink != 1:
            raise ContractError(f"bundle hardlinks are prohibited: {raw_relative}")
        built.append(BundleArtifact(path=relative.as_posix(), sha256=sha256_file(candidate), size=candidate.stat().st_size))
    bindings = verify_release_bindings(
        verified_release_directories,
        accepted_release_root=accepted_release_root,
        expected_project=expected_project,
    )
    data_release_ids = tuple(fields.pop("data_release_ids"))
    if tuple(binding.release_id for binding in bindings) != data_release_ids:
        raise ContractError("bundle release IDs differ from verified release manifests")
    artifacts = tuple(built)
    provisional = SealedBundleMetadata(
        schema_version=3,
        artifacts=artifacts,
        data_release_ids=data_release_ids,
        release_bindings=bindings,
        gate_receipt=gate_receipt,
        sealing_authorization=LocalIntegrityRecord(
            schema_version=2,
            record_type="OWNER_OPERATED_LOCAL_INTEGRITY",
            scope="PREPARATION_ONLY",
            subject_id="PREPARATION_ONLY",
            bindings={"preparation": "not_authority"},
            recorded_at="1970-01-01T00:00:00Z",
            clock_mode="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
            synthetic_permit_id="0" * 64,
            record_id="0" * 64,
        ),
        candidate_id="",
        bundle_id="",
        **fields,
    )
    candidate_json = canonical_json_bytes(provisional.candidate_dict())
    return PreparedBundleCandidate(
        bundle_dir=root,
        candidate_json=candidate_json,
        candidate_id=sha256_bytes(candidate_json),
    )


def build_metadata(
    candidate: PreparedBundleCandidate,
    *,
    sealing_authorization: LocalIntegrityRecord,
    clock: TrustedClock,
) -> SealedBundleMetadata:
    candidate_payload = candidate.candidate_dict()
    trusted_clock = require_trusted_clock(clock)
    _require_reachable_sealing_time(
        str(candidate_payload["sealed_at"]),
        trusted_clock.now(),
    )
    sealing_authorization.validate(
        expected_scope="AUTHORIZE_CANDIDATE_SEALING",
        expected_subject_id=candidate.candidate_id,
        required_bindings=candidate.sealing_bindings(),
        clock=trusted_clock,
    )
    unsigned = {
        **candidate_payload,
        "candidate_id": candidate.candidate_id,
        "sealing_authorization": sealing_authorization.as_dict(),
    }
    payload = {**unsigned, "bundle_id": sha256_bytes(canonical_json_bytes(unsigned))}
    metadata = SealedBundleMetadata.from_dict(payload)
    _verify_artifacts(candidate.bundle_dir, metadata, sealed=False)
    return metadata


def seal_bundle(
    bundle_dir: Path,
    metadata: SealedBundleMetadata,
    *,
    clock: TrustedClock,
) -> Path:
    metadata.validate()
    trusted_clock = require_trusted_clock(clock)
    _require_reachable_sealing_time(metadata.sealed_at, trusted_clock.now())
    metadata.sealing_authorization.validate(
        expected_scope="AUTHORIZE_CANDIDATE_SEALING",
        expected_subject_id=metadata.candidate_id,
        required_bindings=metadata.sealing_bindings(),
        clock=trusted_clock,
    )
    root = Path(bundle_dir)
    manifest = root / BUNDLE_MANIFEST
    if manifest.exists():
        existing = load_bundle(root)
        if existing != metadata:
            raise IntegrityError("sealed bundle cannot be overwritten")
        return manifest
    _verify_artifacts(root, metadata, sealed=False)
    atomic_write(manifest, canonical_json_bytes(metadata.as_dict()))
    load_bundle(root)
    return manifest


def load_bundle(
    bundle_dir: Path,
) -> SealedBundleMetadata:
    root = Path(bundle_dir)
    try:
        payload = json.loads((root / BUNDLE_MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("sealed bundle manifest is missing or invalid") from exc
    metadata = SealedBundleMetadata.from_dict(payload)
    metadata.sealing_authorization.validate_at(
        expected_scope="AUTHORIZE_CANDIDATE_SEALING",
        expected_subject_id=metadata.candidate_id,
        required_bindings=metadata.sealing_bindings(),
        observed_at=parse_utc_z(
            metadata.sealing_authorization.recorded_at,
            "sealing_authorization.recorded_at",
        ),
    )
    _verify_artifacts(root, metadata, sealed=True)
    return metadata


def _verify_artifacts(root: Path, metadata: SealedBundleMetadata, *, sealed: bool) -> None:
    resolved_root = root.resolve(strict=True)
    for artifact in metadata.artifacts:
        candidate = root.joinpath(*safe_relative_path(artifact.path).parts)
        reject_link(candidate)
        if (
            not candidate.is_file()
            or resolved_root not in candidate.resolve(strict=True).parents
            or candidate.stat().st_nlink != 1
            or candidate.stat().st_size != artifact.size
            or sha256_file(candidate) != artifact.sha256
        ):
            raise IntegrityError(f"sealed bundle artifact mismatch: {artifact.path}")
    expected_files = {artifact.path for artifact in metadata.artifacts}
    if sealed:
        expected_files.add(BUNDLE_MANIFEST)
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(root, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError(str(exc)) from exc
