"""In-memory, caveated WFA mechanics for the unregistered Alpaca discovery lane."""

from __future__ import annotations

import math
import hashlib
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .research.builder import _fit_fold_local_ridge
from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .releases import verify_accepted_release
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
JOINED_COLUMNS = (
    "symbol",
    "decision_session",
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
    "proxy_return",
)
_FEATURE_READY = "READY_CAUSAL_RAW_PRICE_FEATURES"
_OUTCOME_READY = "READY_UNTRUSTED_RAW_PRICE_PROXY"
_FEATURE_SCHEMA = pa.schema(
    [("symbol", pa.string()), ("decision_session", pa.date32())]
    + [(name, pa.float64()) for name in FEATURE_COLUMNS[2:5]]
    + [("status", pa.string())]
)
_OUTCOME_SCHEMA = pa.schema(
    [("symbol", pa.string()), ("decision_session", pa.date32()),
     ("entry_session", pa.date32()), ("exit_session", pa.date32()),
     ("entry_open", pa.float64()), ("exit_close", pa.float64()),
     ("proxy_return", pa.float64()), ("status", pa.string()),
     ("target_semantics", pa.string()), ("historical_proxy", pa.bool_()),
     ("canonical_target_equivalent", pa.bool_())]
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
        if parquet.schema_arrow != _FEATURE_SCHEMA:
            raise ResearchContractError("discovery feature schema differs")
        feature_rows += parquet.metadata.num_rows
    outcome = pq.ParquetFile(outcome_path)
    if outcome.schema_arrow != _OUTCOME_SCHEMA:
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


def build_caveated_joined_trial_input_plan(
    feature_paths: tuple[Path, ...],
    *,
    feature_release_id: str,
    outcome_path: Path,
    outcome_release_id: str,
    work_root: Path,
    bucket_count: int = 64,
    batch_size: int = 65536,
) -> dict[str, object]:
    """Freeze one no-write staging plan; payload integrity is rechecked at execution."""

    require_sha256(feature_release_id, "feature_release_id")
    require_sha256(outcome_release_id, "outcome_release_id")
    if not Path(work_root).is_absolute() or not Path(outcome_path).is_absolute() or any(
        not Path(path).is_absolute() for path in feature_paths
    ):
        raise ResearchContractError("discovery join plan paths must be absolute")
    layout = assess_streaming_join_layout(feature_paths, outcome_path=outcome_path)
    if bucket_count < 2 or bucket_count > 256 or bucket_count & (bucket_count - 1):
        raise ResearchContractError("discovery join bucket count differs")
    if batch_size < 1 or batch_size > 65536:
        raise ResearchContractError("discovery join batch bound differs")
    unsigned = {
        "schema_version": 1,
        "mode": "UNREGISTERED_HISTORICAL_DISCOVERY_CAVEATED_JOIN_BUILD_PLAN_ONLY",
        "feature_release_id": feature_release_id,
        "outcome_release_id": outcome_release_id,
        "feature_paths": [str(path) for path in feature_paths],
        "outcome_path": str(outcome_path),
        "work_root": str(Path(work_root)),
        "layout": layout,
        "limits": {
            "bucket_count": bucket_count,
            "source_batch_rows_at_most": batch_size,
            "source_rows_at_most": int(layout["feature_rows"]) + int(layout["outcome_rows"]),
            "joined_rows_at_most": min(int(layout["feature_rows"]), int(layout["outcome_rows"])),
            "network_requests": 0,
            "credentials_read": 0,
        },
        "output": {
            "stage_directory": "derived_from_join_build_plan_id",
            "accepted_release": False,
            "historical_proxy": True,
            "trusted_result_claim": False,
            "alpha_claim": False,
            "candidate_sealing": False,
        },
        "required_authority": {
            "real_row_access": True,
            "generated_evidence_write": True,
            "immutable_publication": False,
            "training_or_evaluation": False,
        },
        "stop_conditions": [
            "accepted input identity drift",
            "schema or duplicate-key failure",
            "row or batch bound violation",
            "partial or ambiguous staging output",
            "attempt to publish or run WFA",
        ],
    }
    return {**unsigned, "join_build_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _bucket(symbol: str, decision_session: object, bucket_count: int) -> int:
    if type(symbol) is not str or not symbol:
        raise ResearchContractError("discovery join symbol differs")
    encoded = f"{symbol}\x1f{decision_session}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big") % bucket_count


def _write_bucketed_batches(
    batches,
    *,
    schema_columns: tuple[str, ...],
    stage: Path,
    prefix: str,
    bucket_count: int,
) -> tuple[Path, ...]:
    """Write bounded, deterministic-key spools without retaining source rows."""

    writers: dict[int, pq.ParquetWriter] = {}
    schemas: dict[int, pa.Schema] = {}
    paths: dict[int, Path] = {}
    try:
        for batch in batches:
            if tuple(batch.schema.names) != schema_columns:
                raise ResearchContractError("discovery spool schema differs")
            grouped: dict[int, list[dict[str, object]]] = {}
            for row in batch.to_pylist():
                number = _bucket(row["symbol"], row["decision_session"], bucket_count)
                grouped.setdefault(number, []).append(row)
            for number, rows in grouped.items():
                path = stage / f"{prefix}-{number:03d}.parquet"
                if number not in writers:
                    schemas[number] = pa.Table.from_pylist(rows).schema
                    writers[number] = pq.ParquetWriter(path, schemas[number])
                    paths[number] = path
                writers[number].write_table(pa.Table.from_pylist(rows, schema=schemas[number]))
    finally:
        for writer in writers.values():
            writer.close()
    return tuple(paths[number] for number in sorted(paths))


def build_caveated_joined_trial_input(
    feature_paths: tuple[Path, ...],
    *,
    outcome_path: Path,
    stage_root: Path,
    bucket_count: int = 64,
    batch_size: int = 65536,
) -> dict[str, object]:
    """Build bounded caveated join shards into a caller-owned staging root.

    This is intentionally not an accepted-release publisher and never invokes
    the WFA.  A real invocation needs separate authority for source rows and
    generated staging files.  Synthetic tests use a temporary root.
    """

    if bucket_count < 2 or bucket_count > 256 or bucket_count & (bucket_count - 1):
        raise ResearchContractError("discovery join bucket count differs")
    if batch_size < 1 or batch_size > 65536:
        raise ResearchContractError("discovery join batch bound differs")
    layout = assess_streaming_join_layout(feature_paths, outcome_path=outcome_path)
    root = Path(stage_root)
    if root.exists():
        raise ResearchContractError("discovery join stage already exists")
    root.mkdir(parents=True)
    feature_spool = root / "feature_spool"
    outcome_spool = root / "outcome_spool"
    joined_root = root / "joined"
    feature_spool.mkdir()
    outcome_spool.mkdir()
    joined_root.mkdir()
    feature_batches = (
        batch
        for path in feature_paths
        for batch in iter_caveated_parquet_batches(
            str(path), columns=FEATURE_COLUMNS, batch_size=batch_size
        )
    )
    feature_paths_by_bucket = _write_bucketed_batches(
        feature_batches, schema_columns=FEATURE_COLUMNS, stage=feature_spool,
        prefix="features", bucket_count=bucket_count,
    )
    outcome_paths_by_bucket = _write_bucketed_batches(
        iter_caveated_parquet_batches(
            str(outcome_path), columns=OUTCOME_COLUMNS, batch_size=batch_size
        ),
        schema_columns=OUTCOME_COLUMNS, stage=outcome_spool, prefix="outcomes",
        bucket_count=bucket_count,
    )
    feature_lookup = {int(path.stem.rsplit("-", 1)[1]): path for path in feature_paths_by_bucket}
    outcome_lookup = {int(path.stem.rsplit("-", 1)[1]): path for path in outcome_paths_by_bucket}
    joined_rows = 0
    excluded_feature_rows = 0
    excluded_outcome_rows = 0
    joined_paths: list[str] = []
    for number in sorted(set(feature_lookup) | set(outcome_lookup)):
        feature_table = pq.read_table(feature_lookup[number]) if number in feature_lookup else pa.table({name: [] for name in FEATURE_COLUMNS})
        outcome_table = pq.read_table(outcome_lookup[number]) if number in outcome_lookup else pa.table({name: [] for name in OUTCOME_COLUMNS})
        feature_index: dict[tuple[str, object], dict[str, object]] = {}
        for row in feature_table.to_pylist():
            key = (row["symbol"], row["decision_session"])
            if key in feature_index:
                raise ResearchContractError("discovery feature join key is duplicated")
            if row["status"] == _FEATURE_READY:
                if any(row[name] is None or not math.isfinite(float(row[name])) for name in FEATURE_COLUMNS[2:5]):
                    raise ResearchContractError("ready discovery feature differs")
                feature_index[key] = row
            else:
                excluded_feature_rows += 1
        output: list[dict[str, object]] = []
        seen_outcomes: set[tuple[str, object]] = set()
        for row in outcome_table.to_pylist():
            key = (row["symbol"], row["decision_session"])
            if key in seen_outcomes:
                raise ResearchContractError("discovery outcome join key is duplicated")
            seen_outcomes.add(key)
            feature = feature_index.get(key)
            if row["status"] != _OUTCOME_READY or feature is None:
                excluded_outcome_rows += 1
                continue
            value = row["proxy_return"]
            if value is None or not math.isfinite(float(value)):
                raise ResearchContractError("ready discovery outcome differs")
            output.append({name: feature[name] for name in FEATURE_COLUMNS[:5]} | {"proxy_return": float(value)})
        if output:
            output.sort(key=lambda row: (row["decision_session"], row["symbol"]))
            path = joined_root / f"bucket={number:03d}.parquet"
            pq.write_table(pa.Table.from_pylist(output), path)
            joined_paths.append(path.relative_to(root).as_posix())
            joined_rows += len(output)
    if joined_rows < 1:
        raise ResearchContractError("discovery join has no ready rows")
    return {
        "mode": "UNREGISTERED_HISTORICAL_DISCOVERY_CAVEATED_JOIN_STAGE",
        "layout": layout,
        "bucket_count": bucket_count,
        "joined_rows": joined_rows,
        "excluded_feature_rows": excluded_feature_rows,
        "excluded_outcome_rows": excluded_outcome_rows,
        "joined_paths": tuple(joined_paths),
        "writes": len(joined_paths),
        "historical_proxy": True,
        "trusted_result_claim": False,
        "alpha_claim": False,
        "candidate_sealing": False,
    }


def execute_caveated_joined_trial_input(
    feature_release_directory: Path,
    *,
    outcome_release_directory: Path,
    accepted_root: Path,
    work_root: Path,
    approved_join_build_plan_id: str,
    bucket_count: int = 64,
    batch_size: int = 65536,
) -> dict[str, object]:
    """Perform one explicitly approved, non-accepted staging build.

    The caller is responsible for a bounded user authorization.  The function
    verifies each accepted input before opening parquet rows and leaves any
    failed or partial staging root intact for separately authorized recovery.
    """

    if os.environ.get("ALPACA_DISCOVERY_JOIN_BUILD_APPROVED") != "YES":
        raise ResearchContractError("discovery join publication confirmation is absent")
    feature_release = verify_accepted_release(
        Path(feature_release_directory), accepted_root=Path(accepted_root)
    )
    outcome_release = verify_accepted_release(
        Path(outcome_release_directory), accepted_root=Path(accepted_root)
    )
    if (
        feature_release.dataset != "alpaca_discovery_proxy_features"
        or outcome_release.dataset != "alpaca_discovery_proxy_outcomes"
        or feature_release.role != "legacy_discovery_only"
        or outcome_release.role != "legacy_discovery_only"
        or feature_release.quality_state != "LEGACY_CAVEATED"
        or outcome_release.quality_state != "LEGACY_CAVEATED"
    ):
        raise ResearchContractError("discovery join accepted inputs differ")
    feature_paths = tuple(sorted((Path(feature_release_directory) / "features").glob("year=*.parquet")))
    outcome_path = Path(outcome_release_directory) / "proxy_outcomes.parquet"
    plan = build_caveated_joined_trial_input_plan(
        feature_paths,
        feature_release_id=feature_release.release_id,
        outcome_path=outcome_path,
        outcome_release_id=outcome_release.release_id,
        work_root=Path(work_root),
        bucket_count=bucket_count,
        batch_size=batch_size,
    )
    if approved_join_build_plan_id != plan["join_build_plan_id"]:
        raise ResearchContractError("approved discovery join build plan ID differs")
    stage = Path(work_root) / str(plan["join_build_plan_id"])
    return {**build_caveated_joined_trial_input(
        feature_paths, outcome_path=outcome_path, stage_root=stage,
        bucket_count=bucket_count, batch_size=batch_size,
    ), "join_build_plan_id": plan["join_build_plan_id"]}


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
