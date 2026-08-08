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
from .controls import NegativeControlResult, NegativeControlState
from .robustness import RobustnessState


class SleeveState(str, Enum):
    REGISTERED = "REGISTERED"
    MECHANICS_READY = "MECHANICS_READY"
    MECHANICS_INCONCLUSIVE_ROBUSTNESS = "MECHANICS_INCONCLUSIVE_ROBUSTNESS"
    MECHANICS_FAIL_CLOSED = "MECHANICS_FAIL_CLOSED"


class PortfolioState(str, Enum):
    MECHANICS_READY = "MECHANICS_READY"
    MECHANICS_INCONCLUSIVE_ROBUSTNESS = "MECHANICS_INCONCLUSIVE_ROBUSTNESS"
    MECHANICS_FAIL_CLOSED = "MECHANICS_FAIL_CLOSED"


REQUIRED_SLEEVES = (
    "stock_long",
    "stock_short",
    "etf_long",
    "etf_short",
)


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
    negative_control_result: NegativeControlResult
    numerically_valid: bool
    robustness_state: RobustnessState


@dataclass(frozen=True)
class SleeveGateResult:
    sleeve_id: str
    state: SleeveState
    failed_gates: tuple[str, ...]
    mechanics_only: bool = True

    def validate(self, *, require_terminal: bool = False) -> None:
        require_unique_ascii_ids((self.sleeve_id,), name="sleeve_id")
        if type(self.state) is not SleeveState:
            raise ResearchContractError("sleeve state must be an exact SleeveState")
        if type(self.failed_gates) is not tuple:
            raise ResearchContractError("failed_gates must be an exact tuple")
        if self.failed_gates:
            require_unique_ascii_ids(self.failed_gates, name="failed_gates")
        if type(self.mechanics_only) is not bool or self.mechanics_only is not True:
            raise ResearchContractError("sleeve result must be mechanics-only")
        if self.state is SleeveState.REGISTERED:
            if require_terminal:
                raise ResearchContractError("portfolio requires terminal sleeve results")
            if self.failed_gates:
                raise ResearchContractError("registered sleeve cannot carry failed gates")
        elif self.state is SleeveState.MECHANICS_FAIL_CLOSED:
            if not self.failed_gates:
                raise ResearchContractError("failed sleeve must identify failed gates")
        elif self.failed_gates:
            raise ResearchContractError("non-failed sleeve cannot carry failed gates")


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
        if registered != REQUIRED_SLEEVES or included != REQUIRED_SLEEVES:
            raise ResearchContractError(
                "portfolio charter must include the exact four required sleeves"
            )
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
        if registered != REQUIRED_SLEEVES or included != REQUIRED_SLEEVES:
            raise ResearchContractError(
                "portfolio charter must include the exact four required sleeves"
            )
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
    for name in ("power_sufficient", "numerically_valid"):
        if type(getattr(metrics, name)) is not bool:
            raise ResearchContractError(f"{name} must be an exact bool")
    if type(metrics.negative_control_result) is not NegativeControlResult:
        raise ResearchContractError(
            "negative_control_result must be an exact NegativeControlResult"
        )
    metrics.negative_control_result.validate()
    if type(metrics.robustness_state) is not RobustnessState:
        raise ResearchContractError("robustness_state must be an exact RobustnessState")
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
    if metrics.negative_control_result.state is not NegativeControlState.CLEAR:
        failed.append("NEGATIVE_CONTROLS")
    if failed:
        state = SleeveState.MECHANICS_FAIL_CLOSED
    elif metrics.robustness_state is RobustnessState.MECHANICS_INCONCLUSIVE:
        state = SleeveState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    else:
        state = SleeveState.MECHANICS_READY
    result = SleeveGateResult(
        sleeve_id=sleeve_id,
        state=state,
        failed_gates=tuple(failed),
    )
    result.validate(require_terminal=True)
    return result


def evaluate_portfolio_mechanics(
    charter: PortfolioCharter,
    sleeve_results: tuple[SleeveGateResult, ...],
) -> PortfolioState:
    """Require every preregistered included sleeve to pass independently."""

    charter.validate()
    for result in sleeve_results:
        if type(result) is not SleeveGateResult:
            raise ResearchContractError("portfolio requires exact SleeveGateResult values")
        result.validate(require_terminal=True)
    ids = require_unique_ascii_ids(
        (result.sleeve_id for result in sleeve_results), name="result_sleeves"
    )
    if set(ids) != set(charter.registered_sleeves):
        raise ResearchContractError("results must cover the complete registered sleeve set")
    by_id = {result.sleeve_id: result for result in sleeve_results}
    included_states = tuple(by_id[sleeve].state for sleeve in charter.included_sleeves)
    if any(state is SleeveState.MECHANICS_FAIL_CLOSED for state in included_states):
        return PortfolioState.MECHANICS_FAIL_CLOSED
    if any(
        state is SleeveState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
        for state in included_states
    ):
        return PortfolioState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    if all(state is SleeveState.MECHANICS_READY for state in included_states):
        return PortfolioState.MECHANICS_READY
    raise ResearchContractError("portfolio contains an unsupported sleeve state")
