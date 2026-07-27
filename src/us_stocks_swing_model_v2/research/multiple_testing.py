"""Romano-Wolf max-T stepdown mechanics with dependence-preserving bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .bootstrap import stationary_bootstrap_index_rows
from .contracts import (
    ResearchContractError,
    finite_float64,
    explicit_int,
    require_unique_ascii_ids,
)
from .hac import hac_t_statistic


@dataclass(frozen=True)
class RomanoWolfResult:
    hypothesis_ids: tuple[str, ...]
    tail: str
    observed_statistics: np.ndarray
    stepdown_order: np.ndarray
    stage_p_values: np.ndarray
    adjusted_p_values: np.ndarray
    resamples: int
    null_centered: bool


def _romano_wolf_stepdown_from_null_statistics(
    observed_statistics: np.ndarray,
    bootstrap_statistics: np.ndarray,
    *,
    hypothesis_ids: tuple[str, ...],
    tail: str,
    null_centered: bool,
    minimum_resamples: int = 999,
    maximum_bootstrap_stat_bytes: int = 256 * 1024 * 1024,
) -> RomanoWolfResult:
    """Compute max-T stepdown only from an explicitly centered null bootstrap."""

    if type(null_centered) is not bool or not null_centered:
        raise ResearchContractError(
            "Romano-Wolf bootstrap statistics must be explicitly null-centered"
        )
    observed = finite_float64(
        observed_statistics, name="observed_statistics", ndim=1
    )
    boot = finite_float64(
        bootstrap_statistics,
        name="bootstrap_statistics",
        ndim=2,
    )
    ids = require_unique_ascii_ids(hypothesis_ids, name="hypothesis_ids")
    if len(observed) != len(ids) or boot.shape[1] != len(ids):
        raise ResearchContractError("statistics and hypothesis_ids must align")
    if tail not in {"greater", "two-sided"}:
        raise ResearchContractError("tail must be 'greater' or 'two-sided'")
    if isinstance(minimum_resamples, bool) or not isinstance(minimum_resamples, int):
        raise ResearchContractError("minimum_resamples must be an integer")
    if minimum_resamples < 1 or boot.shape[0] < minimum_resamples:
        raise ResearchContractError("too few bootstrap resamples")
    direct_cap = explicit_int(
        maximum_bootstrap_stat_bytes, name="maximum_bootstrap_stat_bytes"
    )
    if direct_cap < 1 or boot.nbytes > direct_cap:
        raise ResearchContractError("bootstrap statistic matrix exceeds memory cap")

    observed_extreme = observed if tail == "greater" else np.abs(observed)
    boot_extreme = boot if tail == "greater" else np.abs(boot)
    # Primary key is descending extremeness; hypothesis ID breaks exact ties.
    ids_array = np.asarray(ids, dtype="U")
    order = np.lexsort((ids_array, -observed_extreme)).astype(np.int64)
    stages = np.empty(len(ids), dtype=np.float64)
    adjusted_ordered = np.empty(len(ids), dtype=np.float64)
    running_max = 0.0
    denominator = boot.shape[0] + 1.0
    for stage in range(len(ids)):
        remaining = order[stage:]
        bootstrap_max = np.max(boot_extreme[:, remaining], axis=1)
        exceedances = int(
            np.count_nonzero(bootstrap_max >= observed_extreme[order[stage]])
        )
        stage_p = (1.0 + exceedances) / denominator
        stages[stage] = stage_p
        running_max = max(running_max, stage_p)
        adjusted_ordered[stage] = min(1.0, running_max)

    adjusted = np.empty(len(ids), dtype=np.float64)
    adjusted[order] = adjusted_ordered
    return RomanoWolfResult(
        hypothesis_ids=ids,
        tail=tail,
        observed_statistics=observed.copy(),
        stepdown_order=order,
        stage_p_values=stages,
        adjusted_p_values=adjusted,
        resamples=boot.shape[0],
        null_centered=True,
    )


def romano_wolf_from_differentials(
    differentials: np.ndarray,
    *,
    hypothesis_ids: tuple[str, ...],
    hac_lag: int,
    mean_block_length: float,
    n_resamples: int,
    seed: int,
    tail: str = "greater",
    minimum_resamples: int = 999,
    maximum_bootstrap_stat_bytes: int = 256 * 1024 * 1024,
) -> RomanoWolfResult:
    """Studentize aligned differentials and bootstrap their centered null.

    One stationary-bootstrap index row is shared across every hypothesis.  Each
    bootstrap column is centered before resampling; no raw performance level is
    accepted as a null distribution.
    """

    values = finite_float64(differentials, name="differentials", ndim=2)
    ids = require_unique_ascii_ids(hypothesis_ids, name="hypothesis_ids")
    if values.shape[1] != len(ids):
        raise ResearchContractError("differentials and hypothesis_ids must align")
    resamples = explicit_int(n_resamples, name="n_resamples")
    minimum = explicit_int(minimum_resamples, name="minimum_resamples")
    cap = explicit_int(
        maximum_bootstrap_stat_bytes, name="maximum_bootstrap_stat_bytes"
    )
    if resamples < minimum or minimum < 1:
        raise ResearchContractError("too few bootstrap resamples")
    if cap < 1 or resamples > cap // np.dtype(np.float64).itemsize // len(ids):
        raise ResearchContractError("bootstrap statistic matrix exceeds memory cap")
    observed = np.asarray(
        [hac_t_statistic(values[:, column], lag=hac_lag) for column in range(len(ids))],
        dtype=np.float64,
    )
    centered = values - np.mean(values, axis=0, dtype=np.float64)
    index_rows = stationary_bootstrap_index_rows(
        n_observations=values.shape[0],
        n_resamples=resamples,
        mean_block_length=mean_block_length,
        seed=seed,
    )
    bootstrap_statistics = np.empty((resamples, len(ids)), dtype=np.float64)
    for resample, indices in enumerate(index_rows):
        # Keep only the shared B-by-T index matrix resident; materializing a
        # B-by-T-by-K cube is avoidable and can be prohibitive.
        resampled = centered[indices, :]
        for column in range(len(ids)):
            bootstrap_statistics[resample, column] = hac_t_statistic(
                resampled[:, column], lag=hac_lag
            )
    return _romano_wolf_stepdown_from_null_statistics(
        observed,
        bootstrap_statistics,
        hypothesis_ids=ids,
        tail=tail,
        null_centered=True,
        minimum_resamples=minimum_resamples,
        maximum_bootstrap_stat_bytes=maximum_bootstrap_stat_bytes,
    )
