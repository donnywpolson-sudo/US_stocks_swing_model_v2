"""Causal, discovery-only Alpaca price features and non-executable WFA plan."""

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
from .common import atomic_write, canonical_json_bytes, reject_link, require_contained_path, sha256_bytes, sha256_file
from .environment import validate_environment_lock
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .releases import AtomicReleasePublisher, build_manifest, verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_discovery_proxy_feature_wfa_contract.json"
FEATURE_NAMES = (
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
)
FEATURE_DATASET = "alpaca_discovery_proxy_features"
FEATURE_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("decision_session", pa.date32()),
        ("d0_raw_intraday_return", pa.float64()),
        ("trailing_5_session_raw_return", pa.float64()),
        ("trailing_5_session_raw_volatility", pa.float64()),
        ("status", pa.string()),
    ]
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_feature_wfa_contract(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ContractError("proxy feature/WFA contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise IntegrityError("proxy feature/WFA contract ID differs")
    features, wfa = payload.get("features"), payload.get("wfa")
    claims = payload.get("claims", {})
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_WFA_PLAN_ONLY"
        or type(features) is not dict
        or tuple(features.get("feature_names", ())) != FEATURE_NAMES
        or features.get("lookback_sessions") != 5
        or features.get("uses_only_current_or_prior_pinned_sessions") is not True
        or features.get("may_read_outcomes") is not False
        or features.get("real_row_build_authorized") is not False
        or type(wfa) is not dict
        or wfa.get("outer_protocol") != "rolling_origin"
        or wfa.get("purge_sessions") != 5
        or wfa.get("embargo_sessions") != 5
        or wfa.get("fold_local_transforms_required") is not True
        or wfa.get("trial_registration_required") is not True
        or wfa.get("real_history_execution_authorized") is not False
        or wfa.get("training_or_evaluation_authorized") is not False
        or claims != {"historical_proxy": True, "canonical_target_equivalent": False, "trusted_sleeve_eligible": False, "alpha_claim": False}
    ):
        raise ContractError("proxy feature/WFA contract differs")
    return {**payload, "contract_id": contract_id}


def build_price_only_proxy_features(sessions: Sequence[date], bars: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], ...]:
    """Build features from D0 and its five preceding pinned sessions only."""

    ordered = tuple(sessions)
    if not ordered or tuple(sorted(ordered)) != ordered or len(set(ordered)) != len(ordered) or any(type(value) is not date for value in ordered):
        raise ContractError("feature sessions must be sorted unique exact dates")
    by_symbol: dict[str, dict[date, Mapping[str, object]]] = {}
    for row in bars:
        if type(row) is not dict or set(row) != {"symbol", "session", "open", "close"}:
            raise ContractError("feature bar fields differ")
        symbol, session = row["symbol"], row["session"]
        if type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or type(session) is not date:
            raise ContractError("feature bar identity differs")
        if session not in ordered or session in by_symbol.setdefault(symbol, {}):
            raise ContractError("feature bar session differs")
        by_symbol[symbol][session] = row
    rows: list[dict[str, object]] = []
    for symbol in sorted(by_symbol):
        for index, decision_session in enumerate(ordered[5:], start=5):
            current = by_symbol[symbol].get(decision_session)
            if current is None:
                continue
            history = [by_symbol[symbol].get(session) for session in ordered[index - 5 : index + 1]]
            closes = [None if value is None else value["close"] for value in history]
            open_value = None if current is None else current["open"]
            close_value = None if current is None else current["close"]
            valid = all(type(value) in {int, float} and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) > 0 for value in closes) and type(open_value) in {int, float} and not isinstance(open_value, bool) and math.isfinite(float(open_value)) and float(open_value) > 0 and type(close_value) in {int, float} and not isinstance(close_value, bool) and math.isfinite(float(close_value)) and float(close_value) > 0
            if valid:
                prices = [float(value) for value in closes]
                returns = [prices[position] / prices[position - 1] - 1.0 for position in range(1, len(prices))]
                mean = sum(returns) / len(returns)
                rows.append({"symbol": symbol, "decision_session": decision_session, "d0_raw_intraday_return": float(close_value) / float(open_value) - 1.0, "trailing_5_session_raw_return": prices[-1] / prices[0] - 1.0, "trailing_5_session_raw_volatility": math.sqrt(sum((value - mean) ** 2 for value in returns) / len(returns)), "status": "READY_CAUSAL_RAW_PRICE_FEATURES"})
            else:
                rows.append({"symbol": symbol, "decision_session": decision_session, "d0_raw_intraday_return": None, "trailing_5_session_raw_return": None, "trailing_5_session_raw_volatility": None, "status": "UNRESOLVED_CAUSAL_LOOKBACK"})
    return tuple(rows)


def build_feature_wfa_plan(proxy_outcome_release_directory: Path, *, accepted_root: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Bind a caveated proxy-outcome release without reading its rows."""

    contract = load_feature_wfa_contract(repo_root)
    release = verify_accepted_release(Path(proxy_outcome_release_directory), accepted_root=Path(accepted_root))
    if release.dataset != "alpaca_discovery_proxy_outcomes" or release.role != "legacy_discovery_only" or release.quality_state != "LEGACY_CAVEATED":
        raise ContractError("proxy outcome release differs")
    evidence_path = Path(proxy_outcome_release_directory) / "source_evidence_manifest.json"
    reject_link(evidence_path)
    evidence = json.loads(evidence_path.read_bytes())
    if evidence.get("historical_proxy") is not True or evidence.get("canonical_target_equivalent") is not False or evidence.get("survivorship_safe") is not False or evidence.get("training_or_evaluation") is not False:
        raise ContractError("proxy outcome caveats differ")
    unsigned = {"schema_version": 1, "mode": contract["mode"], "contract_id": contract["contract_id"], "proxy_outcome_release": {"release_id": release.release_id, "manifest_sha256": sha256_file(Path(proxy_outcome_release_directory) / "release_manifest.json"), "row_count": release.row_count, "event_start": release.event_start, "event_end": release.event_end}, "features": contract["features"], "wfa": contract["wfa"], "claims": contract["claims"], "validation_scope": {"proxy_outcome_rows_opened": 0, "files_written": 0}, "required_later_authority": {"real_feature_build": True, "registered_historical_trial": True, "training_or_evaluation": True}, "stop_conditions": contract["stop_conditions"]}
    return {**unsigned, "feature_wfa_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_feature_release_plan(
    source_release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze a no-row, no-write plan for the separate feature-only release."""

    contract = load_feature_wfa_contract(repo_root)
    source = verify_accepted_release(Path(source_release_directory), accepted_root=Path(accepted_root))
    calendar = verify_accepted_release(Path(calendar_release_directory), accepted_root=Path(accepted_root))
    if (
        source.dataset != "alpaca_historical_daily_bars"
        or source.role != "legacy_discovery_only"
        or source.quality_state != "LEGACY_CAVEATED"
        or calendar.dataset != "xnys_sessions"
        or calendar.role != "derived_causal"
        or calendar.quality_state != "PASS"
    ):
        raise ContractError("feature build inputs differ")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_LEGACY_DISCOVERY_PROXY_FEATURE_BUILD_PLAN_ONLY",
        "contract_id": contract["contract_id"],
        "source_release": {
            "release_id": source.release_id,
            "manifest_sha256": sha256_file(Path(source_release_directory) / "release_manifest.json"),
            "row_count": source.row_count,
            "event_start": source.event_start,
            "event_end": source.event_end,
        },
        "calendar_release": {
            "release_id": calendar.release_id,
            "manifest_sha256": sha256_file(Path(calendar_release_directory) / "release_manifest.json"),
        },
        "output": {
            "dataset": FEATURE_DATASET,
            "role": "legacy_discovery_only",
            "quality_state": "LEGACY_CAVEATED",
            "paths": ["features/year=YYYY.parquet", "source_evidence_manifest.json"],
            "release_id": "DEFERRED_UNTIL_REAL_ROWS_AND_PRODUCTION_BUILD_TIME",
            "tracked": False,
        },
        "limits": {"source_rows_at_most": source.row_count, "output_rows_at_most": source.row_count, "network_requests": 0, "credentials_read": 0},
        "validation_scope": {"bar_rows_opened": 0, "calendar_rows_opened": 0, "files_written": 0},
        "required_execution_authority": {"real_row_access": True, "generated_evidence_write": True, "immutable_publication": True, "training_or_evaluation": False},
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "feature_build_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _bar_rows(path: Path, *, maximum_rows: int) -> list[dict[str, object]]:
    reject_link(path)
    table = pq.read_table(path, columns=["provider_symbol", "session", "open", "close"])
    if table.num_rows > maximum_rows or table.column_names != ["provider_symbol", "session", "open", "close"]:
        raise IntegrityError("feature source bar shard differs")
    return [{"symbol": value["provider_symbol"], "session": value["session"], "open": value["open"], "close": value["close"]} for value in table.to_pylist()]


def publish_feature_release(
    source_release_directory: Path,
    *,
    calendar_release_directory: Path,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    approved_feature_build_plan_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Publish the one separately authorized caveated feature-only release."""

    if os.environ.get("ALPACA_DISCOVERY_FEATURE_BUILD_APPROVED") != "YES":
        raise ContractError("feature publication confirmation is absent")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    plan = build_feature_release_plan(source_release_directory, calendar_release_directory=calendar_release_directory, accepted_root=accepted_root, repo_root=root)
    if approved_feature_build_plan_id != plan["feature_build_plan_id"]:
        raise IntegrityError("approved feature build plan ID differs")
    environment_hash = validate_environment_lock(root / "config" / "environment.lock.json")
    calendar = load_xnys_calendar_release(calendar_release_directory, accepted_release_root=Path(accepted_root)).calendar
    sessions = tuple(session for session in calendar.sessions if plan["source_release"]["event_start"] <= session.isoformat() <= plan["source_release"]["event_end"])
    if len(sessions) < 6:
        raise IntegrityError("feature source range has no complete lookback")
    shard_paths = sorted(Path(source_release_directory).glob("bars/year=*.parquet"))
    if not shard_paths:
        raise IntegrityError("feature source release has no bar shards")
    work = Path(work_root).resolve()
    work.mkdir(parents=True, exist_ok=True)
    require_contained_path(work, work)
    stage = Path(tempfile.mkdtemp(prefix="proxy-features-", dir=work))
    try:
        output_paths: list[str] = []
        tail_rows: list[dict[str, object]] = []
        counted_input = 0
        counted_output = 0
        (stage / "features").mkdir()
        for source_path in shard_paths:
            current_rows = _bar_rows(source_path, maximum_rows=int(plan["source_release"]["row_count"]))
            counted_input += len(current_rows)
            current_sessions = {row["session"] for row in current_rows}
            selected_sessions = tuple(session for session in sessions if session in current_sessions)
            if not selected_sessions:
                raise IntegrityError("feature bar shard has no pinned sessions")
            context_start = max(0, sessions.index(selected_sessions[0]) - 5)
            context = sessions[context_start : sessions.index(selected_sessions[-1]) + 1]
            feature_rows = [row for row in build_price_only_proxy_features(context, tail_rows + current_rows) if row["decision_session"] in set(selected_sessions)]
            payload = deterministic_parquet_bytes(pa.Table.from_pylist(feature_rows, schema=FEATURE_SCHEMA), schema=FEATURE_SCHEMA, sort_keys=("symbol", "decision_session"))
            year = selected_sessions[0].year
            relative = f"features/year={year}.parquet"
            atomic_write(stage / relative, payload)
            output_paths.append(relative)
            counted_output += len(feature_rows)
            tail_sessions = set(context[-5:])
            tail_rows = [row for row in current_rows if row["session"] in tail_sessions]
        if counted_input != int(plan["source_release"]["row_count"]) or counted_output > counted_input:
            raise IntegrityError("feature source/output row census differs")
        evidence = {"schema_version": 1, "feature_build_plan_id": plan["feature_build_plan_id"], "source_release": plan["source_release"], "calendar_release": plan["calendar_release"], "feature_names": list(FEATURE_NAMES), "historical_proxy": True, "canonical_target_equivalent": False, "survivorship_safe": False, "outcomes_read": False, "training_or_evaluation": False}
        atomic_write(stage / "source_evidence_manifest.json", canonical_json_bytes(evidence))
        manifest = build_manifest(stage, tuple(output_paths) + ("source_evidence_manifest.json",), project=PROJECT, dataset=FEATURE_DATASET, source_epoch="alpaca_raw_price_features_v1", role="legacy_discovery_only", quality_state="LEGACY_CAVEATED", created_at=created_at, row_count=counted_output, event_start=sessions[5].isoformat(), event_end=sessions[-1].isoformat(), upstream_release_ids=(str(plan["source_release"]["release_id"]), str(plan["calendar_release"]["release_id"])), schema_fingerprint=sha256_bytes(canonical_json_bytes(str(FEATURE_SCHEMA))), code_hash=sha256_file(root / "src" / "us_stocks_swing_model_v2" / "alpaca_discovery_proxy_feature_wfa.py"), config_hash=sha256_file(root / CONTRACT_PATH), environment_hash=environment_hash)
        return AtomicReleasePublisher(Path(accepted_root)).publish(stage, manifest)
    except Exception:
        raise
