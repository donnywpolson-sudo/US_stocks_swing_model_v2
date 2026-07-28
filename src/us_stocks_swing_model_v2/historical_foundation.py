"""No-model HFDL historical-discovery foundation bridge.

The bridge consumes only a verified complete two-epoch HFDL publication and a
pinned accepted XNYS calendar. It publishes independent causal-bar,
feature-input, and outcome-input releases for each source epoch. Historical
membership, security type, actions, delistings, alpha, and candidates remain
explicitly unavailable.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from .capabilities import SyntheticOnlyPermit
from .canonical.hfdl import HFDL_TAGGED_SCHEMA
from .canonical.hfdl_legacy_publisher import (
    EPOCH_DATASETS,
    HFDL_EPOCHS,
    SET_DATASET as HFDL_SET_DATASET,
    verify_hfdl_legacy_publication,
)
from .canonical.parquet import deterministic_table, write_deterministic_parquet
from .common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .environment import validate_environment_lock
from .errors import ContractError, IntegrityError
from .exchange_calendar import LoadedExchangeCalendar, load_xnys_calendar_release
from .locking import ExclusiveFileLock
from .releases import (
    AtomicReleasePublisher,
    ReleaseManifest,
    build_manifest,
    verify_accepted_release,
)


QUALITY_STATE = "LEGACY_CAVEATED"
ROLE = "legacy_discovery_only"
EVIDENCE_CLASS = "LEGACY_DISCOVERY"
POINT_IN_TIME_STATE = "HISTORICAL_PROXY"
HISTORICAL_AVAILABILITY = "UNKNOWN_NOT_AS_RECEIVED"
SOURCE_ADJUSTMENT = "hfdl_clean_source_adjusted"
MEMBERSHIP_STATE = "UNKNOWN_NOT_AS_RECEIVED"
SECURITY_TYPE_STATE = "UNKNOWN_NOT_AS_RECEIVED"
ACTION_STATE = "UNAVAILABLE_NOT_AS_RECEIVED"
DELISTING_STATE = "UNAVAILABLE_NOT_AS_RECEIVED"
OUTPUT_KINDS = ("causal_bars", "feature_inputs", "outcome_inputs")
BRIDGE_SET_DATASET = "hfdl_historical_foundation_bridge_set"
BRIDGE_SET_SOURCE_EPOCH = "hfdl_historical_foundation_no_pooling"

BRIDGE_DATASETS = {
    epoch: {
        kind: f"{epoch}_{kind}"
        for kind in OUTPUT_KINDS
    }
    for epoch in HFDL_EPOCHS
}

CAUSAL_BAR_SCHEMA = pa.schema(
    [
        ("source_series_id", pa.string()),
        ("symbol", pa.string()),
        ("session", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("source_retrieved_at", pa.timestamp("us", tz="UTC")),
        ("bar_status", pa.string()),
        ("source_epoch", pa.string()),
        ("source_adjustment", pa.string()),
        ("evidence_class", pa.string()),
        ("point_in_time_state", pa.string()),
        ("historical_availability_state", pa.string()),
        ("calendar_release_id", pa.string()),
        ("membership_evidence_status", pa.string()),
        ("security_type_evidence_status", pa.string()),
        ("action_evidence_status", pa.string()),
        ("delisting_evidence_status", pa.string()),
    ]
)

FEATURE_INPUT_SCHEMA = pa.schema(
    [
        ("source_series_id", pa.string()),
        ("symbol", pa.string()),
        ("decision_session", pa.date32()),
        ("decision_at", pa.timestamp("us", tz="UTC")),
        ("feature_status", pa.string()),
        ("close_to_close_return_1", pa.float64()),
        ("intraday_return", pa.float64()),
        ("range_fraction", pa.float64()),
        ("log1p_volume", pa.float64()),
        ("source_epoch", pa.string()),
        ("source_adjustment", pa.string()),
        ("evidence_class", pa.string()),
        ("point_in_time_state", pa.string()),
        ("historical_availability_state", pa.string()),
        ("calendar_release_id", pa.string()),
        ("membership_evidence_status", pa.string()),
        ("security_type_evidence_status", pa.string()),
        ("action_evidence_status", pa.string()),
        ("delisting_evidence_status", pa.string()),
    ]
)

OUTCOME_INPUT_SCHEMA = pa.schema(
    [
        ("source_series_id", pa.string()),
        ("symbol", pa.string()),
        ("decision_session", pa.date32()),
        ("entry_session", pa.date32()),
        ("exit_session", pa.date32()),
        ("entry_open", pa.float64()),
        ("exit_close", pa.float64()),
        ("split_normalized_price_return", pa.float64()),
        ("outcome_input_status", pa.string()),
        ("source_epoch", pa.string()),
        ("source_adjustment", pa.string()),
        ("evidence_class", pa.string()),
        ("point_in_time_state", pa.string()),
        ("historical_availability_state", pa.string()),
        ("calendar_release_id", pa.string()),
        ("membership_evidence_status", pa.string()),
        ("security_type_evidence_status", pa.string()),
        ("action_evidence_status", pa.string()),
        ("delisting_evidence_status", pa.string()),
    ]
)

SCHEMAS = {
    "causal_bars": CAUSAL_BAR_SCHEMA,
    "feature_inputs": FEATURE_INPUT_SCHEMA,
    "outcome_inputs": OUTCOME_INPUT_SCHEMA,
}
SORT_KEYS = {
    "causal_bars": ("source_series_id", "session"),
    "feature_inputs": ("source_series_id", "decision_session"),
    "outcome_inputs": ("source_series_id", "decision_session"),
}
STATUS_COLUMNS = {
    "causal_bars": "bar_status",
    "feature_inputs": "feature_status",
    "outcome_inputs": "outcome_input_status",
}


@dataclass(frozen=True)
class HistoricalFoundationResult:
    build_id: str
    epoch_release_directories: Mapping[str, Mapping[str, Path]]
    bridge_set_release_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_contract() -> tuple[dict[str, Any], str]:
    path = _repo_root() / "config" / "hfdl_historical_foundation_contract.json"
    reject_link(path)
    try:
        raw = path.read_bytes()
        contract = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("HFDL historical-foundation contract is unreadable") from exc
    expected_fields = {
        "schema_version",
        "contract_version",
        "project",
        "mechanical_status",
        "source",
        "calendar",
        "required_source_epochs",
        "physical_output_kinds",
        "downstream_dependency",
        "explicit_missing_and_unavailable_denominators_required",
        "feature_inputs",
        "feature_cutoff",
        "outcome_horizon",
        "historical_evidence_scope",
        "membership_evidence_status",
        "security_type_evidence_status",
        "action_evidence_status",
        "delisting_evidence_status",
        "source_series_id_is_persistent_asset_identity",
        "epochs_may_be_pooled",
        "matured_outcomes_may_be_emitted",
        "model_or_evaluation_inputs_allowed",
        "real_history_execution_authorized",
        "alpha_evidence",
        "candidate_eligible",
    }
    if (
        not isinstance(contract, dict)
        or set(contract) != expected_fields
        or contract["schema_version"] != 1
        or contract["contract_version"] != "1.1.0"
        or contract["project"] != "US_stocks_swing_model_v2"
        or contract["mechanical_status"] != "IMPLEMENTED_SYNTHETIC_ADVERSARIAL_TESTED"
        or contract["source"] != "VERIFIED_COMPLETE_HFDL_TWO_EPOCH_SET_ONLY"
        or contract["calendar"] != "PINNED_ACCEPTED_XNYS_RELEASE_ONLY"
        or contract["required_source_epochs"] != list(HFDL_EPOCHS)
        or contract["physical_output_kinds"] != list(OUTPUT_KINDS)
        or contract["downstream_dependency"]
        != "VERIFIED_UNPOOLED_CAUSAL_BAR_RELEASE_ONLY"
        or contract["explicit_missing_and_unavailable_denominators_required"] is not True
        or contract["feature_inputs"]
        != [
            "close_to_close_return_1",
            "intraday_return",
            "range_fraction",
            "log1p_volume",
        ]
        or contract["feature_cutoff"]
        != "DECISION_SESSION_CLOSE_WITH_CURRENT_AND_PRIOR_PINNED_SESSION_ONLY"
        or contract["outcome_horizon"]
        != "D1_OPEN_TO_D5_CLOSE_WITHIN_ONE_SOURCE_EPOCH_ONLY"
        or contract["historical_evidence_scope"]
        != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or contract["membership_evidence_status"] != MEMBERSHIP_STATE
        or contract["security_type_evidence_status"] != SECURITY_TYPE_STATE
        or contract["action_evidence_status"] != ACTION_STATE
        or contract["delisting_evidence_status"] != DELISTING_STATE
        or any(
            contract[name] is not False
            for name in (
                "source_series_id_is_persistent_asset_identity",
                "epochs_may_be_pooled",
                "matured_outcomes_may_be_emitted",
                "model_or_evaluation_inputs_allowed",
                "real_history_execution_authorized",
                "alpha_evidence",
                "candidate_eligible",
            )
        )
    ):
        raise ContractError("HFDL historical-foundation contract differs from the fail-closed policy")
    return contract, sha256_bytes(canonical_json_bytes(contract))


def _implementation_hash() -> str:
    root = _repo_root()
    relatives = (
        "src/us_stocks_swing_model_v2/historical_foundation.py",
        "src/us_stocks_swing_model_v2/canonical/hfdl_legacy_publisher.py",
        "src/us_stocks_swing_model_v2/canonical/hfdl.py",
        "src/us_stocks_swing_model_v2/canonical/parquet.py",
        "src/us_stocks_swing_model_v2/exchange_calendar.py",
        "src/us_stocks_swing_model_v2/releases.py",
        "src/us_stocks_swing_model_v2/common.py",
        "src/us_stocks_swing_model_v2/locking.py",
        "config/hfdl_historical_foundation_contract.json",
    )
    manifest: dict[str, str] = {}
    for relative in relatives:
        candidate = root.joinpath(*safe_relative_path(relative).parts)
        require_contained_path(candidate, root)
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(f"historical-foundation implementation dependency is invalid: {relative}")
        manifest[relative] = sha256_file(candidate)
    return sha256_bytes(canonical_json_bytes(manifest))


def _environment_hash() -> str:
    return validate_environment_lock(_repo_root() / "config" / "environment.lock.json")


def _read_canonical_json(path: Path, expected_fields: set[str], label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != expected_fields or raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} fields or canonical encoding differ")
    return payload


def _source_index(
    epoch_directory: Path,
    *,
    expected_event_start: str,
    expected_event_end: str,
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    epoch_root = Path(epoch_directory)
    index_path = epoch_root / "symbol_index.jsonl"
    require_contained_path(index_path, epoch_root)
    reject_link(index_path)
    if not index_path.is_file() or index_path.stat().st_nlink != 1:
        raise IntegrityError("HFDL source index is not an independent plain file")
    raw = index_path.read_bytes()
    for line in raw.splitlines(keepends=True):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError("HFDL source index is invalid") from exc
        if not isinstance(row, dict) or line != canonical_json_bytes(row) or set(row) != {
            "symbol",
            "pair_id",
            "data_path",
            "sha256",
            "size",
            "row_count",
            "session_start",
            "session_end",
        }:
            raise IntegrityError("HFDL source index fields differ")
        require_sha256(row["pair_id"], "historical_foundation.source_series_id")
        require_sha256(row["sha256"], "historical_foundation.source_sha256")
        if (
            not isinstance(row["symbol"], str)
            or not row["symbol"]
            or type(row["size"]) is not int
            or row["size"] < 0
            or type(row["row_count"]) is not int
            or row["row_count"] <= 0
            or type(row["data_path"]) is not str
            or not isinstance(row["session_start"], str)
            or not isinstance(row["session_end"], str)
        ):
            raise IntegrityError("HFDL source index value types differ")
        relative = safe_relative_path(row["data_path"])
        try:
            start = date.fromisoformat(row["session_start"])
            end = date.fromisoformat(row["session_end"])
        except ValueError as exc:
            raise IntegrityError("HFDL source index session bounds are invalid") from exc
        if start > end:
            raise IntegrityError("HFDL source index session bounds are reversed")
        data_path = epoch_root.joinpath(*relative.parts)
        require_contained_path(data_path, epoch_root)
        reject_link(data_path)
        if (
            not data_path.is_file()
            or data_path.stat().st_nlink != 1
            or data_path.stat().st_size != row["size"]
            or sha256_file(data_path) != row["sha256"]
        ):
            raise IntegrityError(
                "HFDL source index object hash, size, or file identity differs"
            )
        try:
            source = pq.read_table(data_path, columns=["symbol", "session"])
        except (OSError, pa.ArrowException) as exc:
            raise IntegrityError(
                "HFDL source index object is not readable Parquet evidence"
            ) from exc
        symbols = source.column("symbol").to_pylist()
        sessions = source.column("session").to_pylist()
        if (
            source.num_rows != row["row_count"]
            or source.num_rows <= 0
            or any(type(symbol) is not str or symbol != row["symbol"] for symbol in symbols)
            or any(type(session) is not date for session in sessions)
            or min(sessions) != start
            or max(sessions) != end
        ):
            raise IntegrityError(
                "HFDL source index row count, symbol, or session bounds differ"
            )
        rows.append(row)
    if not rows or [row["symbol"] for row in rows] != sorted({row["symbol"] for row in rows}):
        raise IntegrityError("HFDL source index is empty, duplicated, or unsorted")
    if (
        min(row["session_start"] for row in rows) != expected_event_start
        or max(row["session_end"] for row in rows) != expected_event_end
    ):
        raise IntegrityError(
            "HFDL source index epoch bounds differ from its verified manifest"
        )
    return tuple(rows)


def _calendar_context(
    loaded: LoadedExchangeCalendar,
    *,
    event_start: str,
    event_end: str,
) -> tuple[tuple[date, ...], dict[date, dict[str, Any]]]:
    start = date.fromisoformat(event_start)
    end = date.fromisoformat(event_end)
    requested_start = date.fromisoformat(str(loaded.provenance["requested_start"]))
    requested_end = date.fromisoformat(str(loaded.provenance["requested_end"]))
    if requested_start > start or requested_end < end:
        raise ContractError("pinned XNYS calendar does not cover the complete HFDL epoch bounds")
    rows = loaded.schedule.to_pylist()
    mapping = {row["session"]: row for row in rows}
    sessions = tuple(session for session in sorted(mapping) if start <= session <= end)
    if not sessions:
        raise ContractError("HFDL epoch has no pinned XNYS sessions")
    return sessions, mapping


def _caveats(epoch: str, calendar_release_id: str) -> dict[str, object]:
    return {
        "source_epoch": epoch,
        "source_adjustment": SOURCE_ADJUSTMENT,
        "evidence_class": EVIDENCE_CLASS,
        "point_in_time_state": POINT_IN_TIME_STATE,
        "historical_availability_state": HISTORICAL_AVAILABILITY,
        "calendar_release_id": calendar_release_id,
        "membership_evidence_status": MEMBERSHIP_STATE,
        "security_type_evidence_status": SECURITY_TYPE_STATE,
        "action_evidence_status": ACTION_STATE,
        "delisting_evidence_status": DELISTING_STATE,
    }


def _derive_symbol_tables(
    *,
    source_series_id: str,
    symbol: str,
    source: pa.Table,
    epoch: str,
    calendar_sessions: tuple[date, ...],
    calendar_rows: Mapping[date, Mapping[str, Any]],
    calendar_release_id: str,
) -> dict[str, pa.Table]:
    """Derive features/outcome inputs strictly from the canonical causal table."""

    require_sha256(source_series_id, "historical_foundation.source_series_id")
    require_sha256(calendar_release_id, "historical_foundation.calendar_release_id")
    if epoch not in HFDL_EPOCHS or not isinstance(symbol, str) or not symbol:
        raise IntegrityError("HFDL bridge source identity is invalid")
    if calendar_sessions != tuple(sorted(set(calendar_sessions))) or any(
        session not in calendar_rows for session in calendar_sessions
    ):
        raise IntegrityError("HFDL bridge calendar sessions are duplicated, unsorted, or missing")
    if source.schema.remove_metadata() != HFDL_TAGGED_SCHEMA:
        raise IntegrityError("HFDL bridge source schema differs")
    source_rows = source.to_pylist()
    for row in source_rows:
        prices = tuple(row[name] for name in ("open", "high", "low", "close"))
        if (
            not all(
                type(value) is float and math.isfinite(value) and value > 0.0
                for value in prices
            )
            or row["high"] < max(row["open"], row["close"])
            or row["low"] > min(row["open"], row["close"])
            or row["high"] < row["low"]
            or type(row["volume"]) is not int
            or row["volume"] < 0
        ):
            raise IntegrityError(
                "HFDL bridge source OHLCV values violate the canonical contract"
            )
    if (
        not source_rows
        or any(row["symbol"] != symbol or row["source_epoch"] != epoch for row in source_rows)
        or len({row["session"] for row in source_rows}) != len(source_rows)
        or any(
            row["source_adjustment"] != SOURCE_ADJUSTMENT
            or row["evidence_class"] != EVIDENCE_CLASS
            or row["point_in_time_safe"] is not False
            or row["point_in_time_state"] != POINT_IN_TIME_STATE
            or row["historical_availability_state"] != HISTORICAL_AVAILABILITY
            for row in source_rows
        )
    ):
        raise IntegrityError("HFDL bridge source identity/session/epoch/caveats differ")
    by_session = {row["session"]: row for row in source_rows}
    caveats = _caveats(epoch, calendar_release_id)
    causal: list[dict[str, object]] = []

    for session in calendar_sessions:
        current = by_session.get(session)
        causal.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "session": session,
                "open": current["open"] if current else None,
                "high": current["high"] if current else None,
                "low": current["low"] if current else None,
                "close": current["close"] if current else None,
                "volume": current["volume"] if current else None,
                "source_retrieved_at": current["source_retrieved_at"] if current else None,
                "bar_status": "OBSERVED" if current else "MISSING_SOURCE_SESSION_UNKNOWN_CAUSE",
                **caveats,
            }
        )

    for session in sorted(set(by_session) - set(calendar_rows)):
        source_row = by_session[session]
        causal.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "session": session,
                "open": source_row["open"],
                "high": source_row["high"],
                "low": source_row["low"],
                "close": source_row["close"],
                "volume": source_row["volume"],
                "source_retrieved_at": source_row["source_retrieved_at"],
                "bar_status": "SOURCE_SESSION_NOT_IN_PINNED_XNYS",
                **caveats,
            }
        )

    causal_table = deterministic_table(
        pa.Table.from_pylist(causal, schema=CAUSAL_BAR_SCHEMA),
        CAUSAL_BAR_SCHEMA,
        SORT_KEYS["causal_bars"],
    )
    derived = _derive_feature_and_outcome_inputs(
        causal=causal_table,
        epoch=epoch,
        calendar_sessions=calendar_sessions,
        calendar_rows=calendar_rows,
        calendar_release_id=calendar_release_id,
    )
    return {"causal_bars": causal_table, **derived}


def _derive_feature_and_outcome_inputs(
    *,
    causal: pa.Table,
    epoch: str,
    calendar_sessions: tuple[date, ...],
    calendar_rows: Mapping[date, Mapping[str, Any]],
    calendar_release_id: str,
) -> dict[str, pa.Table]:
    """Derive only from one verified, unpooled causal-bar table."""

    require_sha256(calendar_release_id, "historical_foundation.calendar_release_id")
    if epoch not in HFDL_EPOCHS or calendar_sessions != tuple(sorted(set(calendar_sessions))):
        raise IntegrityError("historical-foundation causal epoch/calendar differs")
    if causal.schema.remove_metadata() != CAUSAL_BAR_SCHEMA or causal.num_rows == 0:
        raise IntegrityError("historical-foundation causal input schema/rows differ")
    rows = causal.to_pylist()
    source_ids = {row["source_series_id"] for row in rows}
    symbols = {row["symbol"] for row in rows}
    caveats = _caveats(epoch, calendar_release_id)
    if (
        len(source_ids) != 1
        or len(symbols) != 1
        or len({row["session"] for row in rows}) != len(rows)
        or any(
            row["source_epoch"] != epoch
            or any(row[name] != expected for name, expected in caveats.items())
            for row in rows
        )
    ):
        raise IntegrityError("historical-foundation causal input identity/caveats differ")
    calendar_set = set(calendar_sessions)
    if (
        set(calendar_sessions) - {row["session"] for row in rows}
        or any(
            (
                row["session"] in calendar_set
                and row["bar_status"] not in {
                    "OBSERVED",
                    "MISSING_SOURCE_SESSION_UNKNOWN_CAUSE",
                }
            )
            or (
                row["session"] not in calendar_set
                and row["bar_status"] != "SOURCE_SESSION_NOT_IN_PINNED_XNYS"
            )
            or (
                row["bar_status"] == "OBSERVED"
                and any(row[name] is None for name in ("open", "high", "low", "close", "volume"))
            )
            or (
                row["bar_status"] == "MISSING_SOURCE_SESSION_UNKNOWN_CAUSE"
                and any(row[name] is not None for name in ("open", "high", "low", "close", "volume"))
            )
            for row in rows
        )
    ):
        raise IntegrityError("historical-foundation causal bar-status/denominator differs")
    source_series_id = next(iter(source_ids))
    symbol = next(iter(symbols))
    require_sha256(source_series_id, "historical_foundation.causal_source_series_id")
    by_session = {row["session"]: row for row in rows}
    features: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []

    def observed(session: date | None) -> dict[str, Any] | None:
        row = by_session.get(session) if session is not None else None
        return row if row is not None and row["bar_status"] == "OBSERVED" else None

    for position, session in enumerate(calendar_sessions):
        current = observed(session)
        previous = observed(calendar_sessions[position - 1]) if position else None
        if current is None:
            feature_status = "MISSING_CURRENT_SOURCE_SESSION_UNKNOWN_CAUSE"
            values = (None, None, None, None)
        elif previous is None:
            feature_status = "MISSING_PREVIOUS_SOURCE_SESSION_OR_EPOCH_BOUNDARY"
            values = (None, None, None, None)
        else:
            feature_status = "PRICE_INPUT_READY_PIT_UNRESOLVED"
            values = (
                current["close"] / previous["close"] - 1.0,
                current["close"] / current["open"] - 1.0,
                (current["high"] - current["low"]) / current["close"],
                math.log1p(current["volume"]),
            )
        features.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "decision_session": session,
                "decision_at": calendar_rows[session]["close_at"],
                "feature_status": feature_status,
                "close_to_close_return_1": values[0],
                "intraday_return": values[1],
                "range_fraction": values[2],
                "log1p_volume": values[3],
                **caveats,
            }
        )
        if position + 5 >= len(calendar_sessions):
            entry_session = calendar_sessions[position + 1] if position + 1 < len(calendar_sessions) else None
            exit_session = None
            entry = observed(entry_session)
            exit_row = None
            status = "PENDING_OR_CROSS_EPOCH_HORIZON"
        else:
            entry_session = calendar_sessions[position + 1]
            exit_session = calendar_sessions[position + 5]
            entry = observed(entry_session)
            exit_row = observed(exit_session)
            if entry is None and exit_row is None:
                status = "MISSING_ENTRY_AND_EXIT_SOURCE_SESSIONS_UNKNOWN_CAUSE"
            elif entry is None:
                status = "MISSING_ENTRY_SOURCE_SESSION_UNKNOWN_CAUSE"
            elif exit_row is None:
                status = "MISSING_EXIT_SOURCE_SESSION_UNKNOWN_CAUSE"
            else:
                status = "BLOCKED_ACTION_AND_DELISTING_EVIDENCE"
        outcomes.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "decision_session": session,
                "entry_session": entry_session,
                "exit_session": exit_session,
                "entry_open": entry["open"] if entry else None,
                "exit_close": exit_row["close"] if exit_row else None,
                "split_normalized_price_return": None,
                "outcome_input_status": status,
                **caveats,
            }
        )

    for row in rows:
        if row["bar_status"] != "SOURCE_SESSION_NOT_IN_PINNED_XNYS":
            continue
        session = row["session"]
        features.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "decision_session": session,
                "decision_at": None,
                "feature_status": "SOURCE_SESSION_NOT_IN_PINNED_XNYS",
                "close_to_close_return_1": None,
                "intraday_return": None,
                "range_fraction": None,
                "log1p_volume": None,
                **caveats,
            }
        )
        outcomes.append(
            {
                "source_series_id": source_series_id,
                "symbol": symbol,
                "decision_session": session,
                "entry_session": None,
                "exit_session": None,
                "entry_open": None,
                "exit_close": None,
                "split_normalized_price_return": None,
                "outcome_input_status": "SOURCE_SESSION_NOT_IN_PINNED_XNYS",
                **caveats,
            }
        )

    return {
        "feature_inputs": deterministic_table(
            pa.Table.from_pylist(features, schema=FEATURE_INPUT_SCHEMA),
            FEATURE_INPUT_SCHEMA,
            SORT_KEYS["feature_inputs"],
        ),
        "outcome_inputs": deterministic_table(
            pa.Table.from_pylist(outcomes, schema=OUTCOME_INPUT_SCHEMA),
            OUTCOME_INPUT_SCHEMA,
            SORT_KEYS["outcome_inputs"],
        ),
    }


def _write_or_verify_parquet(path: Path, table: pa.Table, *, kind: str) -> None:
    canonical = deterministic_table(table, SCHEMAS[kind], SORT_KEYS[kind])
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError("historical-foundation partial Parquet is linked or invalid")
        observed = pq.read_table(path)
        if observed.schema.remove_metadata() != SCHEMAS[kind] or not observed.equals(canonical):
            raise IntegrityError("historical-foundation partial Parquet differs from deterministic rebuild")
        return
    write_deterministic_parquet(canonical, path, schema=SCHEMAS[kind], sort_keys=SORT_KEYS[kind])


def _write_or_verify_json(path: Path, payload: Mapping[str, Any]) -> None:
    expected = canonical_json_bytes(payload)
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != expected:
            raise IntegrityError("historical-foundation partial metadata differs")
        return
    atomic_write(path, expected)


def _stage_exact(stage: Path, files: set[str]) -> None:
    directories: set[str] = set()
    for relative in files:
        parent = Path(relative).parent
        while parent != Path("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(stage, files, directories)
    except ContractError as exc:
        raise IntegrityError("historical-foundation stage has missing/extra/linked content") from exc


def _base_census(
    *,
    epoch: str,
    kind: str,
    series_count: int,
    source_rows: int,
    calendar_session_count: int,
    noncalendar_source_rows: int,
    output_rows: int,
    statuses: Counter[str],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source_epoch": epoch,
        "output_kind": kind,
        "source_series_count": series_count,
        "source_rows": source_rows,
        "calendar_sessions_in_epoch": calendar_session_count,
        "calendar_symbol_session_denominator": series_count * calendar_session_count,
        "noncalendar_source_rows": noncalendar_source_rows,
        "output_rows": output_rows,
        "status_counts": dict(sorted(statuses.items())),
        "missing_status_rows": sum(
            count for status, count in statuses.items() if status.startswith("MISSING_")
        ),
        "evidence_denominator_rows": output_rows,
        "membership_evidence_available_rows": 0,
        "membership_evidence_unknown_rows": output_rows,
        "security_type_evidence_available_rows": 0,
        "security_type_evidence_unknown_rows": output_rows,
        "action_evidence_available_rows": 0,
        "action_evidence_unavailable_rows": output_rows,
        "delisting_evidence_available_rows": 0,
        "delisting_evidence_unavailable_rows": output_rows,
        "outcome_evaluable_rows": 0,
        "matured_outcome_rows": 0,
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
    }


def _provenance(
    *,
    build_id: str,
    epoch: str,
    kind: str,
    source_hfdl_release_id: str,
    source_hfdl_set_release_id: str,
    calendar_release_id: str,
    contract_id: str,
    causal_bar_release_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "build_id": build_id,
        "source_epoch": epoch,
        "output_kind": kind,
        "source_hfdl_release_id": source_hfdl_release_id,
        "source_hfdl_set_release_id": source_hfdl_set_release_id,
        "calendar_release_id": calendar_release_id,
        "causal_bar_release_id": causal_bar_release_id,
        "contract_id": contract_id,
        "quality_state": QUALITY_STATE,
        "role": ROLE,
        "evidence_class": EVIDENCE_CLASS,
        "point_in_time_safe": False,
        "point_in_time_state": POINT_IN_TIME_STATE,
        "historical_availability_state": HISTORICAL_AVAILABILITY,
        "source_adjustment": SOURCE_ADJUSTMENT,
        "membership_evidence_status": MEMBERSHIP_STATE,
        "security_type_evidence_status": SECURITY_TYPE_STATE,
        "action_evidence_status": ACTION_STATE,
        "delisting_evidence_status": DELISTING_STATE,
        "source_series_id_is_persistent_asset_identity": False,
        "epochs_may_be_pooled": False,
        "model_or_evaluation_inputs_read": False,
        "real_history_hypothesis_executed": False,
        "matured_outcomes_emitted": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }


def _checkpoint(path: Path, evidence: Mapping[str, Any]) -> dict[str, Any]:
    fields = {*evidence, "state", "release_ids", "checkpoint_id"}
    if path.exists():
        value = _read_canonical_json(path, fields, "historical-foundation checkpoint")
        if any(value[name] != expected for name, expected in evidence.items()):
            raise IntegrityError("historical-foundation checkpoint binds different inputs")
        allowed_release_keys = {
            *(f"{epoch}:{kind}" for epoch in HFDL_EPOCHS for kind in OUTPUT_KINDS),
            "bridge_set",
        }
        if (
            value["state"] not in {"BUILDING_DERIVED_ONLY", "RELEASES_COMPLETE"}
            or not isinstance(value["release_ids"], dict)
            or not set(value["release_ids"]) <= allowed_release_keys
        ):
            raise IntegrityError("historical-foundation checkpoint state/release census differs")
        for key, release_id in value["release_ids"].items():
            require_sha256(release_id, f"historical_foundation.checkpoint.{key}")
        unsigned = dict(value)
        checkpoint_id = unsigned.pop("checkpoint_id")
        if checkpoint_id != sha256_bytes(canonical_json_bytes(unsigned)):
            raise IntegrityError("historical-foundation checkpoint ID differs")
        return value
    unsigned = {**evidence, "state": "BUILDING_DERIVED_ONLY", "release_ids": {}}
    value = {**unsigned, "checkpoint_id": sha256_bytes(canonical_json_bytes(unsigned))}
    atomic_write(path, canonical_json_bytes(value))
    return value


def _write_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    unsigned = dict(checkpoint)
    unsigned.pop("checkpoint_id", None)
    checkpoint["checkpoint_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    atomic_write(path, canonical_json_bytes(checkpoint))


def _manifest(
    *,
    stage: Path,
    files: set[str],
    epoch: str,
    kind: str,
    census: Mapping[str, Any],
    created_at: str,
    implementation_hash: str,
    contract_id: str,
    environment_hash: str,
    upstream_ids: tuple[str, ...],
    event_start: str,
    event_end: str,
) -> ReleaseManifest:
    return build_manifest(
        stage,
        files,
        project="US_stocks_swing_model_v2",
        dataset=BRIDGE_DATASETS[epoch][kind],
        source_epoch=epoch,
        role=ROLE,
        quality_state=QUALITY_STATE,
        created_at=created_at,
        row_count=int(census["output_rows"]),
        event_start=event_start,
        event_end=event_end,
        upstream_release_ids=upstream_ids,
        schema_fingerprint=sha256_bytes(SCHEMAS[kind].serialize().to_pybytes()),
        code_hash=implementation_hash,
        config_hash=contract_id,
        environment_hash=environment_hash,
    )


def publish_hfdl_historical_foundation(
    *,
    hfdl_epoch_set_release_directory: Path,
    calendar_release_directory: Path,
    accepted_release_root: Path,
    derived_work_root: Path,
    created_at: str,
    hfdl_synthetic_permit: SyntheticOnlyPermit | None = None,
) -> HistoricalFoundationResult:
    """Build only mechanical legacy-discovery inputs; never execute history."""

    parse_utc_z(created_at, "historical_foundation.created_at")
    accepted = Path(accepted_release_root)
    work = Path(derived_work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("historical-foundation roots must be absolute")
    accepted.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    reject_link(accepted)
    reject_link(work)
    if accepted.resolve(strict=True) == work.resolve(strict=True) or accepted.resolve(strict=True) in work.resolve(strict=True).parents or work.resolve(strict=True) in accepted.resolve(strict=True).parents:
        raise ContractError("accepted and derived-work roots must be separate")
    contract, contract_id = _load_contract()
    implementation_hash = _implementation_hash()
    environment_hash = _environment_hash()
    hfdl = verify_hfdl_legacy_publication(
        Path(hfdl_epoch_set_release_directory),
        accepted_release_root=accepted,
        synthetic_permit=hfdl_synthetic_permit,
    )
    calendar = load_xnys_calendar_release(
        Path(calendar_release_directory),
        accepted_release_root=accepted,
    )
    hfdl_set_manifest = verify_accepted_release(hfdl.epoch_set_release_directory, accepted_root=accepted)
    epoch_manifests = {
        epoch: verify_accepted_release(directory, accepted_root=accepted)
        for epoch, directory in hfdl.epoch_release_directories.items()
    }
    build_unsigned = {
        "schema_version": 1,
        "hfdl_epoch_set_release_id": hfdl_set_manifest.release_id,
        "hfdl_epoch_release_ids": {
            epoch: epoch_manifests[epoch].release_id for epoch in HFDL_EPOCHS
        },
        "calendar_release_id": calendar.calendar.release_id,
        "contract_id": contract_id,
        "implementation_hash": implementation_hash,
        "environment_hash": environment_hash,
        "created_at": created_at,
        "synthetic_permit_id": hfdl_synthetic_permit.permit_id if hfdl_synthetic_permit else None,
    }
    build_id = sha256_bytes(canonical_json_bytes(build_unsigned))
    build_root = work / "hfdl_foundation" / build_id[:32]
    build_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = build_root / "checkpoint.json"
    with ExclusiveFileLock(
        work / ".locks" / f"hfdl-foundation-{build_id}.lock",
        allowed_root=work,
    ):
        checkpoint = _checkpoint(checkpoint_path, {**build_unsigned, "build_id": build_id})
        if checkpoint["state"] == "RELEASES_COMPLETE":
            set_id = checkpoint["release_ids"].get("bridge_set")
            require_sha256(set_id, "historical_foundation.bridge_set_release_id")
            return load_hfdl_historical_foundation(
                accepted / BRIDGE_SET_DATASET / set_id,
                accepted_release_root=accepted,
                hfdl_synthetic_permit=hfdl_synthetic_permit,
            )

        publisher = AtomicReleasePublisher(accepted)
        epoch_release_directories: dict[str, dict[str, Path]] = {}
        epoch_release_manifests: dict[str, dict[str, ReleaseManifest]] = {}
        for epoch in HFDL_EPOCHS:
            source_directory = hfdl.epoch_release_directories[epoch]
            source_manifest = epoch_manifests[epoch]
            calendar_sessions, calendar_rows = _calendar_context(
                calendar,
                event_start=str(source_manifest.event_start),
                event_end=str(source_manifest.event_end),
            )
            stages = {
                kind: build_root / "stages" / BRIDGE_DATASETS[epoch][kind]
                for kind in OUTPUT_KINDS
            }
            for stage in stages.values():
                stage.mkdir(parents=True, exist_ok=True)
            statuses = {kind: Counter() for kind in OUTPUT_KINDS}
            output_rows = Counter()
            source_rows = 0
            noncalendar_rows = 0
            indexes = _source_index(
                source_directory,
                expected_event_start=str(source_manifest.event_start),
                expected_event_end=str(source_manifest.event_end),
            )
            for index in indexes:
                source_path = source_directory.joinpath(*safe_relative_path(index["data_path"]).parts)
                source_table = pq.read_table(source_path)
                source_rows += source_table.num_rows
                noncalendar_rows += sum(
                    session not in calendar_rows
                    for session in source_table.column("session").to_pylist()
                )
                derived = _derive_symbol_tables(
                    source_series_id=index["pair_id"],
                    symbol=index["symbol"],
                    source=source_table,
                    epoch=epoch,
                    calendar_sessions=calendar_sessions,
                    calendar_rows=calendar_rows,
                    calendar_release_id=calendar.calendar.release_id,
                )
                for kind, table in derived.items():
                    relative = f"data/{index['pair_id']}.parquet"
                    _write_or_verify_parquet(stages[kind] / relative, table, kind=kind)
                    output_rows[kind] += table.num_rows
                    statuses[kind].update(table.column(STATUS_COLUMNS[kind]).to_pylist())
            censuses = {
                kind: _base_census(
                    epoch=epoch,
                    kind=kind,
                    series_count=len(indexes),
                    source_rows=source_rows,
                    calendar_session_count=len(calendar_sessions),
                    noncalendar_source_rows=noncalendar_rows,
                    output_rows=output_rows[kind],
                    statuses=statuses[kind],
                )
                for kind in OUTPUT_KINDS
            }
            data_files = {f"data/{index['pair_id']}.parquet" for index in indexes}
            files_by_kind = {
                kind: data_files | {"census.json", "provenance.json"}
                for kind in OUTPUT_KINDS
            }
            for kind in OUTPUT_KINDS:
                _write_or_verify_json(stages[kind] / "census.json", censuses[kind])

            causal_kind = "causal_bars"
            causal_provenance = _provenance(
                build_id=build_id,
                epoch=epoch,
                kind=causal_kind,
                source_hfdl_release_id=source_manifest.release_id,
                source_hfdl_set_release_id=hfdl_set_manifest.release_id,
                calendar_release_id=calendar.calendar.release_id,
                contract_id=contract_id,
                causal_bar_release_id=None,
            )
            _write_or_verify_json(stages[causal_kind] / "provenance.json", causal_provenance)
            _stage_exact(stages[causal_kind], files_by_kind[causal_kind])
            causal_manifest = _manifest(
                stage=stages[causal_kind],
                files=files_by_kind[causal_kind],
                epoch=epoch,
                kind=causal_kind,
                census=censuses[causal_kind],
                created_at=created_at,
                implementation_hash=implementation_hash,
                contract_id=contract_id,
                environment_hash=environment_hash,
                upstream_ids=(source_manifest.release_id, calendar.calendar.release_id),
                event_start=str(source_manifest.event_start),
                event_end=str(source_manifest.event_end),
            )
            causal_directory = publisher.publish(stages[causal_kind], causal_manifest)
            checkpoint["release_ids"][f"{epoch}:{causal_kind}"] = causal_manifest.release_id
            _write_checkpoint(checkpoint_path, checkpoint)

            epoch_release_directories[epoch] = {causal_kind: causal_directory}
            epoch_release_manifests[epoch] = {causal_kind: causal_manifest}
            for kind in ("feature_inputs", "outcome_inputs"):
                provenance = _provenance(
                    build_id=build_id,
                    epoch=epoch,
                    kind=kind,
                    source_hfdl_release_id=source_manifest.release_id,
                    source_hfdl_set_release_id=hfdl_set_manifest.release_id,
                    calendar_release_id=calendar.calendar.release_id,
                    contract_id=contract_id,
                    causal_bar_release_id=causal_manifest.release_id,
                )
                _write_or_verify_json(stages[kind] / "provenance.json", provenance)
                _stage_exact(stages[kind], files_by_kind[kind])
                manifest = _manifest(
                    stage=stages[kind],
                    files=files_by_kind[kind],
                    epoch=epoch,
                    kind=kind,
                    census=censuses[kind],
                    created_at=created_at,
                    implementation_hash=implementation_hash,
                    contract_id=contract_id,
                    environment_hash=environment_hash,
                    upstream_ids=(causal_manifest.release_id,),
                    event_start=str(source_manifest.event_start),
                    event_end=str(source_manifest.event_end),
                )
                directory = publisher.publish(stages[kind], manifest)
                checkpoint["release_ids"][f"{epoch}:{kind}"] = manifest.release_id
                _write_checkpoint(checkpoint_path, checkpoint)
                epoch_release_directories[epoch][kind] = directory
                epoch_release_manifests[epoch][kind] = manifest

        set_payload = {
            "schema_version": 1,
            "build_id": build_id,
            "publication_state": "COMPLETE_SIX_PHYSICAL_RELEASES_TWO_UNPOOLED_EPOCHS",
            "contract": contract,
            "contract_id": contract_id,
            "source_hfdl_set": {
                "dataset": HFDL_SET_DATASET,
                "release_id": hfdl_set_manifest.release_id,
            },
            "calendar": {
                "dataset": "xnys_sessions",
                "release_id": calendar.calendar.release_id,
            },
            "epochs": {
                epoch: {
                    kind: {
                        "dataset": epoch_release_manifests[epoch][kind].dataset,
                        "release_id": epoch_release_manifests[epoch][kind].release_id,
                        "row_count": epoch_release_manifests[epoch][kind].row_count,
                    }
                    for kind in OUTPUT_KINDS
                }
                for epoch in HFDL_EPOCHS
            },
            "synthetic_permit_id": hfdl_synthetic_permit.permit_id if hfdl_synthetic_permit else None,
            "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
            "epochs_may_be_pooled": False,
            "matured_outcomes_emitted": False,
            "model_or_evaluation_inputs_read": False,
            "real_history_hypothesis_executed": False,
            "alpha_evidence": False,
            "candidate_eligible": False,
        }
        set_stage = build_root / "stages" / BRIDGE_SET_DATASET
        set_stage.mkdir(parents=True, exist_ok=True)
        _write_or_verify_json(set_stage / "bridge_set.json", set_payload)
        set_files = {"bridge_set.json"}
        _stage_exact(set_stage, set_files)
        upstream = sorted(
            {
                hfdl_set_manifest.release_id,
                calendar.calendar.release_id,
                *(
                    manifest.release_id
                    for manifests in epoch_release_manifests.values()
                    for manifest in manifests.values()
                ),
            }
        )
        set_manifest = build_manifest(
            set_stage,
            set_files,
            project="US_stocks_swing_model_v2",
            dataset=BRIDGE_SET_DATASET,
            source_epoch=BRIDGE_SET_SOURCE_EPOCH,
            role=ROLE,
            quality_state=QUALITY_STATE,
            created_at=created_at,
            row_count=6,
            event_start=None,
            event_end=None,
            upstream_release_ids=upstream,
            schema_fingerprint=sha256_bytes(canonical_json_bytes(set_payload)),
            code_hash=implementation_hash,
            config_hash=contract_id,
            environment_hash=environment_hash,
        )
        set_directory = publisher.publish(set_stage, set_manifest)
        checkpoint["state"] = "RELEASES_COMPLETE"
        checkpoint["release_ids"]["bridge_set"] = set_manifest.release_id
        _write_checkpoint(checkpoint_path, checkpoint)
        return load_hfdl_historical_foundation(
            set_directory,
            accepted_release_root=accepted,
            hfdl_synthetic_permit=hfdl_synthetic_permit,
        )


def load_hfdl_historical_foundation(
    bridge_set_release_directory: Path,
    *,
    accepted_release_root: Path,
    hfdl_synthetic_permit: SyntheticOnlyPermit | None = None,
) -> HistoricalFoundationResult:
    """Verify all six physical releases and recompute them from accepted inputs."""

    accepted = Path(accepted_release_root)
    set_directory = Path(bridge_set_release_directory)
    set_manifest = verify_accepted_release(set_directory, accepted_root=accepted)
    if (
        set_manifest.dataset != BRIDGE_SET_DATASET
        or set_manifest.source_epoch != BRIDGE_SET_SOURCE_EPOCH
        or set_manifest.role != ROLE
        or set_manifest.quality_state != QUALITY_STATE
        or set_manifest.row_count != 6
    ):
        raise IntegrityError("historical-foundation set manifest differs")
    if {entry.path for entry in set_manifest.files} != {"bridge_set.json"}:
        raise IntegrityError(
            "historical-foundation set payload census must equal {bridge_set.json}"
        )
    payload = _read_canonical_json(
        set_directory / "bridge_set.json",
        {
            "schema_version",
            "build_id",
            "publication_state",
            "contract",
            "contract_id",
            "source_hfdl_set",
            "calendar",
            "epochs",
            "synthetic_permit_id",
            "historical_evidence_scope",
            "epochs_may_be_pooled",
            "matured_outcomes_emitted",
            "model_or_evaluation_inputs_read",
            "real_history_hypothesis_executed",
            "alpha_evidence",
            "candidate_eligible",
        },
        "historical-foundation bridge set",
    )
    contract, contract_id = _load_contract()
    implementation_hash = _implementation_hash()
    environment_hash = _environment_hash()
    if (
        set_manifest.project != "US_stocks_swing_model_v2"
        or set_manifest.code_hash != implementation_hash
        or set_manifest.config_hash != contract_id
        or set_manifest.environment_hash != environment_hash
        or set_manifest.schema_fingerprint != sha256_bytes(canonical_json_bytes(payload))
        or payload["publication_state"] != "COMPLETE_SIX_PHYSICAL_RELEASES_TWO_UNPOOLED_EPOCHS"
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or not isinstance(payload["build_id"], str)
        or not isinstance(payload["contract"], dict)
        or not isinstance(payload["epochs"], dict)
        or payload["contract"] != contract
        or payload["contract_id"] != contract_id
        or payload["historical_evidence_scope"] != "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
        or any(
            payload[name] is not False
            for name in (
                "epochs_may_be_pooled",
                "matured_outcomes_emitted",
                "model_or_evaluation_inputs_read",
                "real_history_hypothesis_executed",
                "alpha_evidence",
                "candidate_eligible",
            )
        )
        or set(payload["epochs"]) != set(HFDL_EPOCHS)
    ):
        raise IntegrityError("historical-foundation set loses its legacy/PIT/no-model boundary")
    require_sha256(payload["build_id"], "historical_foundation.build_id")
    require_sha256(payload["contract_id"], "historical_foundation.contract_id")
    synthetic_id = payload["synthetic_permit_id"]
    if synthetic_id is not None:
        if hfdl_synthetic_permit is None or hfdl_synthetic_permit.permit_id != synthetic_id:
            raise ContractError("synthetic historical-foundation set requires its exact HFDL permit")
    hfdl_binding = payload["source_hfdl_set"]
    calendar_binding = payload["calendar"]
    if (
        not isinstance(hfdl_binding, dict)
        or set(hfdl_binding) != {"dataset", "release_id"}
        or hfdl_binding["dataset"] != HFDL_SET_DATASET
        or not isinstance(calendar_binding, dict)
        or set(calendar_binding) != {"dataset", "release_id"}
        or calendar_binding["dataset"] != "xnys_sessions"
    ):
        raise IntegrityError("historical-foundation upstream bindings differ")
    require_sha256(hfdl_binding["release_id"], "historical_foundation.hfdl_set_release_id")
    require_sha256(calendar_binding["release_id"], "historical_foundation.calendar_release_id")
    hfdl = verify_hfdl_legacy_publication(
        accepted / hfdl_binding["dataset"] / hfdl_binding["release_id"],
        accepted_release_root=accepted,
        synthetic_permit=hfdl_synthetic_permit,
    )
    calendar = load_xnys_calendar_release(
        accepted / calendar_binding["dataset"] / calendar_binding["release_id"],
        accepted_release_root=accepted,
    )
    hfdl_set_manifest = verify_accepted_release(hfdl.epoch_set_release_directory, accepted_root=accepted)
    result_directories: dict[str, dict[str, Path]] = {}
    derived_ids: set[str] = set()
    for epoch in HFDL_EPOCHS:
        source_directory = hfdl.epoch_release_directories[epoch]
        source_manifest = verify_accepted_release(source_directory, accepted_root=accepted)
        calendar_sessions, calendar_rows = _calendar_context(
            calendar,
            event_start=str(source_manifest.event_start),
            event_end=str(source_manifest.event_end),
        )
        indexes = _source_index(
            source_directory,
            expected_event_start=str(source_manifest.event_start),
            expected_event_end=str(source_manifest.event_end),
        )
        bindings = payload["epochs"][epoch]
        if not isinstance(bindings, dict) or set(bindings) != set(OUTPUT_KINDS):
            raise IntegrityError("historical-foundation epoch bindings differ")
        manifests: dict[str, ReleaseManifest] = {}
        directories: dict[str, Path] = {}
        for kind in OUTPUT_KINDS:
            binding = bindings[kind]
            if not isinstance(binding, dict) or set(binding) != {"dataset", "release_id", "row_count"}:
                raise IntegrityError("historical-foundation release binding fields differ")
            require_sha256(
                binding["release_id"],
                f"historical_foundation.{epoch}.{kind}.release_id",
            )
            if type(binding["row_count"]) is not int or binding["row_count"] < 0:
                raise IntegrityError("historical-foundation release binding row count differs")
            if binding["dataset"] != BRIDGE_DATASETS[epoch][kind]:
                raise IntegrityError("historical-foundation release silently pools/relabels an epoch")
            directory = accepted / binding["dataset"] / binding["release_id"]
            manifest = verify_accepted_release(directory, accepted_root=accepted)
            if (
                manifest.project != "US_stocks_swing_model_v2"
                or manifest.dataset != BRIDGE_DATASETS[epoch][kind]
                or manifest.source_epoch != epoch
                or manifest.role != ROLE
                or manifest.quality_state != QUALITY_STATE
                or manifest.row_count != binding["row_count"]
                or manifest.schema_fingerprint
                != sha256_bytes(SCHEMAS[kind].serialize().to_pybytes())
                or manifest.code_hash != implementation_hash
                or manifest.config_hash != contract_id
                or manifest.environment_hash != environment_hash
                or manifest.event_start != source_manifest.event_start
                or manifest.event_end != source_manifest.event_end
            ):
                raise IntegrityError("historical-foundation physical release contract differs")
            manifests[kind] = manifest
            directories[kind] = directory
            derived_ids.add(manifest.release_id)
        if set(manifests["causal_bars"].upstream_release_ids) != {
            source_manifest.release_id,
            calendar.calendar.release_id,
        }:
            raise IntegrityError("causal-bar release upstream binding differs")
        for kind in ("feature_inputs", "outcome_inputs"):
            if manifests[kind].upstream_release_ids != (manifests["causal_bars"].release_id,):
                raise IntegrityError("feature/outcome release does not bind exactly its causal bars")

        statuses = {kind: Counter() for kind in OUTPUT_KINDS}
        output_rows = Counter()
        source_rows = 0
        noncalendar_rows = 0
        for index in indexes:
            source_table = pq.read_table(
                source_directory.joinpath(*safe_relative_path(index["data_path"]).parts)
            )
            source_rows += source_table.num_rows
            noncalendar_rows += sum(
                session not in calendar_rows
                for session in source_table.column("session").to_pylist()
            )
            expected = _derive_symbol_tables(
                source_series_id=index["pair_id"],
                symbol=index["symbol"],
                source=source_table,
                epoch=epoch,
                calendar_sessions=calendar_sessions,
                calendar_rows=calendar_rows,
                calendar_release_id=calendar.calendar.release_id,
            )
            relative = f"data/{index['pair_id']}.parquet"
            observed_causal = pq.read_table(directories["causal_bars"] / relative)
            if (
                observed_causal.schema.remove_metadata() != CAUSAL_BAR_SCHEMA
                or not observed_causal.equals(expected["causal_bars"])
            ):
                raise IntegrityError("historical-foundation causal bars differ from source recomputation")
            expected_downstream = _derive_feature_and_outcome_inputs(
                causal=observed_causal,
                epoch=epoch,
                calendar_sessions=calendar_sessions,
                calendar_rows=calendar_rows,
                calendar_release_id=calendar.calendar.release_id,
            )
            observed_by_kind = {"causal_bars": observed_causal}
            for kind in ("feature_inputs", "outcome_inputs"):
                observed = pq.read_table(directories[kind] / relative)
                if (
                    observed.schema.remove_metadata() != SCHEMAS[kind]
                    or not observed.equals(expected_downstream[kind])
                ):
                    raise IntegrityError(
                        "historical-foundation downstream data differs from accepted causal bars"
                    )
                observed_by_kind[kind] = observed
            for kind, observed in observed_by_kind.items():
                output_rows[kind] += observed.num_rows
                statuses[kind].update(observed.column(STATUS_COLUMNS[kind]).to_pylist())
        expected_data_files = {f"data/{index['pair_id']}.parquet" for index in indexes}
        for kind in OUTPUT_KINDS:
            expected_census = _base_census(
                epoch=epoch,
                kind=kind,
                series_count=len(indexes),
                source_rows=source_rows,
                calendar_session_count=len(calendar_sessions),
                noncalendar_source_rows=noncalendar_rows,
                output_rows=output_rows[kind],
                statuses=statuses[kind],
            )
            census = _read_canonical_json(
                directories[kind] / "census.json",
                set(expected_census),
                "historical-foundation census",
            )
            if census != expected_census or manifests[kind].row_count != output_rows[kind]:
                raise IntegrityError("historical-foundation census/denominator differs")
            expected_provenance = _provenance(
                build_id=payload["build_id"],
                epoch=epoch,
                kind=kind,
                source_hfdl_release_id=source_manifest.release_id,
                source_hfdl_set_release_id=hfdl_set_manifest.release_id,
                calendar_release_id=calendar.calendar.release_id,
                contract_id=contract_id,
                causal_bar_release_id=(
                    None if kind == "causal_bars" else manifests["causal_bars"].release_id
                ),
            )
            provenance = _read_canonical_json(
                directories[kind] / "provenance.json",
                set(expected_provenance),
                "historical-foundation provenance",
            )
            if provenance != expected_provenance:
                raise IntegrityError("historical-foundation provenance differs")
            declared = {entry.path for entry in manifests[kind].files}
            if declared != expected_data_files | {"census.json", "provenance.json"}:
                raise IntegrityError("historical-foundation physical file census differs")
        result_directories[epoch] = directories

    expected_upstream = {
        hfdl_set_manifest.release_id,
        calendar.calendar.release_id,
        *derived_ids,
    }
    if set(set_manifest.upstream_release_ids) != expected_upstream or len(derived_ids) != 6:
        raise IntegrityError("historical-foundation set does not bind exactly six physical releases")
    return HistoricalFoundationResult(
        build_id=payload["build_id"],
        epoch_release_directories=result_directories,
        bridge_set_release_directory=set_directory,
    )
