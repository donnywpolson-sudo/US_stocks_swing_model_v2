from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa
import pyarrow.parquet as pq

from ..canonical.alpaca import _accept_native_bar
from ..canonical.parquet import deterministic_parquet_bytes, deterministic_table
from ..clock import TrustedClock, require_trusted_clock
from ..common import (
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, IntegrityError
from ..exchange_calendar import load_xnys_calendar_release
from ..releases import AtomicReleasePublisher, ReleaseFile, ReleaseManifest
from .alpaca import AlpacaBarsPolicy, AlpacaBarsRequest, guarded_fetch_landed_pages
from .alpaca_canonical_bars import (
    ACTIVE_ALPACA_SCHEMA,
    BARS_FILENAME,
    DATASET,
    PROJECT,
    QUALITY_STATE,
    RECEIPT_FILENAME,
    ROLE,
    SCHEMA_FINGERPRINT,
    SOURCE_EPOCH,
    SOURCE_NAME,
    _active_source_binding,
    _closure,
    _identity_bindings,
    _json_object,
    _prepare_publication_stage,
    _qualification_binding,
    _repository_binding,
    verify_canonical_bars_release,
)
from .network_execution import NetworkRequestPlan, start_local_network_execution
from .snapshots import AsReceivedSnapshotStore, LandedSnapshot, NetworkAcquisitionRegistry


POLICY_PATH = "config/alpaca_canonical_bars_successor_policy.json"
PUBLICATION_CONFIRMATION_TOKEN = (
    "ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION_APPROVED"
)
PUBLICATION_CONFIRMATION_VALUE = "YES"
EXPECTED_DELTA_SESSIONS = (
    date(2026, 7, 31),
)
EXPECTED_CUMULATIVE_SESSIONS = (date(2026, 7, 30), *EXPECTED_DELTA_SESSIONS)

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_canonical_bars_successor.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_canonical_bars.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/network_execution.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/canonical/alpaca.py",
    "src/us_stocks_swing_model_v2/canonical/parquet.py",
    "src/us_stocks_swing_model_v2/cli/accumulate_canonical_bars.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    POLICY_PATH,
    "config/alpaca_canonical_bars_network_registry.json",
    "config/sources.json",
)


@dataclass(frozen=True)
class SuccessorBarsCandidate:
    table: pa.Table
    acquisition_plan_id: str
    network_request_plan_id: str
    predecessor_release_id: str
    predecessor_bars_sha256: str
    snapshot_id: str
    snapshot_raw_sha256: str
    requested_at: datetime
    retrieved_at: datetime
    delta_row_count: int
    row_count: int
    symbols: tuple[str, ...]
    delta_sessions: tuple[date, ...]
    sessions: tuple[date, ...]
    trust_eligible: bool
    candidate_id: str


@dataclass(frozen=True)
class SuccessorBarsPublication:
    publication_plan_id: str
    release_id: str
    receipt_id: str
    release_directory: Path
    work_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _load_policy(root: Path) -> dict[str, Any]:
    policy = _json_object(root / POLICY_PATH, label="successor bars policy")
    expected_window = {
        "start": "2026-07-31T04:00:00Z",
        "end": "2026-08-01T03:59:59Z",
        "sessions": [item.isoformat() for item in EXPECTED_DELTA_SESSIONS],
        "row_count": 2,
    }
    expected_request = {
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars",
        "feed": "sip",
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "sort": "asc",
        "limit": 10000,
        "minimum_end_lag_minutes": 20,
        "timeout_seconds": 30,
        "host_timeout_seconds": 120,
        "max_pages": 1,
        "max_response_bytes": 1048576,
    }
    if (
        policy.get("schema_version") != 1
        or policy.get("project") != PROJECT
        or policy.get("policy_type")
        != "BOUNDED_ALPACA_CANONICAL_BARS_SUCCESSOR"
        or policy.get("source_key") != "alpaca_basic_delayed_sip"
        or policy.get("source_name") != SOURCE_NAME
        or policy.get("dataset") != DATASET
        or policy.get("source_epoch") != SOURCE_EPOCH
        or policy.get("role") != ROLE
        or policy.get("quality_state") != QUALITY_STATE
        or policy.get("diagnostic_only") is not True
        or policy.get("symbols") != ["AAPL", "SPY"]
        or policy.get("delta_window") != expected_window
        or policy.get("cumulative_sessions")
        != [item.isoformat() for item in EXPECTED_CUMULATIVE_SESSIONS]
        or policy.get("cumulative_row_count") != 4
        or policy.get("earliest_execution_at") != "2026-08-01T04:19:59Z"
        or policy.get("request_contract") != expected_request
    ):
        raise ContractError("successor bars policy identity differs")
    predecessor = policy.get("predecessor")
    if (
        not isinstance(predecessor, dict)
        or predecessor.get("row_count") != 2
        or predecessor.get("event_start") != "2026-07-30"
        or predecessor.get("event_end") != "2026-07-30"
    ):
        raise ContractError("successor predecessor policy differs")
    for field in ("release_id", "receipt_id", "bars_sha256"):
        value = predecessor.get(field)
        require_sha256(value, f"successor.predecessor.{field}")
    return policy


def _calendar_binding(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> dict[str, object]:
    binding = policy["calendar_release"]
    loaded = load_xnys_calendar_release(
        root / binding["relative_directory"],
        accepted_release_root=accepted_root,
    )
    if loaded.calendar.release_id != binding["release_id"]:
        raise IntegrityError("successor calendar release binding differs")
    expected = set(EXPECTED_DELTA_SESSIONS)
    rows = [
        row
        for row in loaded.schedule.to_pylist()
        if row["session"] == date(2026, 7, 31)
    ]
    if [row["session"] for row in rows] != list(EXPECTED_DELTA_SESSIONS):
        raise IntegrityError("successor pinned calendar session binding differs")
    if set(row["session"] for row in rows) != expected:
        raise IntegrityError("successor pinned calendar session census differs")
    return {
        "release_id": loaded.calendar.release_id,
        "sessions": [item.isoformat() for item in EXPECTED_DELTA_SESSIONS],
        "close_ats": [iso_z(row["close_at"]) for row in rows],
    }


def _read_predecessor(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> tuple[dict[str, object], pa.Table]:
    binding = policy["predecessor"]
    directory = root / binding["relative_directory"]
    manifest, receipt = verify_canonical_bars_release(
        directory,
        accepted_root=accepted_root,
        expected_release_id=binding["release_id"],
    )
    bars_entry = next(entry for entry in manifest.files if entry.path == BARS_FILENAME)
    if (
        receipt["receipt_id"] != binding["receipt_id"]
        or bars_entry.sha256 != binding["bars_sha256"]
        or manifest.row_count != binding["row_count"]
        or manifest.event_start != binding["event_start"]
        or manifest.event_end != binding["event_end"]
    ):
        raise IntegrityError("successor predecessor release binding differs")
    table = pq.read_table(directory / BARS_FILENAME)
    return dict(binding), table


def _context(root: Path, *, require_clean: bool) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    policy = _load_policy(resolved)
    if policy["diagnostic_only"]:
        raise ContractError("July canonical-bars successor policy is diagnostic-only and cannot acquire or publish production evidence")
    accepted_root = (resolved / policy["outputs"]["accepted_root"]).resolve(
        strict=True
    )
    registry = NetworkAcquisitionRegistry.load(
        resolved / policy["network_registry"],
        allowed_root=resolved,
    )
    if set(registry.allowed_origin_paths) != {SOURCE_NAME}:
        raise ContractError("successor bars registry source census differs")
    repository = (
        _repository_binding(resolved)
        if require_clean
        else {
            "root": str(resolved),
            "branch": "SYNTHETIC_ONLY",
            "commit": "0" * 64,
            "tree": "0" * 64,
        }
    )
    predecessor, predecessor_table = _read_predecessor(
        resolved,
        policy,
        accepted_root,
    )
    return {
        "root": resolved,
        "policy": policy,
        "repository": repository,
        "registry": registry,
        "source": _active_source_binding(resolved, policy),
        "qualification": _qualification_binding(resolved, policy, accepted_root),
        "identity": _identity_bindings(resolved, policy, accepted_root),
        "calendar": _calendar_binding(resolved, policy, accepted_root),
        "predecessor": predecessor,
        "predecessor_table": predecessor_table,
        "code_closure": _closure(resolved, CODE_CLOSURE_PATHS),
        "config_closure": _closure(resolved, CONFIG_CLOSURE_PATHS),
        "environment_id": sha256_file(resolved / "config/environment.lock.json"),
    }


def build_synthetic_predecessor_table(
    asset_ids: Mapping[str, str],
) -> pa.Table:
    event_at = parse_utc_z("2026-07-30T04:00:00Z", "predecessor.event_at")
    retrieved_at = parse_utc_z(
        "2026-07-31T00:31:00Z",
        "predecessor.retrieved_at",
    )
    rows = []
    for index, symbol in enumerate(("AAPL", "SPY"), start=1):
        base = 200.0 if symbol == "AAPL" else 600.0
        rows.append(
            {
                "provider_symbol": symbol,
                "asset_id": asset_ids[symbol],
                "session": date(2026, 7, 30),
                "open": base,
                "high": base + 4.0,
                "low": base - 1.0,
                "close": base + 3.0,
                "volume": 1000 * index,
                "trade_count": 100 * index,
                "vwap": base + 2.0,
                "bar_event_at": event_at,
                "available_at": retrieved_at,
                "retrieved_at": retrieved_at,
                "source_snapshot_id": str(index) * 64,
                "request_plan_id": str(index + 2) * 64,
                "source_epoch": "SYNTHETIC_ONLY",
                "evidence_class": "SYNTHETIC_MECHANICAL",
                "quality_state": "NOT_TRUST_ELIGIBLE",
                "point_in_time_safe": False,
            }
        )
    return deterministic_table(
        pa.Table.from_pylist(rows, schema=ACTIVE_ALPACA_SCHEMA),
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )


def _fixture_context(root: Path, predecessor_table: pa.Table) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    policy = _load_policy(resolved)
    registry = NetworkAcquisitionRegistry.load(
        resolved / policy["network_registry"],
        allowed_root=resolved,
    )
    identity = policy["identity_release"]
    predecessor_bytes = deterministic_parquet_bytes(
        predecessor_table,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    predecessor = {
        **policy["predecessor"],
        "release_id": "f" * 64,
        "receipt_id": "e" * 64,
        "bars_sha256": sha256_bytes(predecessor_bytes),
        "relative_directory": "SYNTHETIC_ONLY",
    }
    return {
        "root": resolved,
        "policy": policy,
        "repository": {
            "root": str(resolved),
            "branch": "SYNTHETIC_ONLY",
            "commit": "0" * 64,
            "tree": "0" * 64,
        },
        "registry": registry,
        "source": {
            "source_key": policy["source_key"],
            "diagnostic_only": True,
            "active": False,
        },
        "qualification": {
            "release_id": policy["qualification_release"]["release_id"],
            "receipt_id": policy["qualification_release"]["receipt_id"],
            "selected_feed": "sip",
        },
        "identity": (
            {
                "release_id": identity["release_id"],
                "identity_snapshot_id": identity["identity_snapshot_id"],
            },
            dict(sorted(identity["asset_ids"].items())),
        ),
        "calendar": {
            "release_id": policy["calendar_release"]["release_id"],
            "sessions": [item.isoformat() for item in EXPECTED_DELTA_SESSIONS],
            "close_ats": [
                "2026-07-31T20:00:00Z",
            ],
        },
        "predecessor": predecessor,
        "predecessor_table": predecessor_table,
        "code_closure": _closure(resolved, CODE_CLOSURE_PATHS),
        "config_closure": _closure(resolved, CONFIG_CLOSURE_PATHS),
        "environment_id": sha256_file(resolved / "config/environment.lock.json"),
    }


def _network_plan(
    context: Mapping[str, object],
    *,
    requested_at: datetime,
) -> tuple[AlpacaBarsRequest, AlpacaBarsPolicy, NetworkRequestPlan]:
    policy = context["policy"]
    contract = policy["request_contract"]
    request = AlpacaBarsRequest(
        symbols=tuple(policy["symbols"]),
        start=parse_utc_z(policy["delta_window"]["start"], "successor.start"),
        end=parse_utc_z(policy["delta_window"]["end"], "successor.end"),
        requested_at=requested_at,
        limit=contract["limit"],
    )
    bars_policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint=contract["endpoint"],
    )
    network_plan = NetworkRequestPlan.create(
        registry=context["registry"],
        source=SOURCE_NAME,
        initial_url=request.url(bars_policy),
        timeout_seconds=contract["timeout_seconds"],
        max_response_bytes=contract["max_response_bytes"],
        max_pages=1,
        pagination_parameter="page_token",
    )
    return request, bars_policy, network_plan


def _build_plan(context: Mapping[str, object], *, synthetic: bool) -> dict[str, object]:
    policy = context["policy"]
    planned_at = parse_utc_z(
        policy["earliest_execution_at"],
        "successor.earliest_execution_at",
    )
    request, _, network_plan = _network_plan(context, requested_at=planned_at)
    unsigned = {
        "schema_version": 1,
        "mode": (
            "SYNTHETIC_ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN"
            if synthetic
            else "ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN_ONLY"
        ),
        "repository": context["repository"],
        "source": context["source"],
        "qualification": context["qualification"],
        "identity": context["identity"][0],
        "asset_ids": context["identity"][1],
        "calendar": context["calendar"],
        "predecessor": context["predecessor"],
        "request": {
            "method": "GET",
            "url": network_plan.initial_url,
            "symbols": list(request.symbols),
            "start": policy["delta_window"]["start"],
            "end": policy["delta_window"]["end"],
            "feed": "sip",
            "timeframe": "1Day",
            "adjustment": "raw",
            "asof": None,
            "sort": "asc",
            "limit": 10000,
            "expected_sessions": policy["delta_window"]["sessions"],
            "expected_delta_rows": 2,
        },
        "cumulative": {
            "sessions": policy["cumulative_sessions"],
            "row_count": 4,
        },
        "earliest_execution_at": policy["earliest_execution_at"],
        "network_request_plan": network_plan.as_dict(),
        "host_timeout_seconds": 120,
        "code_closure": context["code_closure"],
        "config_closure": context["config_closure"],
        "environment_id": context["environment_id"],
        "outputs": {
            "snapshot_store": str(
                Path(context["root"]) / policy["outputs"]["snapshot_store"]
            ),
            "snapshot_count": 1,
            "snapshot_files": policy["outputs"]["snapshot_files"],
            "canonical_candidate": "PROCESS_LOCAL_METADATA_ONLY",
            "accepted_release": "NOT_PUBLISHED",
        },
        "authorities": {
            "network_calls": 0,
            "credential_access": False,
            "snapshot_write": False,
            "canonical_release_publication": False,
            "source_activation": False,
            "eligible_universe": False,
            "research": False,
        },
    }
    return {
        **unsigned,
        "acquisition_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_successor_bars_acquisition_plan(
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    return _build_plan(_context(root, require_clean=True), synthetic=False)


def build_successor_bars_fixture_plan(
    *,
    repo_root: Path | None = None,
    predecessor_table: pa.Table,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    return _build_plan(
        _fixture_context(root, predecessor_table),
        synthetic=True,
    )


def _validate_plan(acquisition_plan: Mapping[str, Any]) -> str:
    if acquisition_plan.get("mode") not in {
        "ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN_ONLY",
        "SYNTHETIC_ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN",
    }:
        raise ContractError("successor acquisition plan mode differs")
    unsigned = {
        key: acquisition_plan[key]
        for key in acquisition_plan
        if key != "acquisition_plan_id"
    }
    expected = sha256_bytes(canonical_json_bytes(unsigned))
    if acquisition_plan.get("acquisition_plan_id") != expected:
        raise IntegrityError("successor acquisition plan ID differs")
    return expected


def _validate_predecessor_table(
    table: pa.Table,
    *,
    acquisition_plan: Mapping[str, Any],
    synthetic: bool,
) -> pa.Table:
    canonical = deterministic_table(
        table,
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    bars_bytes = deterministic_parquet_bytes(
        canonical,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    rows = canonical.to_pylist()
    expected_asset_ids = acquisition_plan["asset_ids"]
    if (
        len(rows) != 2
        or sha256_bytes(bars_bytes)
        != acquisition_plan["predecessor"]["bars_sha256"]
        or [(row["provider_symbol"], row["session"]) for row in rows]
        != [("AAPL", date(2026, 7, 30)), ("SPY", date(2026, 7, 30))]
        or any(row["asset_id"] != expected_asset_ids[row["provider_symbol"]] for row in rows)
    ):
        raise IntegrityError("successor predecessor table binding differs")
    if synthetic:
        if any(
            row["point_in_time_safe"] is not False
            or row["source_epoch"] != "SYNTHETIC_ONLY"
            or row["evidence_class"] != "SYNTHETIC_MECHANICAL"
            for row in rows
        ):
            raise IntegrityError("synthetic predecessor evidence class differs")
    elif any(
        row["point_in_time_safe"] is not True
        or row["source_epoch"] != SOURCE_EPOCH
        or row["evidence_class"] != "ACTIVE_HISTORICAL"
        or row["quality_state"] != QUALITY_STATE
        for row in rows
    ):
        raise IntegrityError("production predecessor evidence class differs")
    return canonical


def build_successor_bars_candidate(
    snapshot: LandedSnapshot,
    *,
    acquisition_plan: Mapping[str, Any],
    predecessor_table: pa.Table,
    synthetic: bool = False,
) -> SuccessorBarsCandidate:
    plan_id = _validate_plan(acquisition_plan)
    network_plan = acquisition_plan["network_request_plan"]
    if (
        snapshot.source != SOURCE_NAME
        or snapshot.url != acquisition_plan["request"]["url"]
        or snapshot.http_status != 200
        or snapshot.requested_at is None
        or snapshot.request_plan_id != network_plan["plan_id"]
        or snapshot.retrieved_at < snapshot.requested_at
        or (not synthetic and not snapshot.trust_eligible)
        or (synthetic and snapshot.trust_eligible)
    ):
        raise IntegrityError("successor snapshot binding differs")
    predecessor = _validate_predecessor_table(
        predecessor_table,
        acquisition_plan=acquisition_plan,
        synthetic=synthetic,
    )
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("successor raw response is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"bars", "next_page_token"}
        or payload["next_page_token"] is not None
        or not isinstance(payload["bars"], dict)
        or set(payload["bars"]) != {"AAPL", "SPY"}
    ):
        raise ContractError("successor response shape or pagination differs")
    records: list[dict[str, object]] = []
    seen: set[tuple[str, object]] = set()
    eastern = __import__("zoneinfo").ZoneInfo("America/New_York")
    asset_ids = acquisition_plan["asset_ids"]
    for symbol in ("AAPL", "SPY"):
        rows = payload["bars"][symbol]
        if not isinstance(rows, list) or len(rows) != 1:
            raise ContractError(f"successor requires one exact row: {symbol}")
        parsed_sessions: list[date] = []
        for bar in rows:
            (
                event_at,
                session,
                open_,
                high,
                low,
                close,
                volume,
                trade_count,
                vwap,
            ) = _accept_native_bar(
                symbol=symbol,
                bar=bar,
                eastern=eastern,
                seen_keys=seen,
            )
            parsed_sessions.append(session)
            records.append(
                {
                    "provider_symbol": symbol,
                    "asset_id": asset_ids[symbol],
                    "session": session,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "trade_count": trade_count,
                    "vwap": vwap,
                    "bar_event_at": event_at,
                    "available_at": snapshot.retrieved_at,
                    "retrieved_at": snapshot.retrieved_at,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "request_plan_id": snapshot.request_plan_id,
                    "source_epoch": (
                        SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY"
                    ),
                    "evidence_class": (
                        "ACTIVE_HISTORICAL"
                        if not synthetic
                        else "SYNTHETIC_MECHANICAL"
                    ),
                    "quality_state": (
                        QUALITY_STATE if not synthetic else "NOT_TRUST_ELIGIBLE"
                    ),
                    "point_in_time_safe": not synthetic,
                }
            )
        if tuple(parsed_sessions) != EXPECTED_DELTA_SESSIONS:
            raise ContractError(f"successor session census differs: {symbol}")
    delta = deterministic_table(
        pa.Table.from_pylist(records, schema=ACTIVE_ALPACA_SCHEMA),
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    cumulative = deterministic_table(
        pa.concat_tables([predecessor, delta]),
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    expected_keys = [
        (symbol, session)
        for symbol in ("AAPL", "SPY")
        for session in EXPECTED_CUMULATIVE_SESSIONS
    ]
    actual_keys = [
        (row["provider_symbol"], row["session"])
        for row in cumulative.to_pylist()
    ]
    if delta.num_rows != 2 or cumulative.num_rows != 4 or actual_keys != expected_keys:
        raise IntegrityError("successor cumulative session matrix differs")
    bars_bytes = deterministic_parquet_bytes(
        cumulative,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    unsigned = {
        "schema_version": 1,
        "acquisition_plan_id": plan_id,
        "network_request_plan_id": snapshot.request_plan_id,
        "predecessor_release_id": acquisition_plan["predecessor"]["release_id"],
        "predecessor_bars_sha256": acquisition_plan["predecessor"]["bars_sha256"],
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_raw_sha256": snapshot.raw_sha256,
        "requested_at": iso_z(snapshot.requested_at),
        "retrieved_at": iso_z(snapshot.retrieved_at),
        "symbols": ["AAPL", "SPY"],
        "delta_sessions": [item.isoformat() for item in EXPECTED_DELTA_SESSIONS],
        "cumulative_sessions": [
            item.isoformat() for item in EXPECTED_CUMULATIVE_SESSIONS
        ],
        "delta_row_count": 2,
        "row_count": 4,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "bars_sha256": sha256_bytes(bars_bytes),
        "trust_eligible": not synthetic,
    }
    return SuccessorBarsCandidate(
        table=cumulative,
        acquisition_plan_id=plan_id,
        network_request_plan_id=snapshot.request_plan_id,
        predecessor_release_id=acquisition_plan["predecessor"]["release_id"],
        predecessor_bars_sha256=acquisition_plan["predecessor"]["bars_sha256"],
        snapshot_id=snapshot.snapshot_id,
        snapshot_raw_sha256=snapshot.raw_sha256,
        requested_at=snapshot.requested_at,
        retrieved_at=snapshot.retrieved_at,
        delta_row_count=2,
        row_count=4,
        symbols=("AAPL", "SPY"),
        delta_sessions=EXPECTED_DELTA_SESSIONS,
        sessions=EXPECTED_CUMULATIVE_SESSIONS,
        trust_eligible=not synthetic,
        candidate_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


def _publication_artifacts(
    candidate: SuccessorBarsCandidate,
    acquisition_plan: Mapping[str, Any],
    *,
    synthetic: bool,
) -> tuple[bytes, bytes, ReleaseManifest, str]:
    bars_bytes = deterministic_parquet_bytes(
        candidate.table,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    dataset = DATASET if not synthetic else "alpaca_daily_bars_successor_fixture"
    source_epoch = SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY"
    role = ROLE if not synthetic else "qualification_evidence_only"
    quality = QUALITY_STATE if not synthetic else "QUALIFICATION_EVIDENCE"
    receipt_unsigned = {
        "schema_version": 2,
        "project": PROJECT,
        "receipt_class": "ALPACA_CANONICAL_DAILY_BARS_SUCCESSOR",
        "status": (
            "PASS_CUMULATIVE_CANONICAL_BARS_NOT_RESEARCH_AUTHORITY"
            if not synthetic
            else "SYNTHETIC_CUMULATIVE_CANONICAL_BARS_MECHANICS_ONLY"
        ),
        "candidate_id": candidate.candidate_id,
        "acquisition_plan_id": candidate.acquisition_plan_id,
        "network_request_plan_id": candidate.network_request_plan_id,
        "predecessor": {
            "release_id": candidate.predecessor_release_id,
            "bars_sha256": candidate.predecessor_bars_sha256,
            "row_count": 2,
            "event_start": "2026-07-30",
            "event_end": "2026-07-30",
        },
        "delta_snapshot": {
            "snapshot_id": candidate.snapshot_id,
            "raw_sha256": candidate.snapshot_raw_sha256,
            "requested_at": iso_z(candidate.requested_at),
            "retrieved_at": iso_z(candidate.retrieved_at),
            "row_count": candidate.delta_row_count,
            "sessions": [item.isoformat() for item in candidate.delta_sessions],
        },
        "source": {
            "source_name": SOURCE_NAME,
            "feed": "sip",
            "dataset": dataset,
            "source_epoch": source_epoch,
            "role": role,
            "quality_state": quality,
        },
        "request": acquisition_plan["request"],
        "qualification": acquisition_plan["qualification"],
        "identity": acquisition_plan["identity"],
        "asset_ids": acquisition_plan["asset_ids"],
        "calendar": acquisition_plan["calendar"],
        "cumulative_sessions": [
            item.isoformat() for item in candidate.sessions
        ],
        "row_count": candidate.row_count,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "bars_sha256": sha256_bytes(bars_bytes),
        "bars_bytes": len(bars_bytes),
        "code_closure": acquisition_plan["code_closure"],
        "config_closure": acquisition_plan["config_closure"],
        "environment_id": acquisition_plan["environment_id"],
        "authorities": {
            "canonical_bars": not synthetic,
            "source_activation": False,
            "eligible_universe": False,
            "features_or_outcomes": False,
            "research": False,
        },
    }
    receipt_id = sha256_bytes(canonical_json_bytes(receipt_unsigned))
    receipt_bytes = canonical_json_bytes(
        {**receipt_unsigned, "receipt_id": receipt_id}
    )
    files = tuple(
        sorted(
            (
                ReleaseFile(
                    path=BARS_FILENAME,
                    size=len(bars_bytes),
                    sha256=sha256_bytes(bars_bytes),
                ),
                ReleaseFile(
                    path=RECEIPT_FILENAME,
                    size=len(receipt_bytes),
                    sha256=sha256_bytes(receipt_bytes),
                ),
            ),
            key=lambda item: item.path,
        )
    )
    upstream = sorted(
        {
            candidate.predecessor_release_id,
            acquisition_plan["qualification"]["release_id"],
            acquisition_plan["identity"]["release_id"],
            acquisition_plan["calendar"]["release_id"],
        }
    )
    unsigned_manifest = {
        "schema_version": 1,
        "project": PROJECT,
        "dataset": dataset,
        "source_epoch": source_epoch,
        "role": role,
        "quality_state": quality,
        "created_at": iso_z(candidate.retrieved_at),
        "row_count": 4,
        "event_start": "2026-07-30",
        "event_end": "2026-07-31",
        "upstream_release_ids": upstream,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "code_hash": acquisition_plan["code_closure"]["closure_sha256"],
        "config_hash": acquisition_plan["config_closure"]["closure_sha256"],
        "environment_hash": acquisition_plan["environment_id"],
        "files": [entry.as_dict() for entry in files],
    }
    manifest = ReleaseManifest(
        **{
            **unsigned_manifest,
            "upstream_release_ids": tuple(upstream),
            "files": files,
            "release_id": sha256_bytes(canonical_json_bytes(unsigned_manifest)),
        }
    )
    manifest.validate()
    return bars_bytes, receipt_bytes, manifest, receipt_id


def build_successor_bars_publication_plan(
    candidate: SuccessorBarsCandidate,
    *,
    acquisition_plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    synthetic: bool = False,
) -> dict[str, object]:
    _validate_plan(acquisition_plan)
    expected_mode = (
        "SYNTHETIC_ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN"
        if synthetic
        else "ALPACA_CANONICAL_BARS_SUCCESSOR_PLAN_ONLY"
    )
    if acquisition_plan.get("mode") != expected_mode:
        raise ContractError("successor publication plan evidence class differs")
    if candidate.trust_eligible is synthetic:
        raise ContractError("successor publication candidate trust class differs")
    bars, receipt, manifest, receipt_id = _publication_artifacts(
        candidate,
        acquisition_plan,
        synthetic=synthetic,
    )
    unsigned = {
        "schema_version": 1,
        "mode": (
            "ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION"
            if not synthetic
            else "SYNTHETIC_ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION"
        ),
        "candidate_id": candidate.candidate_id,
        "acquisition_plan_id": candidate.acquisition_plan_id,
        "predecessor_release_id": candidate.predecessor_release_id,
        "delta_snapshot_id": candidate.snapshot_id,
        "accepted_root": str(Path(accepted_root).resolve()),
        "work_root": str(Path(work_root).resolve()),
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "release_directory": str(
            Path(accepted_root).resolve() / manifest.dataset / manifest.release_id
        ),
        "receipt_id": receipt_id,
        "delta_row_count": 2,
        "cumulative_row_count": 4,
        "outputs": [
            {
                "path": BARS_FILENAME,
                "size": len(bars),
                "sha256": sha256_bytes(bars),
            },
            {
                "path": RECEIPT_FILENAME,
                "size": len(receipt),
                "sha256": sha256_bytes(receipt),
            },
            {
                "path": "release_manifest.json",
                "size": len(canonical_json_bytes(manifest.as_dict())),
                "sha256": sha256_bytes(canonical_json_bytes(manifest.as_dict())),
            },
        ],
        "publication_count": 1,
        "network_calls": 0,
        "source_activation": False,
        "eligible_universe": False,
        "research": False,
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def verify_successor_bars_release(
    release_directory: Path,
    *,
    accepted_root: Path,
    expected_release_id: str | None = None,
) -> tuple[ReleaseManifest, dict[str, Any]]:
    from ..releases import verify_accepted_release

    manifest = verify_accepted_release(
        Path(release_directory),
        accepted_root=Path(accepted_root),
    )
    if (
        manifest.dataset != DATASET
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != ROLE
        or manifest.quality_state != QUALITY_STATE
        or manifest.row_count != 4
        or manifest.event_start != "2026-07-30"
        or manifest.event_end != "2026-07-31"
        or [entry.path for entry in manifest.files]
        != [BARS_FILENAME, RECEIPT_FILENAME]
        or (
            expected_release_id is not None
            and manifest.release_id != expected_release_id
        )
    ):
        raise IntegrityError("successor release manifest differs")
    receipt = _json_object(
        Path(release_directory) / RECEIPT_FILENAME,
        label="successor canonical bar receipt",
    )
    receipt_id = receipt.get("receipt_id")
    if (
        receipt.get("schema_version") != 2
        or receipt.get("project") != PROJECT
        or receipt.get("receipt_class")
        != "ALPACA_CANONICAL_DAILY_BARS_SUCCESSOR"
        or receipt.get("status")
        != "PASS_CUMULATIVE_CANONICAL_BARS_NOT_RESEARCH_AUTHORITY"
        or receipt.get("row_count") != 4
        or receipt.get("cumulative_sessions")
        != [item.isoformat() for item in EXPECTED_CUMULATIVE_SESSIONS]
        or receipt.get("schema_fingerprint") != SCHEMA_FINGERPRINT
        or receipt.get("authorities")
        != {
            "canonical_bars": True,
            "source_activation": False,
            "eligible_universe": False,
            "features_or_outcomes": False,
            "research": False,
        }
        or not isinstance(receipt_id, str)
    ):
        raise IntegrityError("successor canonical bar receipt differs")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != receipt_id:
        raise IntegrityError("successor canonical bar receipt ID differs")
    predecessor = receipt.get("predecessor")
    delta_snapshot = receipt.get("delta_snapshot")
    qualification = receipt.get("qualification")
    identity = receipt.get("identity")
    calendar = receipt.get("calendar")
    if (
        not isinstance(predecessor, dict)
        or predecessor.get("row_count") != 2
        or predecessor.get("event_start") != "2026-07-30"
        or predecessor.get("event_end") != "2026-07-30"
        or not isinstance(delta_snapshot, dict)
        or delta_snapshot.get("row_count") != 2
        or delta_snapshot.get("sessions")
        != [item.isoformat() for item in EXPECTED_DELTA_SESSIONS]
        or not all(
            isinstance(binding, dict) and isinstance(binding.get("release_id"), str)
            for binding in (qualification, identity, calendar)
        )
    ):
        raise IntegrityError("successor lineage receipt differs")
    require_sha256(predecessor.get("release_id"), "successor.predecessor.release_id")
    require_sha256(predecessor.get("bars_sha256"), "successor.predecessor.bars_sha256")
    bars_entry = next(entry for entry in manifest.files if entry.path == BARS_FILENAME)
    expected_upstream = sorted(
        {
            predecessor["release_id"],
            qualification["release_id"],
            identity["release_id"],
            calendar["release_id"],
        }
    )
    if (
        receipt.get("bars_sha256") != bars_entry.sha256
        or list(manifest.upstream_release_ids) != expected_upstream
    ):
        raise IntegrityError("successor payload or upstream binding differs")
    table = pq.read_table(Path(release_directory) / BARS_FILENAME)
    canonical = deterministic_table(
        table,
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    keys = [
        (row["provider_symbol"], row["session"])
        for row in canonical.to_pylist()
    ]
    expected_keys = [
        (symbol, session)
        for symbol in ("AAPL", "SPY")
        for session in EXPECTED_CUMULATIVE_SESSIONS
    ]
    asset_ids = receipt.get("asset_ids")
    rows = canonical.to_pylist()
    if (
        not isinstance(asset_ids, dict)
        or set(asset_ids) != {"AAPL", "SPY"}
        or keys != expected_keys
        or any(
            row["asset_id"] != asset_ids[row["provider_symbol"]]
            or row["source_epoch"] != SOURCE_EPOCH
            or row["evidence_class"] != "ACTIVE_HISTORICAL"
            or row["quality_state"] != QUALITY_STATE
            or row["point_in_time_safe"] is not True
            for row in rows
        )
    ):
        raise IntegrityError("successor release cumulative matrix differs")
    return manifest, receipt


def publish_successor_bars(
    candidate: SuccessorBarsCandidate,
    *,
    acquisition_plan: Mapping[str, Any],
    approved_publication_plan_id: str,
    accepted_root: Path,
    work_root: Path,
    owner_confirmation: str,
) -> SuccessorBarsPublication:
    if (
        owner_confirmation != PUBLICATION_CONFIRMATION_VALUE
        or os.environ.get(PUBLICATION_CONFIRMATION_TOKEN)
        != PUBLICATION_CONFIRMATION_VALUE
    ):
        raise PermissionError("successor publication confirmation is absent")
    if not candidate.trust_eligible:
        raise ContractError("production successor bars require trust-eligible evidence")
    plan = build_successor_bars_publication_plan(
        candidate,
        acquisition_plan=acquisition_plan,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if plan["publication_plan_id"] != approved_publication_plan_id:
        raise PermissionError("approved successor publication plan differs")
    bars, receipt, manifest, receipt_id = _publication_artifacts(
        candidate,
        acquisition_plan,
        synthetic=False,
    )
    stage = _prepare_publication_stage(work_root, approved_publication_plan_id)
    for name, payload in ((BARS_FILENAME, bars), (RECEIPT_FILENAME, receipt)):
        path = stage / name
        if path.exists():
            if (
                not path.is_file()
                or path.stat().st_nlink != 1
                or path.read_bytes() != payload
            ):
                raise IntegrityError(f"successor staged {name} differs")
        else:
            atomic_write_new(path, payload)
    published = AtomicReleasePublisher(Path(accepted_root).resolve()).publish(
        stage,
        manifest,
    )
    verify_successor_bars_release(
        published,
        accepted_root=Path(accepted_root).resolve(),
        expected_release_id=manifest.release_id,
    )
    return SuccessorBarsPublication(
        publication_plan_id=approved_publication_plan_id,
        release_id=manifest.release_id,
        receipt_id=receipt_id,
        release_directory=published,
        work_directory=stage,
    )


def execute_successor_bars_acquisition(
    *,
    acquisition_plan: Mapping[str, Any],
    approved_acquisition_plan_id: str,
    api_key_id: str,
    api_secret_key: str,
    clock: TrustedClock | None = None,
    repo_root: Path | None = None,
) -> tuple[LandedSnapshot, SuccessorBarsCandidate, dict[str, object]]:
    if acquisition_plan.get("acquisition_plan_id") != approved_acquisition_plan_id:
        raise PermissionError("approved successor acquisition plan differs")
    root = (repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    observed_at = trusted_clock.now()
    context = _context(root, require_clean=True)
    current_plan = _build_plan(context, synthetic=False)
    if current_plan["acquisition_plan_id"] != approved_acquisition_plan_id:
        raise IntegrityError("successor acquisition plan changed before execution")
    earliest = parse_utc_z(
        current_plan["earliest_execution_at"],
        "successor.earliest_execution_at",
    )
    if observed_at < earliest:
        raise ContractError("successor acquisition is earlier than the frozen lag gate")
    request, bars_policy, network_plan = _network_plan(
        context,
        requested_at=observed_at,
    )
    if network_plan.plan_id != current_plan["network_request_plan"]["plan_id"]:
        raise IntegrityError("successor network plan changed before execution")
    session = start_local_network_execution(
        network_plan,
        registry=context["registry"],
        clock=trusted_clock,
    )
    policy = context["policy"]
    store = AsReceivedSnapshotStore(
        root / policy["outputs"]["snapshot_store"],
        allowed_root=root / "data",
        acquisition_registry=context["registry"],
    )
    pages = guarded_fetch_landed_pages(
        request,
        snapshot_store=store,
        api_key_id=api_key_id,
        api_secret_key=api_secret_key,
        policy=bars_policy,
        network_enabled=True,
        max_pages=1,
        clock=trusted_clock,
        authorization_session=session,
        source=SOURCE_NAME,
        timeout_seconds=30,
        max_response_bytes=1048576,
    )
    if len(pages) != 1:
        raise IntegrityError("successor acquisition produced an unexpected page census")
    candidate = build_successor_bars_candidate(
        pages[0],
        acquisition_plan=current_plan,
        predecessor_table=context["predecessor_table"],
    )
    publication_plan = build_successor_bars_publication_plan(
        candidate,
        acquisition_plan=current_plan,
        accepted_root=root / policy["outputs"]["accepted_root"],
        work_root=root / policy["outputs"]["work_root"],
    )
    return pages[0], candidate, publication_plan
