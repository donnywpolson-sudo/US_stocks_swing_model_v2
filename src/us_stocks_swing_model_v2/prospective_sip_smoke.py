"""Plan-only prospective AAPL/SPY SIP smoke capture."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from .clock import TrustedClock, require_trusted_clock
from .common import canonical_json_bytes, iso_z, parse_utc_z, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .identity import _load_identity_release_payload
from .providers.alpaca import AlpacaBarsPolicy, AlpacaBarsRequest
from .providers.network_execution import NetworkRequestPlan
from .providers.snapshots import NetworkAcquisitionRegistry
from .releases import verify_accepted_release


POLICY_PATH = "config/prospective_sip_smoke_policy.json"
IDENTITY_RELEASE_ID = "2c2898a6748dcd5b4d9f7875cd1549e050902c2f491005ed530a5899c685e115"


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective SIP smoke policy is unreadable") from exc
    expected = {"schema_version", "project", "mode", "identity_release_id", "symbols", "session", "earliest_capture_at", "request_contract", "network_registry"}
    contract = {
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars", "feed": "sip",
        "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc",
        "limit": 10000, "timeout_seconds": 30, "host_timeout_seconds": 120,
        "max_pages": 1, "max_response_bytes": 1048576,
    }
    if (
        type(policy) is not dict or set(policy) != expected or policy["schema_version"] != 1
        or policy["project"] != "US_stocks_swing_model_v2"
        or policy["mode"] != "PROSPECTIVE_TWO_SYMBOL_SIP_SMOKE_PLAN_ONLY"
        or policy["identity_release_id"] != IDENTITY_RELEASE_ID
        or policy["symbols"] != ["AAPL", "SPY"] or policy["session"] != "2026-08-03"
        or policy["earliest_capture_at"] != "2026-08-03T20:20:00Z"
        or policy["request_contract"] != contract
        or policy["network_registry"] != "config/alpaca_canonical_bars_network_registry.json"
    ):
        raise ContractError("prospective SIP smoke policy differs")
    return policy


def build_prospective_sip_smoke_plan(*, identity_release_directory: Path, calendar_release_directory: Path, repository_root: Path, clock: TrustedClock | None = None, allow_synthetic: bool = False) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = _load_policy(root)
    trusted_clock = require_trusted_clock(clock)
    if not allow_synthetic and not trusted_clock.trust_eligible:
        raise ContractError("prospective SIP smoke planning requires production system UTC")
    identity_dir = Path(identity_release_directory)
    identity_manifest = verify_accepted_release(identity_dir, accepted_root=root / "data/vault/accepted")
    if (
        identity_manifest.release_id != policy["identity_release_id"] or identity_manifest.dataset != "identity"
        or identity_manifest.role != "prospective_as_received" or identity_manifest.quality_state != "PASS"
        or identity_manifest.source_epoch != "nasdaq_alpaca_active_us_equity_v1"
    ):
        raise IntegrityError("prospective SIP smoke identity release differs")
    snapshots = _load_identity_release_payload(identity_dir, identity_manifest.row_count)
    if len(snapshots) != 1:
        raise IntegrityError("prospective SIP smoke requires one identity snapshot")
    assets: dict[str, str] = {}
    for row in snapshots[0].rows:
        if row.symbol in policy["symbols"] and row.active and row.eligible and row.membership_present and row.abstention_reason is None:
            if row.symbol in assets:
                raise IntegrityError("prospective SIP smoke asset identity is ambiguous")
            assets[row.symbol] = row.asset_id
    if set(assets) != set(policy["symbols"]):
        raise IntegrityError("prospective SIP smoke identity is incomplete")
    calendar = load_xnys_calendar_release(calendar_release_directory, accepted_release_root=root / "data/vault/accepted")
    session = date.fromisoformat(policy["session"])
    rows = [row for row in calendar.schedule.to_pylist() if row["session"] == session]
    if len(rows) != 1 or iso_z(rows[0]["close_at"]) != "2026-08-03T20:00:00Z":
        raise IntegrityError("prospective SIP smoke pinned calendar session differs")
    earliest = parse_utc_z(policy["earliest_capture_at"], "prospective_sip_smoke.earliest_capture_at")
    now = trusted_clock.now()
    if now < earliest:
        raise ContractError("prospective SIP smoke capture is not yet available")
    request_contract = policy["request_contract"]
    request = AlpacaBarsRequest(tuple(policy["symbols"]), parse_utc_z("2026-08-03T04:00:00Z", "prospective_sip_smoke.start"), parse_utc_z("2026-08-03T20:00:00Z", "prospective_sip_smoke.end"), now, limit=10000)
    bars_policy = AlpacaBarsPolicy(feed="sip", timeframe="1Day", adjustment="raw", asof=None, sort="asc", minimum_end_lag_minutes=20, endpoint=request_contract["endpoint"])
    registry = NetworkAcquisitionRegistry.load(root / policy["network_registry"], allowed_root=root / "config")
    network = NetworkRequestPlan.create(registry=registry, source="alpaca_sip_canonical_bars", initial_url=request.url(bars_policy), timeout_seconds=30, max_response_bytes=1048576, max_pages=1, pagination_parameter="page_token")
    unsigned = {
        "schema_version": 1, "mode": "PROSPECTIVE_TWO_SYMBOL_SIP_SMOKE_PLAN_ONLY",
        "identity": {"release_id": identity_manifest.release_id, "snapshot_id": snapshots[0].snapshot_id, "asset_ids": dict(sorted(assets.items()))},
        "calendar": {"release_id": calendar.calendar.release_id, "session": policy["session"], "close_at": "2026-08-03T20:00:00Z"},
        "request": {"method": "GET", "url": network.initial_url, "symbols": policy["symbols"], "start": "2026-08-03T04:00:00Z", "end": "2026-08-03T20:00:00Z", "feed": "sip", "timeframe": "1Day", "adjustment": "raw", "asof": None, "sort": "asc", "limit": 10000},
        "network_request_plan": network.as_dict(), "host_timeout_seconds": 120,
        "earliest_capture_at": policy["earliest_capture_at"], "requested_at": iso_z(now),
        "policy_sha256": sha256_file(root / POLICY_PATH), "environment_sha256": sha256_file(root / "config/environment.lock.json"),
        "authorities": {"network_calls": 0, "snapshot_write": False, "canonical_candidate": False, "canonical_release_publication": False, "research": False},
        "prohibitions": ["legacy_identity_release", "legacy_july_fixture", "proxy_input", "source_activation", "training_or_evaluation"],
    }
    return {**unsigned, "prospective_sip_smoke_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
