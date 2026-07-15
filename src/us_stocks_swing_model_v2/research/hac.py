"""Explicit-lag Newey-West/HAC inference for a sample mean."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import ResearchContractError, explicit_real, finite_float64


@dataclass(frozen=True)
class HACMeanResult:
    observations: int
    lag: int
    mean: float
    long_run_variance: float
    variance_of_mean: float
    standard_error: float
    status: str


def newey_west_mean(x: np.ndarray, *, lag: int) -> HACMeanResult:
    """Estimate mean SE using Bartlett weights and biased autocovariances.

    ``gamma_k = sum(u[t] * u[t-k]) / n`` and
    ``LRV = gamma_0 + 2*sum((1-k/(L+1))*gamma_k)``.
    No automatic bandwidth selection is permitted.
    """

    values = finite_float64(x, name="x", ndim=1)
    if isinstance(lag, bool) or not isinstance(lag, int):
        raise ResearchContractError("lag must be an explicit integer")
    n = len(values)
    if lag < 0 or lag >= n:
        raise ResearchContractError("lag must satisfy 0 <= lag < n")
    if n < max(3, lag + 2):
        raise ResearchContractError("too few observations for the declared lag")

    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            mean = float(np.mean(values, dtype=np.float64))
            centered = values - mean
            gamma0 = float(np.dot(centered, centered) / n)
            long_run_variance = gamma0
            for offset in range(1, lag + 1):
                covariance = float(np.dot(centered[offset:], centered[:-offset]) / n)
                weight = 1.0 - offset / (lag + 1.0)
                long_run_variance += 2.0 * weight * covariance
    except FloatingPointError as error:
        raise ResearchContractError("HAC arithmetic overflowed or became invalid") from error
    if not np.isfinite(mean) or not np.isfinite(gamma0) or not np.isfinite(long_run_variance):
        raise ResearchContractError("HAC arithmetic produced a non-finite value")

    tolerance = (
        np.finfo(np.float64).eps
        * max(1.0, abs(gamma0))
        * max(1, 2 * lag + 1)
        * 64.0
    )
    if long_run_variance < -tolerance:
        raise ResearchContractError("Newey-West long-run variance is materially negative")
    if abs(long_run_variance) <= tolerance:
        long_run_variance = 0.0
    variance_of_mean = long_run_variance / n
    standard_error = float(np.sqrt(variance_of_mean))
    status = "OK" if standard_error > 0.0 else "DEGENERATE"
    return HACMeanResult(
        observations=n,
        lag=lag,
        mean=mean,
        long_run_variance=float(long_run_variance),
        variance_of_mean=float(variance_of_mean),
        standard_error=standard_error,
        status=status,
    )


def hac_t_statistic(x: np.ndarray, *, lag: int, null_mean: float = 0.0) -> float:
    checked_null = explicit_real(null_mean, name="null_mean")
    result = newey_west_mean(x, lag=lag)
    if result.status != "OK":
        raise ResearchContractError("a degenerate HAC estimate cannot produce a t statistic")
    return (result.mean - checked_null) / result.standard_error
