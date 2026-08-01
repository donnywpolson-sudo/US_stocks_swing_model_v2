"""In-memory, caveated WFA mechanics for the unregistered Alpaca discovery lane."""

from __future__ import annotations

import json
import math
import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .research.builder import _fit_fold_local_ridge
from .common import canonical_json_bytes, reject_link, require_sha256, sha256_bytes, sha256_file
from .common import atomic_write
from .releases import AtomicReleasePublisher, ReleaseFile, ReleaseManifest, verify_accepted_release
from .alpaca_discovery_three_class_trial import load_three_class_trial_contract
from .exchange_calendar import load_xnys_calendar_release
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
_STAGING_OVERHEAD_MULTIPLIER = 4
JOINED_DATASET = "alpaca_discovery_joined_trial_inputs"
COMPARISON_CONTRACT_PATH = "config/alpaca_discovery_class_base_rate_comparison_contract.json"
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
    input_bytes = sum(Path(path).stat().st_size for path in feature_paths) + Path(outcome_path).stat().st_size
    unsigned = {
        "schema_version": 1,
        "mode": "UNREGISTERED_HISTORICAL_DISCOVERY_CAVEATED_JOIN_BUILD_PLAN_ONLY",
        "feature_release_id": feature_release_id,
        "outcome_release_id": outcome_release_id,
        "feature_paths": [str(path) for path in feature_paths],
        "outcome_path": str(outcome_path),
        "work_root": str(Path(work_root)),
        "implementation_sha256": sha256_file(Path(__file__)),
        "layout": layout,
        "limits": {
            "bucket_count": bucket_count,
            "source_batch_rows_at_most": batch_size,
            "source_rows_at_most": int(layout["feature_rows"]) + int(layout["outcome_rows"]),
            "joined_rows_at_most": min(int(layout["feature_rows"]), int(layout["outcome_rows"])),
            # Staging retains two reshaped input spools plus joined shards.
            # Four times the compressed input census leaves bounded room for
            # their different parquet encodings while still failing closed.
            "staging_bytes_at_most": input_bytes * _STAGING_OVERHEAD_MULTIPLIER,
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
    maximum_stage_bytes: int,
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
    if type(maximum_stage_bytes) is not int or maximum_stage_bytes < 1:
        raise ResearchContractError("discovery join stage byte bound differs")
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

    def require_stage_bound() -> None:
        if sum(path.stat().st_size for path in root.rglob("*") if path.is_file()) > maximum_stage_bytes:
            raise ResearchContractError("discovery join stage byte bound exceeded")
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
    require_stage_bound()
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
            require_stage_bound()
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
        maximum_stage_bytes=int(plan["limits"]["staging_bytes_at_most"]),
    ), "join_build_plan_id": plan["join_build_plan_id"]}


def build_caveated_joined_publication_plan(
    stage_root: Path,
    *,
    join_build_plan_id: str,
    feature_release_id: str,
    outcome_release_id: str,
    repo_root: Path,
    created_at: str,
) -> dict[str, object]:
    """Freeze an immutable-release plan without copying or publishing data."""

    require_sha256(join_build_plan_id, "join_build_plan_id")
    require_sha256(feature_release_id, "feature_release_id")
    require_sha256(outcome_release_id, "outcome_release_id")
    root = Path(repo_root).resolve(strict=True)
    stage = Path(stage_root)
    if not stage.is_absolute() or not stage.is_dir():
        raise ResearchContractError("discovery joined stage differs")
    joined = tuple(sorted((stage / "joined").glob("bucket=*.parquet")))
    if len(joined) != 64 or tuple(path.name for path in joined) != tuple(f"bucket={number:03d}.parquet" for number in range(64)):
        raise ResearchContractError("discovery joined shard census differs")
    row_count = 0
    files: list[ReleaseFile] = []
    for path in joined:
        parquet = pq.ParquetFile(path)
        if tuple(parquet.schema_arrow.names) != JOINED_COLUMNS:
            raise ResearchContractError("discovery joined shard schema differs")
        row_count += parquet.metadata.num_rows
        files.append(ReleaseFile(path=f"joined/{path.name}", size=path.stat().st_size, sha256=sha256_file(path)))
    if row_count < 1:
        raise ResearchContractError("discovery joined rows are absent")
    evidence = {
        "schema_version": 1,
        "join_build_plan_id": join_build_plan_id,
        "feature_release_id": feature_release_id,
        "outcome_release_id": outcome_release_id,
        "joined_rows": row_count,
        "historical_proxy": True,
        "canonical_target_equivalent": False,
        "survivorship_safe": False,
        "trusted_result_claim": False,
        "alpha_claim": False,
        "candidate_sealing": False,
        "training_or_evaluation": False,
    }
    evidence_bytes = canonical_json_bytes(evidence)
    files.append(ReleaseFile(path="source_evidence_manifest.json", size=len(evidence_bytes), sha256=sha256_bytes(evidence_bytes)))
    manifest = ReleaseManifest(
        schema_version=1, project="US_stocks_swing_model_v2", dataset=JOINED_DATASET,
        source_epoch="alpaca_raw_price_proxy_joined_trial_input_v1",
        role="legacy_discovery_only", quality_state="LEGACY_CAVEATED",
        created_at=created_at, row_count=row_count,
        event_start=None, event_end=None,
        upstream_release_ids=tuple(sorted((feature_release_id, outcome_release_id))),
        schema_fingerprint=sha256_bytes(canonical_json_bytes(list(JOINED_COLUMNS))),
        code_hash=sha256_file(Path(__file__)),
        config_hash=sha256_file(root / "config" / "alpaca_discovery_three_class_trial_contract.json"),
        environment_hash=sha256_file(root / "config" / "environment.lock.json"),
        files=tuple(sorted(files, key=lambda entry: entry.path)),
        release_id="0" * 64,
    )
    unsigned = manifest.unsigned_dict()
    prospective = ReleaseManifest(**{**manifest.__dict__, "release_id": sha256_bytes(canonical_json_bytes(unsigned))})
    prospective.validate()
    unsigned_plan = {
        "schema_version": 1,
        "mode": "UNREGISTERED_HISTORICAL_DISCOVERY_CAVEATED_JOIN_PUBLICATION_PLAN_ONLY",
        "stage_root": str(stage),
        "source_evidence": evidence,
        "prospective_release": prospective.as_dict(),
        "publication": {"accepted_root": "supplied_at_execution", "copy_only_joined_shards": True, "spool_files_included": False, "writes": 0},
        "required_authority": {"generated_evidence_write": True, "immutable_publication": True, "training_or_evaluation": False},
        "stop_conditions": ["stage identity drift", "joined shard census or schema drift", "publication collision", "partial publication", "attempt to run WFA"],
    }
    return {**unsigned_plan, "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned_plan))}


def build_unregistered_wfa_plan(
    joined_release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    repo_root: Path,
) -> dict[str, object]:
    """Bind a future no-write discovery WFA without opening joined rows."""

    joined = verify_accepted_release(Path(joined_release_directory), accepted_root=Path(accepted_root))
    calendar = verify_accepted_release(Path(calendar_release_directory), accepted_root=Path(accepted_root))
    if (joined.dataset != JOINED_DATASET or joined.role != "legacy_discovery_only" or joined.quality_state != "LEGACY_CAVEATED" or calendar.dataset != "xnys_sessions" or calendar.role != "derived_causal" or calendar.quality_state != "PASS" or calendar.row_count < 2016):
        raise ResearchContractError("discovery WFA inputs differ")
    contract = load_three_class_trial_contract(Path(repo_root))
    unsigned = {
        "schema_version": 1,
        "mode": "UNREGISTERED_HISTORICAL_DISCOVERY_WFA_PLAN_ONLY",
        "joined_release_id": joined.release_id,
        "calendar_release_id": calendar.release_id,
        "row_count": joined.row_count,
        "model": contract["model"], "target": contract["target"], "wfa": contract["wfa"],
        "claims": contract["claims"], "registration": contract["registration"],
        "validation_scope": {"joined_rows_opened": 0, "calendar_rows_opened": 0, "writes": 0},
        "required_authority": {"real_row_access": True, "training_or_evaluation": True, "report_write": False, "external_registry_required": False},
        "stop_conditions": ["release or calendar identity drift", "calendar-session mapping failure", "attempt to write a report, candidate, or registry record", "trusted or alpha claim"],
    }
    return {**unsigned, "unregistered_wfa_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def load_unregistered_class_base_rate_comparison_contract(
    repo_root: Path,
) -> dict[str, Any]:
    """Load the fixed, non-executable discovery comparison contract."""

    path = Path(repo_root).resolve(strict=True) / COMPARISON_CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ResearchContractError("discovery comparison contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise ResearchContractError("discovery comparison contract ID differs")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != "US_stocks_swing_model_v2"
        or payload.get("mode") != "ALPACA_DISCOVERY_CLASS_BASE_RATE_COMPARISON_PLAN_ONLY"
        or payload.get("hypothesis_id") != "alpaca_raw_price_proxy_fixed_ridge_vs_fold_local_class_base_rate_v1"
        or payload.get("candidate") != {"family": "linear_distribution_v1_fixed_ridge", "ridge_alpha": 1.0, "hyperparameter_tuning": False, "feature_selection": False}
        or payload.get("baseline") != {"family": "fold_local_class_base_rate_v1", "classes": ["down", "neutral", "up"], "smoothing": 0.0, "uses_outer_fit_only": True}
        or payload.get("target") != {"semantics": "ALPACA_RAW_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1", "classes": ["down", "neutral", "up"], "neutral_band": 0.005}
        or payload.get("metric") != "multiclass_log_loss"
        or payload.get("wfa") != {"outer_protocol": "rolling_origin", "purge_sessions": 5, "embargo_sessions": 5, "fold_local_transforms_required": True}
        or payload.get("claims") != {"historical_proxy": True, "trusted_result_claim": False, "alpha_claim": False, "candidate_sealing": False, "training_or_evaluation_authorized": False}
        or payload.get("registration") != {"trial_write_authorized": False, "real_history_execution_authorized": False, "required_evidence_class": "UNREGISTERED_HISTORICAL_DISCOVERY", "external_registry_required": False}
    ):
        raise ResearchContractError("discovery comparison contract differs")
    return {**payload, "contract_id": contract_id}


def build_unregistered_class_base_rate_comparison_plan(
    wfa_plan: Mapping[str, object], *, repo_root: Path
) -> dict[str, object]:
    """Freeze a fresh comparison before any historical rows are opened."""

    contract = load_unregistered_class_base_rate_comparison_contract(repo_root)
    if (
        type(wfa_plan) is not dict
        or wfa_plan.get("mode") != "UNREGISTERED_HISTORICAL_DISCOVERY_WFA_PLAN_ONLY"
        or wfa_plan.get("model") != contract["candidate"]
        or wfa_plan.get("target") != contract["target"]
        or wfa_plan.get("wfa") != contract["wfa"]
        or wfa_plan.get("claims") != contract["claims"]
        or wfa_plan.get("registration") != contract["registration"]
    ):
        raise ResearchContractError("discovery comparison WFA input differs")
    for field in ("unregistered_wfa_plan_id", "joined_release_id", "calendar_release_id"):
        require_sha256(wfa_plan.get(field), field)
    unsigned = {
        "schema_version": 1,
        "mode": contract["mode"],
        "comparison_contract_id": contract["contract_id"],
        "implementation_sha256": sha256_file(Path(__file__)),
        "input_wfa_plan_id": wfa_plan["unregistered_wfa_plan_id"],
        "joined_release_id": wfa_plan["joined_release_id"],
        "calendar_release_id": wfa_plan["calendar_release_id"],
        "hypothesis_id": contract["hypothesis_id"],
        "candidate": contract["candidate"],
        "baseline": contract["baseline"],
        "target": contract["target"],
        "metric": contract["metric"],
        "wfa": contract["wfa"],
        "claims": contract["claims"],
        "registration": contract["registration"],
        "validation_scope": {"joined_rows_opened": 0, "calendar_rows_opened": 0, "writes": 0},
        "required_authority": {"real_row_access": True, "training_or_evaluation": True, "report_write": False, "external_registry_required": False},
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "comparison_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def publish_caveated_joined_release(
    stage_root: Path,
    *,
    join_build_plan_id: str,
    feature_release_id: str,
    outcome_release_id: str,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    approved_publication_plan_id: str,
    repo_root: Path,
) -> Path:
    """Publish one plan-bound caveated release after explicit authorization."""

    if os.environ.get("ALPACA_DISCOVERY_JOIN_PUBLICATION_APPROVED") != "YES":
        raise ResearchContractError("discovery joined publication confirmation is absent")
    plan = build_caveated_joined_publication_plan(
        stage_root, join_build_plan_id=join_build_plan_id,
        feature_release_id=feature_release_id, outcome_release_id=outcome_release_id,
        repo_root=repo_root, created_at=created_at,
    )
    if approved_publication_plan_id != plan["publication_plan_id"]:
        raise ResearchContractError("approved discovery joined publication plan ID differs")
    work = Path(work_root)
    if not work.is_absolute():
        raise ResearchContractError("discovery publication work root must be absolute")
    package = work / str(plan["publication_plan_id"]) / "stage"
    if package.exists():
        raise ResearchContractError("discovery publication stage already exists")
    (package / "joined").mkdir(parents=True)
    for source in sorted((Path(stage_root) / "joined").glob("bucket=*.parquet")):
        shutil.copyfile(source, package / "joined" / source.name)
    atomic_write(package / "source_evidence_manifest.json", canonical_json_bytes(plan["source_evidence"]))
    manifest = ReleaseManifest.from_dict(plan["prospective_release"])
    return AtomicReleasePublisher(Path(accepted_root)).publish(package, manifest)


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


def execute_streaming_unregistered_discovery_wfa(batch_factory, *, sessions: tuple[date, ...]) -> dict[str, object]:
    """Run fixed-ridge outer folds with two bounded scans and no row retention."""

    if len(sessions) < 2016 or tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise ResearchContractError("discovery WFA sessions differ")
    session_index = {value: number for number, value in enumerate(sessions)}
    outer, _ = _schedule(len(sessions))
    stats = [{"n": 0, "sx": np.zeros(3), "sxx": np.zeros((3, 3)), "sy": 0.0, "sy2": 0.0, "sxy": np.zeros(3)} for _ in outer]

    def rows():
        for batch in batch_factory():
            if tuple(batch.schema.names) != JOINED_COLUMNS or batch.num_rows > 65536:
                raise ResearchContractError("streaming discovery batch differs")
            yield from batch.to_pylist()

    for row in rows():
        decision = row["decision_session"]
        if decision not in session_index or session_index[decision] + 5 >= len(sessions):
            raise ResearchContractError("streaming discovery decision session differs")
        values = np.asarray([row[name] for name in JOINED_COLUMNS[2:5]], dtype=np.float64)
        target = float(row["proxy_return"])
        if not np.all(np.isfinite(values)) or not math.isfinite(target):
            raise ResearchContractError("streaming discovery row is non-finite")
        position = session_index[decision]
        for fold, window in enumerate(outer):
            if position < window.start - 5 and position + 5 < window.start:
                item = stats[fold]
                item["n"] += 1; item["sx"] += values; item["sxx"] += np.outer(values, values)
                item["sy"] += target; item["sy2"] += target * target; item["sxy"] += values * target
    fitted: list[tuple[np.ndarray, float, float]] = []
    for item in stats:
        n = int(item["n"])
        if n < 1:
            raise ResearchContractError("streaming discovery fold is underpowered")
        mean_x, mean_y = item["sx"] / n, item["sy"] / n
        scale = np.sqrt(np.maximum(np.diag(item["sxx"]) / n - mean_x * mean_x, 0.0)); scale = np.where(scale > 0, scale, 1.0)
        gram = (item["sxx"] - np.outer(item["sx"], item["sx"]) / n) / np.outer(scale, scale)
        rhs = (item["sxy"] - item["sx"] * mean_y) / scale
        try:
            scaled = np.linalg.solve(gram + np.eye(3), rhs)
        except np.linalg.LinAlgError as exc:
            raise ResearchContractError("streaming discovery ridge system is not solvable") from exc
        coefficients = scaled / scale; bias = float(mean_y - mean_x @ coefficients)
        rss = item["sy2"] - 2 * coefficients @ item["sxy"] - 2 * bias * item["sy"] + coefficients @ (item["sxx"] @ coefficients) + 2 * bias * coefficients @ item["sx"] + n * bias * bias
        uncertainty = max(math.sqrt(max(float(rss) / n, 0.0)), 1e-12)
        fitted.append((coefficients, bias, uncertainty))
    losses = [[] for _ in outer]
    for row in rows():
        position = session_index[row["decision_session"]]
        values = np.asarray([row[name] for name in JOINED_COLUMNS[2:5]], dtype=np.float64); actual = float(row["proxy_return"])
        for fold, window in enumerate(outer):
            if window.start <= position < window.stop:
                coefficients, bias, uncertainty = fitted[fold]
                mean = float(values @ coefficients + bias)
                upper = 0.5 * (1.0 + math.erf((0.005 - mean) / uncertainty / math.sqrt(2.0)))
                lower = 0.5 * (1.0 + math.erf((-0.005 - mean) / uncertainty / math.sqrt(2.0)))
                probability = 1.0 - upper if actual > 0.005 else lower if actual < -0.005 else upper - lower
                losses[fold].append(-math.log(max(probability, 1e-15)))
    if any(not values for values in losses):
        raise ResearchContractError("streaming discovery outer audit is absent")
    return {"mode": "UNREGISTERED_HISTORICAL_DISCOVERY_WFA_STREAMING_NO_WRITE", "folds": tuple({"outer_fold": number, "fit_samples": int(stats[number]["n"]), "audit_samples": len(values), "multiclass_log_loss": float(np.mean(values))} for number, values in enumerate(losses)), "batch_passes": 2, "historical_proxy": True, "trusted_result_claim": False, "alpha_claim": False, "candidate_sealing": False, "writes": 0}


def _sessions_covering_joined_decisions(
    calendar_sessions: tuple[date, ...], *, lower: date, upper: date
) -> tuple[date, ...]:
    """Return every joined decision session plus its required five-session outcome tail."""

    try:
        start = calendar_sessions.index(lower)
        stop = calendar_sessions.index(upper) + 5
    except ValueError as exc:
        raise ResearchContractError("accepted discovery joined session is absent from calendar") from exc
    if stop >= len(calendar_sessions):
        raise ResearchContractError("accepted discovery joined outcome tail is absent from calendar")
    return calendar_sessions[start:stop + 1]


def execute_planned_streaming_unregistered_wfa(
    joined_release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    repo_root: Path,
    approved_unregistered_wfa_plan_id: str,
) -> dict[str, object]:
    """Execute exactly one separately authorized unregistered discovery WFA."""

    if os.environ.get("ALPACA_DISCOVERY_WFA_EXECUTION_APPROVED") != "YES":
        raise ResearchContractError("discovery WFA execution confirmation is absent")
    plan = build_unregistered_wfa_plan(
        joined_release_directory, calendar_release_directory=calendar_release_directory,
        accepted_root=accepted_root, repo_root=repo_root,
    )
    if approved_unregistered_wfa_plan_id != plan["unregistered_wfa_plan_id"]:
        raise ResearchContractError("approved discovery WFA plan ID differs")
    calendar = load_xnys_calendar_release(
        Path(calendar_release_directory), accepted_release_root=Path(accepted_root)
    ).calendar
    joined_paths = tuple(sorted((Path(joined_release_directory) / "joined").glob("bucket=*.parquet")))
    if len(joined_paths) != 64:
        raise ResearchContractError("accepted discovery joined shard census differs")
    lower: date | None = None
    upper: date | None = None
    for path in joined_paths:
        parquet = pq.ParquetFile(path)
        column = parquet.schema_arrow.get_field_index("decision_session")
        for group in range(parquet.metadata.num_row_groups):
            statistics = parquet.metadata.row_group(group).column(column).statistics
            if statistics is None or not statistics.has_min_max:
                raise ResearchContractError("accepted discovery joined session metadata differs")
            minimum, maximum = statistics.min, statistics.max
            if type(minimum) is not date or type(maximum) is not date:
                raise ResearchContractError("accepted discovery joined session type differs")
            lower = minimum if lower is None or minimum < lower else lower
            upper = maximum if upper is None or maximum > upper else upper
    if lower is None or upper is None:
        raise ResearchContractError("accepted discovery joined session bounds are absent")
    sessions = _sessions_covering_joined_decisions(
        tuple(calendar.sessions), lower=lower, upper=upper
    )
    result = execute_streaming_unregistered_discovery_wfa(
        lambda: (batch for path in joined_paths for batch in iter_caveated_parquet_batches(str(path), columns=JOINED_COLUMNS)),
        sessions=sessions,
    )
    return {**result, "unregistered_wfa_plan_id": plan["unregistered_wfa_plan_id"]}
