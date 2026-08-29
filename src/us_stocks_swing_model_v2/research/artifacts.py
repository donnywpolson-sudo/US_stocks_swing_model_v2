"""Immutable, content-addressed artifacts for synthetic research mechanics."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Any, Iterable

from .contracts import ResearchContractError, canonical_bytes, explicit_int, explicit_real, require_unique_ascii_ids


MODEL_KIND = "linear_distribution_v1"
DIRECTION_SEMANTICS = (
    "SIGNED_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN"
)
PREDICTION_ROLE = "FROZEN_OUTER_PREDICTIONS_SYNTHETIC_MECHANICS_ONLY"


def _sha256(payload: object) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class ExecutorRegistration:
    """Frozen model-selection and output semantics for one mechanics run."""

    feature_schema_id: str
    feature_names: tuple[str, ...]
    ridge_alphas: tuple[float, ...]
    neutral_band: float
    uncertainty_floor: float
    registration_id: str
    schema_version: int = 2
    selection_metric: str = "INNER_WEIGHTED_MEAN_SQUARED_ERROR"
    tie_break: str = "LOWEST_ALPHA"
    model_kind: str = MODEL_KIND
    direction_semantics: str = DIRECTION_SEMANTICS
    rank_used_as_direction: bool = False
    real_history_authorized: bool = False
    candidate_sealing_authorized: bool = False

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "feature_schema_id": self.feature_schema_id,
            "feature_names": list(self.feature_names),
            "ridge_alphas": list(self.ridge_alphas),
            "neutral_band": self.neutral_band,
            "uncertainty_floor": self.uncertainty_floor,
            "selection_metric": self.selection_metric,
            "tie_break": self.tie_break,
            "model_kind": self.model_kind,
            "direction_semantics": self.direction_semantics,
            "rank_used_as_direction": self.rank_used_as_direction,
            "real_history_authorized": self.real_history_authorized,
            "candidate_sealing_authorized": self.candidate_sealing_authorized,
        }

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ResearchContractError(
                "executor registration requires schema version 2; "
                "legacy schema-v1 registrations cannot be migrated or relabeled"
            )
        if (
            not isinstance(self.feature_schema_id, str)
            or not self.feature_schema_id
            or not self.feature_schema_id.isascii()
        ):
            raise ResearchContractError("feature_schema_id must be nonempty ASCII")
        require_unique_ascii_ids(self.feature_names, name="feature_names")
        alphas = tuple(explicit_real(value, name="ridge_alpha") for value in self.ridge_alphas)
        if not alphas or any(value <= 0 for value in alphas):
            raise ResearchContractError("ridge alphas must be strictly positive")
        if alphas != tuple(sorted(set(alphas))):
            raise ResearchContractError("ridge alphas must be sorted and unique")
        if explicit_real(self.neutral_band, name="neutral_band") < 0:
            raise ResearchContractError("neutral_band cannot be negative")
        if explicit_real(self.uncertainty_floor, name="uncertainty_floor") <= 0:
            raise ResearchContractError("uncertainty_floor must be positive")
        if (
            self.selection_metric != "INNER_WEIGHTED_MEAN_SQUARED_ERROR"
            or self.tie_break != "LOWEST_ALPHA"
            or self.model_kind != MODEL_KIND
            or self.direction_semantics != DIRECTION_SEMANTICS
            or self.rank_used_as_direction is not False
            or self.real_history_authorized is not False
            or self.candidate_sealing_authorized is not False
        ):
            raise ResearchContractError(
                "executor registration loses its synthetic signed-return boundary"
            )
        if self.registration_id != _sha256(self.unsigned_dict()):
            raise ResearchContractError("executor registration ID differs from its content")

    @classmethod
    def create(
        cls,
        *,
        feature_schema_id: str,
        feature_names: Iterable[str],
        ridge_alphas: Iterable[float],
        neutral_band: float,
        uncertainty_floor: float,
    ) -> "ExecutorRegistration":
        names = tuple(feature_names)
        alphas = tuple(explicit_real(value, name="ridge_alpha") for value in ridge_alphas)
        checked_band = explicit_real(neutral_band, name="neutral_band")
        checked_floor = explicit_real(uncertainty_floor, name="uncertainty_floor")
        unsigned = {
            "schema_version": 2,
            "feature_schema_id": feature_schema_id,
            "feature_names": list(names),
            "ridge_alphas": list(alphas),
            "neutral_band": checked_band,
            "uncertainty_floor": checked_floor,
            "selection_metric": "INNER_WEIGHTED_MEAN_SQUARED_ERROR",
            "tie_break": "LOWEST_ALPHA",
            "model_kind": MODEL_KIND,
            "direction_semantics": DIRECTION_SEMANTICS,
            "rank_used_as_direction": False,
            "real_history_authorized": False,
            "candidate_sealing_authorized": False,
        }
        value = cls(
            feature_schema_id=feature_schema_id,
            feature_names=names,
            ridge_alphas=alphas,
            neutral_band=checked_band,
            uncertainty_floor=checked_floor,
            registration_id=_sha256(unsigned),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class InnerFoldSampleAudit:
    fold_number: int
    fit_sample_ids: tuple[str, ...]
    audit_sample_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "fold_number": self.fold_number,
            "fit_sample_ids": list(self.fit_sample_ids),
            "audit_sample_ids": list(self.audit_sample_ids),
        }

    def validate(self, outer_fit_ids: set[str]) -> None:
        if explicit_int(self.fold_number, name="inner_fold_number") < 0:
            raise ResearchContractError("inner fold number cannot be negative")
        fit = require_unique_ascii_ids(self.fit_sample_ids, name="inner_fit_sample_ids")
        audit = require_unique_ascii_ids(self.audit_sample_ids, name="inner_audit_sample_ids")
        if set(fit) & set(audit):
            raise ResearchContractError("inner fit/audit sample IDs overlap")
        if not (set(fit) | set(audit)) <= outer_fit_ids:
            raise ResearchContractError("inner sample IDs escape the outer fit partition")


@dataclass(frozen=True)
class FoldFitAudit:
    outer_fit_sample_ids: tuple[str, ...]
    outer_audit_sample_ids: tuple[str, ...]
    inner_folds: tuple[InnerFoldSampleAudit, ...]
    alpha_scores: tuple[tuple[float, float], ...]
    selected_alpha: float

    def as_dict(self) -> dict[str, object]:
        return {
            "outer_fit_sample_ids": list(self.outer_fit_sample_ids),
            "outer_audit_sample_ids": list(self.outer_audit_sample_ids),
            "inner_folds": [value.as_dict() for value in self.inner_folds],
            "alpha_scores": [
                {"alpha": alpha, "weighted_mse": score}
                for alpha, score in self.alpha_scores
            ],
            "selected_alpha": self.selected_alpha,
        }

    def validate(self, registration: ExecutorRegistration) -> None:
        registration.validate()
        fit = require_unique_ascii_ids(self.outer_fit_sample_ids, name="outer_fit_sample_ids")
        audit = require_unique_ascii_ids(self.outer_audit_sample_ids, name="outer_audit_sample_ids")
        if set(fit) & set(audit):
            raise ResearchContractError("outer fit/audit sample IDs overlap")
        if not self.inner_folds:
            raise ResearchContractError("fit audit requires inner folds")
        seen_inner_audit_ids: set[str] = set()
        for expected_number, inner in enumerate(self.inner_folds):
            if inner.fold_number != expected_number:
                raise ResearchContractError("inner fold audit ordering differs")
            inner.validate(set(fit))
            if seen_inner_audit_ids & set(inner.audit_sample_ids):
                raise ResearchContractError(
                    "inner audit sample IDs overlap across folds"
                )
            seen_inner_audit_ids.update(inner.audit_sample_ids)
        if len(self.alpha_scores) != len(registration.ridge_alphas):
            raise ResearchContractError("alpha score census differs from registration")
        for (alpha, score), expected_alpha in zip(
            self.alpha_scores, registration.ridge_alphas, strict=True
        ):
            if alpha != expected_alpha or explicit_real(score, name="inner_weighted_mse") < 0:
                raise ResearchContractError("alpha score differs from the registered grid")
        winner = min(self.alpha_scores, key=lambda item: (item[1], item[0]))[0]
        if self.selected_alpha != winner:
            raise ResearchContractError("selected alpha is not the deterministic inner-only winner")


@dataclass(frozen=True)
class LinearDistributionModel:
    feature_schema_id: str
    coefficients: tuple[tuple[str, float], ...]
    bias: float
    uncertainty: float

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": MODEL_KIND,
            "feature_schema_id": self.feature_schema_id,
            "coefficients": {name: value for name, value in self.coefficients},
            "bias": self.bias,
            "uncertainty": self.uncertainty,
        }

    def validate(self, registration: ExecutorRegistration) -> None:
        registration.validate()
        names = tuple(name for name, _ in self.coefficients)
        if self.feature_schema_id != registration.feature_schema_id or names != registration.feature_names:
            raise ResearchContractError("linear distribution model feature schema differs")
        values = tuple(value for _, value in self.coefficients)
        if any(not math.isfinite(value) for value in (*values, self.bias, self.uncertainty)):
            raise ResearchContractError("linear distribution model contains non-finite values")
        if self.uncertainty < registration.uncertainty_floor:
            raise ResearchContractError("linear distribution uncertainty is below its registered floor")


@dataclass(frozen=True)
class DistributionPrediction:
    sample_id: str
    expected_five_session_return: float
    p_up: float
    p_down: float
    p_neutral: float
    uncertainty: float

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "expected_five_session_return": self.expected_five_session_return,
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "uncertainty": self.uncertainty,
        }

    def validate(self) -> None:
        require_unique_ascii_ids((self.sample_id,), name="prediction_sample_id")
        values = tuple(
            explicit_real(value, name=name)
            for name, value in (
                ("expected_five_session_return", self.expected_five_session_return),
                ("p_up", self.p_up),
                ("p_down", self.p_down),
                ("p_neutral", self.p_neutral),
                ("uncertainty", self.uncertainty),
            )
        )
        if any(value < 0 or value > 1 for value in values[1:4]):
            raise ResearchContractError("distribution probabilities must lie in [0,1]")
        if not math.isclose(sum(values[1:4]), 1.0, rel_tol=0.0, abs_tol=1e-12):
            raise ResearchContractError("absolute distribution probabilities must sum to one")
        if self.uncertainty <= 0:
            raise ResearchContractError("prediction uncertainty must be positive")


@dataclass(frozen=True)
class FrozenPredictionArtifact:
    registration: ExecutorRegistration
    outer_fold_number: int
    fit_audit: FoldFitAudit
    model: LinearDistributionModel
    predictions: tuple[DistributionPrediction, ...]
    artifact_id: str
    schema_version: int = 1
    role: str = PREDICTION_ROLE
    frozen_before_outer_label_access: bool = True
    rank_used_as_direction: bool = False
    real_history_authorized: bool = False
    candidate_eligible: bool = False

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "role": self.role,
            "registration": {**self.registration.unsigned_dict(), "registration_id": self.registration.registration_id},
            "outer_fold_number": self.outer_fold_number,
            "fit_audit": self.fit_audit.as_dict(),
            "model": self.model.as_dict(),
            "predictions": [value.as_dict() for value in self.predictions],
            "frozen_before_outer_label_access": self.frozen_before_outer_label_access,
            "rank_used_as_direction": self.rank_used_as_direction,
            "real_history_authorized": self.real_history_authorized,
            "candidate_eligible": self.candidate_eligible,
        }

    def validate(self) -> None:
        if self.schema_version != 1 or isinstance(self.schema_version, bool):
            raise ResearchContractError("prediction artifact schema is invalid")
        self.registration.validate()
        if explicit_int(self.outer_fold_number, name="outer_fold_number") < 0:
            raise ResearchContractError("outer fold number cannot be negative")
        self.fit_audit.validate(self.registration)
        self.model.validate(self.registration)
        if not self.predictions:
            raise ResearchContractError("prediction artifact must be nonempty")
        for prediction in self.predictions:
            prediction.validate()
        prediction_ids = tuple(value.sample_id for value in self.predictions)
        if prediction_ids != self.fit_audit.outer_audit_sample_ids:
            raise ResearchContractError("prediction IDs differ from the exact outer audit census")
        if (
            self.role != PREDICTION_ROLE
            or self.frozen_before_outer_label_access is not True
            or self.rank_used_as_direction is not False
            or self.real_history_authorized is not False
            or self.candidate_eligible is not False
        ):
            raise ResearchContractError("prediction artifact loses its frozen synthetic-only role")
        if self.artifact_id != _sha256(self.unsigned_dict()):
            raise ResearchContractError("frozen prediction artifact ID differs from its content")

    @classmethod
    def create(
        cls,
        *,
        registration: ExecutorRegistration,
        outer_fold_number: int,
        fit_audit: FoldFitAudit,
        model: LinearDistributionModel,
        predictions: Iterable[DistributionPrediction],
    ) -> "FrozenPredictionArtifact":
        values = tuple(predictions)
        provisional = cls(
            registration=registration,
            outer_fold_number=outer_fold_number,
            fit_audit=fit_audit,
            model=model,
            predictions=values,
            artifact_id="",
        )
        artifact = cls(
            registration=registration,
            outer_fold_number=outer_fold_number,
            fit_audit=fit_audit,
            model=model,
            predictions=values,
            artifact_id=_sha256(provisional.unsigned_dict()),
        )
        artifact.validate()
        return artifact
