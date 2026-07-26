"""Training-only builder for frozen synthetic outer-fold predictions.

This module receives inner labels and outer-fit labels. Its request type has no
field for outer-audit labels, so model selection and fitting cannot consume them.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

from .artifacts import (
    DistributionPrediction,
    ExecutorRegistration,
    FoldFitAudit,
    FrozenPredictionArtifact,
    InnerFoldSampleAudit,
    LinearDistributionModel,
)
from .contracts import (
    ResearchContractError,
    finite_float64,
    require_unique_ascii_ids,
)


@dataclass(frozen=True)
class InnerBuilderFold:
    fit_sample_ids: tuple[str, ...]
    fit_features: np.ndarray
    fit_targets: np.ndarray
    audit_sample_ids: tuple[str, ...]
    audit_features: np.ndarray
    audit_targets: np.ndarray


@dataclass(frozen=True)
class OuterBuilderRequest:
    registration: ExecutorRegistration
    outer_fold_number: int
    fit_sample_ids: tuple[str, ...]
    fit_features: np.ndarray
    fit_targets: np.ndarray
    audit_sample_ids: tuple[str, ...]
    audit_features: np.ndarray
    inner_folds: tuple[InnerBuilderFold, ...]


@dataclass(frozen=True)
class _FittedRidge:
    coefficients: np.ndarray
    bias: float
    uncertainty: float


def _validated_xy(
    sample_ids: tuple[str, ...],
    features: np.ndarray,
    targets: np.ndarray | None,
    *,
    name: str,
    feature_count: int,
) -> tuple[str, ...]:
    ids = require_unique_ascii_ids(sample_ids, name=f"{name}_sample_ids")
    x = finite_float64(features, name=f"{name}_features", ndim=2)
    if x.shape != (len(ids), feature_count):
        raise ResearchContractError(f"{name} feature shape differs from its IDs/schema")
    if targets is not None:
        y = finite_float64(targets, name=f"{name}_targets", ndim=1)
        if y.shape != (len(ids),):
            raise ResearchContractError(f"{name} target shape differs from its IDs")
    return ids


def _validate_request(request: OuterBuilderRequest) -> None:
    if not isinstance(request, OuterBuilderRequest):
        raise ResearchContractError("builder requires an exact OuterBuilderRequest")
    request.registration.validate()
    if (
        isinstance(request.outer_fold_number, bool)
        or not isinstance(request.outer_fold_number, int)
        or request.outer_fold_number < 0
    ):
        raise ResearchContractError("outer fold number must be a nonnegative integer")
    feature_count = len(request.registration.feature_names)
    fit_ids = _validated_xy(
        request.fit_sample_ids,
        request.fit_features,
        request.fit_targets,
        name="outer_fit",
        feature_count=feature_count,
    )
    audit_ids = _validated_xy(
        request.audit_sample_ids,
        request.audit_features,
        None,
        name="outer_audit",
        feature_count=feature_count,
    )
    if set(fit_ids) & set(audit_ids):
        raise ResearchContractError("outer builder fit/audit IDs overlap")
    if not request.inner_folds:
        raise ResearchContractError("builder requires at least one inner fold")
    outer_positions = {sample_id: index for index, sample_id in enumerate(fit_ids)}
    for number, inner in enumerate(request.inner_folds):
        if not isinstance(inner, InnerBuilderFold):
            raise ResearchContractError("builder inner fold type differs")
        inner_fit_ids = _validated_xy(
            inner.fit_sample_ids,
            inner.fit_features,
            inner.fit_targets,
            name=f"inner_{number}_fit",
            feature_count=feature_count,
        )
        inner_audit_ids = _validated_xy(
            inner.audit_sample_ids,
            inner.audit_features,
            inner.audit_targets,
            name=f"inner_{number}_audit",
            feature_count=feature_count,
        )
        if set(inner_fit_ids) & set(inner_audit_ids):
            raise ResearchContractError("inner builder fit/audit IDs overlap")
        if not (set(inner_fit_ids) | set(inner_audit_ids)) <= set(fit_ids):
            raise ResearchContractError("inner builder IDs escape outer fit")
        for ids, x, y in (
            (inner_fit_ids, inner.fit_features, inner.fit_targets),
            (inner_audit_ids, inner.audit_features, inner.audit_targets),
        ):
            positions = np.asarray([outer_positions[value] for value in ids], dtype=np.int64)
            if not np.array_equal(x, request.fit_features[positions]) or not np.array_equal(
                y, request.fit_targets[positions]
            ):
                raise ResearchContractError("inner builder values differ from the outer-fit census")


def _fit_fold_local_ridge(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    alpha: float,
    uncertainty_floor: float,
) -> _FittedRidge:
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            feature_mean = np.mean(features, axis=0, dtype=np.float64)
            feature_scale = np.std(features, axis=0, dtype=np.float64)
            feature_scale = np.where(feature_scale > 0.0, feature_scale, 1.0)
            scaled = (features - feature_mean) / feature_scale
            target_mean = float(np.mean(targets, dtype=np.float64))
            centered_target = targets - target_mean
            gram = scaled.T @ scaled
            penalty = np.eye(features.shape[1], dtype=np.float64) * alpha
            rhs = scaled.T @ centered_target
    except FloatingPointError as exc:
        raise ResearchContractError(
            "fold-local ridge preprocessing exceeded finite float64 bounds"
        ) from exc
    try:
        scaled_coefficients = np.linalg.solve(gram + penalty, rhs)
    except np.linalg.LinAlgError as exc:
        raise ResearchContractError("fold-local ridge system is not solvable") from exc
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            coefficients = scaled_coefficients / feature_scale
            bias = target_mean - float(feature_mean @ coefficients)
            residuals = targets - (features @ coefficients + bias)
            uncertainty = max(
                float(np.sqrt(np.mean(np.square(residuals), dtype=np.float64))),
                uncertainty_floor,
            )
    except FloatingPointError as exc:
        raise ResearchContractError(
            "fold-local ridge fit exceeded finite float64 bounds"
        ) from exc
    if not all(math.isfinite(value) for value in (*coefficients.tolist(), bias, uncertainty)):
        raise ResearchContractError("fold-local ridge fit produced non-finite parameters")
    return _FittedRidge(
        coefficients=np.asarray(coefficients, dtype=np.float64),
        bias=bias,
        uncertainty=uncertainty,
    )


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def build_frozen_outer_predictions(request: OuterBuilderRequest) -> FrozenPredictionArtifact:
    """Select only on inner folds, refit on outer-fit, and freeze predictions."""

    _validate_request(request)
    scores: list[tuple[float, float]] = []
    for alpha in request.registration.ridge_alphas:
        squared_error = 0.0
        sample_count = 0
        for inner in request.inner_folds:
            fitted = _fit_fold_local_ridge(
                inner.fit_features,
                inner.fit_targets,
                alpha=alpha,
                uncertainty_floor=request.registration.uncertainty_floor,
            )
            try:
                with np.errstate(over="raise", invalid="raise"):
                    predictions = inner.audit_features @ fitted.coefficients + fitted.bias
                    squared_error += float(
                        np.sum(
                            np.square(inner.audit_targets - predictions),
                            dtype=np.float64,
                        )
                    )
            except FloatingPointError as exc:
                raise ResearchContractError(
                    "inner-fold score exceeded finite float64 bounds"
                ) from exc
            sample_count += len(inner.audit_targets)
        if not math.isfinite(squared_error):
            raise ResearchContractError("inner-fold score is non-finite")
        scores.append((alpha, squared_error / sample_count))
    selected_alpha = min(scores, key=lambda item: (item[1], item[0]))[0]
    fitted = _fit_fold_local_ridge(
        request.fit_features,
        request.fit_targets,
        alpha=selected_alpha,
        uncertainty_floor=request.registration.uncertainty_floor,
    )
    try:
        with np.errstate(over="raise", invalid="raise"):
            means = request.audit_features @ fitted.coefficients + fitted.bias
    except FloatingPointError as exc:
        raise ResearchContractError(
            "outer-fold prediction exceeded finite float64 bounds"
        ) from exc
    if not np.all(np.isfinite(means)):
        raise ResearchContractError("outer-fold prediction is non-finite")
    predictions: list[DistributionPrediction] = []
    for sample_id, mean_value in zip(request.audit_sample_ids, means, strict=True):
        mean = float(mean_value)
        z_upper = (request.registration.neutral_band - mean) / fitted.uncertainty
        z_lower = (-request.registration.neutral_band - mean) / fitted.uncertainty
        cdf_upper = _normal_cdf(z_upper)
        cdf_lower = _normal_cdf(z_lower)
        predictions.append(
            DistributionPrediction(
                sample_id=sample_id,
                expected_five_session_return=mean,
                p_up=1.0 - cdf_upper,
                p_down=cdf_lower,
                p_neutral=cdf_upper - cdf_lower,
                uncertainty=fitted.uncertainty,
            )
        )
    fit_audit = FoldFitAudit(
        outer_fit_sample_ids=request.fit_sample_ids,
        outer_audit_sample_ids=request.audit_sample_ids,
        inner_folds=tuple(
            InnerFoldSampleAudit(
                fold_number=number,
                fit_sample_ids=inner.fit_sample_ids,
                audit_sample_ids=inner.audit_sample_ids,
            )
            for number, inner in enumerate(request.inner_folds)
        ),
        alpha_scores=tuple(scores),
        selected_alpha=selected_alpha,
    )
    model = LinearDistributionModel(
        feature_schema_id=request.registration.feature_schema_id,
        coefficients=tuple(
            (name, float(value))
            for name, value in zip(
                request.registration.feature_names,
                fitted.coefficients,
                strict=True,
            )
        ),
        bias=fitted.bias,
        uncertainty=fitted.uncertainty,
    )
    return FrozenPredictionArtifact.create(
        registration=request.registration,
        outer_fold_number=request.outer_fold_number,
        fit_audit=fit_audit,
        model=model,
        predictions=predictions,
    )
