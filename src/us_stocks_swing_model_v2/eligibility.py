"""Exact daily eligibility census for omission-safe synthetic inference mechanics.

Production census materialization is intentionally not implemented in this
milestone. Production inference therefore remains blocked at the bundle gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .bundle import SealedBundleMetadata
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError
from .schemas import FeatureRow


ELIGIBILITY_CENSUS_CONTRACT_ID = sha256_bytes(
    canonical_json_bytes(
        {
            "schema": "eligibility_census_v1",
            "expected_assets": "sorted_unique_exact",
            "coverage": "one_prediction_and_one_outcome_per_expected_asset",
            "production_materializer": "NOT_IMPLEMENTED_BLOCKS_PRODUCTION",
        }
    )
)


@dataclass(frozen=True)
class EligibilityCensus:
    schema_version: int
    contract_id: str
    bundle_id: str
    trial_registry_binding_id: str
    trial_id: str
    evaluation_permit_id: str
    gate_receipt_id: str
    decision_session: date
    expected_asset_ids: tuple[str, ...]
    feature_release_ids: tuple[str, ...]
    identity_release_ids: tuple[str, ...]
    security_type_evidence_ids: tuple[str, ...]
    calendar_release_id: str
    action_release_id: str
    source_epochs: tuple[str, ...]
    evidence_state: str
    synthetic_permit_id: str
    census_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "bundle_id": self.bundle_id,
            "trial_registry_binding_id": self.trial_registry_binding_id,
            "trial_id": self.trial_id,
            "evaluation_permit_id": self.evaluation_permit_id,
            "gate_receipt_id": self.gate_receipt_id,
            "decision_session": self.decision_session.isoformat(),
            "expected_asset_ids": list(self.expected_asset_ids),
            "feature_release_ids": list(self.feature_release_ids),
            "identity_release_ids": list(self.identity_release_ids),
            "security_type_evidence_ids": list(self.security_type_evidence_ids),
            "calendar_release_id": self.calendar_release_id,
            "action_release_id": self.action_release_id,
            "source_epochs": list(self.source_epochs),
            "evidence_state": self.evidence_state,
            "synthetic_permit_id": self.synthetic_permit_id,
        }

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.contract_id != ELIGIBILITY_CENSUS_CONTRACT_ID
        ):
            raise ContractError("eligibility census schema/contract differs")
        if self.evidence_state != "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE":
            raise ContractError("production eligibility census is not implemented")
        for name, values in (
            ("expected_asset_ids", self.expected_asset_ids),
            ("feature_release_ids", self.feature_release_ids),
            ("identity_release_ids", self.identity_release_ids),
            ("security_type_evidence_ids", self.security_type_evidence_ids),
            ("source_epochs", self.source_epochs),
        ):
            if not values or list(values) != sorted(set(values)):
                raise ContractError(f"eligibility census {name} must be nonempty, sorted, and unique")
        for name in (
            "contract_id",
            "bundle_id",
            "trial_registry_binding_id",
            "trial_id",
            "evaluation_permit_id",
            "gate_receipt_id",
            "calendar_release_id",
            "action_release_id",
            "synthetic_permit_id",
            "census_id",
        ):
            require_sha256(getattr(self, name), f"eligibility_census.{name}")
        for group_name, values in (
            ("feature_release_ids", self.feature_release_ids),
            ("identity_release_ids", self.identity_release_ids),
            ("security_type_evidence_ids", self.security_type_evidence_ids),
        ):
            for index, value in enumerate(values):
                require_sha256(value, f"eligibility_census.{group_name}[{index}]")
        if self.census_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("eligibility census ID differs from exact expected coverage")

    @classmethod
    def synthetic_from_rows(
        cls,
        metadata: SealedBundleMetadata,
        rows: Iterable[FeatureRow],
        *,
        permit: SyntheticOnlyPermit,
    ) -> "EligibilityCensus":
        verified = require_synthetic_permit(permit, scope="SYNTHETIC_ELIGIBILITY_CENSUS")
        materialized = tuple(rows)
        if not materialized:
            raise ContractError("eligibility census cannot be empty")
        sessions = {row.decision_session for row in materialized}
        if len(sessions) != 1:
            raise ContractError("eligibility census rows must share one decision session")
        unsigned = {
            "schema_version": 1,
            "contract_id": ELIGIBILITY_CENSUS_CONTRACT_ID,
            "bundle_id": metadata.bundle_id,
            "trial_registry_binding_id": metadata.trial_registry_binding_id,
            "trial_id": metadata.trial_id,
            "evaluation_permit_id": metadata.evaluation_permit_id,
            "gate_receipt_id": metadata.gate_receipt.receipt_id,
            "decision_session": next(iter(sessions)).isoformat(),
            "expected_asset_ids": sorted(row.asset_id for row in materialized),
            "feature_release_ids": sorted({row.source_release_id for row in materialized}),
            "identity_release_ids": sorted({row.identity_release_id for row in materialized}),
            "security_type_evidence_ids": sorted(
                {row.security_type_evidence_id for row in materialized}
            ),
            "calendar_release_id": metadata.calendar_release_id,
            "action_release_id": metadata.action_release_id,
            "source_epochs": sorted({row.source_epoch for row in materialized}),
            "evidence_state": "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            "synthetic_permit_id": verified.permit_id,
        }
        census = cls(
            schema_version=1,
            contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
            bundle_id=metadata.bundle_id,
            trial_registry_binding_id=metadata.trial_registry_binding_id,
            trial_id=metadata.trial_id,
            evaluation_permit_id=metadata.evaluation_permit_id,
            gate_receipt_id=metadata.gate_receipt.receipt_id,
            decision_session=next(iter(sessions)),
            expected_asset_ids=tuple(unsigned["expected_asset_ids"]),
            feature_release_ids=tuple(unsigned["feature_release_ids"]),
            identity_release_ids=tuple(unsigned["identity_release_ids"]),
            security_type_evidence_ids=tuple(unsigned["security_type_evidence_ids"]),
            calendar_release_id=metadata.calendar_release_id,
            action_release_id=metadata.action_release_id,
            source_epochs=tuple(unsigned["source_epochs"]),
            evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            synthetic_permit_id=verified.permit_id,
            census_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        census.validate()
        return census
