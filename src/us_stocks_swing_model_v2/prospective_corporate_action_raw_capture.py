"""Fail-closed prospective Alpaca corporate-action raw-evidence capture."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .clock import TrustedClock, require_trusted_clock
from .common import canonical_json_bytes, require_contained_path, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .providers.corporate_actions import CorporateActionsRequest, guarded_fetch_corporate_action_pages, parse_landed_corporate_actions
from .providers.network_execution import NetworkRequestPlan, start_local_network_execution
from .providers.snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry
from .releases import ReleaseManifest, verify_accepted_release


POLICY_PATH = "config/prospective_corporate_action_raw_capture_policy.json"
REGISTRY_PATH = "config/network_acquisition_registry.json"
SNAPSHOT_ROOT = "data/vault/qualification/as_received/prospective_corporate_action_raw"
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/prospective_corporate_action_raw_capture.py",
    "src/us_stocks_swing_model_v2/cli/prospective_corporate_action_raw_capture.py",
)
PROJECT = "US_stocks_swing_model_v2"


def _clean_repository(root: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("prospective corporate-action capture requires a committed Git closure") from exc

    if Path(run("rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("prospective corporate-action capture Git root differs")
    if run("status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("prospective corporate-action capture requires a clean committed tree")
    return {"commit": run("rev-parse", "HEAD"), "tree": run("rev-parse", "HEAD^{tree}")}


def _closure(root: Path) -> dict[str, object]:
    files = [{"path": path, "sha256": sha256_file(root / path)} for path in CODE_CLOSURE_PATHS]
    return {"files": files, "sha256": sha256_bytes(canonical_json_bytes(files))}


def _policy(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective corporate-action raw-capture policy is unreadable") from exc
    expected = {
        "schema_version": 1, "project": PROJECT,
        "mode": "PROSPECTIVE_CORPORATE_ACTION_RAW_CAPTURE_PLAN_ONLY",
        "source": "alpaca_corporate_actions",
        "endpoint": "https://data.alpaca.markets/v1/corporate-actions",
        "replaces_capture_plan_id": "0e68260b72adbc27f1ddb71b8eb3b2f07d781f60828afd6b3b4a10daebcaf1f8",
        "request_contract": {"sort": "asc", "http_timeout_seconds": 30, "host_timeout_seconds": 120, "max_pages": 1, "max_response_bytes": 1048576, "redirects_allowed": False},
        "coverage": {"semantics": "PROVIDER_PROCESS_DATE_ACQUISITION_ONLY", "effective_event_completeness": False, "delisting_evidence_available": False, "outcomes_may_compute": False, "unresolved_rows_remain_in_denominator": True, "imputation_or_drop_allowed": False},
    }
    if value != expected:
        raise ContractError("prospective corporate-action raw-capture policy differs")
    return value


def _source_binding(root: Path) -> dict[str, str]:
    path = root / "config/sources.json"
    try:
        source = json.loads(path.read_text(encoding="utf-8"))["sources"]["alpaca_corporate_actions"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ContractError("corporate-action source configuration is unreadable") from exc
    if source.get("enabled_for_active_pipeline") is not False or source.get("network_default") != "disabled" or source.get("status") != "empty_pending_source_qualification" or source.get("endpoint") != "https://data.alpaca.markets/v1/corporate-actions":
        raise ContractError("corporate-action source configuration differs")
    return {"path": "config/sources.json", "sha256": sha256_file(path)}


def _verified(directory: Path, accepted_root: Path, *, dataset: str, role: str) -> ReleaseManifest:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if manifest.project != PROJECT or manifest.dataset != dataset or manifest.role != role or manifest.quality_state != "PASS":
        raise ContractError(f"corporate-action raw capture requires accepted {dataset} evidence")
    return manifest


def _symbols(values: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(values)
    if not selected or selected != tuple(sorted(set(selected))) or any(type(value) is not str or not value or value != value.strip().upper() for value in selected):
        raise ContractError("corporate-action raw-capture symbols must be sorted unique canonical text")
    return selected


def build_prospective_corporate_action_raw_capture_plan(*, repository_root: Path, identity_release_directory: Path, bars_release_directory: Path, calendar_release_directory: Path, symbols: Iterable[str], process_date_start: date, process_date_end: date) -> dict[str, object]:
    """Build a no-network, no-write plan for one raw provider request."""
    root = Path(repository_root).resolve(strict=True)
    if type(process_date_start) is not date or type(process_date_end) is not date or process_date_end < process_date_start:
        raise ContractError("corporate-action raw-capture process-date interval is invalid")
    policy = _policy(root)
    identity = _verified(Path(identity_release_directory), root / "data/vault/accepted", dataset="identity", role="prospective_as_received")
    bars = _verified(Path(bars_release_directory), root / "data/vault/accepted", dataset="alpaca_daily_bars", role="prospective_as_received")
    calendar = _verified(Path(calendar_release_directory), root / "data/vault/accepted", dataset="xnys_sessions", role="derived_causal")
    if not {identity.release_id, calendar.release_id}.issubset(bars.upstream_release_ids):
        raise ContractError("corporate-action raw capture bars lack identity/calendar lineage")
    selected = _symbols(symbols)
    # The URL is time-independent. Execution supplies the trusted receipt-time request timestamp.
    request = CorporateActionsRequest(start=process_date_start, end=process_date_end, symbols=selected, requested_at=TrustedClock.production().now())
    registry = NetworkAcquisitionRegistry.load(root / REGISTRY_PATH, allowed_root=root / "config")
    network = NetworkRequestPlan.create(registry=registry, source=policy["source"], initial_url=request.url(), timeout_seconds=30, max_response_bytes=1048576, max_pages=1, pagination_parameter="page_token")
    unsigned = {
        "schema_version": 1, "mode": policy["mode"],
        "repository": _clean_repository(root), "code_closure": _closure(root),
        "policy_sha256": sha256_file(root / POLICY_PATH),
        "environment_sha256": sha256_file(root / "config/environment.lock.json"),
        "source_binding": _source_binding(root),
        "network_registry_sha256": sha256_file(root / REGISTRY_PATH),
        "replaces_capture_plan_id": policy["replaces_capture_plan_id"],
        "inputs": {"identity_release_id": identity.release_id, "bars_release_id": bars.release_id, "calendar_release_id": calendar.release_id},
        "coverage": {**policy["coverage"], "symbols": list(selected), "process_date_start": process_date_start.isoformat(), "process_date_end": process_date_end.isoformat()},
        "request": {"url": request.url(), "method": "GET", **policy["request_contract"], "network_request_plan": network.as_dict(), "continuation_token_disposition": "STOP_WITHOUT_SECOND_REQUEST"},
        "authorities": {"network_calls": 0, "credential_access": False, "raw_snapshot_write": False, "release_publication": False, "source_activation": False, "training": False, "evaluation": False},
    }
    return {**unsigned, "capture_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_execution_plan(plan: dict[str, object], *, repository_root: Path) -> tuple[dict[str, object], CorporateActionsRequest, NetworkRequestPlan]:
    root = Path(repository_root).resolve(strict=True)
    if type(plan) is not dict or type(plan.get("capture_plan_id")) is not str:
        raise ContractError("corporate-action raw-capture plan is invalid")
    unsigned = {key: value for key, value in plan.items() if key != "capture_plan_id"}
    if plan["capture_plan_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("corporate-action raw-capture plan identity differs")
    if plan.get("repository") != _clean_repository(root) or plan.get("code_closure") != _closure(root):
        raise IntegrityError("corporate-action raw-capture closure differs")
    if plan.get("policy_sha256") != sha256_file(root / POLICY_PATH) or plan.get("environment_sha256") != sha256_file(root / "config/environment.lock.json") or plan.get("source_binding") != _source_binding(root) or plan.get("network_registry_sha256") != sha256_file(root / REGISTRY_PATH):
        raise IntegrityError("corporate-action raw-capture configuration differs")
    coverage, request_data = plan.get("coverage"), plan.get("request")
    if not isinstance(coverage, dict) or not isinstance(request_data, dict):
        raise IntegrityError("corporate-action raw-capture plan fields differ")
    try:
        request = CorporateActionsRequest(start=date.fromisoformat(coverage["process_date_start"]), end=date.fromisoformat(coverage["process_date_end"]), symbols=tuple(coverage["symbols"]), requested_at=TrustedClock.production().now())
        network = NetworkRequestPlan(**request_data["network_request_plan"])
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrityError("corporate-action raw-capture request fields differ") from exc
    if request.url() != request_data.get("url") or request_data.get("continuation_token_disposition") != "STOP_WITHOUT_SECOND_REQUEST":
        raise IntegrityError("corporate-action raw-capture request binding differs")
    registry = NetworkAcquisitionRegistry.load(root / REGISTRY_PATH, allowed_root=root / "config")
    network.validate(registry=registry)
    return plan, request, network


def execute_prospective_corporate_action_raw_capture(*, plan: dict[str, object], approved_plan_id: str, api_key_id: str, api_secret_key: str, clock: TrustedClock, repository_root: Path) -> dict[str, object]:
    """Run one later authorized capture; it never publishes or clears outcome blockers."""
    if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
        raise PermissionError("corporate-action raw-capture network approval is absent")
    if not api_key_id or not api_secret_key:
        raise PermissionError("Alpaca credentials are absent from the process environment")
    root = Path(repository_root).resolve(strict=True)
    checked, request, network = _validate_execution_plan(plan, repository_root=root)
    if checked["capture_plan_id"] != approved_plan_id:
        raise PermissionError("approved corporate-action raw-capture plan differs")
    trusted = require_trusted_clock(clock)
    executed_at = trusted.now()
    if not trusted.trust_eligible or executed_at.date() < request.end:
        raise ContractError("corporate-action raw capture requires production UTC after its process-date end")
    request = CorporateActionsRequest(start=request.start, end=request.end, symbols=request.symbols, requested_at=executed_at)
    registry = NetworkAcquisitionRegistry.load(root / REGISTRY_PATH, allowed_root=root / "config")
    session = start_local_network_execution(network, registry=registry, clock=trusted)
    store = AsReceivedSnapshotStore(root / SNAPSHOT_ROOT, allowed_root=root / "data", acquisition_registry=registry)
    deadline = time.monotonic() + 120
    pages = guarded_fetch_corporate_action_pages(request, snapshot_store=store, api_key_id=api_key_id, api_secret_key=api_secret_key, network_enabled=True, max_pages=1, timeout_seconds=30, max_response_bytes=1048576, clock=trusted, authorization_session=session)
    if time.monotonic() > deadline:
        raise TimeoutError("corporate-action raw capture exceeded its host timeout")
    if len(pages) != 1:
        raise IntegrityError("corporate-action raw capture exceeded one request")
    parsed = parse_landed_corporate_actions(request, pages)
    return {
        "capture_plan_id": checked["capture_plan_id"], "snapshot_id": pages[0].snapshot_id, "snapshot_directory": str(pages[0].root), "raw_sha256": pages[0].raw_sha256,
        "coverage_id": parsed.coverage.coverage_id, "action_count": len(parsed.actions), "network_calls": 1,
        "coverage_semantics": "PROVIDER_PROCESS_DATE_ACQUISITION_ONLY", "effective_event_completeness": False, "delisting_evidence_available": False, "outcomes_may_compute": False,
        "release_publication": False, "source_activation": False, "training": False, "evaluation": False,
    }
