"""Deterministic negative-control transformations and fail-closed results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from .contracts import ResearchContractError, finite_float64, require_unique_ascii_ids


class NegativeControlState(str, Enum):
    CLEAR = "CLEAR"
    LEAKAGE_SUSPECTED = "LEAKAGE_SUSPECTED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class NegativeControlOutcome:
    control_id: str
    complete: bool
    candidate_gate_passed: bool


@dataclass(frozen=True)
class NegativeControlResult:
    state: NegativeControlState
    control_ids: tuple[str, ...]
    suspicious_controls: tuple[str, ...]
    incomplete_controls: tuple[str, ...]


def circular_block_derangement_indices(
    *,
    n_observations: int,
    block_size: int,
    seed: int,
) -> np.ndarray:
    """Rotate whole blocks by a non-zero PCG64-selected offset.

    The transform preserves within-block order and applies one shared mapping to
    every symbol/sleeve.  It is a negative-control transform, not a bootstrap.
    """

    for name, value in {
        "n_observations": n_observations,
        "block_size": block_size,
        "seed": seed,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ResearchContractError(f"{name} must be an integer")
    if n_observations < 2 or block_size < 1 or n_observations % block_size != 0:
        raise ResearchContractError("block_size must divide at least two observations")
    blocks = n_observations // block_size
    if blocks < 2:
        raise ResearchContractError("negative control needs at least two blocks")
    if not (0 <= seed < 2**64):
        raise ResearchContractError("seed must fit uint64")
    generator = np.random.Generator(np.random.PCG64(seed))
    offset = int(generator.integers(1, blocks))
    block_matrix = np.arange(n_observations, dtype=np.int64).reshape(blocks, block_size)
    return np.roll(block_matrix, shift=offset, axis=0).reshape(-1)


def apply_negative_control_indices(
    values: np.ndarray,
    indices: np.ndarray,
) -> np.ndarray:
    matrix = finite_float64(values, name="values")
    if matrix.ndim not in {1, 2}:
        raise ResearchContractError("negative-control values must be 1D or 2D")
    if not isinstance(indices, np.ndarray) or indices.dtype != np.dtype(np.int64):
        raise ResearchContractError("indices must be an int64 array")
    if indices.ndim != 1 or len(indices) != len(matrix):
        raise ResearchContractError("indices must map every observation exactly once")
    if set(indices.tolist()) != set(range(len(matrix))):
        raise ResearchContractError("indices must be a complete permutation")
    return matrix[indices, ...]


def synthetic_noise_control(
    *,
    shape: tuple[int, ...],
    seed: int,
) -> np.ndarray:
    if not shape or any(isinstance(size, bool) or not isinstance(size, int) or size < 1 for size in shape):
        raise ResearchContractError("shape must contain positive integers")
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= seed < 2**64):
        raise ResearchContractError("seed must fit uint64")
    generator = np.random.Generator(np.random.PCG64(seed))
    return generator.standard_normal(shape, dtype=np.float64)


def evaluate_negative_controls(
    outcomes: tuple[NegativeControlOutcome, ...],
) -> NegativeControlResult:
    if not outcomes:
        raise ResearchContractError("at least one negative control is required")
    ids = require_unique_ascii_ids(
        (outcome.control_id for outcome in outcomes), name="control_ids"
    )
    if any(type(outcome.complete) is not bool for outcome in outcomes):
        raise ResearchContractError("control completeness must be boolean")
    if any(type(outcome.candidate_gate_passed) is not bool for outcome in outcomes):
        raise ResearchContractError("control gate result must be boolean")
    incomplete = tuple(
        outcome.control_id for outcome in outcomes if not outcome.complete
    )
    suspicious = tuple(
        outcome.control_id
        for outcome in outcomes
        if outcome.complete and outcome.candidate_gate_passed
    )
    if incomplete:
        state = NegativeControlState.INVALID
    elif suspicious:
        state = NegativeControlState.LEAKAGE_SUSPECTED
    else:
        state = NegativeControlState.CLEAR
    return NegativeControlResult(
        state=state,
        control_ids=ids,
        suspicious_controls=suspicious,
        incomplete_controls=incomplete,
    )
