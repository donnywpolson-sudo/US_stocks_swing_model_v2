from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .bundle import SealedBundleMetadata, load_bundle
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import (
    canonical_json_bytes,
    parse_utc_z,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError, IntegrityError
from .eligibility import EligibilityCensus
from .feature_release import load_feature_release
from .governance import AuthorizationAuthority
from .ledger import PredictionLedger
from .schemas import FeatureRow, SecurityType, UnderlyingPrediction


SYNTHETIC_DIRECT_PREDICTION_SCOPE = (
    "SYNTHETIC_DIRECT_PREDICTION_NOT_PUBLISHABLE"
)


def synthetic_direct_prediction_binding_id(
    *,
    bundle_id: str,
    eligibility_census_id: str,
    ordered_feature_row_hashes: Iterable[str],
) -> str:
    require_sha256(bundle_id, "synthetic_direct_prediction.bundle_id")
    require_sha256(
        eligibility_census_id,
        "synthetic_direct_prediction.eligibility_census_id",
    )
    row_hashes = tuple(ordered_feature_row_hashes)
    for index, row_hash in enumerate(row_hashes):
        require_sha256(
            row_hash,
            f"synthetic_direct_prediction.ordered_feature_row_hashes[{index}]",
        )
    payload = {
        "schema_version": 1,
        "scope": SYNTHETIC_DIRECT_PREDICTION_SCOPE,
        "bundle_id": bundle_id,
        "eligibility_census_id": eligibility_census_id,
        "ordered_feature_row_hashes": list(row_hashes),
    }
    return sha256_bytes(canonical_json_bytes(payload))


@dataclass(frozen=True)
class LinearDistributionArtifact:
    feature_schema_id: str
    coefficients: dict[str, float]
    bias: float
    uncertainty: float

    @classmethod
    def load(cls, path: Path, metadata: SealedBundleMetadata) -> "LinearDistributionArtifact":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError("model artifact is missing or invalid") from exc
        if set(payload) != {"kind", "feature_schema_id", "coefficients", "bias", "uncertainty"}:
            raise ContractError("model artifact fields differ from the sealed contract")
        if payload.get("kind") != "linear_distribution_v1":
            raise ContractError("model artifact kind does not match fit-free loader")
        if not isinstance(payload["feature_schema_id"], str) or not payload["feature_schema_id"]:
            raise ContractError("model feature_schema_id must be an explicit nonempty JSON string")
        if not isinstance(payload["coefficients"], dict) or not payload["coefficients"]:
            raise ContractError("model coefficients must be a nonempty JSON object")
        coefficients: dict[str, float] = {}
        for name, value in payload["coefficients"].items():
            if not isinstance(name, str) or not name:
                raise ContractError("model coefficient names must be nonempty JSON strings")
            coefficients[name] = _explicit_json_number(value, f"coefficient.{name}")
        artifact = cls(
            feature_schema_id=payload["feature_schema_id"],
            coefficients=coefficients,
            bias=_explicit_json_number(payload["bias"], "bias"),
            uncertainty=_explicit_json_number(payload["uncertainty"], "uncertainty"),
        )
        if artifact.feature_schema_id != metadata.feature_schema_id:
            raise ContractError("model and bundle feature schema differ")
        if tuple(artifact.coefficients) != metadata.feature_names:
            raise ContractError("model coefficient order differs from sealed feature order")
        if not all(math.isfinite(value) for value in (*artifact.coefficients.values(), artifact.bias, artifact.uncertainty)):
            raise ContractError("model artifact contains non-finite values")
        if artifact.uncertainty <= 0:
            raise ContractError("model uncertainty must be positive")
        return artifact

    def predict_distribution(self, values: dict[str, float]) -> tuple[float, float]:
        if tuple(values) != tuple(self.coefficients):
            raise ContractError("inference feature order differs from sealed model")
        mean = self.bias + sum(self.coefficients[name] * values[name] for name in self.coefficients)
        return mean, self.uncertainty


class FitFreeInferenceEngine:
    """Loads a sealed, non-trainable artifact and emits underlying forecasts."""

    def __init__(
        self,
        bundle_dir: Path,
        *,
        authorization_authority: AuthorizationAuthority,
        accepted_release_root: Path,
        clock: TrustedClock | None = None,
    ):
        self.bundle_dir = Path(bundle_dir)
        self.accepted_release_root = Path(accepted_release_root)
        self._clock = require_trusted_clock(clock)
        self.metadata = load_bundle(
            self.bundle_dir,
            authorization_authority=authorization_authority,
        )
        if self._clock.trust_eligible and not self.metadata.trust_eligible:
            raise ContractError(
                "production inference rejects synthetic or readiness-blocked bundles"
            )
        model_paths = [item.path for item in self.metadata.artifacts if item.path.endswith("model.json")]
        if len(model_paths) != 1:
            raise ContractError("bundle must contain exactly one model.json artifact")
        self.model = LinearDistributionArtifact.load(self.bundle_dir / model_paths[0], self.metadata)

    def predict(
        self,
        rows: Iterable[FeatureRow],
        *,
        eligibility_census: EligibilityCensus,
        synthetic_permit: SyntheticOnlyPermit,
    ) -> tuple[UnderlyingPrediction, ...]:
        materialized = tuple(rows)
        permit = require_synthetic_permit(
            synthetic_permit,
            scope=SYNTHETIC_DIRECT_PREDICTION_SCOPE,
        )
        expected_binding = synthetic_direct_prediction_binding_id(
            bundle_id=self.metadata.bundle_id,
            eligibility_census_id=eligibility_census.census_id,
            ordered_feature_row_hashes=(
                row.row_hash for row in materialized
            ),
        )
        if permit.fixture_id != expected_binding:
            raise ContractError(
                "synthetic direct-prediction permit differs from the exact "
                "bundle, census, and ordered feature rows"
            )
        if self._clock.trust_eligible:
            raise ContractError("production inference cannot expose uncommitted predictions")
        return self._predict_rows(
            materialized,
            eligibility_census=eligibility_census,
        )

    def predict_and_commit(
        self,
        *,
        feature_release_directory: Path,
        eligibility_census: EligibilityCensus,
        prediction_ledger: PredictionLedger,
        previous_anchor: Path | None = None,
    ) -> Mapping[str, object]:
        manifest, rows = load_feature_release(
            feature_release_directory,
            accepted_release_root=self.accepted_release_root,
        )
        if (
            (manifest.release_id,) != eligibility_census.feature_release_ids
            or manifest.release_id not in self.metadata.allowed_feature_release_ids
        ):
            raise ContractError("verified feature release differs from census/bundle allowlists")
        predictions = self._predict_rows(rows, eligibility_census=eligibility_census)
        return prediction_ledger._append_census_from_engine(
            predictions,
            census=eligibility_census,
            bundle_id=self.metadata.bundle_id,
            feature_release_id=manifest.release_id,
            previous_anchor=previous_anchor,
        )

    def _predict_rows(
        self,
        rows: Iterable[FeatureRow],
        *,
        eligibility_census: EligibilityCensus,
    ) -> tuple[UnderlyingPrediction, ...]:
        eligibility_census.validate()
        if (
            eligibility_census.contract_id != self.metadata.eligibility_census_contract_id
            or eligibility_census.bundle_id != self.metadata.bundle_id
            or eligibility_census.trial_registry_binding_id
            != self.metadata.trial_registry_binding_id
            or eligibility_census.trial_id != self.metadata.trial_id
            or eligibility_census.evaluation_permit_id
            != self.metadata.evaluation_permit_id
            or eligibility_census.gate_receipt_id
            != self.metadata.gate_receipt.receipt_id
        ):
            raise ContractError("eligibility census differs from the sealed bundle contract")
        materialized = tuple(rows)
        asset_ids = tuple(sorted(row.asset_id for row in materialized))
        if asset_ids != eligibility_census.expected_asset_ids:
            raise ContractError("feature rows do not exactly cover the expected eligibility census")
        evidence_sets = {
            "feature": tuple(sorted({row.source_release_id for row in materialized})),
            "identity": tuple(sorted({row.identity_release_id for row in materialized})),
            "security_type": tuple(
                sorted({row.security_type_evidence_id for row in materialized})
            ),
            "action": tuple(sorted({row.action_release_id for row in materialized})),
            "source_epoch": tuple(sorted({row.source_epoch for row in materialized})),
        }
        if (
            evidence_sets["feature"] != eligibility_census.feature_release_ids
            or evidence_sets["identity"] != eligibility_census.identity_release_ids
            or evidence_sets["security_type"]
            != eligibility_census.security_type_evidence_ids
            or evidence_sets["source_epoch"] != eligibility_census.source_epochs
            or {row.calendar_release_id for row in materialized}
            != {eligibility_census.calendar_release_id}
            or evidence_sets["action"] != (eligibility_census.action_release_id,)
            or eligibility_census.action_release_id != self.metadata.action_release_id
        ):
            raise ContractError("feature evidence does not exactly match the eligibility census")
        observed = require_aware_utc(self._clock.now(), "inference.clock")
        staged: list[dict[str, object]] = []
        for row in materialized:
            row.validate()
            if row.decision_session != eligibility_census.decision_session:
                raise ContractError("feature decision session differs from eligibility census")
            decision = require_aware_utc(row.decision_at, "decision_at")
            if parse_utc_z(self.metadata.sealed_at, "bundle.sealed_at") > decision:
                raise ContractError("sealed bundle cannot be used before its sealing time")
            if observed < decision:
                raise ContractError("inference recorded_at cannot backdate a prediction")
            if observed > row.prediction_deadline_at or observed >= row.information_barrier_at:
                raise ContractError("inference occurred after the entry/label-safe deadline")
            latency_minutes = (observed - decision).total_seconds() / 60
            if latency_minutes > self.metadata.maximum_inference_latency_minutes:
                raise ContractError("inference exceeded the sealed maximum latency")
            reason: str | None = None
            if row.point_in_time_state != "PIT_CONFIRMED":
                reason = f"untrusted_point_in_time_state:{row.point_in_time_state.lower()}"
            elif row.source_release_id not in self.metadata.allowed_feature_release_ids:
                reason = "feature_release_not_sealed"
            elif row.identity_release_id not in self.metadata.allowed_identity_release_ids:
                reason = "identity_release_not_sealed"
            elif row.security_type_evidence_id not in self.metadata.allowed_security_type_evidence_ids:
                reason = "security_type_evidence_not_sealed"
            elif row.calendar_release_id != self.metadata.calendar_release_id:
                reason = "calendar_release_mismatch"
            elif row.source_epoch not in self.metadata.allowed_source_epochs:
                reason = "source_epoch_not_sealed"
            elif row.security_type is SecurityType.UNKNOWN:
                reason = "unknown_security_type"
            elif row.feature_schema_id != self.metadata.feature_schema_id:
                reason = "feature_schema_mismatch"
            elif tuple(row.values) != self.metadata.feature_names:
                reason = "feature_order_mismatch"
            elif any(type(row.values[name]) is not float for name in self.metadata.feature_names):
                reason = "feature_type_mismatch"
            else:
                age_minutes = (row.decision_at - row.available_at).total_seconds() / 60
                if age_minutes < 0 or age_minutes > self.metadata.maximum_feature_age_minutes:
                    reason = "stale_feature"
                identity_age_minutes = (row.decision_at - row.identity_known_at).total_seconds() / 60
                if identity_age_minutes < 0 or identity_age_minutes > self.metadata.maximum_identity_age_minutes:
                    reason = "stale_identity_evidence"
            if reason:
                staged.append({"row": row, "abstention": reason})
                continue
            mean, uncertainty = self.model.predict_distribution(dict(row.values))
            if uncertainty > self.metadata.maximum_uncertainty:
                staged.append({"row": row, "abstention": "uncertainty_above_sealed_limit"})
                continue
            z_upper = (self.metadata.neutral_band - mean) / uncertainty
            z_lower = (-self.metadata.neutral_band - mean) / uncertainty
            cdf_upper = _normal_cdf(z_upper)
            cdf_lower = _normal_cdf(z_lower)
            staged.append(
                {
                    "row": row,
                    "mean": mean,
                    "uncertainty": uncertainty,
                    "p_down": cdf_lower,
                    "p_neutral": cdf_upper - cdf_lower,
                    "p_up": 1.0 - cdf_upper,
                }
            )
        actionable = sorted(
            (entry for entry in staged if "mean" in entry),
            key=lambda entry: (-abs(float(entry["mean"])), entry["row"].symbol),
        )
        ranks = {id(entry): rank for rank, entry in enumerate(actionable, start=1)}
        predictions: list[UnderlyingPrediction] = []
        for entry in staged:
            row = entry["row"]
            abstention = entry.get("abstention")
            prediction = UnderlyingPrediction.create(
                asset_id=row.asset_id,
                symbol=row.symbol,
                security_type=row.security_type,
                decision_session=row.decision_session,
                decision_at=row.decision_at,
                recorded_at=observed,
                time_authority=self._clock.mode,
                synthetic_clock_permit_id=self._clock.synthetic_permit_id,
                eligibility_census_id=eligibility_census.census_id,
                bundle_id=self.metadata.bundle_id,
                feature_release_id=row.source_release_id,
                feature_row_hash=row.row_hash,
                identity_release_id=row.identity_release_id,
                security_type_evidence_id=row.security_type_evidence_id,
                calendar_release_id=row.calendar_release_id,
                action_release_id=self.metadata.action_release_id,
                source_epoch=row.source_epoch,
                point_in_time_state=row.point_in_time_state,
                prediction_deadline_at=row.prediction_deadline_at,
                information_barrier_at=row.information_barrier_at,
                expected_five_session_return=None if abstention else float(entry["mean"]),
                p_up=None if abstention else float(entry["p_up"]),
                p_down=None if abstention else float(entry["p_down"]),
                p_neutral=None if abstention else float(entry["p_neutral"]),
                uncertainty=None if abstention else float(entry["uncertainty"]),
                rank=None if abstention else ranks[id(entry)],
                abstain=bool(abstention),
                abstention_reason=str(abstention) if abstention else None,
            )
            prediction.validate()
            predictions.append(prediction)
        return tuple(predictions)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _explicit_json_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"model {name} must be an explicit JSON numeric scalar")
    number = float(value)
    if not math.isfinite(number):
        raise ContractError(f"model {name} must be finite")
    return number
