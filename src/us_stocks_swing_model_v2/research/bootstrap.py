"""Deterministic stationary-bootstrap index generation."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from .contracts import ResearchContractError, explicit_int, finite_float64


def _validate_generation_parameters(
    *,
    n_observations: int,
    n_resamples: int,
    mean_block_length: float,
    seed: int,
) -> tuple[int, int, float, int]:
    checked_n = explicit_int(n_observations, name="n_observations")
    checked_resamples = explicit_int(n_resamples, name="n_resamples")
    checked_seed = explicit_int(seed, name="seed")
    if checked_n < 2:
        raise ResearchContractError("stationary bootstrap needs at least two observations")
    if checked_resamples < 1:
        raise ResearchContractError("n_resamples must be positive")
    if not (0 <= checked_seed < 2**64):
        raise ResearchContractError("seed must be a uint64-compatible integer")
    if isinstance(mean_block_length, (bool, np.bool_)) or not isinstance(
        mean_block_length, (int, float, np.integer, np.floating)
    ):
        raise ResearchContractError("mean_block_length must be numeric")
    checked_block = float(mean_block_length)
    if not np.isfinite(checked_block) or not (1.0 <= checked_block <= checked_n):
        raise ResearchContractError(
            "mean_block_length must be finite and between one and n_observations"
        )
    return checked_n, checked_resamples, checked_block, checked_seed


def stationary_bootstrap_index_kernel(
    *,
    n_observations: int,
    mean_block_length: float,
    restart_uniforms: np.ndarray,
    start_uniforms: np.ndarray,
) -> np.ndarray:
    """Map caller-supplied uniforms to Politis-Romano bootstrap indices.

    Supplying uniforms makes the small oracle independent of RNG details.
    Index row ``b`` must be applied to every strategy/sleeve column so that
    contemporaneous dependence is retained.
    """

    if isinstance(n_observations, bool) or not isinstance(n_observations, int):
        raise ResearchContractError("n_observations must be an integer")
    if n_observations < 2:
        raise ResearchContractError("stationary bootstrap needs at least two observations")
    if isinstance(mean_block_length, (bool, np.bool_)) or not isinstance(
        mean_block_length, (int, float, np.integer, np.floating)
    ):
        raise ResearchContractError("mean_block_length must be numeric")
    if not np.isfinite(mean_block_length):
        raise ResearchContractError("mean_block_length must be finite")
    if not (1.0 <= mean_block_length <= n_observations):
        raise ResearchContractError(
            "mean_block_length must be between one and n_observations"
        )
    restart = finite_float64(restart_uniforms, name="restart_uniforms", ndim=2)
    starts = finite_float64(start_uniforms, name="start_uniforms", ndim=2)
    if restart.shape != starts.shape or restart.shape[1] != n_observations:
        raise ResearchContractError("uniform arrays must share shape (B, n_observations)")
    if bool(np.any(restart < 0.0)) or bool(np.any(restart >= 1.0)):
        raise ResearchContractError("restart_uniforms must lie in [0,1)")
    if bool(np.any(starts < 0.0)) or bool(np.any(starts >= 1.0)):
        raise ResearchContractError("start_uniforms must lie in [0,1)")

    probability = 1.0 / float(mean_block_length)
    indices = np.empty(restart.shape, dtype=np.int64)
    indices[:, 0] = np.floor(starts[:, 0] * n_observations).astype(np.int64)
    for column in range(1, n_observations):
        new_start = np.floor(starts[:, column] * n_observations).astype(np.int64)
        continuation = (indices[:, column - 1] + 1) % n_observations
        indices[:, column] = np.where(
            restart[:, column] < probability,
            new_start,
            continuation,
        )
    return indices


def stationary_bootstrap_indices(
    *,
    n_observations: int,
    n_resamples: int,
    mean_block_length: float,
    seed: int,
    maximum_materialized_bytes: int = 256 * 1024 * 1024,
) -> np.ndarray:
    """Materialize shared indices only below an explicit memory ceiling."""

    n, resamples, block, checked_seed = _validate_generation_parameters(
        n_observations=n_observations,
        n_resamples=n_resamples,
        mean_block_length=mean_block_length,
        seed=seed,
    )
    cap = explicit_int(maximum_materialized_bytes, name="maximum_materialized_bytes")
    if cap < 1 or resamples > cap // np.dtype(np.int64).itemsize // n:
        raise ResearchContractError("materialized bootstrap index matrix exceeds memory cap")
    result = np.empty((resamples, n), dtype=np.int64)
    for row_number, row in enumerate(
        stationary_bootstrap_index_rows(
            n_observations=n,
            n_resamples=resamples,
            mean_block_length=block,
            seed=checked_seed,
        )
    ):
        result[row_number, :] = row
    return result


def stationary_bootstrap_index_rows(
    *,
    n_observations: int,
    n_resamples: int,
    mean_block_length: float,
    seed: int,
) -> Iterator[np.ndarray]:
    """Yield O(T)-memory index rows in a fixed PCG64 per-row draw order."""

    n, resamples, block, checked_seed = _validate_generation_parameters(
        n_observations=n_observations,
        n_resamples=n_resamples,
        mean_block_length=mean_block_length,
        seed=seed,
    )
    generator = np.random.Generator(np.random.PCG64(checked_seed))
    for _ in range(resamples):
        restart = generator.random((1, n), dtype=np.float64)
        starts = generator.random((1, n), dtype=np.float64)
        yield stationary_bootstrap_index_kernel(
            n_observations=n,
            mean_block_length=block,
            restart_uniforms=restart,
            start_uniforms=starts,
        )[0]


def apply_shared_indices(values: np.ndarray, indices: np.ndarray) -> np.ndarray:
    """Apply the same time indices to all columns of a float64 matrix."""

    matrix = finite_float64(values, name="values", ndim=2)
    if not isinstance(indices, np.ndarray) or indices.dtype != np.dtype(np.int64):
        raise ResearchContractError("indices must be an int64 numpy array")
    if indices.ndim != 2 or indices.shape[1] != matrix.shape[0]:
        raise ResearchContractError("indices must have shape (B, values.shape[0])")
    if indices.size == 0 or bool(np.any(indices < 0)) or bool(np.any(indices >= len(matrix))):
        raise ResearchContractError("indices contain an invalid observation")
    return matrix[indices, :]
