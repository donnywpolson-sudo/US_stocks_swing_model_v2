"""Fit-free evaluation of already frozen synthetic prediction artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math

import numpy as np

from .artifacts import FrozenPredictionArtifact
from .contracts import ResearchContractError, canonical_bytes, finite_float64, require_unique_ascii_ids


@dataclass(frozen=True)
class FoldEvaluation:
    prediction_artifact_id: str
    outer_fold_number: int
    audit_sample_ids: tuple[str, ...]
    mean_squared_error: float
    multiclass_log_loss: float
    evaluated_sample_count: int
    evaluation_id: str
    role: str = "SYNTHETIC_MECHANICS_EVALUATION_ONLY"
    evaluator_fit_calls: int = 0
    real_history_authorized: bool = False
    candidate_eligible: bool = False

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "prediction_artifact_id": self.prediction_artifact_id,
            "outer_fold_number": self.outer_fold_number,
            "audit_sample_ids": list(self.audit_sample_ids),
            "mean_squared_error": self.mean_squared_error,
            "multiclass_log_loss": self.multiclass_log_loss,
            "evaluated_sample_count": self.evaluated_sample_count,
            "role": self.role,
            "evaluator_fit_calls": self.evaluator_fit_calls,
            "real_history_authorized": self.real_history_authorized,
            "candidate_eligible": self.candidate_eligible,
        }

    def validate(self) -> None:
        require_unique_ascii_ids(self.audit_sample_ids, name="evaluation_audit_sample_ids")
        if (
            self.evaluated_sample_count != len(self.audit_sample_ids)
            or isinstance(self.evaluated_sample_count, bool)
            or not isinstance(self.evaluated_sample_count, int)
        ):
            raise ResearchContractError("evaluation sample denominator differs")
        if not math.isfinite(self.mean_squared_error) or self.mean_squared_error < 0:
            raise ResearchContractError("evaluation MSE is invalid")
        if not math.isfinite(self.multiclass_log_loss) or self.multiclass_log_loss < 0:
            raise ResearchContractError("evaluation log loss is invalid")
        if (
            self.role != "SYNTHETIC_MECHANICS_EVALUATION_ONLY"
            or self.evaluator_fit_calls != 0
            or self.real_history_authorized is not False
            or self.candidate_eligible is not False
        ):
            raise ResearchContractError("evaluation loses its fit-free synthetic-only role")
        expected = hashlib.sha256(canonical_bytes(self.unsigned_dict())).hexdigest()
        if self.evaluation_id != expected:
            raise ResearchContractError("evaluation ID differs from its content")


def evaluate_frozen_predictions(
    artifact: FrozenPredictionArtifact,
    *,
    audit_sample_ids: tuple[str, ...],
    audit_targets: np.ndarray,
) -> FoldEvaluation:
    """Score exact frozen predictions; this role has no training dependency."""

    artifact.validate()
    ids = require_unique_ascii_ids(audit_sample_ids, name="evaluation_audit_sample_ids")
    targets = finite_float64(audit_targets, name="evaluation_audit_targets", ndim=1)
    if ids != artifact.fit_audit.outer_audit_sample_ids or targets.shape != (len(ids),):
        raise ResearchContractError("evaluator inputs differ from the frozen outer audit census")
    means = np.asarray(
        [value.expected_five_session_return for value in artifact.predictions],
        dtype=np.float64,
    )
    try:
        with np.errstate(over="raise", invalid="raise"):
            mse = float(np.mean(np.square(targets - means), dtype=np.float64))
    except FloatingPointError as exc:
        raise ResearchContractError(
            "evaluation MSE exceeded finite float64 bounds"
        ) from exc
    if not math.isfinite(mse):
        raise ResearchContractError("evaluation MSE is non-finite")
    losses: list[float] = []
    band = artifact.registration.neutral_band
    for target, prediction in zip(targets, artifact.predictions, strict=True):
        if target > band:
            probability = prediction.p_up
        elif target < -band:
            probability = prediction.p_down
        else:
            probability = prediction.p_neutral
        losses.append(-math.log(max(probability, 1e-15)))
    unsigned = {
        "prediction_artifact_id": artifact.artifact_id,
        "outer_fold_number": artifact.outer_fold_number,
        "audit_sample_ids": list(ids),
        "mean_squared_error": mse,
        "multiclass_log_loss": float(np.mean(np.asarray(losses, dtype=np.float64))),
        "evaluated_sample_count": len(ids),
        "role": "SYNTHETIC_MECHANICS_EVALUATION_ONLY",
        "evaluator_fit_calls": 0,
        "real_history_authorized": False,
        "candidate_eligible": False,
    }
    evaluation = FoldEvaluation(
        prediction_artifact_id=artifact.artifact_id,
        outer_fold_number=artifact.outer_fold_number,
        audit_sample_ids=ids,
        mean_squared_error=mse,
        multiclass_log_loss=unsigned["multiclass_log_loss"],
        evaluated_sample_count=len(ids),
        evaluation_id=hashlib.sha256(canonical_bytes(unsigned)).hexdigest(),
    )
    evaluation.validate()
    return evaluation
