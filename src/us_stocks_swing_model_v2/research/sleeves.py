"""Independent synthetic sleeve-gate mechanics without an alpha transition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib

import numpy as np

from .contracts import (
    ResearchContractError,
    SyntheticOnlyPermit,
    explicit_real,
    require_synthetic_permit,
    require_unique_ascii_ids,
)


class SleeveState(str, Enum):
    REGISTERED = "REGISTERED"
    MECHANICS_READY = "MECHANICS_READY"
    MECHANICS_FAIL_CLOSED = "MECHANICS_FAIL_CLOSED"


class PortfolioState(str, Enum):
    MECHANICS_READY = "MECHANICS_READY"
    MECHANICS_FAIL_CLOSED = "MECHANICS_FAIL_CLOSED"


@dataclass(frozen=True)
class SleeveThresholds:
    alpha: float
    dsr_probability_minimum: float
    pbo_conservative_maximum: float

    def validate(self) -> None:
        alpha = explicit_real(self.alpha, name="alpha")
        dsr = explicit_real(
            self.dsr_probability_minimum, name="dsr_probability_minimum"
        )
        pbo = explicit_real(
            self.pbo_conservative_maximum, name="pbo_conservative_maximum"
        )
        if not (0.0 < alpha < 1.0):
            raise ResearchContractError("alpha must lie in (0,1)")
        if not (0.0 < dsr < 1.0):
            raise ResearchContractError("DSR threshold must lie in (0,1)")
        if not (0.0 <= pbo < 1.0):
            raise ResearchContractError("PBO threshold must lie in [0,1)")


@dataclass(frozen=True)
class SyntheticSleeveMetrics:
    mean_after_costs: float
    confidence_lower_bound: float
    minimum_economically_effective_mean: float
    romano_wolf_adjusted_p: float
    dsr_probability: float
    pbo_conservative: float
    power_sufficient: bool
    negative_controls_clear: bool
    numerically_valid: bool


@dataclass(frozen=True)
class SleeveGateResult:
    sleeve_id: str
    state: SleeveState
    failed_gates: tuple[str, ...]
    mechanics_only: bool = True


@dataclass(frozen=True)
class PortfolioCharter:
    registered_sleeves: tuple[str, ...]
    included_sleeves: tuple[str, ...]
    charter_hash: str

    def validate(self) -> None:
        registered = require_unique_ascii_ids(
            self.registered_sleeves, name="registered_sleeves"
        )
        included = require_unique_ascii_ids(
            self.included_sleeves, name="included_sleeves"
        )
        if not set(included).issubset(set(registered)):
            raise ResearchContractError("included sleeves must be preregistered")
        payload = ("\0".join(registered) + "\1" + "\0".join(included)).encode("ascii")
        expected = hashlib.sha256(payload).hexdigest()
        if self.charter_hash != expected:
            raise ResearchContractError("portfolio charter hash is invalid")

    @classmethod
    def create(
        cls,
        *,
        registered_sleeves: tuple[str, ...],
        included_sleeves: tuple[str, ...],
    ) -> "PortfolioCharter":
        registered = require_unique_ascii_ids(
            registered_sleeves, name="registered_sleeves"
        )
        included = require_unique_ascii_ids(included_sleeves, name="included_sleeves")
        if not set(included).issubset(set(registered)):
            raise ResearchContractError("included sleeves must be preregistered")
        payload = ("\0".join(registered) + "\1" + "\0".join(included)).encode("ascii")
        return cls(
            registered_sleeves=registered,
            included_sleeves=included,
            charter_hash=hashlib.sha256(payload).hexdigest(),
        )


def evaluate_synthetic_sleeve(
    *,
    sleeve_id: str,
    metrics: SyntheticSleeveMetrics,
    thresholds: SleeveThresholds,
    permit: SyntheticOnlyPermit,
    fixture: np.ndarray,
) -> SleeveGateResult:
    """Exercise gates synthetically; never return a historical evidence state."""

    require_unique_ascii_ids((sleeve_id,), name="sleeve_id")
    require_synthetic_permit(permit, fixture)
    thresholds.validate()
    numeric_metric_names = (
        "mean_after_costs",
        "confidence_lower_bound",
        "minimum_economically_effective_mean",
        "romano_wolf_adjusted_p",
        "dsr_probability",
        "pbo_conservative",
    )
    for name in numeric_metric_names:
        value = getattr(metrics, name)
        if isinstance(value, (bool, np.bool_)) or not isinstance(
            value, (float, np.floating)
        ):
            raise ResearchContractError(f"{name} must be an explicit real float")
        if not np.isfinite(value):
            raise ResearchContractError(f"{name} must be finite")
    for name in ("power_sufficient", "negative_controls_clear", "numerically_valid"):
        if type(getattr(metrics, name)) is not bool:
            raise ResearchContractError(f"{name} must be an exact bool")
    numeric_values = np.asarray(
        [
            metrics.mean_after_costs,
            metrics.confidence_lower_bound,
            metrics.minimum_economically_effective_mean,
            metrics.romano_wolf_adjusted_p,
            metrics.dsr_probability,
            metrics.pbo_conservative,
        ],
        dtype=np.float64,
    )
    failed: list[str] = []
    if not metrics.numerically_valid:
        failed.append("NUMERICAL_VALIDITY")
    if metrics.minimum_economically_effective_mean <= 0.0:
        failed.append("MEES_STRICTLY_POSITIVE")
    if metrics.mean_after_costs <= metrics.minimum_economically_effective_mean:
        failed.append("MEAN_AFTER_COSTS")
    if metrics.confidence_lower_bound <= metrics.minimum_economically_effective_mean:
        failed.append("CONFIDENCE_LOWER_BOUND")
    if not (0.0 <= metrics.romano_wolf_adjusted_p <= thresholds.alpha):
        failed.append("ROMANO_WOLF")
    if not (
        thresholds.dsr_probability_minimum
        <= metrics.dsr_probability
        <= 1.0
    ):
        failed.append("DEFLATED_SHARPE")
    if not (
        0.0
        <= metrics.pbo_conservative
        <= thresholds.pbo_conservative_maximum
    ):
        failed.append("PBO")
    if metrics.power_sufficient is not True:
        failed.append("POWER")
    if metrics.negative_controls_clear is not True:
        failed.append("NEGATIVE_CONTROLS")
    state = (
        SleeveState.MECHANICS_READY
        if not failed
        else SleeveState.MECHANICS_FAIL_CLOSED
    )
    return SleeveGateResult(
        sleeve_id=sleeve_id,
        state=state,
        failed_gates=tuple(failed),
    )


def evaluate_portfolio_mechanics(
    charter: PortfolioCharter,
    sleeve_results: tuple[SleeveGateResult, ...],
) -> PortfolioState:
    """Require every preregistered included sleeve to pass independently."""

    charter.validate()
    ids = require_unique_ascii_ids(
        (result.sleeve_id for result in sleeve_results), name="result_sleeves"
    )
    if set(ids) != set(charter.registered_sleeves):
        raise ResearchContractError("results must cover the complete registered sleeve set")
    by_id = {result.sleeve_id: result for result in sleeve_results}
    if any(result.mechanics_only is not True for result in sleeve_results):
        raise ResearchContractError("portfolio accepts mechanics-only sleeve results")
    if all(
        by_id[sleeve].state == SleeveState.MECHANICS_READY
        for sleeve in charter.included_sleeves
    ):
        return PortfolioState.MECHANICS_READY
    return PortfolioState.MECHANICS_FAIL_CLOSED
