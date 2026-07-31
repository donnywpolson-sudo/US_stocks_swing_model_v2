from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from ..common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, IntegrityError
from ..exchange_calendar import load_xnys_calendar_release
from ..releases import verify_accepted_release
from .alpaca import AlpacaBarsPolicy, AlpacaBarsRequest
from .network_execution import NetworkRequestPlan
from .snapshots import NetworkAcquisitionRegistry


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_historical_backfill_policy.json"
REGISTRY_PATH = "config/alpaca_historical_backfill_network_registry.json"
SOURCE_NAME = "alpaca_sip_historical_backfill"
POLICY_TYPE = "ALPACA_SIP_HISTORICAL_BACKFILL_PLAN_ONLY"
NEW_YORK = ZoneInfo("America/New_York")

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_historical_backfill.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/network_execution.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/cli/plan_alpaca_historical_backfill.py",
    "src/us_stocks_swing_model_v2/releases.py",
    "src/us_stocks_swing_model_v2/exchange_calendar.py",
)
CONFIG_CLOSURE_PATHS = (
    POLICY_PATH,
    REGISTRY_PATH,
    "config/sources.json",
    "config/environment.lock.json",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} root must be an object")
    return value


def _run_git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
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
    return result.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    resolved = root.resolve(strict=True)
    git_root = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve(
        strict=True
    )
    if git_root != resolved or _run_git(resolved, "branch", "--show-current") != "main":
        raise ContractError("historical backfill planning requires the exact main repository")
    if _run_git(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ContractError("historical backfill planning requires a clean repository")
    return {
        "root": str(resolved),
        "branch": "main",
        "commit": _run_git(resolved, "rev-parse", "HEAD"),
        "tree": _run_git(resolved, "rev-parse", "HEAD^{tree}"),
    }


def _closure(root: Path, paths: Sequence[str]) -> dict[str, object]:
    files = [
        {"path": path, "sha256": sha256_file(root / path)}
        for path in sorted(paths)
    ]
    return {
        "files": files,
        "closure_sha256": sha256_bytes(canonical_json_bytes(files)),
    }


def load_historical_backfill_policy(
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy = _json_object(root / POLICY_PATH, label="historical backfill policy")
    if (
        policy.get("schema_version") != 1
        or policy.get("project") != PROJECT
        or policy.get("policy_type") != POLICY_TYPE
        or policy.get("source_key") != "alpaca_basic_delayed_sip"
        or policy.get("source_name") != SOURCE_NAME
        or policy.get("evidence_class") != "LEGACY_DISCOVERY"
        or policy.get("quality_state")
        != "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED"
    ):
        raise ContractError("historical backfill policy identity differs")
    if policy.get("authorities") != {
        "provider_access": False,
        "credential_access": False,
        "snapshot_write": False,
        "offline_verification": False,
        "publication": False,
        "activation": False,
        "research": False,
    }:
        raise ContractError("historical backfill policy grants authority")
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
        "host_timeout_seconds_per_execution_group": 1800,
        "max_pages_per_unit": 3,
        "max_response_bytes_per_page": 16777216,
    }:
        raise ContractError("historical backfill request contract differs")
    if policy.get("batching") != {
        "symbols_per_batch": 100,
        "batches_per_execution_group": 5,
        "max_initial_url_bytes": 4096,
    }:
        raise ContractError("historical backfill batching contract differs")
    if policy.get("window") != {
        "first_session": "2016-01-04",
        "last_session": "2026-07-10",
        "shard": "calendar_year",
        "expected_session_count": 2644,
    }:
        raise ContractError("historical backfill window differs")
    outputs = policy.get("outputs")
    if outputs != {
        "snapshot_store": (
            "data/vault/qualification/as_received/alpaca_sip_historical_backfill"
        ),
        "work_root": "data/w/alpaca_sip_historical_backfill",
        "plan_disposition": "PROCESS_LOCAL_CONVERSATION_ONLY",
        "publication": False,
    }:
        raise ContractError("historical backfill output contract differs")
    policy_id = sha256_bytes(canonical_json_bytes(policy))
    return policy, policy_id


def _active_source_binding(root: Path, policy: Mapping[str, Any]) -> dict[str, str]:
    binding = policy["source_config"]
    path = root / binding["path"]
    if sha256_file(path) != binding["sha256"]:
        raise IntegrityError("historical backfill source configuration changed")
    source = _json_object(path, label="source configuration").get("sources", {}).get(
        policy["source_key"]
    )
    if (
        not isinstance(source, dict)
        or source.get("enabled_for_active_pipeline") is not True
        or source.get("request_contract")
        != {
            "qualified_feed": "sip",
            "qualification_candidates": ["sip", "iex"],
            "timeframe": "1Day",
            "adjustment": "raw",
            "asof": None,
            "minimum_end_lag_minutes": 20,
            "sort": "asc",
        }
    ):
        raise IntegrityError("historical backfill active SIP source binding differs")
    return {
        "path": binding["path"],
        "sha256": binding["sha256"],
        "qualified_feed": "sip",
    }


def _identity_rows(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> list[dict[str, object]]:
    binding = policy["identity_release"]
    directory = root / binding["relative_directory"]
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        manifest.release_id != binding["release_id"]
        or manifest.dataset != "identity"
        or manifest.role != "prospective_as_received"
        or manifest.quality_state != "PASS"
        or sha256_file(directory / "identity_snapshots.json")
        != binding["identity_snapshots_sha256"]
    ):
        raise IntegrityError("historical backfill identity release binding differs")
    payload = _json_object(directory / "identity_snapshots.json", label="identity release")
    snapshots = payload.get("snapshots")
    if (
        not isinstance(snapshots, list)
        or len(snapshots) != 1
        or not isinstance(snapshots[0], dict)
        or snapshots[0].get("snapshot_id") != binding["identity_snapshot_id"]
        or not isinstance(snapshots[0].get("rows"), list)
    ):
        raise IntegrityError("historical backfill identity snapshot differs")
    return snapshots[0]["rows"]


def _rehabilitated_symbols(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> list[str]:
    binding = policy["rehabilitated_release"]
    directory = root / binding["relative_directory"]
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        manifest.release_id != binding["release_id"]
        or manifest.dataset != "alpaca_legacy_daily_bars"
        or manifest.role != "legacy_discovery"
        or manifest.quality_state != "LEGACY_CAVEATED"
        or sha256_file(directory / "bars.parquet") != binding["bars_sha256"]
    ):
        raise IntegrityError("rehabilitated Alpaca release binding differs")
    values = pq.read_table(directory / "bars.parquet", columns=["provider_symbol"])
    symbols = sorted(set(values.column("provider_symbol").to_pylist()))
    if len(symbols) != binding["symbol_count"]:
        raise IntegrityError("rehabilitated Alpaca symbol census differs")
    return symbols


def _calendar_sessions(
    root: Path,
    policy: Mapping[str, Any],
    accepted_root: Path,
) -> list[date]:
    binding = policy["calendar_release"]
    loaded = load_xnys_calendar_release(
        root / binding["relative_directory"],
        accepted_release_root=accepted_root,
    )
    if loaded.calendar.release_id != binding["release_id"]:
        raise IntegrityError("historical backfill calendar release differs")
    window = policy["window"]
    first = date.fromisoformat(window["first_session"])
    last = date.fromisoformat(window["last_session"])
    sessions = [
        row["session"]
        for row in loaded.schedule.to_pylist()
        if first <= row["session"] <= last
    ]
    if (
        len(sessions) != window["expected_session_count"]
        or sessions[0] != first
        or sessions[-1] != last
        or sessions != sorted(set(sessions))
    ):
        raise IntegrityError("historical backfill calendar session census differs")
    return sessions


def _symbol_digest(symbols: Sequence[str]) -> str:
    return sha256_bytes(canonical_json_bytes(list(symbols)))


def _select_cohort(
    rows: Iterable[Mapping[str, object]],
    rehabilitated_symbols: Iterable[str],
    *,
    policy: Mapping[str, Any],
) -> tuple[list[dict[str, str]], dict[str, object]]:
    identity_id = policy["identity_release"]["identity_snapshot_id"]
    selected: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise ContractError("identity rows must be objects")
        if (
            row.get("eligible") is not True
            or row.get("active") is not True
            or row.get("membership_present") is not True
            or row.get("security_type") not in {"STOCK", "ETF"}
        ):
            continue
        symbol = row.get("symbol")
        asset_id = row.get("asset_id")
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip().upper()
            or not isinstance(asset_id, str)
            or not asset_id
            or row.get("identity_snapshot_id") != identity_id
            or symbol in selected
        ):
            raise IntegrityError("eligible identity cohort contains an invalid duplicate")
        selected[symbol] = {
            "symbol": symbol,
            "asset_id": asset_id,
            "security_type": str(row["security_type"]),
        }
    eligible_symbols = sorted(selected)
    legacy = sorted(set(rehabilitated_symbols))
    if any(not isinstance(symbol, str) or not symbol for symbol in legacy):
        raise ContractError("rehabilitated symbol census is invalid")
    overlap = sorted(set(eligible_symbols) & set(legacy))
    missing = sorted(set(eligible_symbols) - set(legacy))
    legacy_only = sorted(set(legacy) - set(eligible_symbols))
    selection = policy["selection"]
    checks = (
        (len(eligible_symbols), selection["expected_eligible_count"]),
        (_symbol_digest(eligible_symbols), selection["expected_eligible_symbols_sha256"]),
        (len(overlap), selection["expected_overlap_count"]),
        (_symbol_digest(overlap), selection["expected_overlap_symbols_sha256"]),
        (len(missing), selection["expected_missing_count"]),
        (_symbol_digest(missing), selection["expected_missing_symbols_sha256"]),
        (len(legacy_only), selection["expected_legacy_only_count"]),
        (_symbol_digest(legacy_only), selection["expected_legacy_only_symbols_sha256"]),
    )
    if any(actual != expected for actual, expected in checks):
        raise IntegrityError("historical backfill cohort differs from policy")
    missing_rows = [selected[symbol] for symbol in missing]
    summary = {
        "eligible_count": len(eligible_symbols),
        "eligible_security_types": dict(
            sorted(Counter(row["security_type"] for row in selected.values()).items())
        ),
        "eligible_symbols_sha256": _symbol_digest(eligible_symbols),
        "rehabilitated_symbol_count": len(legacy),
        "rehabilitated_symbols_sha256": _symbol_digest(legacy),
        "overlap_count": len(overlap),
        "overlap_symbols_sha256": _symbol_digest(overlap),
        "missing_count": len(missing),
        "missing_symbols_sha256": _symbol_digest(missing),
        "legacy_only_count": len(legacy_only),
        "legacy_only_symbols_sha256": _symbol_digest(legacy_only),
    }
    return missing_rows, summary


def _year_windows(sessions: Sequence[date]) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    for year in sorted({session.year for session in sessions}):
        selected = [session for session in sessions if session.year == year]
        start = datetime.combine(selected[0], time.min, NEW_YORK).astimezone(
            timezone.utc
        )
        end = (
            datetime.combine(selected[-1] + timedelta(days=1), time.min, NEW_YORK)
            - timedelta(seconds=1)
        ).astimezone(timezone.utc)
        windows.append(
            {
                "year": year,
                "start": iso_z(start),
                "end": iso_z(end),
                "first_session": selected[0].isoformat(),
                "last_session": selected[-1].isoformat(),
                "session_count": len(selected),
            }
        )
    return windows


def _request_units(
    *,
    cohort: Sequence[Mapping[str, str]],
    windows: Sequence[Mapping[str, object]],
    policy: Mapping[str, Any],
    registry: NetworkAcquisitionRegistry,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    batching = policy["batching"]
    request_policy = policy["request_contract"]
    batches = [
        list(cohort[index : index + batching["symbols_per_batch"]])
        for index in range(0, len(cohort), batching["symbols_per_batch"])
    ]
    units: list[dict[str, object]] = []
    fixed_requested_at = parse_utc_z(
        "2026-07-30T04:20:00Z", "historical_backfill.fixed_requested_at"
    )
    bars_policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint=request_policy["endpoint"],
    )
    for batch_index, batch in enumerate(batches, start=1):
        symbols = tuple(row["symbol"] for row in batch)
        for window_index, window in enumerate(windows, start=1):
            request = AlpacaBarsRequest(
                symbols=symbols,
                start=parse_utc_z(str(window["start"]), "historical_backfill.start"),
                end=parse_utc_z(str(window["end"]), "historical_backfill.end"),
                requested_at=fixed_requested_at,
                limit=request_policy["limit"],
            )
            url = request.url(bars_policy)
            if len(url.encode("utf-8")) > batching["max_initial_url_bytes"]:
                raise ContractError("historical backfill request URL exceeds its bound")
            maximum_rows = len(symbols) * int(window["session_count"])
            expected_pages = math.ceil(maximum_rows / request_policy["limit"])
            if expected_pages > request_policy["max_pages_per_unit"]:
                raise ContractError("historical backfill request unit exceeds its page bound")
            network = NetworkRequestPlan.create(
                registry=registry,
                source=SOURCE_NAME,
                initial_url=url,
                timeout_seconds=request_policy["timeout_seconds"],
                max_response_bytes=request_policy["max_response_bytes_per_page"],
                max_pages=request_policy["max_pages_per_unit"],
                pagination_parameter="page_token",
            )
            units.append(
                {
                    "unit_index": len(units) + 1,
                    "batch_index": batch_index,
                    "window_index": window_index,
                    "symbols": list(symbols),
                    "asset_ids": [row["asset_id"] for row in batch],
                    "security_types": [row["security_type"] for row in batch],
                    "window": dict(window),
                    "maximum_possible_rows": maximum_rows,
                    "expected_maximum_pages": expected_pages,
                    "network_request_plan": network.as_dict(),
                }
            )
    group_size = batching["batches_per_execution_group"] * len(windows)
    groups: list[dict[str, object]] = []
    for index in range(0, len(units), group_size):
        selected = units[index : index + group_size]
        groups.append(
            {
                "group_index": len(groups) + 1,
                "first_unit": selected[0]["unit_index"],
                "last_unit": selected[-1]["unit_index"],
                "unit_count": len(selected),
                "maximum_gets": len(selected)
                * request_policy["max_pages_per_unit"],
                "maximum_response_bytes": len(selected)
                * request_policy["max_pages_per_unit"]
                * request_policy["max_response_bytes_per_page"],
                "host_timeout_seconds": request_policy[
                    "host_timeout_seconds_per_execution_group"
                ],
                "request_plan_ids_sha256": sha256_bytes(
                    canonical_json_bytes(
                        [
                            unit["network_request_plan"]["plan_id"]
                            for unit in selected
                        ]
                    )
                ),
            }
        )
    return units, groups


def _build_plan(
    *,
    root: Path,
    policy: Mapping[str, Any],
    policy_id: str,
    repository: Mapping[str, str],
    rows: Iterable[Mapping[str, object]],
    rehabilitated_symbols: Iterable[str],
    sessions: Sequence[date],
    registry: NetworkAcquisitionRegistry,
    source: Mapping[str, str],
    synthetic: bool,
) -> dict[str, object]:
    cohort, cohort_summary = _select_cohort(
        rows, rehabilitated_symbols, policy=policy
    )
    windows = _year_windows(sessions)
    units, groups = _request_units(
        cohort=cohort,
        windows=windows,
        policy=policy,
        registry=registry,
    )
    request = policy["request_contract"]
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "plan_type": "ALPACA_SIP_HISTORICAL_BACKFILL",
        "mode": "SYNTHETIC_PLAN_ONLY" if synthetic else "PLAN_ONLY",
        "repository": dict(repository),
        "policy_id": policy_id,
        "network_registry_id": registry.registry_id,
        "source": dict(source),
        "identity_release": dict(policy["identity_release"]),
        "rehabilitated_release": dict(policy["rehabilitated_release"]),
        "calendar_release": dict(policy["calendar_release"]),
        "cohort": cohort_summary,
        "evidence_boundary": {
            "evidence_class": "LEGACY_DISCOVERY",
            "quality_state": "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
            "historical_membership_proven": False,
            "survivorship_safe": False,
            "may_support_confirmation": False,
            "hfdl_included": False,
        },
        "windows": windows,
        "batch_count": math.ceil(
            len(cohort) / policy["batching"]["symbols_per_batch"]
        ),
        "request_unit_count": len(units),
        "request_units_sha256": sha256_bytes(canonical_json_bytes(units)),
        "request_units": units,
        "execution_groups": groups,
        "command_census": {
            "planned_request_units": len(units),
            "planned_execution_groups": len(groups),
            "maximum_gets": len(units) * request["max_pages_per_unit"],
            "maximum_response_bytes": (
                len(units)
                * request["max_pages_per_unit"]
                * request["max_response_bytes_per_page"]
            ),
            "timeout_seconds_per_get": request["timeout_seconds"],
            "host_timeout_seconds_per_group": request[
                "host_timeout_seconds_per_execution_group"
            ],
        },
        "credential_boundary": {
            "required_for_plan": False,
            "future_execution": (
                "process-local APCA_API_KEY_ID and APCA_API_SECRET_KEY only"
            ),
            "values_must_not_be_printed_logged_hashed_copied_or_retained": True,
        },
        "outputs": dict(policy["outputs"]),
        "verification_contract": {
            "network_required": False,
            "writes": False,
            "future_offline_checks": [
                "exact terminal pagination",
                "requested-symbol containment",
                "pinned-XNYS-session containment",
                "unique symbol-session rows",
                "valid daily raw OHLCV timestamps",
                "zero-row symbols retained as explicit exclusions",
            ],
        },
        "authorities": dict(policy["authorities"]),
        "stop_conditions": [
            "repository or closure drift",
            "accepted release or evidence hash mismatch",
            "cohort or calendar census drift",
            "network registry or request contract drift",
            "URL, page, response, request-count, or timeout bound violation",
            "credential, transport, pagination, landing, or verification failure",
            "unexpected write or output",
        ],
        "code_closure": _closure(root, CODE_CLOSURE_PATHS),
        "config_closure": _closure(root, CONFIG_CLOSURE_PATHS),
    }
    return {
        **unsigned,
        "backfill_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_historical_backfill_plan(
    *,
    repo_root: Path | None = None,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy, policy_id = load_historical_backfill_policy(root)
    accepted_root = (root / "data/vault/accepted").resolve(strict=True)
    registry = NetworkAcquisitionRegistry.load(
        root / policy["network_registry"],
        allowed_root=root,
    )
    if set(registry.allowed_origin_paths) != {SOURCE_NAME}:
        raise ContractError("historical backfill registry source census differs")
    return _build_plan(
        root=root,
        policy=policy,
        policy_id=policy_id,
        repository=_repository_binding(root),
        rows=_identity_rows(root, policy, accepted_root),
        rehabilitated_symbols=_rehabilitated_symbols(root, policy, accepted_root),
        sessions=_calendar_sessions(root, policy, accepted_root),
        registry=registry,
        source=_active_source_binding(root, policy),
        synthetic=False,
    )


def build_historical_backfill_fixture_plan(
    *,
    repo_root: Path,
    identity_rows: Iterable[Mapping[str, object]],
    rehabilitated_symbols: Iterable[str],
    sessions: Sequence[date],
    expected_selection: Mapping[str, object],
) -> dict[str, object]:
    root = repo_root.resolve(strict=True)
    policy, _ = load_historical_backfill_policy(root)
    fixture_policy = json.loads(json.dumps(policy))
    fixture_policy["selection"] = dict(expected_selection)
    fixture_policy["window"] = {
        "first_session": sessions[0].isoformat(),
        "last_session": sessions[-1].isoformat(),
        "shard": "calendar_year",
        "expected_session_count": len(sessions),
    }
    policy_id = sha256_bytes(canonical_json_bytes(fixture_policy))
    registry = NetworkAcquisitionRegistry.load(
        root / fixture_policy["network_registry"],
        allowed_root=root,
    )
    return _build_plan(
        root=root,
        policy=fixture_policy,
        policy_id=policy_id,
        repository={
            "root": str(root),
            "branch": "SYNTHETIC_ONLY",
            "commit": "0" * 64,
            "tree": "0" * 64,
        },
        rows=identity_rows,
        rehabilitated_symbols=rehabilitated_symbols,
        sessions=sessions,
        registry=registry,
        source={
            "path": "SYNTHETIC_ONLY",
            "sha256": "0" * 64,
            "qualified_feed": "sip",
        },
        synthetic=True,
    )


def plan_summary(plan: Mapping[str, object]) -> dict[str, object]:
    require_sha256(str(plan.get("backfill_plan_id")), "backfill plan ID")
    return {
        "backfill_plan_id": plan["backfill_plan_id"],
        "policy_id": plan["policy_id"],
        "network_registry_id": plan["network_registry_id"],
        "repository": plan["repository"],
        "cohort": plan["cohort"],
        "window_count": len(plan["windows"]),
        "batch_count": plan["batch_count"],
        "request_unit_count": plan["request_unit_count"],
        "request_units_sha256": plan["request_units_sha256"],
        "execution_groups": plan["execution_groups"],
        "command_census": plan["command_census"],
        "credential_boundary": plan["credential_boundary"],
        "outputs": plan["outputs"],
        "evidence_boundary": plan["evidence_boundary"],
        "authorities": plan["authorities"],
        "stop_conditions": plan["stop_conditions"],
    }
