"""Metadata-only downstream planning for caveated Alpaca historical bars."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .common import canonical_json_bytes, reject_link, require_sha256, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_legacy_discovery_downstream_contract.json"
EVIDENCE_PATH = "source_evidence_manifest.json"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_contract(repo_root: Path | None = None) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / CONTRACT_PATH
    reject_link(path)
    payload = json.loads(path.read_bytes())
    if type(payload) is not dict:
        raise ContractError("Alpaca downstream contract must be an object")
    contract_id = payload.pop("contract_id", None)
    if contract_id != sha256_bytes(canonical_json_bytes(payload)):
        raise IntegrityError("Alpaca downstream contract ID differs")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("mode") != "ALPACA_LEGACY_DISCOVERY_DOWNSTREAM_PLAN_ONLY"
        or any(payload.get("authorities", {}).values())
    ):
        raise ContractError("Alpaca downstream contract differs")
    return {**payload, "contract_id": contract_id}


def build_downstream_plan(
    release_directory: Path,
    *,
    accepted_root: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Verify release metadata and emit a no-row, no-write downstream plan."""

    contract = load_contract(repo_root)
    release = verify_accepted_release(Path(release_directory), accepted_root=Path(accepted_root))
    source = contract["source"]
    if (
        release.dataset != source["dataset"]
        or release.role != source["role"]
        or release.quality_state != source["quality_state"]
    ):
        raise ContractError("Alpaca downstream release identity differs")
    evidence_file = next((entry for entry in release.files if entry.path == EVIDENCE_PATH), None)
    if evidence_file is None:
        raise IntegrityError("Alpaca downstream release lacks source evidence metadata")
    evidence_path = Path(release_directory) / EVIDENCE_PATH
    reject_link(evidence_path)
    raw = evidence_path.read_bytes()
    if len(raw) != evidence_file.size or sha256_bytes(raw) != evidence_file.sha256:
        raise IntegrityError("Alpaca downstream evidence metadata differs")
    evidence = json.loads(raw)
    for name in ("input_quality_state", "historical_membership_proven", "point_in_time_safe", "survivorship_safe"):
        if evidence.get(name) != source[name]:
            raise ContractError("Alpaca downstream source caveat differs")
    unsigned = {
        "schema_version": 1,
        "mode": contract["mode"],
        "contract_id": contract["contract_id"],
        "release": {
            "release_id": release.release_id,
            "manifest_sha256": sha256_file(Path(release_directory) / "release_manifest.json"),
            "row_count": release.row_count,
            "event_start": release.event_start,
            "event_end": release.event_end,
        },
        "eligibility": contract["eligibility"],
        "features": contract["features"],
        "outcomes": contract["outcomes"],
        "wfa": contract["wfa"],
        "metadata_validation_scope": {"release_verified": True, "bar_rows_opened": 0, "outcomes_computed": 0, "files_written": 0},
        "authorities": contract["authorities"],
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
