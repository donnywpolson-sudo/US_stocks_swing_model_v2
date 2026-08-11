from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from .alpaca_free_bounded import EvidenceClass, PROFILE_ID, load_profile, load_qualified_profile_calendar
from .common import atomic_write, atomic_write_new, canonical_json_bytes, iso_z, require_aware_utc, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError
from .free_acquisition import execute_one_source_request
from .free_source_evidence import RawEvidenceStore, SourceRequestPlan, alpaca_bars_plan
from .free_source_evidence import validate_capture_ledger
from .locking import ExclusiveFileLock


WARMUP_SESSIONS = 90
MINIMUM_VALID_SESSIONS = 60


@dataclass(frozen=True)
class WarmupUnit:
    unit_index: int
    symbols: tuple[str, ...]
    source_plan: SourceRequestPlan

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_index": self.unit_index,
            "symbols": list(self.symbols),
            "source_plan": self.source_plan.as_dict(),
        }


@dataclass(frozen=True)
class LiquidityWarmupPlan:
    warmup_plan_id: str
    source_snapshot_id: str
    source_snapshot_path: str
    signal_session: date
    information_cutoff_session: date
    calendar_release_id: str
    sessions: tuple[date, ...]
    units: tuple[WarmupUnit, ...]
    pilot_symbol_count: int | None

    def unsigned(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "profile_id": PROFILE_ID,
            "source_snapshot_id": self.source_snapshot_id,
            "source_snapshot_path": self.source_snapshot_path,
            "signal_session": self.signal_session.isoformat(),
            "information_cutoff_session": self.information_cutoff_session.isoformat(),
            "calendar_release_id": self.calendar_release_id,
            "sessions": [value.isoformat() for value in self.sessions],
            "units": [unit.as_dict() for unit in self.units],
            "pilot_symbol_count": self.pilot_symbol_count,
            "evidence_class": EvidenceClass.HISTORICAL_RECONSTRUCTED.value,
            "purpose": "PROSPECTIVE_UNIVERSE_LIQUIDITY_ELIGIBILITY_ONLY",
            "training_authorized": False,
            "evaluation_authorized": False,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned(), "warmup_plan_id": self.warmup_plan_id}


def _load_snapshot(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("prospective candidate snapshot is unreadable") from exc
    claimed = require_sha256(payload.get("universe_snapshot_id"), "universe snapshot ID")
    unsigned = {key: value for key, value in payload.items() if key != "universe_snapshot_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != claimed:
        raise IntegrityError("prospective candidate snapshot differs from canonical content")
    if payload.get("evidence_class") != EvidenceClass.PROSPECTIVE_AS_OBSERVED.value:
        raise ContractError("warm-up source membership must be prospectively observed")
    return payload


def build_liquidity_warmup_plan(
    *, repository_root: Path, source_snapshot_path: Path, pilot_symbol_count: int | None = None
) -> LiquidityWarmupPlan:
    root = Path(repository_root).resolve(strict=True)
    source_path = Path(source_snapshot_path).resolve(strict=True)
    payload = _load_snapshot(source_path)
    loaded = load_qualified_profile_calendar(repository_root=root)
    rows = loaded.schedule.to_pylist()
    cutoff = date.fromisoformat(str(payload["information_cutoff_session"]))
    signal = date.fromisoformat(str(payload["session"]))
    position = {row["session"]: index for index, row in enumerate(rows)}.get(cutoff)
    if position is None or position + 1 < WARMUP_SESSIONS or rows[position + 1]["session"] != signal:
        raise ContractError("warm-up snapshot does not bind adjacent qualified XNYS sessions")
    sessions = tuple(row["session"] for row in rows[position + 1 - WARMUP_SESSIONS : position + 1])
    eligible = sorted(
        (
            (str(row["stable_asset_id"]), str(row["symbol"]))
            for row in payload["candidates"]
            if row.get("candidate_eligible") is True
        ),
        key=lambda item: (item[0], item[1]),
    )
    if len({symbol for _, symbol in eligible}) != len(eligible):
        raise IntegrityError("eligible prospective inventory contains duplicate symbols")
    if pilot_symbol_count is not None:
        if not 1 <= pilot_symbol_count <= 20:
            raise ContractError("warm-up pilot must contain between 1 and 20 symbols")
        eligible = eligible[:pilot_symbol_count]
    symbols = tuple(symbol for _, symbol in eligible)
    if not symbols:
        raise ContractError("warm-up requires a nonempty eligible prospective inventory")
    batch_size = int(load_profile(root)["bars"]["deterministic_batch_size"])
    units = tuple(
        WarmupUnit(
            unit_index=index // batch_size,
            symbols=symbols[index : index + batch_size],
            source_plan=alpaca_bars_plan(
                repository_root=root,
                symbols=symbols[index : index + batch_size],
                start=sessions[0],
                end_exclusive=sessions[-1] + timedelta(days=1),
                evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
            ),
        )
        for index in range(0, len(symbols), batch_size)
    )
    shell = LiquidityWarmupPlan(
        warmup_plan_id="",
        source_snapshot_id=str(payload["universe_snapshot_id"]),
        source_snapshot_path=str(source_path),
        signal_session=signal,
        information_cutoff_session=cutoff,
        calendar_release_id=loaded.calendar.release_id,
        sessions=sessions,
        units=units,
        pilot_symbol_count=pilot_symbol_count,
    )
    return LiquidityWarmupPlan(**{**shell.__dict__, "warmup_plan_id": sha256_bytes(canonical_json_bytes(shell.unsigned()))})


def _checkpoint_payload(plan: LiquidityWarmupPlan, completed: Sequence[Mapping[str, object]]) -> dict[str, object]:
    unsigned = {
        "schema_version": 1,
        "warmup_plan_id": plan.warmup_plan_id,
        "source_snapshot_id": plan.source_snapshot_id,
        "completed_units": list(completed),
        "complete": len(completed) == len(plan.units),
        "training_authorized": False,
        "evaluation_authorized": False,
    }
    return {**unsigned, "checkpoint_id": sha256_bytes(canonical_json_bytes(unsigned))}


def load_warmup_checkpoint(path: Path, plan: LiquidityWarmupPlan) -> dict[str, object]:
    if not path.exists():
        return _checkpoint_payload(plan, [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = require_sha256(payload.get("checkpoint_id"), "warm-up checkpoint ID")
    unsigned = {key: value for key, value in payload.items() if key != "checkpoint_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != claimed or payload.get("warmup_plan_id") != plan.warmup_plan_id:
        raise IntegrityError("warm-up checkpoint differs from its exact plan")
    indexes = [int(row["unit_index"]) for row in payload["completed_units"]]
    if indexes != list(range(len(indexes))) or len(indexes) > len(plan.units):
        raise IntegrityError("warm-up checkpoint unit sequence is invalid")
    return payload


def execute_liquidity_warmup(
    *, plan: LiquidityWarmupPlan, approved_plan_id: str, checkpoint_path: Path,
    evidence_store: RawEvidenceStore, network_registry, clock, network_enabled: bool,
    alpaca_key_id: str | None, alpaca_secret_key: str | None,
) -> dict[str, object]:
    if approved_plan_id != plan.warmup_plan_id:
        raise PermissionError("approved warm-up plan differs from deterministic plan")
    checkpoint = load_warmup_checkpoint(checkpoint_path, plan)
    completed = list(checkpoint["completed_units"])
    for unit in plan.units[len(completed):]:
        receipt_ids: list[str] = []
        page_token = None
        parent = None
        for page_index in range(unit.source_plan.maximum_pages):
            result = execute_one_source_request(
                plan=unit.source_plan,
                approved_plan_id=unit.source_plan.plan_id,
                evidence_store=evidence_store,
                network_registry=network_registry,
                clock=clock,
                network_enabled=network_enabled,
                alpaca_key_id=alpaca_key_id,
                alpaca_secret_key=alpaca_secret_key,
                page_index=page_index,
                requested_page_token=page_token,
                parent_request_id=parent,
            )
            receipt_ids.append(result.receipt.receipt_id)
            if result.state != "PAGE_ACCEPTED":
                return {
                    "state": "PARTIAL_FAIL_CLOSED",
                    "unit_index": unit.unit_index,
                    "result": result.summary(),
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "completed_unit_count": len(completed),
                }
            if result.terminal_page:
                break
            page_token = result.next_page_token
            parent = result.receipt.logical_request_id
        else:
            raise IntegrityError("warm-up pagination exceeded its bounded plan")
        completed.append({
            "unit_index": unit.unit_index,
            "source_plan_id": unit.source_plan.plan_id,
            "symbols": list(unit.symbols),
            "receipt_ids": receipt_ids,
        })
        checkpoint = _checkpoint_payload(plan, completed)
        atomic_write(checkpoint_path, canonical_json_bytes(checkpoint))
    return {
        "state": "COMPLETE",
        "checkpoint_id": checkpoint["checkpoint_id"],
        "completed_unit_count": len(completed),
        "unit_count": len(plan.units),
    }


def build_liquidity_universe_snapshot(
    *, plan: LiquidityWarmupPlan, checkpoint_path: Path, evidence_store: RawEvidenceStore,
    output_root: Path,
) -> dict[str, object]:
    checkpoint = load_warmup_checkpoint(checkpoint_path, plan)
    if checkpoint.get("complete") is not True:
        raise ContractError("warm-up checkpoint is incomplete")
    observations: dict[str, list[dict[str, object]]] = {}
    warmup_hashes: dict[str, set[str]] = {}
    receipt_ids: list[str] = []
    eastern = ZoneInfo("America/New_York")
    for completed in checkpoint["completed_units"]:
        for receipt_id in completed["receipt_ids"]:
            receipt = evidence_store.receipt(str(receipt_id))
            receipt_ids.append(receipt.receipt_id)
            payload = json.loads(evidence_store.read_raw(receipt))
            for symbol, rows in payload["bars"].items():
                warmup_hashes.setdefault(symbol, set()).add(receipt.raw_sha256)
                for row in rows:
                    session = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(eastern).date()
                    observations.setdefault(symbol, []).append({
                        "session": session,
                        "close": float(row["c"]),
                        "volume": float(row["v"]),
                    })
    source = _load_snapshot(Path(plan.source_snapshot_path))
    prepared: list[dict[str, object]] = []
    for row in source["candidates"]:
        symbol = str(row["symbol"])
        bars = sorted(observations.get(symbol, []), key=lambda item: item["session"])
        valid = [bar for bar in bars if bar["session"] in set(plan.sessions)]
        reasons = list(row["inclusion_or_exclusion_reasons"])
        median = None
        previous_close = valid[-1]["close"] if valid else None
        if row.get("candidate_eligible") is True:
            reasons = []
            if len(valid) < MINIMUM_VALID_SESSIONS:
                reasons.append("INSUFFICIENT_60_SESSION_LOOKBACK")
            if previous_close is None or previous_close < 5:
                reasons.append("PREVIOUS_CLOSE_BELOW_5")
            if len(valid) >= MINIMUM_VALID_SESSIONS:
                median = statistics.median(bar["close"] * bar["volume"] for bar in valid[-60:])
        prepared.append({
            **row,
            "valid_prior_session_count": len(valid),
            "missing_expected_session_count": len(plan.sessions) - len(valid),
            "previous_close": previous_close,
            "trailing_60_median_dollar_volume": median,
            "liquidity_rank": None,
            "selected": False,
            "final_exclusion_reasons": sorted(set(reasons)),
            "historical_warmup_content_hashes": sorted(warmup_hashes.get(symbol, set())),
        })
    ranked = sorted(
        (row for row in prepared if not row["final_exclusion_reasons"]),
        key=lambda row: (-float(row["trailing_60_median_dollar_volume"]), str(row["stable_asset_id"])),
    )
    for rank, row in enumerate(ranked, 1):
        row["liquidity_rank"] = rank
        row["selected"] = rank <= 500
        if rank > 500:
            row["final_exclusion_reasons"] = ["OUTSIDE_LIQUIDITY_CUTOFF"]
    prepared.sort(key=lambda row: str(row["stable_asset_id"]))
    selected = [row for row in prepared if row["selected"]]
    unsigned = {
        "schema_version": 1,
        "profile_id": PROFILE_ID,
        "snapshot_type": "PROSPECTIVE_SOURCE_WITH_HISTORICAL_LIQUIDITY_WARMUP",
        "signal_session": plan.signal_session.isoformat(),
        "information_cutoff_session": plan.information_cutoff_session.isoformat(),
        "calendar_release_id": plan.calendar_release_id,
        "source_snapshot_id": plan.source_snapshot_id,
        "warmup_plan_id": plan.warmup_plan_id,
        "warmup_checkpoint_id": checkpoint["checkpoint_id"],
        "warmup_receipt_ids": receipt_ids,
        "candidate_count": len(prepared),
        "liquidity_ready_count": len(ranked),
        "selected_count": len(selected),
        "rank_cutoff": selected[-1]["trailing_60_median_dollar_volume"] if selected else None,
        "evidence_class_composition": {
            "membership_and_identity": EvidenceClass.PROSPECTIVE_AS_OBSERVED.value,
            "liquidity_bars": EvidenceClass.HISTORICAL_RECONSTRUCTED.value,
        },
        "rows": prepared,
        "training_authorized": False,
        "evaluation_authorized": False,
    }
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    payload = {**unsigned, "universe_snapshot_id": snapshot_id}
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / f"{snapshot_id}.json"
    if destination.exists() and destination.read_bytes() != canonical_json_bytes(payload):
        raise IntegrityError("liquidity universe snapshot ID collision")
    if not destination.exists():
        atomic_write_new(destination, canonical_json_bytes(payload))
    return payload


def _load_soak_generations(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    generations: list[dict[str, object]] = []
    predecessor = None
    for line in path.read_bytes().splitlines():
        payload = json.loads(line)
        if payload.get("predecessor_soak_run_id") != predecessor:
            raise IntegrityError("soak generation predecessor chain is broken")
        soak_run_id = require_sha256(payload.get("soak_run_id"), "soak run ID")
        unsigned = {key: value for key, value in payload.items() if key != "soak_run_id"}
        if sha256_bytes(canonical_json_bytes(unsigned)) != soak_run_id:
            raise IntegrityError("soak generation differs from canonical content")
        generations.append(payload)
        predecessor = soak_run_id
    return generations


def start_soak_generation(
    *, repository_root: Path, original_ledger_path: Path, generation_ledger_path: Path,
    remediation_commit: str, sip_availability_rule: str, warmup_checkpoint_id: str,
    universe_snapshot_id: str, started_at: datetime,
) -> dict[str, object]:
    if len(remediation_commit) != 40 or any(ch not in "0123456789abcdef" for ch in remediation_commit):
        raise ContractError("soak remediation commit must be a lowercase Git object ID")
    require_sha256(warmup_checkpoint_id, "warm-up checkpoint ID")
    require_sha256(universe_snapshot_id, "universe snapshot ID")
    started = require_aware_utc(started_at, "soak generation start")
    original = validate_capture_ledger(ledger_path=original_ledger_path)
    if original["soak"]["state"] != "PROSPECTIVE_CAPTURE_SOAK_FAILED":
        raise ContractError("new soak generation requires preserved failed original evidence")
    loaded = load_qualified_profile_calendar(repository_root=repository_root)
    generation_path = Path(generation_ledger_path).resolve()
    with ExclusiveFileLock(generation_path.with_suffix(generation_path.suffix + ".lock"), allowed_root=Path(repository_root).resolve() / "data"):
        prior = _load_soak_generations(generation_path)
        generation_number = len(prior) + 1
        session_ledger = generation_path.parent / f"prospective_capture_soak_{generation_number:04d}.jsonl"
        unsigned = {
            "schema_version": 1,
            "generation_number": generation_number,
            "predecessor_soak_run_id": prior[-1]["soak_run_id"] if prior else None,
            "started_at": iso_z(started),
            "state": "PROSPECTIVE_CAPTURE_SOAK_NOT_STARTED",
            "completed_consecutive_sessions": 0,
            "required_consecutive_sessions": 20,
            "original_failed_ledger_path": str(Path(original_ledger_path).resolve()),
            "original_failed_soak_state": "PROSPECTIVE_CAPTURE_SOAK_FAILED",
            "session_ledger_path": str(session_ledger),
            "remediation_commit": remediation_commit,
            "sip_availability_rule": sip_availability_rule,
            "calendar_release_id": loaded.calendar.release_id,
            "warmup_checkpoint_id": warmup_checkpoint_id,
            "universe_snapshot_id": universe_snapshot_id,
            "inherited_completed_session_credit": 0,
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
        }
        payload = {**unsigned, "soak_run_id": sha256_bytes(canonical_json_bytes(unsigned))}
        generation_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(generation_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, canonical_json_bytes(payload))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return payload


def validate_soak_generations(path: Path) -> dict[str, object]:
    generations = _load_soak_generations(path)
    return {
        "state": "PASS",
        "generation_count": len(generations),
        "current": generations[-1] if generations else None,
        "prospective_research_ready": False,
        "training_authorized": False,
        "evaluation_authorized": False,
    }
