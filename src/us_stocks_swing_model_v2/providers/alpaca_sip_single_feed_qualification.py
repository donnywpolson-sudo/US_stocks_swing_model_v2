"""Bounded, no-publication Alpaca SIP source-qualification lane."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import subprocess
from time import monotonic
from typing import Any, Mapping

from ..clock import TrustedClock, require_trusted_clock
from ..common import canonical_json_bytes, iso_z, parse_utc_z, require_sha256, sha256_bytes, sha256_file
from ..errors import ContractError, IntegrityError
from ..exchange_calendar import load_xnys_calendar_release
from .alpaca import AlpacaBarsPolicy, AlpacaBarsRequest, assess_landed_alpaca_sip, guarded_fetch_landed_pages
from .network_execution import NetworkRequestPlan, start_local_network_execution
from .snapshots import AsReceivedSnapshotStore, LandedSnapshot, NetworkAcquisitionRegistry


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_sip_single_feed_qualification_policy.json"
SOURCE_NAME = "alpaca_sip_qualification"
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_sip_single_feed_qualification.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/network_execution.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/cli/qualify_alpaca_sip.py",
    "src/us_stocks_swing_model_v2/exchange_calendar.py",
)
CONFIG_CLOSURE_PATHS = (POLICY_PATH, "config/sources.json", "config/network_acquisition_registry.json", "config/environment.lock.json")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _run_git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError(f"git {' '.join(args)} failed") from exc
    return result.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    resolved = root.resolve(strict=True)
    if Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve(strict=True) != resolved:
        raise ContractError("SIP qualification requires the exact repository root")
    if _run_git(resolved, "branch", "--show-current") != "main":
        raise ContractError("SIP qualification requires main")
    if _run_git(resolved, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ContractError("SIP qualification requires a clean repository closure")
    return {"root": str(resolved), "branch": "main", "commit": _run_git(resolved, "rev-parse", "HEAD"), "tree": _run_git(resolved, "rev-parse", "HEAD^{tree}")}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    files = [{"path": path, "sha256": sha256_file(root / path)} for path in sorted(paths)]
    return {"files": files, "closure_sha256": sha256_bytes(canonical_json_bytes(files))}


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise ContractError(f"{label} must be an object")
    return value


def load_policy(root: Path) -> tuple[dict[str, Any], str]:
    policy = _read_json(root / POLICY_PATH, "SIP qualification policy")
    expected = {"schema_version", "project", "policy_type", "source_key", "source_name", "symbols", "window", "request_contract", "network_registry", "outputs", "authorities"}
    request = {"endpoint": "https://data.alpaca.markets/v2/stocks/bars", "feed": "sip", "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc", "limit": 10000, "minimum_end_lag_minutes": 20, "timeout_seconds": 30, "host_timeout_seconds": 120, "max_pages": 1, "max_response_bytes": 1048576}
    if (set(policy) != expected or policy.get("schema_version") != 1 or policy.get("project") != PROJECT
            or policy.get("policy_type") != "ALPACA_SIP_SINGLE_FEED_QUALIFICATION" or policy.get("source_key") != "alpaca_basic_delayed_sip"
            or policy.get("source_name") != SOURCE_NAME or policy.get("symbols") != ["AAPL", "SPY"]
            or policy.get("window") != {"start": "2026-07-27T04:00:00Z", "end": "2026-08-01T03:59:59Z", "sessions": ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"]}
            or policy.get("request_contract") != request or policy.get("network_registry") != "config/network_acquisition_registry.json"
            or policy.get("outputs") != {"snapshot_store": "data/vault/qualification/as_received"}
            or policy.get("authorities") != {"provider_access": False, "credential_access": False, "snapshot_write": False, "offline_verification": False, "qualification_receipt_publication": False, "source_activation": False, "canonical_bars": False, "training_or_evaluation": False}):
        raise ContractError("SIP single-feed qualification policy differs")
    return policy, sha256_file(root / POLICY_PATH)


def _source_binding(root: Path, policy: Mapping[str, Any]) -> dict[str, object]:
    sources = _read_json(root / "config/sources.json", "sources policy")
    source = sources.get("sources", {}).get(policy["source_key"])
    contract = policy["request_contract"]
    expected_contract = {"qualified_feed": None, "timeframe": "1Day", "adjustment": "raw", "asof": None, "minimum_end_lag_minutes": 20, "sort": "asc"}
    if (not isinstance(source, dict) or source.get("enabled_for_active_pipeline") is not False
            or source.get("status") != "pending_single_sip_requalification" or source.get("qualification_receipt") is not None
            or source.get("request_contract") != expected_contract or source.get("endpoint") != contract["endpoint"]):
        raise ContractError("sources policy is not pending the exact SIP requalification")
    calendar = sources.get("qualification_calendar_release")
    if type(calendar) is not str:
        raise ContractError("sources policy lacks the pinned qualification calendar")
    return {"path": "config/sources.json", "sha256": sha256_file(root / "config/sources.json"), "status": source["status"], "qualified_feed": None, "calendar_release_directory": calendar}


def _calendar_binding(root: Path, directory: Path, sessions: list[str]) -> dict[str, object]:
    accepted_root = root / "data/vault/accepted"
    calendar = load_xnys_calendar_release(directory, accepted_release_root=accepted_root)
    observed = [row["session"].isoformat() for row in calendar.schedule.to_pylist() if row["session"].isoformat() in sessions]
    if observed != sessions:
        raise IntegrityError("pinned XNYS calendar session census differs")
    return {"release_id": calendar.calendar.release_id, "directory": str(directory.resolve(strict=True)), "sessions": sessions}


def _request(
    policy: Mapping[str, Any], requested_at: datetime
) -> tuple[AlpacaBarsPolicy, AlpacaBarsRequest]:
    contract = policy["request_contract"]
    bars_policy = AlpacaBarsPolicy(feed="sip", timeframe="1Day", adjustment="raw", asof=None, sort="asc", minimum_end_lag_minutes=20, endpoint=contract["endpoint"])
    request = AlpacaBarsRequest(tuple(policy["symbols"]), parse_utc_z(policy["window"]["start"], "qualification.start"), parse_utc_z(policy["window"]["end"], "qualification.end"), requested_at, limit=10000)
    request.parameters(bars_policy)
    return bars_policy, request


def build_qualification_plan(*, repo_root: Path | None = None, clock: TrustedClock | None = None) -> dict[str, object]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise ContractError("production SIP qualification planning requires trusted UTC")
    repository = _repository_binding(root)
    policy, policy_sha256 = load_policy(root)
    source = _source_binding(root, policy)
    calendar = _calendar_binding(root, Path(str(source["calendar_release_directory"])), list(policy["window"]["sessions"]))
    registry = NetworkAcquisitionRegistry.load(root / policy["network_registry"], allowed_root=root)
    if registry.allowed_origin_paths.get(SOURCE_NAME) != "https://data.alpaca.markets/v2/stocks/bars":
        raise ContractError("network registry lacks the exact SIP qualification source")
    now = trusted_clock.now()
    bars_policy, request = _request(policy, now)
    network = NetworkRequestPlan.create(registry=registry, source=SOURCE_NAME, initial_url=request.url(bars_policy), timeout_seconds=30, max_response_bytes=1048576, max_pages=1, pagination_parameter="none")
    # ``requested_at`` is deliberately recorded outside the stable plan hash:
    # it is the owner-operated execution observation, not a caller-controlled
    # request parameter.  This permits a later, exact-plan execution while the
    # static request, code, policy, calendar, registry, and environment remain
    # hash-bound.
    unsigned = {"schema_version": 1, "project": PROJECT, "plan_type": "ALPACA_SIP_SINGLE_FEED_QUALIFICATION", "mode": "PLAN_ONLY", "repository": repository, "policy_sha256": policy_sha256, "network_registry_id": registry.registry_id, "source": source, "calendar": calendar, "request": {"symbols": list(request.symbols), "start": iso_z(request.start), "end": iso_z(request.end), "feed": "sip", "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc", "limit": 10000}, "network_request_plan": network.as_dict(), "host_timeout_seconds": 120, "request_order": 1, "authorities": dict(policy["authorities"]), "execution_contract": {"exact_request_plan_id_required": True, "execute_network_flag_required": True, "owner_confirmation_environment": "FREE_SOURCE_QUALIFICATION_APPROVED=YES", "credentials": "PROCESS_ENVIRONMENT_ONLY", "maximum_gets": 1, "retry": False, "publication": False, "activation": False}, "code_closure": _closure(root, CODE_CLOSURE_PATHS), "config_closure": _closure(root, CONFIG_CLOSURE_PATHS)}
    return {**unsigned, "planned_at": iso_z(now), "qualification_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_plan(plan: Mapping[str, object]) -> str:
    plan_id = plan.get("qualification_plan_id")
    require_sha256(plan_id, "SIP qualification plan ID")
    unsigned = {key: value for key, value in plan.items() if key not in {"qualification_plan_id", "planned_at"}}
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id:
        raise IntegrityError("SIP qualification plan ID differs")
    network = plan.get("network_request_plan")
    if not isinstance(network, Mapping) or plan.get("plan_type") != "ALPACA_SIP_SINGLE_FEED_QUALIFICATION" or plan.get("mode") != "PLAN_ONLY":
        raise IntegrityError("SIP qualification plan contract differs")
    if network.get("source") != SOURCE_NAME or network.get("max_pages") != 1 or network.get("pagination_parameter") != "none":
        raise IntegrityError("SIP qualification network bounds differ")
    return str(plan_id)


def execute_qualification_capture(*, plan: Mapping[str, object], approved_plan_id: str, api_key_id: str, api_secret_key: str, clock: TrustedClock, repo_root: Path | None = None) -> tuple[LandedSnapshot, dict[str, object]]:
    if _validate_plan(plan) != approved_plan_id:
        raise PermissionError("approved SIP qualification plan differs")
    root = (repo_root or _repo_root()).resolve(strict=True)
    fresh = build_qualification_plan(repo_root=root, clock=clock)
    if fresh["qualification_plan_id"] != approved_plan_id:
        raise IntegrityError("SIP qualification closure or request plan drifted")
    policy, _ = load_policy(root)
    registry = NetworkAcquisitionRegistry.load(root / policy["network_registry"], allowed_root=root)
    network = NetworkRequestPlan(**dict(fresh["network_request_plan"]))
    session = start_local_network_execution(network, registry=registry, clock=clock)
    bars_policy, request = _request(policy, clock.now())
    store = AsReceivedSnapshotStore(root / policy["outputs"]["snapshot_store"], allowed_root=root / "data", acquisition_registry=registry)
    started = monotonic()
    pages = guarded_fetch_landed_pages(request, snapshot_store=store, api_key_id=api_key_id, api_secret_key=api_secret_key, policy=bars_policy, network_enabled=True, max_pages=1, clock=clock, authorization_session=session, source=SOURCE_NAME, timeout_seconds=30, max_response_bytes=1048576)
    if monotonic() - started > 120:
        raise IntegrityError("SIP qualification exceeded its 120-second host limit")
    if len(pages) != 1:
        raise IntegrityError("SIP qualification must land exactly one snapshot")
    snapshot = store.load(pages[0].root)
    calendar_dir = Path(str(fresh["calendar"]["directory"]))
    assessment = assess_landed_alpaca_sip(request, sip_snapshot=snapshot, network_registry_id=registry.registry_id, calendar_release_directory=calendar_dir, accepted_release_root=root / "data/vault/accepted")
    return snapshot, {**assessment, "qualification_plan_id": approved_plan_id, "receipt_publication": False, "source_activation": False}


def verify_qualification_snapshot(*, snapshot_directory: Path, plan: Mapping[str, object], repo_root: Path | None = None) -> dict[str, object]:
    _validate_plan(plan)
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy, _ = load_policy(root)
    registry = NetworkAcquisitionRegistry.load(root / policy["network_registry"], allowed_root=root)
    store = AsReceivedSnapshotStore(root / policy["outputs"]["snapshot_store"], allowed_root=root / "data", acquisition_registry=registry)
    snapshot = store.load(snapshot_directory)
    if snapshot.request_plan_id != plan["network_request_plan"]["plan_id"] or snapshot.source != SOURCE_NAME:
        raise IntegrityError("snapshot is not bound to this SIP qualification plan")
    bars_policy, request = _request(policy, snapshot.requested_at)
    return {**assess_landed_alpaca_sip(request, sip_snapshot=snapshot, network_registry_id=registry.registry_id, calendar_release_directory=Path(str(plan["calendar"]["directory"])), accepted_release_root=root / "data/vault/accepted"), "qualification_plan_id": plan["qualification_plan_id"], "receipt_publication": False, "source_activation": False}
