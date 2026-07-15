"""Training-only normal-approximation power and MDE planning."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil

import numpy as np
from scipy.special import ndtri

from .contracts import ResearchContractError, explicit_real, finite_float64
from .hac import newey_west_mean


@dataclass(frozen=True)
class PowerPlan:
    training_observations: int
    planned_evaluation_observations: int
    hac_lag: int
    long_run_variance: float
    variance_inflation: float
    alpha: float
    target_power: float
    alternative: str
    minimum_detectable_mean: float
    economic_mean_hurdle: float
    required_evaluation_observations: int
    adequately_powered: bool
    status: str


def training_only_mde(
    training_differentials: np.ndarray,
    *,
    partition_role: str,
    hac_lag: int,
    planned_evaluation_observations: int,
    alpha: float,
    target_power: float,
    alternative: str,
    economic_mean_hurdle: float,
    variance_inflation: float = 1.0,
) -> PowerPlan:
    """Plan sensitivity from training data only; never compute post-hoc power."""

    values = finite_float64(
        training_differentials, name="training_differentials", ndim=1
    )
    if partition_role != "TRAIN":
        raise ResearchContractError("MDE inputs must be explicitly TRAIN partition data")
    if (
        isinstance(planned_evaluation_observations, bool)
        or not isinstance(planned_evaluation_observations, int)
        or planned_evaluation_observations < 1
    ):
        raise ResearchContractError("planned evaluation observations must be positive")
    checked_alpha = explicit_real(alpha, name="alpha")
    checked_power = explicit_real(target_power, name="target_power")
    checked_hurdle = explicit_real(
        economic_mean_hurdle, name="economic_mean_hurdle"
    )
    checked_inflation = explicit_real(
        variance_inflation, name="variance_inflation"
    )
    if not (0.0 < checked_alpha < 1.0) or not (0.0 < checked_power < 1.0):
        raise ResearchContractError("alpha and target_power must lie in (0,1)")
    if alternative not in {"greater", "two-sided"}:
        raise ResearchContractError("alternative must be 'greater' or 'two-sided'")
    if checked_hurdle <= 0.0:
        raise ResearchContractError("economic_mean_hurdle must be strictly positive")
    if checked_inflation < 1.0:
        raise ResearchContractError("variance_inflation must be at least one")

    hac = newey_west_mean(values, lag=hac_lag)
    if hac.status != "OK" or hac.long_run_variance <= 0.0:
        raise ResearchContractError("training long-run variance is degenerate")
    critical_probability = 1.0 - (
        checked_alpha if alternative == "greater" else checked_alpha / 2.0
    )
    critical = float(ndtri(critical_probability))
    power_quantile = float(ndtri(checked_power))
    if critical <= 0.0 or power_quantile <= 0.0:
        raise ResearchContractError(
            "this fail-closed planner requires alpha < 0.5 and target_power > 0.5"
        )
    z_sum = critical + power_quantile
    inflated_lrv = hac.long_run_variance * checked_inflation
    mde = z_sum * np.sqrt(inflated_lrv / planned_evaluation_observations)
    if not np.isfinite(inflated_lrv) or not np.isfinite(mde):
        raise ResearchContractError("power-plan arithmetic is non-finite")
    required_float = inflated_lrv * (z_sum / checked_hurdle) ** 2
    if not np.isfinite(required_float):
        raise ResearchContractError("required sample size is non-finite")
    required = ceil(required_float)
    adequately_powered = bool(
        planned_evaluation_observations >= required
        and checked_hurdle >= mde
    )
    return PowerPlan(
        training_observations=len(values),
        planned_evaluation_observations=planned_evaluation_observations,
        hac_lag=hac_lag,
        long_run_variance=hac.long_run_variance,
        variance_inflation=checked_inflation,
        alpha=checked_alpha,
        target_power=checked_power,
        alternative=alternative,
        minimum_detectable_mean=float(mde),
        economic_mean_hurdle=checked_hurdle,
        required_evaluation_observations=required,
        adequately_powered=adequately_powered,
        status="TRAINING_PLAN_ONLY",
    )
