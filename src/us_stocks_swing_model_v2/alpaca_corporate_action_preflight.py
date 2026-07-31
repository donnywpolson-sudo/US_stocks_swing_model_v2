"""Plan one caveated Alpaca corporate-actions acquisition without network access."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, parse_utc_z, sha256_bytes, sha256_file
from .errors import ContractError
from .providers.corporate_actions import CorporateActionsRequest, MAX_PAGES, MAX_RESPONSE_BYTES
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
            "redirects_allowed": False, "request_count": "UNRESOLVED_UNTIL_SEPARATE_EXECUTION_PLAN",
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
