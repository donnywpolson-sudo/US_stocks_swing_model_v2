"""Metadata-only downstream planning for caveated Alpaca historical bars."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from .common import canonical_json_bytes, reject_link, require_sha256, sha256_bytes, sha256_file
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
CONTRACT_PATH = "config/alpaca_legacy_discovery_downstream_contract.json"
EVIDENCE_PATH = "source_evidence_manifest.json"
DISCOVERY_PROXY = {
    "state": "SOURCE_ADJUSTED_RAW_PRICE_PROXY_PLANNED_NOT_MATERIALIZED",
    "target_semantics": "ALPACA_RAW_NEXT_OPEN_TO_FIFTH_CLOSE_SIMPLE_PRICE_RETURN_PROXY_V1",
    "historical_proxy": True,
    "canonical_target_equivalent": False,
    "trusted_sleeve_eligible": False,
    "real_history_row_access_authorized": False,
    "generated_evidence_write_authorized": False,
    "training_or_evaluation_authorized": False,
}


def build_raw_price_proxy_outcomes(
    sessions: Sequence[date], bars: Sequence[Mapping[str, object]]
) -> tuple[dict[str, object], ...]:
    """Build only untrusted five-session raw-price proxy outcomes in memory."""

    ordered_sessions = tuple(sessions)
    if (
        not ordered_sessions
        or tuple(sorted(ordered_sessions)) != ordered_sessions
        or len(set(ordered_sessions)) != len(ordered_sessions)
        or any(type(value) is not date for value in ordered_sessions)
    ):
        raise ContractError("proxy sessions must be sorted unique exact dates")
    by_symbol: dict[str, dict[date, Mapping[str, object]]] = {}
    for row in bars:
        if type(row) is not dict or set(row) != {"symbol", "session", "open", "close"}:
            raise ContractError("proxy bar fields differ")
        symbol, session = row["symbol"], row["session"]
        if type(symbol) is not str or not symbol or symbol != symbol.strip().upper() or type(session) is not date:
            raise ContractError("proxy bar identity differs")
        if session not in ordered_sessions:
            raise ContractError("proxy bar session is outside the pinned calendar")
        if session in by_symbol.setdefault(symbol, {}):
            raise ContractError("proxy bars contain a duplicate symbol/session")
        by_symbol[symbol][session] = row
    outcomes: list[dict[str, object]] = []
    for symbol in sorted(by_symbol):
        for index, decision_session in enumerate(ordered_sessions[:-5]):
            entry_session, exit_session = ordered_sessions[index + 1], ordered_sessions[index + 5]
            entry = by_symbol[symbol].get(entry_session)
            exit_bar = by_symbol[symbol].get(exit_session)
            entry_open = entry["open"] if entry is not None else None
            exit_close = exit_bar["close"] if exit_bar is not None else None
            valid = (
                type(entry_open) in {int, float}
                and not isinstance(entry_open, bool)
                and type(exit_close) in {int, float}
                and not isinstance(exit_close, bool)
                and math.isfinite(float(entry_open))
                and math.isfinite(float(exit_close))
                and float(entry_open) > 0
                and float(exit_close) > 0
            )
            outcomes.append({
                "symbol": symbol,
                "decision_session": decision_session,
                "entry_session": entry_session,
                "exit_session": exit_session,
                "entry_open": float(entry_open) if valid else None,
                "exit_close": float(exit_close) if valid else None,
                "proxy_return": float(exit_close) / float(entry_open) - 1.0 if valid else None,
                "status": "READY_UNTRUSTED_RAW_PRICE_PROXY" if valid else "UNRESOLVED_RAW_HORIZON",
                "target_semantics": DISCOVERY_PROXY["target_semantics"],
                "historical_proxy": True,
                "canonical_target_equivalent": False,
            })
    return tuple(outcomes)


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
        or payload.get("discovery_proxy") != DISCOVERY_PROXY
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
        "discovery_proxy": contract["discovery_proxy"],
        "wfa": contract["wfa"],
        "metadata_validation_scope": {"release_verified": True, "bar_rows_opened": 0, "outcomes_computed": 0, "files_written": 0},
        "authorities": contract["authorities"],
        "stop_conditions": contract["stop_conditions"],
    }
    return {**unsigned, "plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
