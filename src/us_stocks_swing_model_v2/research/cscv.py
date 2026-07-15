"""Exhaustive combinatorially symmetric cross-validation/PBO mechanics."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb

import numpy as np

from .contracts import (
    ResearchContractError,
    explicit_int,
    explicit_real,
    finite_float64,
    require_unique_ascii_ids,
)


@dataclass(frozen=True)
class CSCVResult:
    strategy_ids: tuple[str, ...]
    blocks: int
    combinations: int
    selected_strategy_indices: np.ndarray
    oos_rank_logits: np.ndarray
    pbo_strict: float
    pbo_conservative: float
    metric: str
    status: str


def _score(values: np.ndarray, *, metric: str) -> np.ndarray:
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            means = np.mean(values, axis=0, dtype=np.float64)
    except FloatingPointError as error:
        raise ResearchContractError("CSCV score arithmetic overflowed") from error
    if bool(np.any(~np.isfinite(means))):
        raise ResearchContractError("CSCV mean score is non-finite")
    if metric == "mean":
        return means
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            deviations = np.std(values, axis=0, ddof=1)
    except FloatingPointError as error:
        raise ResearchContractError("CSCV Sharpe arithmetic overflowed") from error
    if bool(np.any(~np.isfinite(deviations))) or bool(np.any(deviations <= 0.0)):
        raise ResearchContractError("a CSCV Sharpe slice has degenerate variance")
    return means / deviations


def exhaustive_cscv_pbo(
    strategy_returns: np.ndarray,
    *,
    strategy_ids: tuple[str, ...],
    blocks: int,
    metric: str = "mean",
    maximum_combinations: int = 200_000,
    tie_tolerance: float = 0.0,
) -> CSCVResult:
    """Evaluate every symmetric half-block split; never Monte Carlo subsample."""

    values = finite_float64(strategy_returns, name="strategy_returns", ndim=2)
    ids = require_unique_ascii_ids(strategy_ids, name="strategy_ids")
    if values.shape[1] != len(ids) or len(ids) < 2:
        raise ResearchContractError("CSCV needs at least two aligned strategies")
    if isinstance(blocks, bool) or not isinstance(blocks, int):
        raise ResearchContractError("blocks must be an integer")
    if blocks < 4 or blocks % 2 != 0:
        raise ResearchContractError("blocks must be even and at least four")
    if values.shape[0] % blocks != 0:
        raise ResearchContractError("observations must divide exactly into equal blocks")
    if metric not in {"mean", "sharpe"}:
        raise ResearchContractError("metric must be 'mean' or 'sharpe'")
    checked_tolerance = explicit_real(tie_tolerance, name="tie_tolerance")
    if checked_tolerance < 0.0:
        raise ResearchContractError("tie_tolerance must be finite and non-negative")
    checked_maximum = explicit_int(
        maximum_combinations, name="maximum_combinations"
    )
    total_combinations = comb(blocks, blocks // 2)
    if checked_maximum < 1 or total_combinations > checked_maximum:
        raise ResearchContractError("exhaustive CSCV exceeds the declared combination cap")

    block_indices = np.arange(values.shape[0], dtype=np.int64).reshape(blocks, -1)
    all_blocks = frozenset(range(blocks))
    winners: list[int] = []
    logits: list[float] = []
    for in_sample_tuple in combinations(range(blocks), blocks // 2):
        in_sample_blocks = frozenset(in_sample_tuple)
        out_sample_blocks = sorted(all_blocks.difference(in_sample_blocks))
        in_rows = block_indices[list(in_sample_tuple), :].reshape(-1)
        out_rows = block_indices[out_sample_blocks, :].reshape(-1)
        in_scores = _score(values[in_rows, :], metric=metric)
        best = float(np.max(in_scores))
        tied_winners = np.flatnonzero(np.abs(in_scores - best) <= checked_tolerance)
        if len(tied_winners) != 1:
            raise ResearchContractError("CSCV in-sample winner is tied or ambiguous")
        winner = int(tied_winners[0])
        out_scores = _score(values[out_rows, :], metric=metric)
        selected_score = out_scores[winner]
        selected_ties = np.flatnonzero(
            np.abs(out_scores - selected_score) <= checked_tolerance
        )
        if len(selected_ties) != 1:
            raise ResearchContractError("CSCV selected OOS score has an ambiguous rank")
        rank_worst_to_best = 1 + int(np.count_nonzero(out_scores < selected_score))
        relative_rank = rank_worst_to_best / (len(ids) + 1.0)
        logit = float(np.log(relative_rank / (1.0 - relative_rank)))
        winners.append(winner)
        logits.append(logit)

    winner_array = np.asarray(winners, dtype=np.int64)
    logit_array = np.asarray(logits, dtype=np.float64)
    return CSCVResult(
        strategy_ids=ids,
        blocks=blocks,
        combinations=total_combinations,
        selected_strategy_indices=winner_array,
        oos_rank_logits=logit_array,
        pbo_strict=float(np.mean(logit_array < 0.0)),
        pbo_conservative=float(np.mean(logit_array <= 0.0)),
        metric=metric,
        status="MECHANICS_ONLY",
    )
