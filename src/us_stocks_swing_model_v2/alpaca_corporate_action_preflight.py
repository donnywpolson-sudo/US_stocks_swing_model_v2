"""Plan one caveated Alpaca corporate-actions acquisition without network access."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, parse_utc_z, sha256_bytes, sha256_file
from .errors import ContractError
from .clock import TrustedClock
from .providers.corporate_actions import (
    CorporateActionsRequest,
    MAX_PAGES,
    MAX_RESPONSE_BYTES,
    guarded_fetch_corporate_action_pages,
    parse_landed_corporate_actions,
)
from .providers.network_execution import NetworkRequestPlan, start_local_network_execution
from .providers.snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry
from .releases import verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
DATASET = "alpaca_historical_daily_bars"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def build_corporate_action_preflight(
    *,
    release_directory: Path,
    accepted_root: Path,
    start: date,
    end: date,
    symbols: tuple[str, ...],
    max_pages: int,
    created_at: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Return one no-network request plan; outcomes remain explicitly unresolved."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    release = verify_accepted_release(Path(release_directory), accepted_root=Path(accepted_root))
    if release.dataset != DATASET or release.role != "legacy_discovery_only" or release.quality_state != "LEGACY_CAVEATED":
        raise ContractError("corporate-action preflight requires the caveated Alpaca historical release")
    if not 1 <= max_pages <= MAX_PAGES:
        raise ContractError("corporate-action preflight page bound differs")
    requested_at = parse_utc_z(created_at, "corporate-action preflight created_at")
    request = CorporateActionsRequest(start=start, end=end, symbols=symbols, requested_at=requested_at)
    request.validate_against_trusted_time(requested_at)
    if start < date.fromisoformat(str(release.event_start)) or end > date.fromisoformat(str(release.event_end)):
        raise ContractError("corporate-action preflight interval escapes the bars release")
    sources_path = root / "config" / "sources.json"
    sources = json.loads(sources_path.read_bytes())
    source = sources.get("sources", {}).get("alpaca_corporate_actions", {})
    if source.get("network_default") != "disabled" or source.get("status") != "empty_pending_source_qualification":
        raise ContractError("corporate-action source policy differs")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root,
    )
    network_request = NetworkRequestPlan.create(
        registry=registry,
        source="alpaca_corporate_actions",
        initial_url=request.url(),
        timeout_seconds=30,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_pages=max_pages,
        pagination_parameter="page_token",
    )
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_CORPORATE_ACTIONS_CAVEATED_PREFLIGHT_PLAN_ONLY",
        "release": {"release_id": release.release_id, "manifest_sha256": sha256_file(Path(release_directory) / "release_manifest.json")},
        "source_config_sha256": sha256_file(sources_path),
        "created_at": created_at,
        "request": {
            "url": request.url(), "start": start.isoformat(), "end": end.isoformat(),
            "symbols": list(symbols), "page_limit": request.limit, "max_pages": max_pages,
            "http_timeout_seconds": 30, "response_limit_bytes": MAX_RESPONSE_BYTES,
            "redirects_allowed": False, "request_count": 1,
            "continuation_token_disposition": "STOP_WITHOUT_SECOND_REQUEST",
            "network_request_plan": network_request.as_dict(),
        },
        "outcome_boundary": {
            "corporate_action_process_date_coverage_only": True,
            "effective_event_completeness_proven": False,
            "delisting_evidence_available": False,
            "historical_membership_proven": False,
            "outcomes_may_compute": False,
        },
        "authorities": {"network": False, "credentials": False, "writes": False, "publication": False, "outcome_computation": False, "training": False, "evaluation": False},
        "stop_conditions": ["release or source-policy drift", "noncanonical symbols or interval", "page-bound drift", "outcome or trusted-completeness claim"],
    }
    return {**unsigned, "plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def execute_corporate_action_preflight(
    *,
    plan: dict[str, Any],
    approved_plan_id: str,
    api_key_id: str,
    api_secret_key: str,
    clock: TrustedClock,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Execute exactly the preflight's bound one-page request and verify it."""

    if type(plan) is not dict or type(plan.get("plan_id")) is not str:
        raise ContractError("corporate-action preflight plan is invalid")
    unsigned_plan = {key: value for key, value in plan.items() if key != "plan_id"}
    if plan["plan_id"] != sha256_bytes(canonical_json_bytes(unsigned_plan)):
        raise ContractError("corporate-action preflight plan identity differs")
    if plan["plan_id"] != approved_plan_id:
        raise ContractError("approved corporate-action preflight plan differs")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    request_data = plan.get("request")
    if type(request_data) is not dict:
        raise ContractError("corporate-action preflight request is invalid")
    try:
        request = CorporateActionsRequest(
            start=date.fromisoformat(request_data["start"]),
            end=date.fromisoformat(request_data["end"]),
            symbols=tuple(request_data["symbols"]),
            requested_at=parse_utc_z(plan["created_at"], "corporate-action preflight created_at"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("corporate-action preflight request is invalid") from exc
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root,
    )
    expected_network_request = NetworkRequestPlan.create(
        registry=registry,
        source="alpaca_corporate_actions",
        initial_url=request.url(),
        timeout_seconds=30,
        max_response_bytes=MAX_RESPONSE_BYTES,
        max_pages=request_data["max_pages"],
        pagination_parameter="page_token",
    )
    if request_data.get("network_request_plan") != expected_network_request.as_dict():
        raise ContractError("corporate-action preflight network request differs")
    if request_data.get("request_count") != 1 or request_data.get("max_pages") != 1:
        raise ContractError("corporate-action sample must remain exactly one page")
    store = AsReceivedSnapshotStore(
        root / "data" / "vault" / "qualification" / "as_received" / "alpaca_corporate_actions",
        allowed_root=root / "data",
        acquisition_registry=registry,
    )
    session = start_local_network_execution(
        expected_network_request, registry=registry, clock=clock
    )
    pages = guarded_fetch_corporate_action_pages(
        request,
        snapshot_store=store,
        api_key_id=api_key_id,
        api_secret_key=api_secret_key,
        network_enabled=True,
        max_pages=1,
        timeout_seconds=30,
        clock=clock,
        authorization_session=session,
    )
    parsed = parse_landed_corporate_actions(request, pages)
    return {
        "plan_id": approved_plan_id,
        "snapshot_ids": [page.snapshot_id for page in pages],
        "raw_sha256": [page.raw_sha256 for page in pages],
        "action_count": len(parsed.actions),
        "coverage_id": parsed.coverage.coverage_id,
        "network_calls": len(pages),
        "verified": True,
        "published": False,
        "outcomes_may_compute": False,
    }
