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
        if type(self.minimum_folds) is not int or self.minimum_folds < 2:
            raise ResearchContractError("temporal minimum_folds must be an integer at least two")
        if type(self.minimum_positive_fraction) is not float or not (
            0.0 < self.minimum_positive_fraction <= 1.0
        ):
            raise ResearchContractError("temporal positive-fold fraction is invalid")
        if type(self.require_positive_leave_one_out) is not bool:
            raise ResearchContractError("leave-one-out policy must be an exact bool")


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
        for name, value in (
            ("minimum_positive_variant_fraction", self.minimum_positive_variant_fraction),
            ("minimum_median_retention", self.minimum_median_retention),
        ):
            if type(value) is not float or not (0.0 < value <= 1.0):
                raise ResearchContractError(f"{name} must lie in (0,1]")


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
class SourceEpochPolicy:
    required_epoch_ids: tuple[str, ...]
    minimum_distinct_oos_dates: int = 252

    def validate(self) -> None:
        if type(self.required_epoch_ids) is not tuple or not self.required_epoch_ids:
            raise ResearchContractError("source-epoch policy requires explicit epochs")
        if any(type(value) is not str or not value.isascii() or not value for value in self.required_epoch_ids):
            raise ResearchContractError("source-epoch IDs must be nonempty ASCII strings")
        if tuple(sorted(set(self.required_epoch_ids))) != self.required_epoch_ids:
            raise ResearchContractError("source-epoch IDs must be unique and sorted")
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
    if type(variants) is not tuple or not variants:
        raise ResearchContractError("registered stability variants must be nonempty")
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
    permit: SyntheticOnlyPermit, fixture: np.ndarray,
) -> SourceEpochResult:
    require_synthetic_permit(permit, fixture)
    policy.validate()
    if type(effects) is not tuple:
        raise ResearchContractError("source-epoch effects must be a tuple")
    ids = tuple(item.epoch_id for item in effects)
    if ids != tuple(sorted(set(ids))) or ids != policy.required_epoch_ids:
        raise ResearchContractError("source-epoch evidence must exactly cover required sorted epochs")
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
