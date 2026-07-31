from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import subprocess
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from ..canonical.alpaca import _accept_native_bar
from ..clock import TrustedClock, require_trusted_clock
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
from .alpaca import guarded_fetch_landed_pages
from .network_execution import NetworkRequestPlan, start_local_network_execution
from .snapshots import (
    AsReceivedSnapshotStore,
    LandedSnapshot,
    NetworkAcquisitionRegistry,
)


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
        or manifest.role != "legacy_discovery_only"
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
        "execution_contract": {
            "groups_per_invocation": 1,
            "units_in_plan_order": True,
            "fresh_network_session_per_unit": True,
            "approved_plan_id_required": True,
            "approved_group_request_plan_ids_sha256_required": True,
            "approved_continuation_plan_id_required": True,
            "retained_complete_unit_revalidation_before_network": True,
            "owner_confirmation_environment": "FREE_SOURCE_QUALIFICATION_APPROVED=YES",
            "credentials": "PROCESS_ENVIRONMENT_ONLY",
            "atomic_snapshot_landing": True,
            "offline_unit_verification_before_next_unit": True,
            "group_assessment_disposition": "PROCESS_LOCAL_CONVERSATION_ONLY",
            "retry": False,
            "cleanup": False,
            "publication": False,
        },
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
        "execution_contract": plan["execution_contract"],
        "command_census": plan["command_census"],
        "credential_boundary": plan["credential_boundary"],
        "outputs": plan["outputs"],
        "evidence_boundary": plan["evidence_boundary"],
        "authorities": plan["authorities"],
        "stop_conditions": plan["stop_conditions"],
    }


def _validated_plan_id(plan: Mapping[str, object]) -> str:
    if not isinstance(plan, Mapping):
        raise ContractError("historical backfill plan must be an object")
    plan_id = plan.get("backfill_plan_id")
    require_sha256(plan_id, "historical backfill plan ID")
    unsigned = {key: value for key, value in plan.items() if key != "backfill_plan_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id:
        raise IntegrityError("historical backfill plan ID differs")
    units = plan.get("request_units")
    if (
        plan.get("plan_type") != "ALPACA_SIP_HISTORICAL_BACKFILL"
        or plan.get("mode") not in {"PLAN_ONLY", "SYNTHETIC_PLAN_ONLY"}
        or not isinstance(units, list)
        or plan.get("request_unit_count") != len(units)
        or plan.get("request_units_sha256")
        != sha256_bytes(canonical_json_bytes(units))
        or plan.get("authorities")
        != {
            "provider_access": False,
            "credential_access": False,
            "snapshot_write": False,
            "offline_verification": False,
            "publication": False,
            "activation": False,
            "research": False,
        }
        or plan.get("execution_contract")
        != {
            "groups_per_invocation": 1,
            "units_in_plan_order": True,
            "fresh_network_session_per_unit": True,
            "approved_plan_id_required": True,
            "approved_group_request_plan_ids_sha256_required": True,
            "approved_continuation_plan_id_required": True,
            "retained_complete_unit_revalidation_before_network": True,
            "owner_confirmation_environment": "FREE_SOURCE_QUALIFICATION_APPROVED=YES",
            "credentials": "PROCESS_ENVIRONMENT_ONLY",
            "atomic_snapshot_landing": True,
            "offline_unit_verification_before_next_unit": True,
            "group_assessment_disposition": "PROCESS_LOCAL_CONVERSATION_ONLY",
            "retry": False,
            "cleanup": False,
            "publication": False,
        }
    ):
        raise IntegrityError("historical backfill plan contract differs")
    if [unit.get("unit_index") for unit in units] != list(
        range(1, len(units) + 1)
    ):
        raise IntegrityError("historical backfill unit ordering differs")
    return str(plan_id)


def _network_plan_from_unit(
    unit: Mapping[str, object],
    *,
    registry: NetworkAcquisitionRegistry,
) -> NetworkRequestPlan:
    value = unit.get("network_request_plan")
    if not isinstance(value, Mapping):
        raise ContractError("historical backfill unit lacks a network plan")
    try:
        plan = NetworkRequestPlan(**dict(value))
    except TypeError as exc:
        raise ContractError("historical backfill network plan fields differ") from exc
    plan.validate(registry=registry)
    return plan


def _selected_execution_group(
    plan: Mapping[str, object],
    *,
    group_index: int,
    registry: NetworkAcquisitionRegistry,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    _validated_plan_id(plan)
    if type(group_index) is not int or group_index < 1:
        raise ContractError("historical backfill group index must be positive")
    groups = plan.get("execution_groups")
    units = plan.get("request_units")
    if not isinstance(groups, list) or not isinstance(units, list):
        raise IntegrityError("historical backfill execution census differs")
    matches = [group for group in groups if group.get("group_index") == group_index]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise ContractError("historical backfill execution group is absent")
    group = matches[0]
    first = group.get("first_unit")
    last = group.get("last_unit")
    if type(first) is not int or type(last) is not int or first > last:
        raise IntegrityError("historical backfill execution group bounds differ")
    selected = [unit for unit in units if first <= unit.get("unit_index", 0) <= last]
    if (
        len(selected) != group.get("unit_count")
        or [unit["unit_index"] for unit in selected] != list(range(first, last + 1))
    ):
        raise IntegrityError("historical backfill execution group unit census differs")
    request_ids = [
        _network_plan_from_unit(unit, registry=registry).plan_id
        for unit in selected
    ]
    if sha256_bytes(canonical_json_bytes(request_ids)) != group.get(
        "request_plan_ids_sha256"
    ):
        raise IntegrityError("historical backfill execution group identity differs")
    if (
        group.get("maximum_gets") != len(selected) * 3
        or group.get("maximum_response_bytes")
        != len(selected) * 3 * 16777216
        or group.get("host_timeout_seconds") != 1800
    ):
        raise IntegrityError("historical backfill execution group bounds differ")
    return group, selected


def _unit_calendar_sessions(
    unit: Mapping[str, object],
    calendar_sessions: Sequence[date],
) -> set[date]:
    window = unit.get("window")
    if not isinstance(window, Mapping):
        raise ContractError("historical backfill unit window differs")
    first = date.fromisoformat(str(window.get("first_session")))
    last = date.fromisoformat(str(window.get("last_session")))
    selected = {session for session in calendar_sessions if first <= session <= last}
    if (
        len(selected) != window.get("session_count")
        or not selected
        or min(selected) != first
        or max(selected) != last
    ):
        raise IntegrityError("historical backfill unit calendar binding differs")
    return selected


def _normalize_zero_vwap_for_verification(
    bar: object,
) -> tuple[object, bool]:
    """Map Alpaca's exact numeric zero VWAP sentinel to unavailable."""

    if type(bar) is not dict:
        return bar, False
    vwap = bar.get("vw")
    if type(vwap) in {int, float} and float(vwap) == 0.0:
        normalized = dict(bar)
        normalized["vw"] = None
        return normalized, True
    return bar, False


def verify_historical_backfill_unit(
    unit: Mapping[str, object],
    pages: Sequence[LandedSnapshot],
    *,
    calendar_sessions: Sequence[date],
    registry: NetworkAcquisitionRegistry,
    synthetic: bool,
) -> dict[str, object]:
    network_plan = _network_plan_from_unit(unit, registry=registry)
    symbols = unit.get("symbols")
    asset_ids = unit.get("asset_ids")
    security_types = unit.get("security_types")
    if (
        not isinstance(symbols, list)
        or not symbols
        or symbols != sorted(set(symbols))
        or not isinstance(asset_ids, list)
        or len(asset_ids) != len(symbols)
        or len(set(asset_ids)) != len(asset_ids)
        or not isinstance(security_types, list)
        or len(security_types) != len(symbols)
        or set(security_types) - {"STOCK", "ETF"}
    ):
        raise IntegrityError("historical backfill unit identity mapping differs")
    page_list = list(pages)
    if not 1 <= len(page_list) <= network_plan.max_pages:
        raise IntegrityError("historical backfill unit page census differs")
    allowed_sessions = _unit_calendar_sessions(unit, calendar_sessions)
    requested_at = page_list[0].requested_at
    if requested_at is None:
        raise IntegrityError("historical backfill snapshot request time is absent")
    window = unit["window"]
    request_policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint="https://data.alpaca.markets/v2/stocks/bars",
    )
    expected_token: str | None = None
    seen_tokens: set[str] = set()
    seen_keys: set[tuple[str, object]] = set()
    observed_symbols: set[str] = set()
    observed_sessions: set[date] = set()
    normalized_zero_vwap_rows = 0
    snapshot_rows: list[dict[str, object]] = []
    for page_index, snapshot in enumerate(page_list):
        request = AlpacaBarsRequest(
            symbols=tuple(symbols),
            start=parse_utc_z(str(window["start"]), "historical_backfill.start"),
            end=parse_utc_z(str(window["end"]), "historical_backfill.end"),
            requested_at=requested_at,
            limit=10000,
            page_token=expected_token,
        )
        if (
            snapshot.source != SOURCE_NAME
            or snapshot.url != request.url(request_policy)
            or snapshot.http_status != 200
            or snapshot.request_plan_id != network_plan.plan_id
            or snapshot.requested_at != requested_at
            or snapshot.retrieved_at < requested_at
            or (not synthetic and not snapshot.local_integrity_verified)
        ):
            raise IntegrityError("historical backfill snapshot binding differs")
        try:
            payload = json.loads(snapshot.read_verified_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ContractError("historical backfill response is invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or set(payload) != {"bars", "next_page_token"}
            or not isinstance(payload["bars"], dict)
            or set(payload["bars"]) - set(symbols)
        ):
            raise ContractError("historical backfill response schema differs")
        page_bar_count = 0
        page_normalized_zero_vwap_rows = 0
        for symbol, bars in payload["bars"].items():
            if not isinstance(bars, list) or not bars:
                raise ContractError("historical backfill symbol bars are empty or invalid")
            for bar in bars:
                normalized_bar, normalized = (
                    _normalize_zero_vwap_for_verification(bar)
                )
                accepted = _accept_native_bar(
                    symbol=symbol,
                    bar=normalized_bar,
                    eastern=NEW_YORK,
                    seen_keys=seen_keys,
                )
                session = accepted[1]
                if session not in allowed_sessions:
                    raise ContractError("historical backfill bar is outside the pinned calendar window")
                observed_symbols.add(symbol)
                observed_sessions.add(session)
                page_bar_count += 1
                if normalized:
                    normalized_zero_vwap_rows += 1
                    page_normalized_zero_vwap_rows += 1
        token = payload["next_page_token"]
        if token is not None and (
            not isinstance(token, str) or not token or token in seen_tokens
        ):
            raise ContractError("historical backfill pagination token is malformed or repeated")
        if isinstance(token, str):
            seen_tokens.add(token)
        if page_index < len(page_list) - 1 and token is None:
            raise ContractError("historical backfill pagination terminated before the final page")
        if page_index == len(page_list) - 1 and token is not None:
            raise ContractError("historical backfill pagination is not terminal")
        expected_token = token
        snapshot_rows.append(
            {
                "page_index": page_index + 1,
                "snapshot_id": snapshot.snapshot_id,
                "raw_sha256": snapshot.raw_sha256,
                "raw_bytes": snapshot.raw_path.stat().st_size,
                "bar_count": page_bar_count,
                "normalized_zero_vwap_rows": page_normalized_zero_vwap_rows,
            }
        )
    zero_row_symbols = sorted(set(symbols) - observed_symbols)
    unsigned = {
        "schema_version": 1,
        "mode": (
            "SYNTHETIC_BACKFILL_UNIT_VERIFICATION"
            if synthetic
            else "BACKFILL_UNIT_VERIFIED_NOT_PUBLISHED"
        ),
        "unit_index": unit["unit_index"],
        "network_request_plan_id": network_plan.plan_id,
        "symbol_count": len(symbols),
        "observed_symbol_count": len(observed_symbols),
        "zero_row_symbol_count": len(zero_row_symbols),
        "zero_row_symbols": zero_row_symbols,
        "bar_count": len(seen_keys),
        "normalized_zero_vwap_rows": normalized_zero_vwap_rows,
        "observed_session_count": len(observed_sessions),
        "first_observed_session": (
            min(observed_sessions).isoformat() if observed_sessions else None
        ),
        "last_observed_session": (
            max(observed_sessions).isoformat() if observed_sessions else None
        ),
        "pages": snapshot_rows,
        "terminal_pagination": True,
        "local_integrity_verified": not synthetic,
        "historical_membership_proven": False,
        "publication": False,
    }
    return {
        **unsigned,
        "unit_assessment_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def _retained_snapshot_inventory(
    store: AsReceivedSnapshotStore,
) -> tuple[LandedSnapshot, ...]:
    source_root = store.root / SOURCE_NAME
    if not source_root.exists():
        return ()
    if not source_root.is_dir():
        raise IntegrityError("historical backfill snapshot source root differs")
    snapshots: list[LandedSnapshot] = []
    for candidate in sorted(source_root.iterdir(), key=lambda path: path.name):
        if not candidate.is_dir():
            raise IntegrityError("historical backfill snapshot source tree differs")
        snapshots.append(store.load(candidate))
    return tuple(snapshots)


def _ordered_retained_lineage(
    unit: Mapping[str, object],
    candidates: Sequence[LandedSnapshot],
    *,
    registry: NetworkAcquisitionRegistry,
) -> tuple[LandedSnapshot, ...] | None:
    if not candidates:
        return None
    network_plan = _network_plan_from_unit(unit, registry=registry)
    requested_at = candidates[0].requested_at
    if requested_at is None or any(
        snapshot.requested_at != requested_at
        or snapshot.request_plan_id != network_plan.plan_id
        for snapshot in candidates
    ):
        raise IntegrityError("retained historical backfill request binding differs")
    by_url: dict[str, LandedSnapshot] = {}
    for snapshot in candidates:
        if snapshot.url in by_url:
            raise IntegrityError("retained historical backfill lineage is ambiguous")
        by_url[snapshot.url] = snapshot
    symbols = unit["symbols"]
    window = unit["window"]
    policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint="https://data.alpaca.markets/v2/stocks/bars",
    )
    expected_token: str | None = None
    seen_tokens: set[str] = set()
    ordered: list[LandedSnapshot] = []
    for _page_index in range(network_plan.max_pages):
        request = AlpacaBarsRequest(
            symbols=tuple(symbols),
            start=parse_utc_z(str(window["start"]), "historical_backfill.start"),
            end=parse_utc_z(str(window["end"]), "historical_backfill.end"),
            requested_at=requested_at,
            limit=10000,
            page_token=expected_token,
        )
        snapshot = by_url.pop(request.url(policy), None)
        if snapshot is None:
            if by_url:
                raise IntegrityError(
                    "retained historical backfill lineage URL differs"
                )
            return None
        try:
            payload = json.loads(snapshot.read_verified_bytes())
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            raise ContractError(
                "retained historical backfill response is invalid JSON"
            ) from exc
        if not isinstance(payload, dict) or "next_page_token" not in payload:
            raise ContractError("retained historical backfill pagination differs")
        token = payload["next_page_token"]
        if token is not None and (
            not isinstance(token, str) or not token or token in seen_tokens
        ):
            raise ContractError("retained historical backfill pagination differs")
        ordered.append(snapshot)
        if token is None:
            if by_url:
                raise IntegrityError(
                    "retained historical backfill lineage has extra pages"
                )
            return tuple(ordered)
        seen_tokens.add(token)
        expected_token = token
    if by_url:
        raise IntegrityError("retained historical backfill lineage exceeds its bound")
    return None


def build_historical_backfill_group_continuation(
    *,
    backfill_plan: Mapping[str, object],
    group_index: int,
    snapshot_store: AsReceivedSnapshotStore,
    calendar_sessions: Sequence[date],
    registry: NetworkAcquisitionRegistry,
    synthetic: bool,
) -> dict[str, object]:
    plan_id = _validated_plan_id(backfill_plan)
    group, units = _selected_execution_group(
        backfill_plan,
        group_index=group_index,
        registry=registry,
    )
    unit_by_request_id = {
        _network_plan_from_unit(unit, registry=registry).plan_id: unit
        for unit in units
    }
    inventory = _retained_snapshot_inventory(snapshot_store)
    matching = [
        snapshot
        for snapshot in inventory
        if snapshot.request_plan_id in unit_by_request_id
    ]
    by_request: dict[str, list[LandedSnapshot]] = {}
    for snapshot in matching:
        if snapshot.requested_at is None:
            raise IntegrityError("retained historical backfill request time is absent")
        by_request.setdefault(str(snapshot.request_plan_id), []).append(snapshot)

    retained_units: list[dict[str, object]] = []
    capture_unit_indices: list[int] = []
    candidate_lineage_count = 0
    incomplete_lineage_count = 0
    superseded_valid_lineage_count = 0
    for unit in units:
        network_plan = _network_plan_from_unit(unit, registry=registry)
        grouped: dict[datetime, list[LandedSnapshot]] = {}
        for snapshot in by_request.get(network_plan.plan_id, []):
            if snapshot.requested_at is None:
                raise IntegrityError(
                    "retained historical backfill request time is absent"
                )
            grouped.setdefault(snapshot.requested_at, []).append(snapshot)
        valid: list[
            tuple[datetime, tuple[LandedSnapshot, ...], dict[str, object]]
        ] = []
        candidate_lineage_count += len(grouped)
        for requested_at, candidates in sorted(grouped.items()):
            ordered = _ordered_retained_lineage(
                unit,
                candidates,
                registry=registry,
            )
            if ordered is None:
                incomplete_lineage_count += 1
                continue
            assessment = verify_historical_backfill_unit(
                unit,
                ordered,
                calendar_sessions=calendar_sessions,
                registry=registry,
                synthetic=synthetic,
            )
            valid.append((requested_at, ordered, assessment))
        if not valid:
            capture_unit_indices.append(int(unit["unit_index"]))
            continue
        selected_at, selected_pages, selected_assessment = max(
            valid,
            key=lambda item: item[0],
        )
        superseded_valid_lineage_count += len(valid) - 1
        retained_units.append(
            {
                "unit_index": unit["unit_index"],
                "network_request_plan_id": network_plan.plan_id,
                "requested_at": iso_z(selected_at),
                "snapshot_ids": [page.snapshot_id for page in selected_pages],
                "raw_sha256s": [page.raw_sha256 for page in selected_pages],
                "page_count": len(selected_pages),
                "unit_assessment_id": selected_assessment["unit_assessment_id"],
            }
        )

    request = backfill_plan["command_census"]
    max_pages = 3
    max_bytes_per_page = 16777216
    selected_snapshot_ids = [
        snapshot_id
        for retained in retained_units
        for snapshot_id in retained["snapshot_ids"]
    ]
    matching_snapshot_ids = sorted(snapshot.snapshot_id for snapshot in matching)
    unsigned = {
        "schema_version": 1,
        "plan_type": "ALPACA_SIP_HISTORICAL_BACKFILL_GROUP_CONTINUATION",
        "mode": "SYNTHETIC_PLAN_ONLY" if synthetic else "PLAN_ONLY",
        "backfill_plan_id": plan_id,
        "group_index": group_index,
        "group_request_plan_ids_sha256": group["request_plan_ids_sha256"],
        "first_unit": group["first_unit"],
        "last_unit": group["last_unit"],
        "unit_count": group["unit_count"],
        "selection_policy": (
            "NEWEST_COMPLETE_VALID_LINEAGE_BY_REQUESTED_AT_EXACTLY_BOUND"
        ),
        "matching_snapshot_count": len(matching),
        "matching_snapshot_ids_sha256": sha256_bytes(
            canonical_json_bytes(matching_snapshot_ids)
        ),
        "candidate_lineage_count": candidate_lineage_count,
        "incomplete_lineage_count": incomplete_lineage_count,
        "superseded_valid_lineage_count": superseded_valid_lineage_count,
        "retained_unit_count": len(retained_units),
        "retained_page_count": len(selected_snapshot_ids),
        "selected_snapshot_ids_sha256": sha256_bytes(
            canonical_json_bytes(selected_snapshot_ids)
        ),
        "retained_units": retained_units,
        "capture_unit_count": len(capture_unit_indices),
        "capture_unit_indices": capture_unit_indices,
        "capture_unit_indices_sha256": sha256_bytes(
            canonical_json_bytes(capture_unit_indices)
        ),
        "command_census": {
            "maximum_new_gets": len(capture_unit_indices) * max_pages,
            "maximum_new_response_bytes": (
                len(capture_unit_indices) * max_pages * max_bytes_per_page
            ),
            "timeout_seconds_per_get": request["timeout_seconds_per_get"],
            "host_timeout_seconds": group["host_timeout_seconds"],
        },
        "execution_contract": {
            "retained_pages_reverified_before_network": True,
            "selected_snapshot_ids_exactly_bound": True,
            "capture_only_missing_units": True,
            "units_in_plan_order": True,
            "retry": False,
            "cleanup": False,
            "publication": False,
        },
        "authorities": {
            "provider_access": False,
            "credential_access": False,
            "snapshot_write": False,
            "publication": False,
            "activation": False,
            "research": False,
        },
        "stop_conditions": [
            "backfill plan, group, or retained snapshot census drift",
            "snapshot identity, lineage, pagination, or verification failure",
            "continuation plan identity or approval mismatch",
            "request, response, timeout, landing, or output bound violation",
            "unexpected write, retry, cleanup, publication, or activation",
        ],
    }
    return {
        **unsigned,
        "continuation_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_historical_backfill_group_continuation_plan(
    *,
    backfill_plan: Mapping[str, object],
    group_index: int,
    repo_root: Path | None = None,
) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy, _ = load_historical_backfill_policy(root)
    registry = NetworkAcquisitionRegistry.load(
        root / policy["network_registry"],
        allowed_root=root,
    )
    accepted_root = (root / "data/vault/accepted").resolve(strict=True)
    store = AsReceivedSnapshotStore(
        root / policy["outputs"]["snapshot_store"],
        allowed_root=root / "data",
        acquisition_registry=registry,
    )
    return build_historical_backfill_group_continuation(
        backfill_plan=backfill_plan,
        group_index=group_index,
        snapshot_store=store,
        calendar_sessions=_calendar_sessions(root, policy, accepted_root),
        registry=registry,
        synthetic=False,
    )


def continuation_plan_summary(
    plan: Mapping[str, object],
) -> dict[str, object]:
    plan_id = require_sha256(
        plan.get("continuation_plan_id"),
        "historical backfill continuation plan ID",
    )
    unsigned = {
        key: value for key, value in plan.items() if key != "continuation_plan_id"
    }
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id:
        raise IntegrityError("historical backfill continuation plan ID differs")
    return {
        key: plan[key]
        for key in (
            "continuation_plan_id",
            "backfill_plan_id",
            "group_index",
            "group_request_plan_ids_sha256",
            "unit_count",
            "matching_snapshot_count",
            "matching_snapshot_ids_sha256",
            "candidate_lineage_count",
            "incomplete_lineage_count",
            "superseded_valid_lineage_count",
            "retained_unit_count",
            "retained_page_count",
            "selected_snapshot_ids_sha256",
            "capture_unit_count",
            "capture_unit_indices_sha256",
            "command_census",
            "execution_contract",
            "authorities",
            "stop_conditions",
        )
    }


def _validated_continuation_plan(
    continuation: Mapping[str, object],
    *,
    backfill_plan_id: str,
    group: Mapping[str, object],
) -> str:
    plan_id = continuation.get("continuation_plan_id")
    require_sha256(plan_id, "historical backfill continuation plan ID")
    unsigned = {
        key: value
        for key, value in continuation.items()
        if key != "continuation_plan_id"
    }
    if (
        sha256_bytes(canonical_json_bytes(unsigned)) != plan_id
        or continuation.get("plan_type")
        != "ALPACA_SIP_HISTORICAL_BACKFILL_GROUP_CONTINUATION"
        or continuation.get("backfill_plan_id") != backfill_plan_id
        or continuation.get("group_index") != group.get("group_index")
        or continuation.get("group_request_plan_ids_sha256")
        != group.get("request_plan_ids_sha256")
        or continuation.get("unit_count") != group.get("unit_count")
    ):
        raise IntegrityError("historical backfill continuation plan differs")
    return str(plan_id)


def _load_retained_pages_from_continuation(
    continuation: Mapping[str, object],
    *,
    snapshot_store: AsReceivedSnapshotStore,
) -> dict[int, tuple[LandedSnapshot, ...]]:
    retained = continuation.get("retained_units")
    if not isinstance(retained, list):
        raise IntegrityError("historical backfill retained-unit plan differs")
    loaded: dict[int, tuple[LandedSnapshot, ...]] = {}
    for item in retained:
        if not isinstance(item, Mapping):
            raise IntegrityError("historical backfill retained-unit entry differs")
        unit_index = item.get("unit_index")
        snapshot_ids = item.get("snapshot_ids")
        raw_hashes = item.get("raw_sha256s")
        if (
            type(unit_index) is not int
            or unit_index in loaded
            or not isinstance(snapshot_ids, list)
            or not isinstance(raw_hashes, list)
            or len(snapshot_ids) != len(raw_hashes)
            or len(snapshot_ids) != item.get("page_count")
        ):
            raise IntegrityError("historical backfill retained-unit entry differs")
        pages = tuple(
            snapshot_store.load(
                snapshot_store.root / SOURCE_NAME / str(snapshot_id)
            )
            for snapshot_id in snapshot_ids
        )
        if [page.raw_sha256 for page in pages] != raw_hashes:
            raise IntegrityError("historical backfill retained raw identity differs")
        loaded[unit_index] = pages
    return loaded


def run_historical_backfill_group(
    *,
    backfill_plan: Mapping[str, object],
    approved_backfill_plan_id: str,
    group_index: int,
    approved_group_request_plan_ids_sha256: str,
    capture_unit: Callable[[Mapping[str, object]], Sequence[LandedSnapshot]],
    calendar_sessions: Sequence[date],
    registry: NetworkAcquisitionRegistry,
    synthetic: bool,
    continuation_plan: Mapping[str, object] | None = None,
    retained_pages_by_unit: Mapping[
        int, Sequence[LandedSnapshot]
    ] | None = None,
) -> tuple[tuple[LandedSnapshot, ...], dict[str, object]]:
    plan_id = _validated_plan_id(backfill_plan)
    require_sha256(approved_backfill_plan_id, "approved historical backfill plan ID")
    require_sha256(
        approved_group_request_plan_ids_sha256,
        "approved historical backfill group request-plan hash",
    )
    group, units = _selected_execution_group(
        backfill_plan,
        group_index=group_index,
        registry=registry,
    )
    if plan_id != approved_backfill_plan_id:
        raise PermissionError("approved historical backfill plan ID differs")
    if (
        group["request_plan_ids_sha256"]
        != approved_group_request_plan_ids_sha256
    ):
        raise PermissionError("approved historical backfill execution group differs")
    if (continuation_plan is None) != (retained_pages_by_unit is None):
        raise ContractError("historical backfill continuation inputs are incomplete")
    continuation_plan_id: str | None = None
    retained_map = dict(retained_pages_by_unit or {})
    planned_retained_entries: dict[int, Mapping[str, object]] = {}
    if continuation_plan is not None:
        continuation_plan_id = _validated_continuation_plan(
            continuation_plan,
            backfill_plan_id=plan_id,
            group=group,
        )
        planned_retained = {
            item["unit_index"] for item in continuation_plan["retained_units"]
        }
        planned_retained_entries = {
            item["unit_index"]: item
            for item in continuation_plan["retained_units"]
        }
        planned_capture = set(continuation_plan["capture_unit_indices"])
        selected_indices = {unit["unit_index"] for unit in units}
        if (
            set(retained_map) != planned_retained
            or planned_retained & planned_capture
            or planned_retained | planned_capture != selected_indices
        ):
            raise IntegrityError("historical backfill continuation unit census differs")
    assessments: list[dict[str, object]] = []
    snapshots: list[LandedSnapshot] = []
    retained_unit_indices: list[int] = []
    captured_unit_indices: list[int] = []
    retained_assessments: dict[int, dict[str, object]] = {}
    for unit in units:
        unit_index = int(unit["unit_index"])
        if unit_index not in retained_map:
            continue
        pages = tuple(retained_map[unit_index])
        planned_entry = planned_retained_entries[unit_index]
        if [page.snapshot_id for page in pages] != planned_entry["snapshot_ids"]:
            raise IntegrityError("historical backfill retained selection differs")
        assessment = verify_historical_backfill_unit(
            unit,
            pages,
            calendar_sessions=calendar_sessions,
            registry=registry,
            synthetic=synthetic,
        )
        if assessment["unit_assessment_id"] != planned_entry["unit_assessment_id"]:
            raise IntegrityError("historical backfill retained assessment differs")
        retained_assessments[unit_index] = assessment
    for unit in units:
        unit_index = int(unit["unit_index"])
        if unit_index in retained_map:
            pages = tuple(retained_map[unit_index])
            assessment = retained_assessments[unit_index]
            retained_unit_indices.append(unit_index)
        else:
            pages = tuple(capture_unit(unit))
            assessment = verify_historical_backfill_unit(
                unit,
                pages,
                calendar_sessions=calendar_sessions,
                registry=registry,
                synthetic=synthetic,
            )
            captured_unit_indices.append(unit_index)
        snapshots.extend(pages)
        assessments.append(assessment)
    unsigned = {
        "schema_version": 1,
        "mode": (
            "SYNTHETIC_BACKFILL_GROUP_ASSESSMENT"
            if synthetic
            else "BACKFILL_GROUP_VERIFIED_NOT_PUBLISHED"
        ),
        "backfill_plan_id": plan_id,
        "continuation_plan_id": continuation_plan_id,
        "group_index": group_index,
        "group_request_plan_ids_sha256": group["request_plan_ids_sha256"],
        "first_unit": group["first_unit"],
        "last_unit": group["last_unit"],
        "unit_count": len(units),
        "page_count": len(snapshots),
        "bar_count": sum(item["bar_count"] for item in assessments),
        "normalized_zero_vwap_rows": sum(
            item["normalized_zero_vwap_rows"] for item in assessments
        ),
        "retained_unit_count": len(retained_unit_indices),
        "retained_unit_indices": retained_unit_indices,
        "captured_unit_count": len(captured_unit_indices),
        "captured_unit_indices": captured_unit_indices,
        "observed_symbol_unit_count": sum(
            item["observed_symbol_count"] for item in assessments
        ),
        "zero_row_symbol_unit_count": sum(
            item["zero_row_symbol_count"] for item in assessments
        ),
        "local_integrity_verified": not synthetic,
        "unit_assessments": assessments,
        "evidence_class": "LEGACY_DISCOVERY",
        "quality_state": "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
        "historical_membership_proven": False,
        "survivorship_safe": False,
        "publication": False,
        "activation": False,
        "research": False,
    }
    return tuple(snapshots), {
        **unsigned,
        "group_assessment_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def execute_historical_backfill_group(
    *,
    backfill_plan: Mapping[str, object],
    approved_backfill_plan_id: str,
    group_index: int,
    approved_group_request_plan_ids_sha256: str,
    approved_continuation_plan_id: str,
    api_key_id: str,
    api_secret_key: str,
    clock: TrustedClock | None = None,
    repo_root: Path | None = None,
) -> tuple[tuple[LandedSnapshot, ...], dict[str, object]]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise ContractError("historical backfill execution requires production UTC")
    if not api_key_id or not api_secret_key:
        raise PermissionError("Alpaca credentials are absent from the process environment")
    current_plan = build_historical_backfill_plan(repo_root=root)
    if current_plan["backfill_plan_id"] != approved_backfill_plan_id:
        raise IntegrityError("historical backfill plan changed before execution")
    if backfill_plan.get("backfill_plan_id") != current_plan["backfill_plan_id"]:
        raise IntegrityError("supplied historical backfill plan is stale")
    policy, _ = load_historical_backfill_policy(root)
    registry = NetworkAcquisitionRegistry.load(
        root / policy["network_registry"],
        allowed_root=root,
    )
    accepted_root = (root / "data/vault/accepted").resolve(strict=True)
    calendar_sessions = _calendar_sessions(root, policy, accepted_root)
    store = AsReceivedSnapshotStore(
        root / policy["outputs"]["snapshot_store"],
        allowed_root=root / "data",
        acquisition_registry=registry,
    )
    current_continuation = build_historical_backfill_group_continuation(
        backfill_plan=current_plan,
        group_index=group_index,
        snapshot_store=store,
        calendar_sessions=calendar_sessions,
        registry=registry,
        synthetic=False,
    )
    require_sha256(
        approved_continuation_plan_id,
        "approved historical backfill continuation plan ID",
    )
    if (
        current_continuation["continuation_plan_id"]
        != approved_continuation_plan_id
    ):
        raise IntegrityError("historical backfill continuation plan changed")
    retained_pages = _load_retained_pages_from_continuation(
        current_continuation,
        snapshot_store=store,
    )
    request_contract = policy["request_contract"]
    bars_policy = AlpacaBarsPolicy(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof=None,
        sort="asc",
        minimum_end_lag_minutes=20,
        endpoint=request_contract["endpoint"],
    )

    def capture(unit: Mapping[str, object]) -> Sequence[LandedSnapshot]:
        network_plan = _network_plan_from_unit(unit, registry=registry)
        window = unit["window"]
        request = AlpacaBarsRequest(
            symbols=tuple(unit["symbols"]),
            start=parse_utc_z(str(window["start"]), "historical_backfill.start"),
            end=parse_utc_z(str(window["end"]), "historical_backfill.end"),
            requested_at=trusted_clock.now(),
            limit=request_contract["limit"],
        )
        if request.url(bars_policy) != network_plan.initial_url:
            raise IntegrityError("historical backfill request URL changed before execution")
        session = start_local_network_execution(
            network_plan,
            registry=registry,
            clock=trusted_clock,
        )
        return guarded_fetch_landed_pages(
            request,
            snapshot_store=store,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            policy=bars_policy,
            network_enabled=True,
            max_pages=request_contract["max_pages_per_unit"],
            clock=trusted_clock,
            authorization_session=session,
            source=SOURCE_NAME,
            timeout_seconds=request_contract["timeout_seconds"],
            max_response_bytes=request_contract["max_response_bytes_per_page"],
        )

    return run_historical_backfill_group(
        backfill_plan=current_plan,
        approved_backfill_plan_id=approved_backfill_plan_id,
        group_index=group_index,
        approved_group_request_plan_ids_sha256=(
            approved_group_request_plan_ids_sha256
        ),
        capture_unit=capture,
        calendar_sessions=calendar_sessions,
        registry=registry,
        synthetic=False,
        continuation_plan=current_continuation,
        retained_pages_by_unit=retained_pages,
    )
