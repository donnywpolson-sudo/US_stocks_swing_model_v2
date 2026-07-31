"""Preregistration-bound input-adapter mechanics for synthetic legacy fixtures.

This module deliberately exposes no trainer, evaluator, WFA executor, outcome
unlock, registry writer, or real-history authorization constructor.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..common import canonical_json_bytes, require_sha256, sha256_bytes
from ..errors import ContractError, IntegrityError
from ..legacy_discovery_derivative import (
    InMemoryProxyDerivative,
    TARGET_SEMANTICS,
)
from ..trials import TrialSpec


ADAPTER_SCOPE = "SYNTHETIC_LEGACY_DISCOVERY_PREREGISTERED_ADAPTER"
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class LegacyDiscoveryPreregistration:
    schema_version: int
    source_epoch: str
    derivative_id: str
    trial_id: str
    trial_declaration_id: str
    trial_registry_binding_id: str
    trial_ledger_head_id: str
    charter_id: str
    feature_spec_id: str
    proxy_label_spec_id: str
    split_spec_id: str
    cost_spec_id: str
    robustness_policy_id: str
    code_commit: str
    code_hash: str
    environment_id: str
    evidence_class: str
    target_semantics: str
    epochs_may_be_pooled: bool
    trusted_sleeves: tuple[()]
    candidate_eligible: bool
    alpha_evidence: bool
    registration_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("registration_id")
        payload["trusted_sleeves"] = list(self.trusted_sleeves)
        return payload

    @classmethod
    def create(
        cls,
        *,
        derivative: InMemoryProxyDerivative,
        trial_spec: TrialSpec,
        trial_declaration_id: str,
        trial_registry_binding_id: str,
        trial_ledger_head_id: str,
        charter_id: str,
        code_commit: str,
    ) -> "LegacyDiscoveryPreregistration":
        derivative.validate()
        trial_spec.validate()
        if (
            trial_spec.evidence_class != "REGISTERED_HISTORICAL_DISCOVERY"
            or trial_spec.data_release_ids != (derivative.derivative_id,)
            or len(trial_spec.release_bindings) != 1
            or trial_spec.release_bindings[0].source_epoch != derivative.source_epoch
            or trial_spec.release_bindings[0].role != "legacy_discovery_only"
            or trial_spec.release_bindings[0].quality_state != "LEGACY_CAVEATED"
        ):
            raise ContractError("trial specification does not bind this proxy derivative")
        unsigned = {
            "schema_version": 1,
            "source_epoch": derivative.source_epoch,
            "derivative_id": derivative.derivative_id,
            "trial_id": trial_spec.trial_id,
            "trial_declaration_id": trial_declaration_id,
            "trial_registry_binding_id": trial_registry_binding_id,
            "trial_ledger_head_id": trial_ledger_head_id,
            "charter_id": charter_id,
            "feature_spec_id": trial_spec.feature_schema_id,
            "proxy_label_spec_id": trial_spec.outcome_schema_id,
            "split_spec_id": trial_spec.split_plan_id,
            "cost_spec_id": trial_spec.cost_policy_id,
            "robustness_policy_id": trial_spec.robustness_policy_id,
            "code_commit": code_commit,
            "code_hash": trial_spec.code_hash,
            "environment_id": trial_spec.environment_hash,
            "evidence_class": trial_spec.evidence_class,
            "target_semantics": TARGET_SEMANTICS,
            "epochs_may_be_pooled": False,
            "trusted_sleeves": [],
            "candidate_eligible": False,
            "alpha_evidence": False,
        }
        registration = cls(
            schema_version=1,
            source_epoch=derivative.source_epoch,
            derivative_id=derivative.derivative_id,
            trial_id=trial_spec.trial_id,
            trial_declaration_id=trial_declaration_id,
            trial_registry_binding_id=trial_registry_binding_id,
            trial_ledger_head_id=trial_ledger_head_id,
            charter_id=charter_id,
            feature_spec_id=trial_spec.feature_schema_id,
            proxy_label_spec_id=trial_spec.outcome_schema_id,
            split_spec_id=trial_spec.split_plan_id,
            cost_spec_id=trial_spec.cost_policy_id,
            robustness_policy_id=trial_spec.robustness_policy_id,
            code_commit=code_commit,
            code_hash=trial_spec.code_hash,
            environment_id=trial_spec.environment_hash,
            evidence_class=trial_spec.evidence_class,
            target_semantics=TARGET_SEMANTICS,
            epochs_may_be_pooled=False,
            trusted_sleeves=(),
            candidate_eligible=False,
            alpha_evidence=False,
            registration_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        registration.validate()
        return registration

    def validate(self) -> None:
        for name in (
            "derivative_id",
            "trial_id",
            "trial_declaration_id",
            "trial_registry_binding_id",
            "trial_ledger_head_id",
            "charter_id",
            "feature_spec_id",
            "proxy_label_spec_id",
            "split_spec_id",
            "cost_spec_id",
            "robustness_policy_id",
            "code_hash",
            "environment_id",
            "registration_id",
        ):
            require_sha256(getattr(self, name), f"preregistration.{name}")
        if (
            self.schema_version != 1
            or not self.source_epoch
            or not _COMMIT_RE.fullmatch(self.code_commit)
            or self.evidence_class != "REGISTERED_HISTORICAL_DISCOVERY"
            or self.target_semantics != TARGET_SEMANTICS
            or self.epochs_may_be_pooled
            or self.trusted_sleeves != ()
            or self.candidate_eligible
            or self.alpha_evidence
        ):
            raise ContractError("legacy-discovery preregistration weakens its boundary")
        if self.registration_id != sha256_bytes(
            canonical_json_bytes(self.unsigned_dict())
        ):
            raise IntegrityError("preregistration ID differs from canonical content")


def synthetic_adapter_fixture_id(
    derivative: InMemoryProxyDerivative,
    registration: LegacyDiscoveryPreregistration,
) -> str:
    derivative.validate()
    registration.validate()
    return sha256_bytes(
        canonical_json_bytes(
            {
                "derivative_id": derivative.derivative_id,
                "registration_id": registration.registration_id,
            }
        )
    )


@dataclass(frozen=True)
class PreparedLegacyDiscoveryAdapter:
    schema_version: int
    mode: str
    source_epoch: str
    derivative_id: str
    registration_id: str
    trial_id: str
    sample_count: int
    mechanically_complete_sample_count: int
    sample_keys_sha256: str
    feature_payload_sha256: str
    proxy_target_payload_sha256: str
    synthetic_fixture_id: str
    synthetic_permit_id: str
    executor_entrypoint: None
    real_history_execution_authorized: bool
    generated_evidence_eligible: bool
    trusted_gate_eligible: bool
    candidate_eligible: bool
    alpha_evidence: bool
    adapter_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("adapter_id")
        return payload

    def validate(self) -> None:
        for name in (
            "derivative_id",
            "registration_id",
            "trial_id",
            "sample_keys_sha256",
            "feature_payload_sha256",
            "proxy_target_payload_sha256",
            "synthetic_fixture_id",
            "synthetic_permit_id",
            "adapter_id",
        ):
            require_sha256(getattr(self, name), f"adapter.{name}")
        if (
            self.schema_version != 1
            or self.mode != "PREREGISTERED_INPUT_ADAPTER_ONLY"
            or type(self.sample_count) is not int
            or self.sample_count <= 0
            or type(self.mechanically_complete_sample_count) is not int
            or not 0 <= self.mechanically_complete_sample_count <= self.sample_count
            or self.executor_entrypoint is not None
            or self.real_history_execution_authorized
            or self.generated_evidence_eligible
            or self.trusted_gate_eligible
            or self.candidate_eligible
            or self.alpha_evidence
        ):
            raise ContractError("prepared adapter exposes forbidden authority or state")
        if self.adapter_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("prepared adapter ID differs from canonical content")


def prepare_synthetic_legacy_discovery_adapter(
    derivative: InMemoryProxyDerivative,
    registration: LegacyDiscoveryPreregistration,
    *,
    permit: SyntheticOnlyPermit | None,
) -> PreparedLegacyDiscoveryAdapter:
    """Hash exact synthetic inputs into a non-executable preregistered adapter."""

    derivative.validate()
    registration.validate()
    synthetic = require_synthetic_permit(permit, scope=ADAPTER_SCOPE)
    fixture_id = synthetic_adapter_fixture_id(derivative, registration)
    if synthetic.fixture_id != fixture_id:
        raise ContractError("synthetic permit does not bind this adapter fixture")
    if (
        registration.derivative_id != derivative.derivative_id
        or registration.source_epoch != derivative.source_epoch
    ):
        raise ContractError("preregistration does not bind this derivative")
    sample_keys = [list(row.sample_key) for row in derivative.wfa_samples]
    features = [list(row.feature_values) for row in derivative.wfa_samples]
    targets = [
        {
            "sample_key": list(row.sample_key),
            "proxy_status": row.proxy_status,
            "proxy_return": row.proxy_return,
        }
        for row in derivative.wfa_samples
    ]
    unsigned = {
        "schema_version": 1,
        "mode": "PREREGISTERED_INPUT_ADAPTER_ONLY",
        "source_epoch": derivative.source_epoch,
        "derivative_id": derivative.derivative_id,
        "registration_id": registration.registration_id,
        "trial_id": registration.trial_id,
        "sample_count": len(derivative.wfa_samples),
        "mechanically_complete_sample_count": sum(
            row.mechanically_complete for row in derivative.wfa_samples
        ),
        "sample_keys_sha256": sha256_bytes(canonical_json_bytes(sample_keys)),
        "feature_payload_sha256": sha256_bytes(canonical_json_bytes(features)),
        "proxy_target_payload_sha256": sha256_bytes(canonical_json_bytes(targets)),
        "synthetic_fixture_id": fixture_id,
        "synthetic_permit_id": synthetic.permit_id,
        "executor_entrypoint": None,
        "real_history_execution_authorized": False,
        "generated_evidence_eligible": False,
        "trusted_gate_eligible": False,
        "candidate_eligible": False,
        "alpha_evidence": False,
    }
    adapter = PreparedLegacyDiscoveryAdapter(
        **unsigned,
        adapter_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    adapter.validate()
    return adapter
