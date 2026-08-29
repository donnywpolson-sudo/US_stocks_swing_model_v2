from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

import pyarrow as pa

from ..canonical.alpaca import _accept_native_bar
from ..canonical.parquet import deterministic_parquet_bytes, deterministic_table
from ..clock import TrustedClock, require_trusted_clock
from ..common import (
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    load_independent_json_object,
    parse_utc_z,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, IntegrityError
from ..exchange_calendar import load_xnys_calendar_release
from ..releases import (
    AtomicReleasePublisher,
    ReleaseFile,
    ReleaseManifest,
    verify_accepted_release,
)
from .alpaca import (
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    guarded_fetch_landed_pages,
)
from .network_execution import NetworkRequestPlan, start_local_network_execution
from .snapshots import (
    AsReceivedSnapshotStore,
    LandedSnapshot,
    NetworkAcquisitionRegistry,
)


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_canonical_bars_policy.json"
SOURCE_NAME = "alpaca_sip_canonical_bars"
DATASET = "alpaca_daily_bars"
SOURCE_EPOCH = "alpaca_basic_sip_raw_v1"
ROLE = "active_historical"
QUALITY_STATE = "PASS"
RECEIPT_FILENAME = "canonical_bar_receipt.json"
BARS_FILENAME = "bars.parquet"
PUBLICATION_CONFIRMATION_TOKEN = "ALPACA_CANONICAL_BARS_PUBLICATION_APPROVED"
PUBLICATION_CONFIRMATION_VALUE = "YES"

ACTIVE_ALPACA_SCHEMA = pa.schema(
    [
        ("provider_symbol", pa.string()),
        ("asset_id", pa.string()),
        ("session", pa.date32()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.int64()),
        ("trade_count", pa.int64()),
        ("vwap", pa.float64()),
        ("bar_event_at", pa.timestamp("us", tz="UTC")),
        ("available_at", pa.timestamp("us", tz="UTC")),
        ("retrieved_at", pa.timestamp("us", tz="UTC")),
        ("source_snapshot_id", pa.string()),
        ("request_plan_id", pa.string()),
        ("source_epoch", pa.string()),
        ("evidence_class", pa.string()),
        ("quality_state", pa.string()),
        ("point_in_time_safe", pa.bool_()),
    ]
)
SCHEMA_FINGERPRINT = sha256_bytes(
    canonical_json_bytes(str(ACTIVE_ALPACA_SCHEMA))
)

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_canonical_bars.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/network_execution.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/canonical/alpaca.py",
    "src/us_stocks_swing_model_v2/canonical/parquet.py",
    "src/us_stocks_swing_model_v2/cli/acquire_canonical_bars.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    POLICY_PATH,
    "config/alpaca_canonical_bars_network_registry.json",
    "config/sources.json",
)


@dataclass(frozen=True)
class CanonicalBarsCandidate:
    table: pa.Table
    acquisition_plan_id: str
    network_request_plan_id: str
    snapshot_id: str
    snapshot_raw_sha256: str
    requested_at: datetime
    retrieved_at: datetime
    row_count: int
    symbols: tuple[str, ...]
    sessions: tuple[date, ...]
    trust_eligible: bool
    candidate_id: str


@dataclass(frozen=True)
class CanonicalBarsPublication:
    publication_plan_id: str
    release_id: str
    receipt_id: str
    release_directory: Path
    work_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run_git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"git {' '.join(arguments)} failed") from exc
    return completed.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    resolved = root.resolve(strict=True)
    git_root = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if git_root != resolved or _run_git(resolved, "branch", "--show-current") != "main":
        raise ContractError("canonical bars require the exact main repository")
    if _run_git(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ContractError("canonical bars require a clean repository")
    return {
        "root": str(resolved),
        "branch": "main",
        "commit": _run_git(resolved, "rev-parse", "HEAD"),
        "tree": _run_git(resolved, "rev-parse", "HEAD^{tree}"),
    }


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    files = [
        {"path": path, "sha256": sha256_file(root / path)}
        for path in sorted(paths)
    ]
    return {
        "files": files,
        "closure_sha256": sha256_bytes(canonical_json_bytes(files)),
    }


def _load_policy(root: Path) -> dict[str, Any]:
    policy = load_independent_json_object(root / POLICY_PATH, label="canonical bars policy")
    if (
        policy.get("schema_version") != 1
        or policy.get("project") != PROJECT
        or policy.get("policy_type") != "FIRST_BOUNDED_ALPACA_CANONICAL_BARS"
        or policy.get("source_key") != "alpaca_basic_delayed_sip"
        or policy.get("source_name") != SOURCE_NAME
        or policy.get("dataset") != DATASET
        or policy.get("source_epoch") != SOURCE_EPOCH
        or policy.get("role") != ROLE
        or policy.get("quality_state") != QUALITY_STATE
        or policy.get("diagnostic_only") is not True
        or policy.get("symbols") != ["AAPL", "SPY"]
        or policy.get("window")
        != {
            "start": "2026-07-30T04:00:00Z",
            "end": "2026-07-30T23:30:00Z",
            "sessions": ["2026-07-30"],
        }
    ):
        raise ContractError("canonical bars policy identity differs")
    request = policy.get("request_contract")
    if request != {
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
    }:
        raise ContractError("canonical bars request policy differs")
    return policy


def _active_source_binding(root: Path, policy: Mapping[str, Any]) -> dict[str, str]:
    source_config = policy["source_config"]
    path = root / source_config["path"]
    if sha256_file(path) != source_config["sha256"]:
        raise IntegrityError("active source configuration hash differs")
    value = load_independent_json_object(path, label="source configuration")
    source = value.get("sources", {}).get(policy["source_key"])
    if (
        not isinstance(source, dict)
        or source.get("enabled_for_active_pipeline") is not True
        or source.get("qualification_receipt")
        != (
            "data/vault/accepted/alpaca_feed_qualification/"
            "8bce929303039efa69c6d9456dcb9b64b593a7397d0f7ffd479dbc358a5b33a2/"
            "alpaca_feed_qualification_receipt.json"
        )
        or source.get("status") != "active_sip_qualified_pending_canonical_bars"
        or source.get("request_contract")
        != {
            "qualified_feed": "sip",
            "timeframe": "1Day",
            "adjustment": "raw",
            "asof": None,
            "minimum_end_lag_minutes": 20,
            "sort": "asc",
        }
    ):
        raise ContractError("active Alpaca SIP source binding differs")
    return {
        "path": source_config["path"],
        "sha256": source_config["sha256"],
        "source_key": policy["source_key"],
        "qualified_feed": "sip",
    }


def _identity_bindings(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    binding = policy["identity_release"]
    directory = root / binding["relative_directory"]
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        manifest.release_id != binding["release_id"]
        or manifest.dataset != "identity"
        or manifest.role != "prospective_as_received"
        or manifest.quality_state != "PASS"
    ):
        raise IntegrityError("identity release binding differs")
    payload = load_independent_json_object(directory / "identity_snapshots.json", label="identity release")
    snapshots = payload.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) != 1:
        raise IntegrityError("identity release snapshot census differs")
    selected = _selected_asset_ids(
        snapshots[0],
        expected_snapshot_id=binding["identity_snapshot_id"],
        expected_asset_ids=binding["asset_ids"],
    )
    return (
        {
            "release_id": manifest.release_id,
            "identity_snapshot_id": binding["identity_snapshot_id"],
        },
        selected,
    )


def _selected_asset_ids(
    snapshot: object,
    *,
    expected_snapshot_id: str,
    expected_asset_ids: Mapping[str, str],
) -> dict[str, str]:
    require_sha256(expected_snapshot_id, "identity.expected_snapshot_id")
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("snapshot_id") != expected_snapshot_id
        or not isinstance(snapshot.get("rows"), list)
    ):
        raise IntegrityError("identity snapshot identity differs")
    selected: dict[str, str] = {}
    for row in snapshot["rows"]:
        if not isinstance(row, dict) or row.get("symbol") not in {"AAPL", "SPY"}:
            continue
        symbol = row["symbol"]
        if (
            symbol in selected
            or row.get("eligible") is not True
            or row.get("active") is not True
            or row.get("membership_present") is not True
            or row.get("security_type") not in {"STOCK", "ETF"}
            or row.get("identity_snapshot_id") != expected_snapshot_id
            or not isinstance(row.get("asset_id"), str)
            or not row["asset_id"]
        ):
            raise IntegrityError(f"identity row is not eligible and exact: {symbol}")
        selected[symbol] = row["asset_id"]
    if set(selected) != {"AAPL", "SPY"}:
        raise IntegrityError("identity release lacks exact AAPL/SPY bindings")
    if selected != expected_asset_ids:
        raise IntegrityError("identity asset UUID bindings differ from policy")
    return dict(sorted(selected.items()))


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
        raise IntegrityError("calendar release binding differs")
    sessions = [
        row
        for row in loaded.schedule.to_pylist()
        if row["session"] == date(2026, 7, 30)
    ]
    if (
        len(sessions) != 1
        or iso_z(sessions[0]["close_at"]) != "2026-07-30T20:00:00Z"
    ):
        raise IntegrityError("pinned calendar session binding differs")
    return {
        "release_id": loaded.calendar.release_id,
        "sessions": ["2026-07-30"],
        "close_at": "2026-07-30T20:00:00Z",
    }


def _qualification_binding(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> dict[str, str]:
    del policy, accepted_root
    sources = json.loads((root / "config" / "sources.json").read_text(encoding="utf-8"))
    contract = sources["sources"]["alpaca_basic_delayed_sip"]
    request = contract["request_contract"]
    if (
        contract["status"] != "qualified_sip_not_active"
        or request["qualified_feed"] != "sip"
        or contract["qualification_receipt"] is None
    ):
        raise IntegrityError("qualified-but-non-active SIP binding is unavailable")
    return {
        "receipt_id": str(contract["qualification_receipt"]),
        "selected_feed": "sip",
    }


def _context(root: Path, *, require_clean: bool) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    policy = _load_policy(resolved)
    if policy["diagnostic_only"]:
        raise ContractError("July canonical-bars policy is diagnostic-only and cannot acquire or publish production evidence")
    outputs = policy["outputs"]
    accepted_root = (resolved / outputs["accepted_root"]).resolve(strict=True)
    registry_path = resolved / policy["network_registry"]
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=resolved,
    )
    if set(registry.allowed_origin_paths) != {SOURCE_NAME}:
        raise ContractError("canonical bars registry source census differs")
    repository = _repository_binding(resolved) if require_clean else {
        "root": str(resolved),
        "branch": "SYNTHETIC_ONLY",
        "commit": "0" * 64,
        "tree": "0" * 64,
    }
    return {
        "root": resolved,
        "policy": policy,
        "repository": repository,
        "registry": registry,
        "source": _active_source_binding(resolved, policy),
        "qualification": _qualification_binding(resolved, policy, accepted_root),
        "identity": _identity_bindings(resolved, policy, accepted_root),
        "calendar": _calendar_binding(resolved, policy, accepted_root),
        "code_closure": _closure(resolved, CODE_CLOSURE_PATHS),
        "config_closure": _closure(resolved, CONFIG_CLOSURE_PATHS),
        "environment_id": sha256_file(resolved / "config/environment.lock.json"),
    }


def _fixture_context(root: Path) -> dict[str, object]:
    resolved = root.resolve(strict=True)
    policy = _load_policy(resolved)
    registry = NetworkAcquisitionRegistry.load(
        resolved / policy["network_registry"],
        allowed_root=resolved,
    )
    identity = policy["identity_release"]
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
            "sessions": ["2026-07-30"],
            "close_at": "2026-07-30T20:00:00Z",
        },
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
    request_contract = policy["request_contract"]
    request = AlpacaBarsRequest(
        symbols=tuple(policy["symbols"]),
        start=parse_utc_z(policy["window"]["start"], "canonical_bars.start"),
        end=parse_utc_z(policy["window"]["end"], "canonical_bars.end"),
        requested_at=requested_at,
        limit=request_contract["limit"],
    )
    bars_policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint=request_contract["endpoint"],
    )
    network_plan = NetworkRequestPlan.create(
        registry=context["registry"],
        source=SOURCE_NAME,
        initial_url=request.url(bars_policy),
        timeout_seconds=request_contract["timeout_seconds"],
        max_response_bytes=request_contract["max_response_bytes"],
        max_pages=1,
        pagination_parameter="page_token",
    )
    return request, bars_policy, network_plan


def build_canonical_bars_acquisition_plan(
    *,
    repo_root: Path | None = None,
    clock: TrustedClock | None = None,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    requested_at = trusted_clock.now()
    context = _context(root, require_clean=True)
    request, _, network_plan = _network_plan(
        context,
        requested_at=requested_at,
    )
    policy = context["policy"]
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_CANONICAL_BARS_PLAN_ONLY",
        "repository": context["repository"],
        "source": context["source"],
        "qualification": context["qualification"],
        "identity": context["identity"][0],
        "asset_ids": context["identity"][1],
        "calendar": context["calendar"],
        "request": {
            "method": "GET",
            "url": network_plan.initial_url,
            "symbols": list(request.symbols),
            "start": policy["window"]["start"],
            "end": policy["window"]["end"],
            "feed": "sip",
            "timeframe": "1Day",
            "adjustment": "raw",
            "asof": None,
            "sort": "asc",
            "limit": 10000,
            "expected_sessions": policy["window"]["sessions"],
        },
        "network_request_plan": network_plan.as_dict(),
        "host_timeout_seconds": 120,
        "code_closure": context["code_closure"],
        "config_closure": context["config_closure"],
        "environment_id": context["environment_id"],
        "outputs": {
            "snapshot_store": str(root / policy["outputs"]["snapshot_store"]),
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
            "research": False,
        },
    }
    return {
        **unsigned,
        "acquisition_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_canonical_bars_fixture_plan(
    *,
    repo_root: Path | None = None,
    clock: TrustedClock,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    requested_at = require_trusted_clock(clock).now()
    context = _fixture_context(root)
    request, _, network_plan = _network_plan(
        context,
        requested_at=requested_at,
    )
    policy = context["policy"]
    unsigned = {
        "schema_version": 1,
        "mode": "SYNTHETIC_ALPACA_CANONICAL_BARS_PLAN",
        "repository": context["repository"],
        "source": context["source"],
        "qualification": context["qualification"],
        "identity": context["identity"][0],
        "asset_ids": context["identity"][1],
        "calendar": context["calendar"],
        "request": {
            "method": "GET",
            "url": network_plan.initial_url,
            "symbols": list(request.symbols),
            "start": policy["window"]["start"],
            "end": policy["window"]["end"],
            "feed": "sip",
            "timeframe": "1Day",
            "adjustment": "raw",
            "asof": None,
            "sort": "asc",
            "limit": 10000,
            "expected_sessions": policy["window"]["sessions"],
        },
        "network_request_plan": network_plan.as_dict(),
        "host_timeout_seconds": 120,
        "code_closure": context["code_closure"],
        "config_closure": context["config_closure"],
        "environment_id": context["environment_id"],
        "outputs": {"canonical_candidate": "SYNTHETIC_ONLY_NO_PUBLICATION"},
        "authorities": {
            "network_calls": 0,
            "credential_access": False,
            "snapshot_write": False,
            "canonical_release_publication": False,
            "research": False,
        },
    }
    return {
        **unsigned,
        "acquisition_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_canonical_bars_candidate(
    snapshot: LandedSnapshot,
    *,
    acquisition_plan: Mapping[str, Any],
    synthetic: bool = False,
) -> CanonicalBarsCandidate:
    if acquisition_plan.get("mode") not in {
        "ALPACA_CANONICAL_BARS_PLAN_ONLY",
        "SYNTHETIC_ALPACA_CANONICAL_BARS_PLAN",
    }:
        raise ContractError("canonical bars acquisition plan mode differs")
    unsigned_plan = {
        key: acquisition_plan[key]
        for key in acquisition_plan
        if key != "acquisition_plan_id"
    }
    expected_plan_id = sha256_bytes(canonical_json_bytes(unsigned_plan))
    if acquisition_plan.get("acquisition_plan_id") != expected_plan_id:
        raise IntegrityError("canonical bars acquisition plan ID differs")
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
        raise IntegrityError("canonical bars snapshot binding differs")
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("canonical bars raw response is invalid JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"bars", "next_page_token"}
        or payload["next_page_token"] is not None
        or not isinstance(payload["bars"], dict)
        or set(payload["bars"]) != {"AAPL", "SPY"}
    ):
        raise ContractError("canonical bars response shape or pagination differs")
    records: list[dict[str, object]] = []
    seen: set[tuple[str, object]] = set()
    eastern = __import__("zoneinfo").ZoneInfo("America/New_York")
    asset_ids = acquisition_plan["asset_ids"]
    for symbol in ("AAPL", "SPY"):
        rows = payload["bars"][symbol]
        if not isinstance(rows, list) or len(rows) != 1:
            raise ContractError(f"canonical bars require one exact row: {symbol}")
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
            bar=rows[0],
            eastern=eastern,
            seen_keys=seen,
        )
        if session != date(2026, 7, 30):
            raise ContractError("canonical bars session differs")
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
                "source_epoch": SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY",
                "evidence_class": (
                    "ACTIVE_HISTORICAL"
                    if not synthetic
                    else "SYNTHETIC_MECHANICAL"
                ),
                "quality_state": QUALITY_STATE if not synthetic else "NOT_TRUST_ELIGIBLE",
                "point_in_time_safe": not synthetic,
            }
        )
    table = deterministic_table(
        pa.Table.from_pylist(records, schema=ACTIVE_ALPACA_SCHEMA),
        ACTIVE_ALPACA_SCHEMA,
        ("provider_symbol", "session"),
    )
    bars_bytes = deterministic_parquet_bytes(
        table,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    unsigned = {
        "schema_version": 1,
        "acquisition_plan_id": expected_plan_id,
        "network_request_plan_id": snapshot.request_plan_id,
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_raw_sha256": snapshot.raw_sha256,
        "requested_at": iso_z(snapshot.requested_at),
        "retrieved_at": iso_z(snapshot.retrieved_at),
        "symbols": ["AAPL", "SPY"],
        "sessions": ["2026-07-30"],
        "row_count": table.num_rows,
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "bars_sha256": sha256_bytes(bars_bytes),
        "trust_eligible": not synthetic,
    }
    return CanonicalBarsCandidate(
        table=table,
        acquisition_plan_id=expected_plan_id,
        network_request_plan_id=snapshot.request_plan_id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_raw_sha256=snapshot.raw_sha256,
        requested_at=snapshot.requested_at,
        retrieved_at=snapshot.retrieved_at,
        row_count=table.num_rows,
        symbols=("AAPL", "SPY"),
        sessions=(date(2026, 7, 30),),
        trust_eligible=not synthetic,
        candidate_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


def _publication_artifacts(
    candidate: CanonicalBarsCandidate,
    acquisition_plan: Mapping[str, Any],
    *,
    synthetic: bool,
) -> tuple[bytes, bytes, ReleaseManifest, str]:
    bars_bytes = deterministic_parquet_bytes(
        candidate.table,
        schema=ACTIVE_ALPACA_SCHEMA,
        sort_keys=("provider_symbol", "session"),
    )
    dataset = DATASET if not synthetic else "alpaca_daily_bars_fixture"
    source_epoch = SOURCE_EPOCH if not synthetic else "SYNTHETIC_ONLY"
    role = ROLE if not synthetic else "qualification_evidence_only"
    quality = QUALITY_STATE if not synthetic else "QUALIFICATION_EVIDENCE"
    receipt_unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "receipt_class": "ALPACA_CANONICAL_DAILY_BARS",
        "status": (
            "PASS_CANONICAL_BARS_NOT_RESEARCH_AUTHORITY"
            if not synthetic
            else "SYNTHETIC_CANONICAL_BARS_MECHANICS_ONLY"
        ),
        "candidate_id": candidate.candidate_id,
        "acquisition_plan_id": candidate.acquisition_plan_id,
        "network_request_plan_id": candidate.network_request_plan_id,
        "snapshot_id": candidate.snapshot_id,
        "snapshot_raw_sha256": candidate.snapshot_raw_sha256,
        "requested_at": iso_z(candidate.requested_at),
        "retrieved_at": iso_z(candidate.retrieved_at),
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
    unsigned_manifest = {
        "schema_version": 1,
        "project": PROJECT,
        "dataset": dataset,
        "source_epoch": source_epoch,
        "role": role,
        "quality_state": quality,
        "created_at": iso_z(candidate.retrieved_at),
        "row_count": candidate.row_count,
        "event_start": "2026-07-30",
        "event_end": "2026-07-30",
        "upstream_release_ids": sorted(
            {
                acquisition_plan["qualification"]["release_id"],
                acquisition_plan["identity"]["release_id"],
                acquisition_plan["calendar"]["release_id"],
            }
        ),
        "schema_fingerprint": SCHEMA_FINGERPRINT,
        "code_hash": acquisition_plan["code_closure"]["closure_sha256"],
        "config_hash": acquisition_plan["config_closure"]["closure_sha256"],
        "environment_hash": acquisition_plan["environment_id"],
        "files": [entry.as_dict() for entry in files],
    }
    manifest = ReleaseManifest(
        **{
            **unsigned_manifest,
            "upstream_release_ids": tuple(unsigned_manifest["upstream_release_ids"]),
            "files": files,
            "release_id": sha256_bytes(canonical_json_bytes(unsigned_manifest)),
        }
    )
    manifest.validate()
    return bars_bytes, receipt_bytes, manifest, receipt_id


def build_canonical_bars_publication_plan(
    candidate: CanonicalBarsCandidate,
    *,
    acquisition_plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    synthetic: bool = False,
) -> dict[str, object]:
    bars, receipt, manifest, receipt_id = _publication_artifacts(
        candidate,
        acquisition_plan,
        synthetic=synthetic,
    )
    unsigned = {
        "schema_version": 1,
        "mode": (
            "ALPACA_CANONICAL_BARS_PUBLICATION"
            if not synthetic
            else "SYNTHETIC_ALPACA_CANONICAL_BARS_PUBLICATION"
        ),
        "candidate_id": candidate.candidate_id,
        "acquisition_plan_id": candidate.acquisition_plan_id,
        "snapshot_id": candidate.snapshot_id,
        "accepted_root": str(Path(accepted_root).resolve()),
        "work_root": str(Path(work_root).resolve()),
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "release_directory": str(
            Path(accepted_root).resolve() / manifest.dataset / manifest.release_id
        ),
        "receipt_id": receipt_id,
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
        "research": False,
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def _prepare_publication_stage(work_root: Path, publication_plan_id: str) -> Path:
    supplied_work = Path(work_root)
    if not supplied_work.is_absolute():
        raise ContractError("canonical bars publication work root must be absolute")
    work = Path(os.path.abspath(supplied_work))
    anchor = Path(work.anchor)
    if work == anchor:
        raise ContractError(
            "canonical bars publication work root cannot be a filesystem root"
        )
    require_contained_path(work, anchor, must_exist=False)
    work.mkdir(parents=True, exist_ok=True)
    require_contained_path(work, anchor, must_exist=True)
    stage = work / publication_plan_id
    require_contained_path(stage, work, must_exist=False)
    stage.mkdir(exist_ok=True)
    require_contained_path(stage, work, must_exist=True)
    return stage


def publish_canonical_bars(
    candidate: CanonicalBarsCandidate,
    *,
    acquisition_plan: Mapping[str, Any],
    approved_publication_plan_id: str,
    accepted_root: Path,
    work_root: Path,
    owner_confirmation: str,
) -> CanonicalBarsPublication:
    if (
        owner_confirmation != PUBLICATION_CONFIRMATION_VALUE
        or os.environ.get(PUBLICATION_CONFIRMATION_TOKEN)
        != PUBLICATION_CONFIRMATION_VALUE
    ):
        raise PermissionError("canonical bars publication confirmation is absent")
    if not candidate.trust_eligible:
        raise ContractError("production canonical bars require trust-eligible evidence")
    plan = build_canonical_bars_publication_plan(
        candidate,
        acquisition_plan=acquisition_plan,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if plan["publication_plan_id"] != approved_publication_plan_id:
        raise PermissionError("approved canonical bars publication plan differs")
    bars, receipt, manifest, receipt_id = _publication_artifacts(
        candidate,
        acquisition_plan,
        synthetic=False,
    )
    stage = _prepare_publication_stage(work_root, approved_publication_plan_id)
    for name, payload in ((BARS_FILENAME, bars), (RECEIPT_FILENAME, receipt)):
        path = stage / name
        if path.exists():
            if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != payload:
                raise IntegrityError(f"canonical bars staged {name} differs")
        else:
            atomic_write_new(path, payload)
    published = AtomicReleasePublisher(Path(accepted_root).resolve()).publish(
        stage,
        manifest,
    )
    verify_canonical_bars_release(
        published,
        accepted_root=Path(accepted_root).resolve(),
        expected_release_id=manifest.release_id,
    )
    return CanonicalBarsPublication(
        publication_plan_id=approved_publication_plan_id,
        release_id=manifest.release_id,
        receipt_id=receipt_id,
        release_directory=published,
        work_directory=stage,
    )


def verify_canonical_bars_release(
    release_directory: Path,
    *,
    accepted_root: Path,
    expected_release_id: str | None = None,
) -> tuple[ReleaseManifest, dict[str, Any]]:
    manifest = verify_accepted_release(
        Path(release_directory),
        accepted_root=Path(accepted_root),
    )
    if (
        manifest.dataset != DATASET
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != ROLE
        or manifest.quality_state != QUALITY_STATE
        or manifest.row_count != 2
        or manifest.event_start != "2026-07-30"
        or manifest.event_end != "2026-07-30"
        or [entry.path for entry in manifest.files]
        != [BARS_FILENAME, RECEIPT_FILENAME]
        or (expected_release_id is not None and manifest.release_id != expected_release_id)
    ):
        raise IntegrityError("canonical bars release manifest differs")
    receipt = load_independent_json_object(
        Path(release_directory) / RECEIPT_FILENAME,
        label="canonical bar receipt",
    )
    receipt_id = receipt.get("receipt_id")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("project") != PROJECT
        or receipt.get("status") != "PASS_CANONICAL_BARS_NOT_RESEARCH_AUTHORITY"
        or receipt.get("row_count") != 2
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
        raise IntegrityError("canonical bar receipt differs")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != receipt_id:
        raise IntegrityError("canonical bar receipt ID differs")
    return manifest, receipt


def execute_canonical_bars_acquisition(
    *,
    acquisition_plan: Mapping[str, Any],
    approved_acquisition_plan_id: str,
    api_key_id: str,
    api_secret_key: str,
    clock: TrustedClock | None = None,
    repo_root: Path | None = None,
) -> tuple[LandedSnapshot, CanonicalBarsCandidate, dict[str, object]]:
    if acquisition_plan.get("acquisition_plan_id") != approved_acquisition_plan_id:
        raise PermissionError("approved canonical bars acquisition plan differs")
    root = (repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    context = _context(root, require_clean=True)
    current_plan = build_canonical_bars_acquisition_plan(
        repo_root=root,
        clock=trusted_clock,
    )
    if current_plan["acquisition_plan_id"] != approved_acquisition_plan_id:
        raise IntegrityError("canonical bars acquisition plan changed before execution")
    request, bars_policy, network_plan = _network_plan(
        context,
        requested_at=trusted_clock.now(),
    )
    if network_plan.plan_id != current_plan["network_request_plan"]["plan_id"]:
        raise IntegrityError("canonical bars network plan changed before execution")
    session = start_local_network_execution(
        network_plan,
        registry=context["registry"],
        clock=trusted_clock,
    )
    policy = context["policy"]
    snapshot_store = AsReceivedSnapshotStore(
        root / policy["outputs"]["snapshot_store"],
        allowed_root=root / "data",
        acquisition_registry=context["registry"],
    )
    pages = guarded_fetch_landed_pages(
        request,
        snapshot_store=snapshot_store,
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
        raise IntegrityError("canonical bars acquisition produced an unexpected page census")
    candidate = build_canonical_bars_candidate(
        pages[0],
        acquisition_plan=current_plan,
    )
    publication_plan = build_canonical_bars_publication_plan(
        candidate,
        acquisition_plan=current_plan,
        accepted_root=root / policy["outputs"]["accepted_root"],
        work_root=root / policy["outputs"]["work_root"],
    )
    return pages[0], candidate, publication_plan
