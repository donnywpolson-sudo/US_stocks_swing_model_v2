"""Probabilistic/deflated Sharpe mechanics for a complete trial census."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
from scipy.special import ndtr, ndtri

from .contracts import (
    ResearchArrayBinding,
    ResearchContractError,
    finite_float64,
)


_EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True)
class DeflatedSharpeResult:
    observations: int
    raw_trial_count: int
    selected_sharpe_per_period: float
    selected_trial_index: int
    selection_rule: str
    trial_sharpe_mean: float
    trial_sharpe_std: float
    expected_maximum_sharpe: float
    skewness: float
    kurtosis_non_excess: float
    test_statistic: float
    probability: float
    status: str


def deflated_sharpe_ratio(
    returns: np.ndarray,
    trial_sharpes: np.ndarray,
    *,
    raw_trial_count: int,
    selected_trial_index: int,
    sample_ids: Iterable[str],
    binding: ResearchArrayBinding,
    selection_rule: str = "MAX_SHARPE",
) -> DeflatedSharpeResult:
    """Compute DSR using per-period Sharpe and the raw registered trial count.

    The Sharpe values must not be annualized.  Kurtosis is non-excess.  The
    complete raw trial census is used as a conservative upper bound on the
    number of independent trials; no data-derived effective-count reduction is
    performed here. Both the selected returns and every trial Sharpe must be
    authenticated by one complete ``ResearchArrayBinding``.
    """

    values = finite_float64(returns, name="returns", ndim=1).copy()
    census = finite_float64(
        trial_sharpes,
        name="trial_sharpes",
        ndim=1,
    ).copy()
    if type(binding) is not ResearchArrayBinding:
        raise ResearchContractError(
            "DSR requires an exact ResearchArrayBinding"
        )
    materialized_sample_ids = tuple(sample_ids)
    if len(materialized_sample_ids) != len(values):
        raise ResearchContractError(
            "DSR sample IDs must exactly cover selected returns"
        )
    binding.validate_inputs(
        sample_ids=materialized_sample_ids,
        arrays={
            "selected_returns": values,
            "trial_sharpes": census,
        },
    )
    if isinstance(raw_trial_count, bool) or not isinstance(raw_trial_count, int):
        raise ResearchContractError("raw_trial_count must be an integer")
    if raw_trial_count != len(census) or raw_trial_count < 2:
        raise ResearchContractError("raw_trial_count must equal a census of at least two")
    if (
        isinstance(selected_trial_index, (bool, np.bool_))
        or not isinstance(selected_trial_index, (int, np.integer))
    ):
        raise ResearchContractError("selected_trial_index must be an explicit integer")
    selected_index = int(selected_trial_index)
    if not (0 <= selected_index < raw_trial_count):
        raise ResearchContractError("selected_trial_index is out of bounds")
    if selection_rule != "MAX_SHARPE":
        raise ResearchContractError("only the prebound MAX_SHARPE selection rule is supported")
    if len(values) < 3:
        raise ResearchContractError("DSR needs at least three returns")

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            standard_deviation = float(np.std(values, ddof=1))
            selected_mean = float(np.mean(values, dtype=np.float64))
            centered = values - selected_mean
            second_moment = float(np.mean(centered**2, dtype=np.float64))
            third_moment = float(np.mean(centered**3, dtype=np.float64))
            fourth_moment = float(np.mean(centered**4, dtype=np.float64))
    except FloatingPointError as error:
        raise ResearchContractError("DSR return moments overflowed or became invalid") from error
    if not np.isfinite(standard_deviation) or standard_deviation <= 0.0:
        raise ResearchContractError("selected returns have degenerate variance")
    if not all(
        np.isfinite(value)
        for value in (selected_mean, second_moment, third_moment, fourth_moment)
    ):
        raise ResearchContractError("DSR return moments are non-finite")
    selected_sharpe = float(selected_mean / standard_deviation)
    if second_moment <= 0.0:
        raise ResearchContractError("selected returns have degenerate moments")
    skewness = float(third_moment / second_moment ** 1.5)
    kurtosis = float(fourth_moment / second_moment**2)

    tolerance = 1e-12
    if not np.isclose(
        census[selected_index],
        selected_sharpe,
        rtol=tolerance,
        atol=tolerance,
    ):
        raise ResearchContractError("selected returns do not match the bound trial census row")
    maximum = float(np.max(census))
    winners = np.flatnonzero(
        np.isclose(census, maximum, rtol=tolerance, atol=tolerance)
    )
    if len(winners) != 1 or int(winners[0]) != selected_index:
        raise ResearchContractError("selected trial is not the unique deterministic census winner")

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            trial_mean = float(np.mean(census, dtype=np.float64))
            trial_std = float(np.std(census, ddof=1))
    except FloatingPointError as error:
        raise ResearchContractError("trial Sharpe variance overflowed") from error
    if not np.isfinite(trial_std) or trial_std <= 0.0:
        raise ResearchContractError("trial Sharpe census has degenerate variance")
    n_trials = float(raw_trial_count)
    extreme_quantile = (
        (1.0 - _EULER_MASCHERONI) * ndtri(1.0 - 1.0 / n_trials)
        + _EULER_MASCHERONI
        * ndtri(1.0 - 1.0 / (n_trials * np.e))
    )
    if not np.isfinite(trial_mean):
        raise ResearchContractError("trial Sharpe mean is non-finite")
    expected_maximum = trial_mean + trial_std * float(extreme_quantile)
    if not np.isfinite(expected_maximum):
        raise ResearchContractError("expected maximum Sharpe is non-finite")
    denominator_squared = (
        1.0
        - skewness * selected_sharpe
        + ((kurtosis - 1.0) / 4.0) * selected_sharpe**2
    )
    if not np.isfinite(denominator_squared) or denominator_squared <= 0.0:
        raise ResearchContractError("DSR moment correction is non-positive")
    statistic = (
        (selected_sharpe - expected_maximum)
        * np.sqrt(len(values) - 1.0)
        / np.sqrt(denominator_squared)
    )
    probability = float(ndtr(statistic))
    if not np.isfinite(probability):
        raise ResearchContractError("DSR probability is non-finite")
    return DeflatedSharpeResult(
        observations=len(values),
        raw_trial_count=raw_trial_count,
        selected_sharpe_per_period=selected_sharpe,
        selected_trial_index=selected_index,
        selection_rule=selection_rule,
        trial_sharpe_mean=trial_mean,
        trial_sharpe_std=trial_std,
        expected_maximum_sharpe=expected_maximum,
        skewness=skewness,
        kurtosis_non_excess=kurtosis,
        test_statistic=float(statistic),
        probability=probability,
        status="MECHANICS_ONLY",
    )
