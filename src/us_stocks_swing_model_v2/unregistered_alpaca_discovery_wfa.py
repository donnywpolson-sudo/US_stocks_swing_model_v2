"""In-memory, caveated WFA mechanics for the unregistered Alpaca discovery lane."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from .research.builder import _fit_fold_local_ridge
from .research.contracts import ResearchContractError, finite_float64, require_unique_ascii_ids
from .research.splits import SessionWindow, TemporalSamples, nested_chronological_splits


FEATURE_COLUMNS = (
    "symbol",
    "decision_session",
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
    "status",
)
OUTCOME_COLUMNS = (
    "symbol",
    "decision_session",
    "entry_session",
    "exit_session",
    "entry_open",
    "exit_close",
    "proxy_return",
    "status",
    "target_semantics",
    "historical_proxy",
    "canonical_target_equivalent",
)


def iter_caveated_parquet_batches(path: str, *, columns: tuple[str, ...], batch_size: int = 65536):
    """Yield bounded batches; callers must separately authorize opening real releases."""
    if not columns or batch_size < 1 or batch_size > 65536:
        raise ResearchContractError("discovery parquet batch bounds differ")
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != tuple(columns):
        raise ResearchContractError("discovery parquet schema differs")
    yield from parquet.iter_batches(batch_size=batch_size, columns=list(columns))


def assess_streaming_join_layout(
    feature_paths: tuple[Path, ...], *, outcome_path: Path
) -> dict[str, object]:
    """Inspect only parquet metadata before an execution authorization.

    The accepted feature release is year partitioned, whereas the accepted
    outcome release is one globally symbol-sorted file.  They cannot be joined
    in one lockstep pass without either retaining a large side table or first
    creating a separately authorized, caveated derived input.  This helper
    makes that limit explicit before any real rows are opened.
    """

    if not feature_paths:
        raise ResearchContractError("discovery feature paths are absent")
    normalized = tuple(Path(path) for path in feature_paths)
    if tuple(sorted(normalized)) != normalized or len(set(normalized)) != len(normalized):
        raise ResearchContractError("discovery feature paths differ")
    feature_rows = 0
    for path in normalized:
        parquet = pq.ParquetFile(path)
        if tuple(parquet.schema_arrow.names) != FEATURE_COLUMNS:
            raise ResearchContractError("discovery feature schema differs")
        feature_rows += parquet.metadata.num_rows
    outcome = pq.ParquetFile(outcome_path)
    if tuple(outcome.schema_arrow.names) != OUTCOME_COLUMNS:
        raise ResearchContractError("discovery outcome schema differs")
    if outcome.metadata.num_rows < 1 or feature_rows < 1:
        raise ResearchContractError("discovery input rows are absent")
    return {
        "mode": "UNREGISTERED_DISCOVERY_STREAMING_JOIN_LAYOUT_ASSESSMENT",
        "feature_files": len(normalized),
        "feature_rows": feature_rows,
        "outcome_rows": outcome.metadata.num_rows,
        "direct_single_pass_join": False,
        "required_next_input": "separately_authorized_caveated_joined_trial_input",
        "rows_opened": 0,
        "writes": 0,
    }


@dataclass(frozen=True)
class UnregisteredDiscoveryDataset:
    sample_ids: tuple[str, ...]
    features: np.ndarray
    proxy_returns: np.ndarray
    temporal_samples: TemporalSamples

    def validate(self) -> int:
        ids = require_unique_ascii_ids(self.sample_ids, name="discovery_sample_ids")
        features = finite_float64(self.features, name="discovery_features", ndim=2)
        returns = finite_float64(self.proxy_returns, name="discovery_proxy_returns", ndim=1)
        if features.shape != (len(ids), 3) or returns.shape != (len(ids),):
            raise ResearchContractError("discovery dataset shapes differ")
        if self.temporal_samples.validate() != len(ids):
            raise ResearchContractError("discovery temporal census differs")
        return len(ids)


def _schedule(session_count: int) -> tuple[tuple[SessionWindow, ...], tuple[tuple[SessionWindow, ...], ...]]:
    if session_count < 2016:
        raise ResearchContractError("discovery WFA requires at least 2016 sessions")
    outer = tuple(SessionWindow(1008 + 126 * fold, 1134 + 126 * fold) for fold in range(8))
    if outer[-1].stop > session_count:
        raise ResearchContractError("discovery WFA lacks eight complete outer blocks")
    inner = tuple(
        tuple(SessionWindow(window.start - 5 - 504 + 126 * part, window.start - 5 - 378 + 126 * part) for part in range(4))
        for window in outer
    )
    return outer, inner


def execute_unregistered_discovery_wfa(dataset: UnregisteredDiscoveryDataset, *, session_count: int) -> dict[str, object]:
    """Return fixed-ridge chronological metrics; never writes, registers, or claims alpha."""

    dataset.validate()
    outer, inner = _schedule(session_count)
    folds = nested_chronological_splits(dataset.temporal_samples, outer, inner, session_embargo=5, minimum_fit_samples=1, minimum_audit_samples=1)
    results: list[dict[str, object]] = []
    for number, fold in enumerate(folds):
        fitted = _fit_fold_local_ridge(dataset.features[fold.fit_indices], dataset.proxy_returns[fold.fit_indices], alpha=1.0, uncertainty_floor=1e-12)
        means = dataset.features[fold.audit_indices] @ fitted.coefficients + fitted.bias
        if not np.all(np.isfinite(means)):
            raise ResearchContractError("discovery WFA prediction is non-finite")
        losses = []
        for actual, mean in zip(dataset.proxy_returns[fold.audit_indices], means, strict=True):
            upper = 0.5 * (1.0 + math.erf((0.005 - float(mean)) / fitted.uncertainty / math.sqrt(2.0)))
            lower = 0.5 * (1.0 + math.erf((-0.005 - float(mean)) / fitted.uncertainty / math.sqrt(2.0)))
            probability = 1.0 - upper if actual > 0.005 else lower if actual < -0.005 else upper - lower
            losses.append(-math.log(max(probability, 1e-15)))
        results.append({"outer_fold": number, "fit_samples": len(fold.fit_indices), "audit_samples": len(fold.audit_indices), "multiclass_log_loss": float(np.mean(losses))})
    return {"mode": "UNREGISTERED_HISTORICAL_DISCOVERY_WFA_IN_MEMORY", "folds": results, "historical_proxy": True, "trusted_result_claim": False, "alpha_claim": False, "candidate_sealing": False, "writes": 0}
