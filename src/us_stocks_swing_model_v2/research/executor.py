"""Registered deterministic nested-WFA executor for synthetic mechanics only."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

import numpy as np

from .artifacts import ExecutorRegistration, FrozenPredictionArtifact
from .builder import InnerBuilderFold, OuterBuilderRequest, build_frozen_outer_predictions
from .contracts import (
    ResearchContractError,
    SyntheticOnlyPermit,
    finite_float64,
    require_synthetic_permit,
    require_unique_ascii_ids,
)
from .evaluator import FoldEvaluation, evaluate_frozen_predictions
from .splits import SessionWindow, TemporalSamples, nested_chronological_splits


EXECUTOR_ENTRYPOINT = (
    "us_stocks_swing_model_v2.research.executor:execute_synthetic_nested_wfa"
)
EXECUTOR_MECHANICS_VERSION = "synthetic_nested_wfa_linear_distribution_v1"


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


@dataclass(frozen=True)
class SyntheticResearchDataset:
    sample_ids: tuple[str, ...]
    feature_names: tuple[str, ...]
    features: np.ndarray
    targets: np.ndarray
    temporal_samples: TemporalSamples

    def validate(self) -> int:
        ids = require_unique_ascii_ids(self.sample_ids, name="synthetic_sample_ids")
        names = require_unique_ascii_ids(self.feature_names, name="synthetic_feature_names")
        features = finite_float64(self.features, name="synthetic_features", ndim=2)
        targets = finite_float64(self.targets, name="synthetic_targets", ndim=1)
        temporal_count = self.temporal_samples.validate()
        if features.shape != (len(ids), len(names)) or targets.shape != (len(ids),):
            raise ResearchContractError("synthetic dataset shapes differ from IDs/schema")
        if temporal_count != len(ids):
            raise ResearchContractError("synthetic temporal census differs from sample IDs")
        return len(ids)


def synthetic_fixture_vector(dataset: SyntheticResearchDataset) -> np.ndarray:
    """Bind values, temporal coordinates, sample IDs, and feature schema."""

    dataset.validate()
    metadata = hashlib.sha256(
        _canonical_bytes(
            {
                "sample_ids": list(dataset.sample_ids),
                "feature_names": list(dataset.feature_names),
            }
        )
    ).digest()
    metadata_values = np.frombuffer(metadata, dtype=np.uint8).astype(np.float64)
    temporal = np.column_stack(
        (
            dataset.temporal_samples.decision_session,
            dataset.temporal_samples.label_start,
            dataset.temporal_samples.label_end,
            dataset.temporal_samples.label_known_session,
        )
    ).astype(np.float64, copy=False)
    return np.concatenate(
        (
            np.ravel(dataset.features),
            dataset.targets,
            np.ravel(temporal),
            metadata_values,
        )
    ).astype(np.float64, copy=False)


@dataclass(frozen=True)
class SyntheticNestedWfaPlan:
    outer_test_windows: tuple[SessionWindow, ...]
    inner_validation_windows: tuple[tuple[SessionWindow, ...], ...]
    session_embargo: int
    minimum_fit_samples: int
    minimum_audit_samples: int

    def as_dict(self) -> dict[str, object]:
        return {
            "outer_test_windows": [
                {"start": value.start, "stop": value.stop}
                for value in self.outer_test_windows
            ],
            "inner_validation_windows": [
                [{"start": value.start, "stop": value.stop} for value in schedule]
                for schedule in self.inner_validation_windows
            ],
            "session_embargo": self.session_embargo,
            "minimum_fit_samples": self.minimum_fit_samples,
            "minimum_audit_samples": self.minimum_audit_samples,
        }


@dataclass(frozen=True)
class SyntheticResearchExecution:
    registration_id: str
    permit_dataset_sha256: str
    plan: SyntheticNestedWfaPlan
    prediction_artifacts: tuple[FrozenPredictionArtifact, ...]
    evaluations: tuple[FoldEvaluation, ...]
    execution_id: str
    state: str = "SYNTHETIC_MECHANICS_ONLY"
    executor_entrypoint: str = EXECUTOR_ENTRYPOINT
    mechanics_version: str = EXECUTOR_MECHANICS_VERSION
    all_predictions_frozen_before_evaluation: bool = True
    real_history_authorized: bool = False
    alpha_evidence: bool = False
    candidate_eligible: bool = False

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "registration_id": self.registration_id,
            "permit_dataset_sha256": self.permit_dataset_sha256,
            "plan": self.plan.as_dict(),
            "prediction_artifact_ids": [
                value.artifact_id for value in self.prediction_artifacts
            ],
            "evaluation_ids": [value.evaluation_id for value in self.evaluations],
            "state": self.state,
            "executor_entrypoint": self.executor_entrypoint,
            "mechanics_version": self.mechanics_version,
            "all_predictions_frozen_before_evaluation": self.all_predictions_frozen_before_evaluation,
            "real_history_authorized": self.real_history_authorized,
            "alpha_evidence": self.alpha_evidence,
            "candidate_eligible": self.candidate_eligible,
        }

    def validate(self) -> None:
        if not self.prediction_artifacts or len(self.prediction_artifacts) != len(self.evaluations):
            raise ResearchContractError("execution fold artifact/evaluation census differs")
        audit_ids: list[str] = []
        for expected_fold, (artifact, evaluation) in enumerate(
            zip(self.prediction_artifacts, self.evaluations, strict=True)
        ):
            artifact.validate()
            evaluation.validate()
            if (
                artifact.outer_fold_number != expected_fold
                or evaluation.outer_fold_number != expected_fold
                or evaluation.prediction_artifact_id != artifact.artifact_id
                or artifact.registration.registration_id != self.registration_id
            ):
                raise ResearchContractError("execution fold bindings differ")
            audit_ids.extend(artifact.fit_audit.outer_audit_sample_ids)
        if len(audit_ids) != len(set(audit_ids)):
            raise ResearchContractError("execution outer audit sample IDs overlap")
        if (
            self.state != "SYNTHETIC_MECHANICS_ONLY"
            or self.executor_entrypoint != EXECUTOR_ENTRYPOINT
            or self.mechanics_version != EXECUTOR_MECHANICS_VERSION
            or self.all_predictions_frozen_before_evaluation is not True
            or self.real_history_authorized is not False
            or self.alpha_evidence is not False
            or self.candidate_eligible is not False
        ):
            raise ResearchContractError("execution loses its synthetic non-alpha boundary")
        expected = hashlib.sha256(_canonical_bytes(self.unsigned_dict())).hexdigest()
        if self.execution_id != expected:
            raise ResearchContractError("execution ID differs from its content")


def _slice_ids(sample_ids: tuple[str, ...], indices: np.ndarray) -> tuple[str, ...]:
    return tuple(sample_ids[int(index)] for index in indices)


@dataclass(frozen=True)
class _PhaseOneArtifactPlan:
    """Capability-restricted build inputs: outer audit labels cannot be represented."""

    requests: tuple[OuterBuilderRequest, ...]

    def validate(self) -> None:
        if not self.requests or any(
            type(request) is not OuterBuilderRequest for request in self.requests
        ):
            raise ResearchContractError("phase-one artifact plan is invalid")


def _build_phase_one_artifacts(
    plan: _PhaseOneArtifactPlan,
) -> tuple[FrozenPredictionArtifact, ...]:
    """Build/freeze without any capability to access the full labeled dataset."""

    if type(plan) is not _PhaseOneArtifactPlan:
        raise ResearchContractError("phase one requires its restricted artifact plan")
    plan.validate()
    frozen = tuple(build_frozen_outer_predictions(request) for request in plan.requests)
    for artifact in frozen:
        artifact.validate()
    return frozen


def execute_synthetic_nested_wfa(
    dataset: SyntheticResearchDataset,
    *,
    permit: SyntheticOnlyPermit,
    registration: ExecutorRegistration,
    plan: SyntheticNestedWfaPlan,
) -> SyntheticResearchExecution:
    """Build every frozen prediction artifact before unlocking outer labels."""

    dataset.validate()
    fixture = synthetic_fixture_vector(dataset)
    require_synthetic_permit(permit, fixture)
    registration.validate()
    if dataset.feature_names != registration.feature_names:
        raise ResearchContractError("dataset feature schema differs from executor registration")
    folds = nested_chronological_splits(
        dataset.temporal_samples,
        plan.outer_test_windows,
        plan.inner_validation_windows,
        session_embargo=plan.session_embargo,
        minimum_fit_samples=plan.minimum_fit_samples,
        minimum_audit_samples=plan.minimum_audit_samples,
    )

    # Phase one has no outer-audit target in its request contract. Complete and
    # content-address every prediction artifact before phase two sees labels.
    build_inputs: list[OuterBuilderRequest] = []
    for fold_number, fold in enumerate(folds):
        inner_requests = tuple(
            InnerBuilderFold(
                fit_sample_ids=_slice_ids(dataset.sample_ids, inner.fit_indices),
                fit_features=dataset.features[inner.fit_indices].copy(),
                fit_targets=dataset.targets[inner.fit_indices].copy(),
                audit_sample_ids=_slice_ids(dataset.sample_ids, inner.audit_indices),
                audit_features=dataset.features[inner.audit_indices].copy(),
                audit_targets=dataset.targets[inner.audit_indices].copy(),
            )
            for inner in fold.inner_folds
        )
        request = OuterBuilderRequest(
            registration=registration,
            outer_fold_number=fold_number,
            fit_sample_ids=_slice_ids(dataset.sample_ids, fold.fit_indices),
            fit_features=dataset.features[fold.fit_indices].copy(),
            fit_targets=dataset.targets[fold.fit_indices].copy(),
            audit_sample_ids=_slice_ids(dataset.sample_ids, fold.audit_indices),
            audit_features=dataset.features[fold.audit_indices].copy(),
            inner_folds=inner_requests,
        )
        build_inputs.append(request)
    frozen = _build_phase_one_artifacts(
        _PhaseOneArtifactPlan(requests=tuple(build_inputs))
    )

    # Phase two is implemented in a separate fit-free module and receives only
    # frozen predictions plus the exact corresponding outer target slice.
    evaluations = tuple(
        evaluate_frozen_predictions(
            artifact,
            audit_sample_ids=artifact.fit_audit.outer_audit_sample_ids,
            audit_targets=dataset.targets[fold.audit_indices].copy(),
        )
        for artifact, fold in zip(frozen, folds, strict=True)
    )
    provisional = SyntheticResearchExecution(
        registration_id=registration.registration_id,
        permit_dataset_sha256=permit.dataset_sha256,
        plan=plan,
        prediction_artifacts=frozen,
        evaluations=evaluations,
        execution_id="",
    )
    execution = SyntheticResearchExecution(
        registration_id=registration.registration_id,
        permit_dataset_sha256=permit.dataset_sha256,
        plan=plan,
        prediction_artifacts=frozen,
        evaluations=evaluations,
        execution_id=hashlib.sha256(_canonical_bytes(provisional.unsigned_dict())).hexdigest(),
    )
    execution.validate()
    return execution
