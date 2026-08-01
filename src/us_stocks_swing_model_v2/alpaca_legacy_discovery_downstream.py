"""Metadata-only downstream planning for caveated Alpaca historical bars."""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from .canonical.parquet import deterministic_parquet_bytes
from .common import atomic_write, canonical_json_bytes, reject_link, require_contained_path, require_sha256, sha256_bytes, sha256_file
from .environment import validate_environment_lock
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .releases import AtomicReleasePublisher, build_manifest, verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_legacy_discovery_downstream_contract.json"
EVIDENCE_PATH = "source_evidence_manifest.json"
DISCOVERY_PROXY = {
    "state": "SOURCE_ADJUSTED_RAW_PRICE_PROXY_PLANNED_NOT_MATERIALIZED",
    "target_semantics": "ALPACA_RAW_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1",
    "historical_proxy": True,
    "canonical_target_equivalent": False,
    "trusted_sleeve_eligible": False,
    "real_history_row_access_authorized": False,
    "generated_evidence_write_authorized": False,
    "training_or_evaluation_authorized": False,
}
PROXY_OUTCOME_DATASET = "alpaca_discovery_proxy_outcomes"
PROXY_OUTCOME_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("decision_session", pa.date32()),
        ("entry_session", pa.date32()),
        ("exit_session", pa.date32()),
        ("entry_open", pa.float64()),
        ("exit_close", pa.float64()),
        ("proxy_return", pa.float64()),
        ("status", pa.string()),
        ("target_semantics", pa.string()),
        ("historical_proxy", pa.bool_()),
        ("canonical_target_equivalent", pa.bool_()),
    ]
)


def build_raw_price_proxy_outcomes(
    sessions: Sequence[date], bars: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], ...]:
    """Build only untrusted five-session raw-price proxy outcomes in memory."""

    ordered_sessions = tuple(sessions)
    if (
        not ordered_sessions
        or tuple(sorted(ordered_sessions)) != ordered_sessions
        or len(set(ordered_sessions)) != len(ordered_sessions)
        or any(type(value) is not date for value in ordered_sessions)
    ):
        raise ContractError("proxy sessions must be sorted unique exact dates")
    by_symbol: dict[str, dict[date, Mapping[str, object]]] = {}
    for row in bars:
        if type(row) is not dict or set(row) != {"symbol", "session", "open", "close"}:
            raise ContractError("proxy bar fields differ")
        symbol, session = row["symbol"], row["session"]
        if type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or type(session) is not date:
            raise ContractError("proxy bar identity differs")
        if session not in ordered_sessions:
            raise ContractError("proxy bar session is outside the pinned calendar")
        if session in by_symbol.setdefault(symbol, {}):
            raise ContractError("proxy bars contain a duplicate symbol/session")
        by_symbol[symbol][session] = row
    outcomes: list[dict[str, object]] = []
    for symbol in sorted(by_symbol):
        for index, decision_session in enumerate(ordered_sessions[:-5]):
            # A proxy target exists only for an observed D0 bar.  This keeps
            # the derivative row census bounded by the accepted input census
            # while retaining an explicit unresolved status when D1 or D5 is
            # absent or unusable.
            if decision_session not in by_symbol[symbol]:
                continue
            entry_session, exit_session = ordered_sessions[index + 1], ordered_sessions[index + 5]
            entry = by_symbol[symbol].get(entry_session)
            exit_bar = by_symbol[symbol].get(exit_session)
            entry_open = entry["open"] if entry is not None else None
            exit_close = exit_bar["close"] if exit_bar is not None else None
            valid = (
                type(entry_open) in {int, float}
                and not isinstance(entry_open, bool)
                and type(exit_close) in {int, float}
                and not isinstance(exit_close, bool)
                and math.isfinite(float(entry_open))
                and math.isfinite(float(exit_close))
                and float(entry_open) > 0
                and float(exit_close) > 0
            )
            outcomes.append({
                "symbol": symbol,
                "decision_session": decision_session,
                "entry_session": entry_session,
                "exit_session": exit_session,
                "entry_open": float(entry_open) if valid else None,
                "exit_close": float(exit_close) if valid else None,
                "proxy_return": float(exit_close) / float(entry_open) - 1.0 if valid else None,
                "status": "READY_UNTRUSTED_RAW_PRICE_PROXY" if valid else "UNRESOLVED_RAW_HORIZON",
                "target_semantics": DISCOVERY_PROXY["target_semantics"],
                "historical_proxy": True,
                "canonical_target_equivalent": False,
            })
    return tuple(outcomes)


def build_raw_price_proxy_outcome_table(
    sessions: Sequence[date], bars: Sequence[Mapping[str, object]]
) -> pa.Table:
    """Return deterministic, explicitly caveated proxy-outcome rows.

    This is deliberately a mechanical transform.  It neither constructs
    features nor authorizes fitting, evaluation, or trusted target claims.
    """

    outcomes = build_raw_price_proxy_outcomes(sessions, bars)
    return pa.Table.from_pylist(list(outcomes), schema=PROXY_OUTCOME_SCHEMA)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_contract(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ContractError("Alpaca downstream contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise IntegrityError("Alpaca downstream contract ID differs")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "ALPACA_LEGACY_DISCOVERY_DOWNSTREAM_PLAN_ONLY"
        or payload.get("discovery_proxy") != DISCOVERY_PROXY
        or any(payload.get("authorities", {}).values())
    ):
        raise ContractError("Alpaca downstream contract differs")
    return {**payload, "contract_id": contract_id}


def build_downstream_plan(
    release_directory: Path,
    *,
    accepted_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Verify release metadata and emit a no-row, no-write downstream plan."""

    contract = load_contract(repo_root)
    release = verify_accepted_release(Path(release_directory), accepted_root=Path(accepted_root))
    source = contract["source"]
    if (
        release.dataset != source["dataset"]
        or release.role != source["role"]
        or release.quality_state != source["quality_state"]
    ):
        raise ContractError("Alpaca downstream release identity differs")
    evidence_file = next((entry for entry in release.files if entry.path == EVIDENCE_PATH), None)
    if evidence_file is None:
        raise IntegrityError("Alpaca downstream release lacks source evidence metadata")
    evidence_path = Path(release_directory) / EVIDENCE_PATH
    reject_link(evidence_path)
    raw = evidence_path.read_bytes()
    if len(raw) != evidence_file.size or sha256_bytes(raw) != evidence_file.sha256:
        raise IntegrityError("Alpaca downstream evidence metadata differs")
    evidence = json.loads(raw)
    for name in ("input_quality_state", "historical_membership_proven", "point_in_time_safe", "survivorship_safe"):
        if evidence.get(name) != source[name]:
            raise ContractError("Alpaca downstream source caveat differs")
    unsigned = {
        "schema_version": 1,
        "mode": contract["mode"],
        "contract_id": contract["contract_id"],
        "release": {
            "release_id": release.release_id,
            "manifest_sha256": sha256_file(Path(release_directory) / "release_manifest.json"),
            "row_count": release.row_count,
            "event_start": release.event_start,
            "event_end": release.event_end,
        },
        "eligibility": contract["eligibility"],
        "features": contract["features"],
        "outcomes": contract["outcomes"],
        "discovery_proxy": contract["discovery_proxy"],
        "wfa": contract["wfa"],
        "metadata_validation_scope": {"release_verified": True, "bar_rows_opened": 0, "outcomes_computed": 0, "files_written": 0},
        "authorities": contract["authorities"],
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_proxy_outcome_plan(
    release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze a no-row, no-write plan for the caveated proxy transform.

    The plan intentionally leaves the content-addressed output release ID
    deferred: it depends on the exact real rows and production build time.
    """

    downstream = build_downstream_plan(
        release_directory, accepted_root=accepted_root, repo_root=repo_root
    )
    calendar = verify_accepted_release(
        Path(calendar_release_directory), accepted_root=Path(accepted_root)
    )
    if (
        calendar.dataset != "xnys_sessions"
        or calendar.role != "derived_causal"
        or calendar.quality_state != "PASS"
    ):
        raise ContractError("proxy plan calendar release differs")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_LEGACY_DISCOVERY_PROXY_OUTCOME_BUILD_PLAN_ONLY",
        "downstream_plan_id": downstream["plan_id"],
        "source_release": downstream["release"],
        "calendar_release": {
            "release_id": calendar.release_id,
            "manifest_sha256": sha256_file(
                Path(calendar_release_directory) / "release_manifest.json"
            ),
            "event_start": calendar.event_start,
            "event_end": calendar.event_end,
        },
        "output": {
            "dataset": PROXY_OUTCOME_DATASET,
            "role": "legacy_discovery_only",
            "quality_state": "LEGACY_CAVEATED",
            "paths": ["proxy_outcomes.parquet", "source_evidence_manifest.json"],
            "release_id": "DEFERRED_UNTIL_REAL_ROWS_AND_PRODUCTION_BUILD_TIME",
            "tracked": False,
            "immutable_publication_required": True,
        },
        "transform": {
            "target_semantics": DISCOVERY_PROXY["target_semantics"],
            "entry": "next_pinned_session_open",
            "exit": "fifth_pinned_session_close",
            "input_columns": ["provider_symbol", "session", "open", "close"],
            "output_schema": str(PROXY_OUTCOME_SCHEMA),
            "features_constructed": False,
            "training_or_evaluation": False,
            "canonical_target_equivalent": False,
            "survivorship_safe": False,
        },
        "limits": {
            "source_rows_at_most": downstream["release"]["row_count"],
            "output_rows_at_most": downstream["release"]["row_count"],
            "network_requests": 0,
            "credentials_read": 0,
        },
        "plan_validation_scope": {
            "bar_rows_opened": 0,
            "calendar_rows_opened": 0,
            "files_written": 0,
        },
        "required_execution_authority": {
            "real_row_access": True,
            "generated_evidence_write": True,
            "immutable_publication": True,
            "feature_or_model_work": False,
        },
        "stop_conditions": [
            "source or calendar accepted-release identity mismatch",
            "source caveat drift",
            "row, schema, output-bound, or immutable-publication failure",
            "feature, training, evaluation, or trusted-result request",
        ],
    }
    return {**unsigned, "proxy_build_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _source_bar_rows(release_directory: Path, *, expected_row_count: int) -> list[dict[str, object]]:
    """Load only the four raw columns needed by the future approved build."""

    paths = sorted(Path(release_directory).glob("bars/year=*.parquet"))
    if not paths:
        raise IntegrityError("proxy source release has no canonical bar shards")
    rows: list[dict[str, object]] = []
    for path in paths:
        reject_link(path)
        table = pq.read_table(path, columns=["provider_symbol", "session", "open", "close"])
        if table.column_names != ["provider_symbol", "session", "open", "close"]:
            raise IntegrityError("proxy source bar columns differ")
        rows.extend(
            {
                "symbol": value["provider_symbol"],
                "session": value["session"],
                "open": value["open"],
                "close": value["close"],
            }
            for value in table.to_pylist()
        )
        if len(rows) > expected_row_count:
            raise IntegrityError("proxy source rows exceed its accepted manifest")
    if len(rows) != expected_row_count:
        raise IntegrityError("proxy source row count differs from its accepted manifest")
    return rows


def publish_proxy_outcomes(
    release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    approved_proxy_build_plan_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Publish one immutable caveated proxy-outcomes release after explicit approval.

    This function is intentionally not reached by planning.  Its caller must
    bind a current plan ID and the exact confirmation environment variable.
    """

    if os.environ.get("ALPACA_DISCOVERY_PROXY_BUILD_APPROVED") != "YES":
        raise ContractError("proxy outcome publication confirmation is absent")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    plan = build_proxy_outcome_plan(
        release_directory,
        calendar_release_directory=calendar_release_directory,
        accepted_root=accepted_root,
        repo_root=root,
    )
    if approved_proxy_build_plan_id != plan["proxy_build_plan_id"]:
        raise IntegrityError("approved proxy build plan ID differs")
    environment_hash = validate_environment_lock(root / "config" / "environment.lock.json")
    calendar = load_xnys_calendar_release(
        calendar_release_directory, accepted_release_root=Path(accepted_root)
    )
    source_sessions = tuple(
        session for session in calendar.calendar.sessions
        if plan["source_release"]["event_start"] <= session.isoformat() <= plan["source_release"]["event_end"]
    )
    if len(source_sessions) < 6:
        raise IntegrityError("proxy source range has no five-session horizon")
    rows = _source_bar_rows(
        release_directory, expected_row_count=int(plan["source_release"]["row_count"])
    )
    outcome_bytes = deterministic_parquet_bytes(
        build_raw_price_proxy_outcome_table(source_sessions, rows),
        schema=PROXY_OUTCOME_SCHEMA,
        sort_keys=("symbol", "decision_session"),
    )
    outcome_table = pq.read_table(pa.BufferReader(outcome_bytes))
    evidence = {
        "schema_version": 1,
        "proxy_build_plan_id": plan["proxy_build_plan_id"],
        "source_release": plan["source_release"],
        "calendar_release": plan["calendar_release"],
        "target_semantics": DISCOVERY_PROXY["target_semantics"],
        "historical_proxy": True,
        "canonical_target_equivalent": False,
        "survivorship_safe": False,
        "features_constructed": False,
        "training_or_evaluation": False,
    }
    work = Path(work_root).resolve()
    work.mkdir(parents=True, exist_ok=True)
    require_contained_path(work, work)
    stage = Path(tempfile.mkdtemp(prefix="proxy-outcomes-", dir=work))
    try:
        atomic_write(stage / "proxy_outcomes.parquet", outcome_bytes)
        atomic_write(stage / "source_evidence_manifest.json", canonical_json_bytes(evidence))
        manifest = build_manifest(
            stage,
            ("proxy_outcomes.parquet", "source_evidence_manifest.json"),
            project=PROJECT,
            dataset=PROXY_OUTCOME_DATASET,
            source_epoch="alpaca_raw_price_proxy_v1",
            role="legacy_discovery_only",
            quality_state="LEGACY_CAVEATED",
            created_at=created_at,
            row_count=outcome_table.num_rows,
            event_start=source_sessions[0].isoformat(),
            event_end=source_sessions[-6].isoformat(),
            upstream_release_ids=(str(plan["source_release"]["release_id"]), str(plan["calendar_release"]["release_id"])),
            schema_fingerprint=sha256_bytes(canonical_json_bytes(str(PROXY_OUTCOME_SCHEMA))),
            code_hash=sha256_file(root / "src" / "us_stocks_swing_model_v2" / "alpaca_legacy_discovery_downstream.py"),
            config_hash=sha256_file(root / CONTRACT_PATH),
            environment_hash=environment_hash,
        )
        return AtomicReleasePublisher(Path(accepted_root)).publish(stage, manifest)
    except Exception:
        # The stage is deliberately retained for a separately authorized recovery.
        raise
