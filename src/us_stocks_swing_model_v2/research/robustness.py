"""Synthetic-only temporal, variant, and source-epoch robustness gates."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import math
import re

import numpy as np

from .contracts import ResearchContractError, SyntheticOnlyPermit, require_synthetic_permit


_SHA256 = re.compile(r"[0-9a-f]{64}")


class RobustnessState(str, Enum):
    MECHANICS_READY = "MECHANICS_READY"
    MECHANICS_INCONCLUSIVE = "MECHANICS_INCONCLUSIVE"


@dataclass(frozen=True)
class TemporalConcentrationPolicy:
    minimum_folds: int = 8
    minimum_positive_fraction: float = 0.625
    require_positive_leave_one_out: bool = True

    def validate(self) -> None:
        if type(self.minimum_folds) is not int or self.minimum_folds != 8:
            raise ResearchContractError("temporal minimum_folds must remain exactly eight")
        if (
            type(self.minimum_positive_fraction) is not float
            or self.minimum_positive_fraction != 0.625
        ):
            raise ResearchContractError(
                "temporal positive-fold fraction must remain exactly 0.625"
            )
        if self.require_positive_leave_one_out is not True:
            raise ResearchContractError(
                "positive leave-one-fold-out policy must remain required"
            )


@dataclass(frozen=True)
class FoldEffect:
    fold_id: str
    observation_count: int
    stress_cost_effect: float


@dataclass(frozen=True)
class TemporalConcentrationResult:
    state: RobustnessState
    positive_folds: int
    required_positive_folds: int
    leave_one_out_effects: tuple[float, ...]
    reasons: tuple[str, ...]
    mechanics_only: bool = True


@dataclass(frozen=True)
class StabilityPolicy:
    seed_count: int = 5
    minimum_positive_variant_fraction: float = 0.8
    minimum_median_retention: float = 0.5

    def validate(self) -> None:
        if type(self.seed_count) is not int or self.seed_count != 5:
            raise ResearchContractError("stability seed_count must remain exactly five")
        if (
            type(self.minimum_positive_variant_fraction) is not float
            or self.minimum_positive_variant_fraction != 0.8
        ):
            raise ResearchContractError(
                "minimum_positive_variant_fraction must remain exactly 0.8"
            )
        if (
            type(self.minimum_median_retention) is not float
            or self.minimum_median_retention != 0.5
        ):
            raise ResearchContractError(
                "minimum_median_retention must remain exactly 0.5"
            )


@dataclass(frozen=True)
class VariantEffect:
    variant_id: str
    stress_cost_effect: float


@dataclass(frozen=True)
class StabilityResult:
    state: RobustnessState
    positive_variants: int
    required_positive_variants: int
    median_retention: float
    reasons: tuple[str, ...]
    mechanics_only: bool = True


@dataclass(frozen=True)
class SourceEpochReleaseBinding:
    release_id: str
    source_epoch: str

    def validate(self) -> None:
        if type(self.release_id) is not str or _SHA256.fullmatch(self.release_id) is None:
            raise ResearchContractError(
                "source-epoch release ID must be an exact lowercase SHA-256"
            )
        if (
            type(self.source_epoch) is not str
            or not self.source_epoch
            or not self.source_epoch.isascii()
        ):
            raise ResearchContractError(
                "source-epoch release binding requires a nonempty ASCII epoch"
            )


@dataclass(frozen=True)
class SourceEpochPolicy:
    minimum_distinct_oos_dates: int = 252

    def validate(self) -> None:
        if type(self.minimum_distinct_oos_dates) is not int or self.minimum_distinct_oos_dates != 252:
            raise ResearchContractError("source-epoch minimum must remain exactly 252 OOS dates")


@dataclass(frozen=True)
class SourceEpochEffect:
    epoch_id: str
    distinct_oos_dates: int
    stress_cost_effect: float


@dataclass(frozen=True)
class SourceEpochResult:
    state: RobustnessState
    reasons: tuple[str, ...]
    mechanics_only: bool = True


def _validate_fold_effects(folds: tuple[FoldEffect, ...]) -> None:
    if type(folds) is not tuple or not folds:
        raise ResearchContractError("fold effects must be a nonempty tuple")
    ids = tuple(item.fold_id for item in folds)
    if any(type(value) is not str or not value.isascii() or not value for value in ids):
        raise ResearchContractError("fold IDs must be nonempty ASCII strings")
    if len(set(ids)) != len(ids):
        raise ResearchContractError("fold IDs must be unique")
    for item in folds:
        if type(item.observation_count) is not int or item.observation_count <= 0:
            raise ResearchContractError("fold observation counts must be positive integers")
        if type(item.stress_cost_effect) is not float or not math.isfinite(item.stress_cost_effect):
            raise ResearchContractError("fold effects must be finite explicit floats")


def evaluate_temporal_concentration(
    *, folds: tuple[FoldEffect, ...], policy: TemporalConcentrationPolicy,
    permit: SyntheticOnlyPermit, fixture: np.ndarray,
) -> TemporalConcentrationResult:
    require_synthetic_permit(permit, fixture)
    policy.validate()
    _validate_fold_effects(folds)
    required = math.ceil(policy.minimum_positive_fraction * len(folds))
    positive = sum(item.stress_cost_effect > 0.0 for item in folds)
    leave_one_out: list[float] = []
    if len(folds) > 1:
        for omitted in range(len(folds)):
            retained = tuple(
                item for index, item in enumerate(folds) if index != omitted
            )
            count = sum(item.observation_count for item in retained)
            leave_one_out.append(
                sum(
                    item.stress_cost_effect * item.observation_count
                    for item in retained
                )
                / count
            )
    reasons: list[str] = []
    if len(folds) < policy.minimum_folds:
        reasons.append("INSUFFICIENT_OUTER_FOLDS")
    if positive < required:
        reasons.append("INSUFFICIENT_POSITIVE_FOLDS")
    if policy.require_positive_leave_one_out and any(value <= 0.0 for value in leave_one_out):
        reasons.append("LEAVE_ONE_FOLD_OUT_NONPOSITIVE")
    return TemporalConcentrationResult(
        RobustnessState.MECHANICS_READY if not reasons else RobustnessState.MECHANICS_INCONCLUSIVE,
        positive, required, tuple(leave_one_out), tuple(reasons),
    )


def deterministic_stability_seeds(trial_id: str, *, count: int = 5) -> tuple[int, ...]:
    if type(trial_id) is not str or _SHA256.fullmatch(trial_id) is None:
        raise ResearchContractError("trial_id must be an exact lowercase SHA-256")
    if type(count) is not int or count != 5:
        raise ResearchContractError("the global stability policy requires exactly five seeds")
    seeds = tuple(
        int.from_bytes(hashlib.sha256(f"{trial_id}:stability:{index}".encode("ascii")).digest()[:4], "big")
        for index in range(count)
    )
    if len(set(seeds)) != count:
        raise ResearchContractError("derived stability seeds unexpectedly collided")
    return seeds


def verify_deterministic_repeat(first_hash: str, second_hash: str) -> bool:
    for value in (first_hash, second_hash):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ResearchContractError("deterministic repeat hashes must be SHA-256 values")
    return first_hash == second_hash


def evaluate_variant_stability(
    *, base_effect: float, variants: tuple[VariantEffect, ...], policy: StabilityPolicy,
    permit: SyntheticOnlyPermit, fixture: np.ndarray,
) -> StabilityResult:
    require_synthetic_permit(permit, fixture)
    policy.validate()
    if type(base_effect) is not float or not math.isfinite(base_effect) or base_effect <= 0.0:
        raise ResearchContractError("base effect must be a finite positive explicit float")
    if type(variants) is not tuple or len(variants) != policy.seed_count:
        raise ResearchContractError(
            "registered stability variant census must exactly match seed_count"
        )
    ids = tuple(item.variant_id for item in variants)
    if any(type(value) is not str or not value.isascii() or not value for value in ids) or len(set(ids)) != len(ids):
        raise ResearchContractError("variant IDs must be unique nonempty ASCII strings")
    effects = np.asarray([item.stress_cost_effect for item in variants], dtype=np.float64)
    if any(type(item.stress_cost_effect) is not float for item in variants) or not np.isfinite(effects).all():
        raise ResearchContractError("variant effects must be finite explicit floats")
    positive = int(np.sum(effects > 0.0))
    required = math.ceil(policy.minimum_positive_variant_fraction * len(variants))
    retention = float(np.median(effects) / base_effect)
    reasons: list[str] = []
    if positive < required:
        reasons.append("INSUFFICIENT_POSITIVE_VARIANTS")
    if retention < policy.minimum_median_retention:
        reasons.append("MEDIAN_EFFECT_RETENTION")
    return StabilityResult(
        RobustnessState.MECHANICS_READY if not reasons else RobustnessState.MECHANICS_INCONCLUSIVE,
        positive, required, retention, tuple(reasons),
    )


def evaluate_source_epoch_robustness(
    *, effects: tuple[SourceEpochEffect, ...], policy: SourceEpochPolicy,
    release_bindings: tuple[SourceEpochReleaseBinding, ...],
    permit: SyntheticOnlyPermit, fixture: np.ndarray,
) -> SourceEpochResult:
    require_synthetic_permit(permit, fixture)
    policy.validate()
    if type(release_bindings) is not tuple or not release_bindings:
        raise ResearchContractError(
            "source-epoch robustness requires release-derived bindings"
        )
    release_ids: list[str] = []
    for binding in release_bindings:
        if type(binding) is not SourceEpochReleaseBinding:
            raise ResearchContractError("source-epoch release binding is invalid")
        binding.validate()
        release_ids.append(binding.release_id)
    if release_ids != sorted(set(release_ids)):
        raise ResearchContractError(
            "source-epoch release bindings must be sorted and unique"
        )
    required_epoch_ids = tuple(
        sorted({binding.source_epoch for binding in release_bindings})
    )
    if type(effects) is not tuple:
        raise ResearchContractError("source-epoch effects must be a tuple")
    ids = tuple(item.epoch_id for item in effects)
    if ids != tuple(sorted(set(ids))) or ids != required_epoch_ids:
        raise ResearchContractError(
            "source-epoch evidence must exactly cover release-derived sorted epochs"
        )
    reasons: list[str] = []
    for item in effects:
        if type(item.distinct_oos_dates) is not int or item.distinct_oos_dates < 0:
            raise ResearchContractError("source-epoch OOS date counts must be nonnegative integers")
        if type(item.stress_cost_effect) is not float or not math.isfinite(item.stress_cost_effect):
            raise ResearchContractError("source-epoch effects must be finite explicit floats")
        if item.distinct_oos_dates < policy.minimum_distinct_oos_dates:
            reasons.append(f"{item.epoch_id}:INSUFFICIENT_OOS_DATES")
        if item.stress_cost_effect <= 0.0:
            reasons.append(f"{item.epoch_id}:NONPOSITIVE_STRESS_COST_EFFECT")
    return SourceEpochResult(
        RobustnessState.MECHANICS_READY if not reasons else RobustnessState.MECHANICS_INCONCLUSIVE,
        tuple(reasons),
    )
