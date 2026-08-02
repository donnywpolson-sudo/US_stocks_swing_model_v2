"""Plan-first prospective corporate-action and delisting evidence lane."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .common import canonical_json_bytes, require_sha256, sha256_bytes, sha256_file
from .errors import ContractError
from .releases import ReleaseManifest, verify_accepted_release


POLICY_PATH = "config/prospective_corporate_action_capture_policy.json"
PROJECT = "US_stocks_swing_model_v2"


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("prospective corporate-action policy is unreadable") from exc
    expected = {"schema_version", "project", "mode", "source", "endpoint", "request_contract", "coverage", "publication"}
    if set(value) != expected or value["schema_version"] != 1 or value["project"] != PROJECT or value["mode"] != "PROSPECTIVE_CORPORATE_ACTION_CAPTURE_PLAN_ONLY":
        raise ContractError("prospective corporate-action policy differs")
    if value["source"] != "alpaca_corporate_actions" or value["endpoint"] != "https://data.alpaca.markets/v1/corporate-actions":
        raise ContractError("prospective corporate-action source differs")
    if value["request_contract"] != {"sort": "asc", "http_timeout_seconds": 30, "host_timeout_seconds": 120, "max_pages": 1000, "max_response_bytes": 1048576, "redirects_allowed": False}:
        raise ContractError("prospective corporate-action request contract differs")
    if value["coverage"] != {"semantics": "EFFECTIVE_EVENT_COMPLETENESS", "late_arrival_policy": "revise_outcome_or_abstain_never_backdate", "unresolved_rows_remain_in_denominator": True, "imputation_or_drop_allowed": False}:
        raise ContractError("prospective corporate-action coverage contract differs")
    if value["publication"] != {"dataset": "corporate_actions", "role": "prospective_as_received", "quality_state": "PASS", "activation_authorized": False}:
        raise ContractError("prospective corporate-action publication contract differs")
    return value


def _verified(directory: Path, accepted_root: Path, *, dataset: str, role: str, quality: str) -> ReleaseManifest:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if manifest.project != PROJECT or manifest.dataset != dataset or manifest.role != role or manifest.quality_state != quality:
        raise ContractError(f"prospective corporate-action {dataset} input is not accepted prospective evidence")
    if manifest.role == "legacy_discovery_only" or manifest.dataset.startswith("alpaca_discovery_"):
        raise ContractError("legacy/proxy evidence cannot enter prospective corporate-action capture")
    return manifest


def build_prospective_corporate_action_capture_plan(
    *,
    repository_root: Path,
    accepted_root: Path,
    identity_release_directory: Path,
    bars_release_directory: Path,
    calendar_release_directory: Path,
    symbols: Iterable[str],
    effective_start_session: date,
    effective_end_session: date,
) -> dict[str, object]:
    """Bind one future capture. This function has zero network and writes."""
    root = Path(repository_root).resolve(strict=True)
    accepted = Path(accepted_root).resolve(strict=True)
    policy = _load_policy(root)
    identity = _verified(Path(identity_release_directory), accepted, dataset="identity", role="prospective_as_received", quality="PASS")
    bars = _verified(Path(bars_release_directory), accepted, dataset="alpaca_daily_bars", role="prospective_as_received", quality="PASS")
    calendar = _verified(Path(calendar_release_directory), accepted, dataset="xnys_sessions", role="derived_causal", quality="PASS")
    if not {identity.release_id, calendar.release_id}.issubset(bars.upstream_release_ids):
        raise ContractError("prospective bars do not bind identity/calendar lineage")
    selected = tuple(symbols)
    if not selected or selected != tuple(sorted(set(selected))) or any(type(item) is not str or not item or item != item.upper() for item in selected):
        raise ContractError("corporate-action symbols must be sorted unique canonical text")
    if type(effective_start_session) is not date or type(effective_end_session) is not date or effective_end_session < effective_start_session:
        raise ContractError("corporate-action effective-session interval is invalid")
    unsigned = {
        "schema_version": 1,
        "mode": policy["mode"],
        "policy_sha256": sha256_file(root / POLICY_PATH),
        "inputs": {
            "identity_release_id": identity.release_id,
            "bars_release_id": bars.release_id,
            "calendar_release_id": calendar.release_id,
            "source_epoch": bars.source_epoch,
        },
        "coverage": {
            **policy["coverage"],
            "symbols": list(selected),
            "effective_start_session": effective_start_session.isoformat(),
            "effective_end_session": effective_end_session.isoformat(),
        },
        "request": {"method": "GET", "endpoint": policy["endpoint"], **policy["request_contract"], "page_token": None},
        "authorities": {"network_calls": 0, "credential_access": False, "raw_snapshot_write": False, "release_publication": False, "source_activation": False, "training": False, "evaluation": False},
    }
    return {**unsigned, "capture_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_prospective_corporate_action_publication_plan(
    *,
    capture_plan: dict[str, object],
    snapshot_ids: Iterable[str],
    raw_sha256: Iterable[str],
    coverage_id: str,
) -> dict[str, object]:
    """Bind an already-landed capture to a later separate publication gate."""
    if type(capture_plan) is not dict or set(capture_plan) != {"capture_plan_id", "schema_version", "mode", "policy_sha256", "inputs", "coverage", "request", "authorities"}:
        raise ContractError("corporate-action capture plan fields differ")
    unsigned_capture = {key: value for key, value in capture_plan.items() if key != "capture_plan_id"}
    if capture_plan.get("capture_plan_id") != sha256_bytes(canonical_json_bytes(unsigned_capture)):
        raise ContractError("corporate-action capture plan hash differs")
    snapshots, raws = tuple(snapshot_ids), tuple(raw_sha256)
    if not snapshots or len(snapshots) != len(raws) or snapshots != tuple(sorted(set(snapshots))):
        raise ContractError("corporate-action landed snapshot census is invalid")
    for index, value in enumerate((*snapshots, *raws)):
        require_sha256(value, f"corporate_action_publication.input[{index}]")
    require_sha256(coverage_id, "corporate_action_publication.coverage_id")
    unsigned = {
        "schema_version": 1,
        "mode": "PUBLISH_PROSPECTIVE_CORPORATE_ACTIONS_PLAN_ONLY",
        "capture_plan_id": capture_plan["capture_plan_id"],
        "inputs": capture_plan["inputs"],
        "coverage": {**capture_plan["coverage"], "coverage_id": coverage_id},
        "landed_raw": {"snapshot_ids": list(snapshots), "raw_sha256": list(raws)},
        "publication": {"dataset": "corporate_actions", "role": "prospective_as_received", "quality_state": "PASS", "activation_authorized": False},
        "authorities": {"release_publication": False, "source_activation": False, "training": False, "evaluation": False},
    }
    return {**unsigned, "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
