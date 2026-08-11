from __future__ import annotations

import json
import os
import statistics
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .alpaca_free_bounded import EvidenceClass, PROFILE_ID, load_profile, load_qualified_profile_calendar
from .clock import TrustedClock
from .common import atomic_write, atomic_write_new, canonical_json_bytes, iso_z, require_aware_utc, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError, NetworkGuardError
from .free_acquisition import execute_one_source_request
from .free_source_evidence import (
    RawEvidenceReceipt,
    RawEvidenceStore,
    SourceRequestPlan,
    alpaca_bars_plan,
    build_daily_capture_plan,
    build_prospective_universe_snapshot,
    prospective_source_plans,
    validate_complete_pagination,
)
from .local_credentials import load_local_api_env
from .locking import ExclusiveFileLock
from .providers.snapshots import NetworkAcquisitionRegistry
from .prospective_liquidity_warmup import _git_commit_exists, _load_snapshot


POLICY_PATH = "config/alpaca_free_automation_acceptance_v1.json"
POLICY_ID = "TWO_SESSION_AUTOMATION_ACCEPTANCE_V1"
TASK_NAME = "USStocksSwingV2-Alpaca-Free-Daily-Capture"
STRICT_REFERENCE = "c29e244174940f76babf75bcf91bbd11ca470c46"
QUALIFIED_CALENDAR = "834ee91a92b21e0c0d053b80f6e0404c14a7d0520417fc83f530b78d475ba3f7"
LEGACY_VALID_SOAK_RUN = "ea1f806cf0609e236d1dbb68702907d2a6fc847e0f0e8bdae464ba7984f05018"
LEGACY_INVALID_SOAK_RUN = "31e0f697943542dbe6221a5d2e042eb8b32a84ab6c60fa1a4cc20a7634fc99d9"

ACCEPTANCE_STATES = {
    "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED",
    "TWO_SESSION_AUTOMATION_ACCEPTANCE_IN_PROGRESS",
    "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED",
    "TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE",
    "AUTOMATION_PAUSED_STRUCTURAL_FAILURE",
}
STRUCTURAL_FAILURES = {
    "SCHEMA_DRIFT",
    "CREDENTIAL_LEAKAGE_RISK",
    "RECEIPT_CHAIN_CORRUPTION",
    "LEDGER_CORRUPTION",
    "QUALIFIED_CALENDAR_INTEGRITY_FAILURE",
    "INCONSISTENT_UNIVERSE_RECONSTRUCTION",
    "CODE_COMMIT_MISMATCH",
    "SECRET_OR_RAW_DATA_GIT_EXPOSURE",
    "REPEATED_DETERMINISTIC_PARSER_FAILURE",
}
TRANSIENT_FAILURES = {
    "CREDENTIAL_UNAVAILABLE",
    "PROVIDER_UNAVAILABLE",
    "PROVIDER_RATE_LIMIT",
    "MACHINE_WAKE_DELAY",
    "TEMPORARY_NETWORK_LOSS",
    "PROVIDER_PARTIAL_RESPONSE",
    "LATE_CAPTURE",
}


class AutomationFailure(RuntimeError):
    def __init__(self, classification: str, message: str, *, structural: bool) -> None:
        super().__init__(message)
        self.classification = classification
        self.structural = structural


def load_automation_policy(repository_root: Path) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    try:
        payload = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("automation acceptance policy is unavailable") from exc
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != "US_stocks_swing_model_v2"
        or payload.get("profile_id") != PROFILE_ID
        or payload.get("policy_id") != POLICY_ID
        or payload.get("required_consecutive_sessions") != 2
        or payload.get("inherited_completed_session_credit") != 0
        or payload.get("background_monitor", {}).get("rolling_session_window") != 20
        or payload.get("background_monitor", {}).get("blocking") is not False
        or payload.get("rolling_liquidity", {}).get("required_feed") != "sip"
        or payload.get("rolling_liquidity", {}).get("timeframe") != "1Day"
        or payload.get("rolling_liquidity", {}).get("adjustment") != "raw"
        or payload.get("rolling_liquidity", {}).get("iex_fallback") is not False
        or set(payload.get("structural_failures", [])) != STRUCTURAL_FAILURES
        or set(payload.get("transient_failures", [])) != TRANSIENT_FAILURES
        or any(payload.get("authorities", {}).values())
    ):
        raise ContractError("automation acceptance policy differs from its fail-closed contract")
    return payload


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True,
    )
    if result.returncode != 0:
        raise IntegrityError("Git runtime validation failed")
    return result.stdout.strip()


def _canonical_ledger(path: Path, *, id_field: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    predecessor = None
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise IntegrityError("automation ledger is unreadable") from exc
    for line in lines:
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError("automation ledger contains invalid JSON") from exc
        if row.get("predecessor_event_id") != predecessor:
            raise IntegrityError("automation ledger predecessor chain is broken")
        event_id = require_sha256(row.get(id_field), id_field)
        unsigned = {key: value for key, value in row.items() if key != id_field}
        if sha256_bytes(canonical_json_bytes(unsigned)) != event_id:
            raise IntegrityError("automation ledger event differs from canonical content")
        rows.append(row)
        predecessor = event_id
    return rows


def _append_event(path: Path, payload: Mapping[str, object], *, id_field: str, allowed_root: Path) -> dict[str, object]:
    resolved = path.resolve()
    with ExclusiveFileLock(resolved.with_suffix(resolved.suffix + ".lock"), allowed_root=allowed_root):
        rows = _canonical_ledger(resolved, id_field=id_field)
        unsigned = {
            **payload,
            "predecessor_event_id": rows[-1][id_field] if rows else None,
        }
        event = {**unsigned, id_field: sha256_bytes(canonical_json_bytes(unsigned))}
        resolved.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(resolved, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, canonical_json_bytes(event))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return event


def _paths(root: Path, policy: Mapping[str, object]) -> dict[str, Path]:
    return {key: root / str(value) for key, value in policy["paths"].items()}


def supersede_legacy_soak_and_initialize_acceptance(
    *, repository_root: Path, remediation_commit: str, initialized_at: datetime,
) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    initialized = require_aware_utc(initialized_at, "automation acceptance initialization time")
    if not _git_commit_exists(root, remediation_commit):
        raise ContractError("automation remediation commit does not identify a repository commit")
    loaded = load_qualified_profile_calendar(repository_root=root)
    if loaded.calendar.release_id != QUALIFIED_CALENDAR:
        raise IntegrityError("automation acceptance calendar differs from the qualified profile release")
    paths = _paths(root, policy)
    existing = _canonical_ledger(paths["acceptance_ledger"], id_field="event_id")
    if existing:
        current = acceptance_status(repository_root=root)
        if current["remediation_commit"] != remediation_commit:
            raise IntegrityError("automation acceptance was already initialized for another commit")
        return current
    supersession = _append_event(
        paths["policy_events"],
        {
            "schema_version": 1,
            "event_type": "LEGACY_SOAK_SUPERSESSION",
            "recorded_at": iso_z(initialized),
            "legacy_soak_run_id": LEGACY_VALID_SOAK_RUN,
            "invalid_legacy_soak_run_id": LEGACY_INVALID_SOAK_RUN,
            "legacy_completed_session_credit": 0,
            "legacy_state": "PROSPECTIVE_CAPTURE_SOAK_NOT_STARTED",
            "new_state": "SUPERSEDED_BY_OWNER_ACCEPTANCE_POLICY_CHANGE",
            "reason": "OWNER_CHANGED_BLOCKING_OPERATIONAL_ACCEPTANCE_THRESHOLD_FROM_20_TO_2",
            "evidence_deleted": False,
            "scientific_failure": False,
            "operational_failure": False,
            "inherited_credit_allowed": False,
            "original_failed_capture_ledger_preserved": True,
            "invalid_generation_preserved": True,
            "valid_not_started_generation_preserved": True,
            "background_monitor_policy_id": "NONBLOCKING_BACKGROUND_RELIABILITY_MONITOR",
            "background_monitor_window_sessions": 20,
            "background_monitor_blocking": False,
        },
        id_field="event_id",
        allowed_root=root / "data",
    )
    run_unsigned = {
        "policy_id": POLICY_ID,
        "remediation_commit": remediation_commit,
        "calendar_release_id": loaded.calendar.release_id,
        "legacy_predecessor_soak_run_id": LEGACY_VALID_SOAK_RUN,
        "supersession_event_id": supersession["event_id"],
        "initialized_at": iso_z(initialized),
        "required_consecutive_sessions": 2,
        "inherited_completed_session_credit": 0,
    }
    run_id = sha256_bytes(canonical_json_bytes(run_unsigned))
    event = _append_event(
        paths["acceptance_ledger"],
        {
            "schema_version": 1,
            "event_type": "ACCEPTANCE_GENERATION_STARTED",
            "recorded_at": iso_z(initialized),
            "acceptance_run_id": run_id,
            **run_unsigned,
            "state": "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED",
            "completed_consecutive_sessions": 0,
            "latest_completed_session": None,
            "failure_classification": None,
            "prospective_capture_automation_accepted": False,
            "prospective_capture_operational": False,
            "background_reliability_monitoring_active": False,
            "next_phase_historical_exploratory_development_eligible": False,
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
        },
        id_field="event_id",
        allowed_root=root / "data",
    )
    return {**event, "supersession": supersession}


def acceptance_status(*, repository_root: Path) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    rows = _canonical_ledger(_paths(root, policy)["acceptance_ledger"], id_field="event_id")
    if not rows:
        return {
            "state": "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED",
            "initialized": False,
            "completed_consecutive_sessions": 0,
            "required_consecutive_sessions": 2,
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
        }
    current = dict(rows[-1])
    current["initialized"] = True
    return current


def _calendar_sessions(root: Path) -> tuple[object, list[dict[str, object]]]:
    loaded = load_qualified_profile_calendar(repository_root=root)
    if loaded.calendar.release_id != QUALIFIED_CALENDAR:
        raise AutomationFailure(
            "QUALIFIED_CALENDAR_INTEGRITY_FAILURE",
            "qualified automation calendar differs",
            structural=True,
        )
    return loaded, loaded.schedule.to_pylist()


def session_context(*, repository_root: Path, local_date: date) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    loaded, rows = _calendar_sessions(root)
    positions = {row["session"]: index for index, row in enumerate(rows)}
    index = positions.get(local_date)
    if index is None:
        return {
            "state": "SKIP_NON_XNYS_SESSION",
            "local_date": local_date.isoformat(),
            "calendar_release_id": loaded.calendar.release_id,
            "provider_requests": 0,
            "acceptance_credit_change": 0,
        }
    if index == 0:
        raise IntegrityError("qualified calendar lacks the previous session")
    row = rows[index]
    previous = rows[index - 1]
    open_at = require_aware_utc(row["open_at"], "XNYS open")
    offsets = policy["phase_offsets_minutes_before_open"]
    context = {
        "state": "XNYS_SESSION",
        "session": row["session"].isoformat(),
        "previous_session": previous["session"].isoformat(),
        "open_at": iso_z(open_at),
        "phase_b_target": iso_z(open_at - timedelta(minutes=int(offsets["phase_b_target"]))),
        "phase_b_validation_deadline": iso_z(open_at - timedelta(minutes=int(offsets["phase_b_validation_deadline"]))),
        "phase_a_target": iso_z(open_at - timedelta(minutes=int(offsets["phase_a_target"]))),
        "final_cutoff": iso_z(open_at - timedelta(minutes=int(offsets["final_cutoff"]))),
        "calendar_release_id": loaded.calendar.release_id,
    }
    if not (
        datetime.fromisoformat(context["phase_b_target"].replace("Z", "+00:00"))
        < datetime.fromisoformat(context["phase_b_validation_deadline"].replace("Z", "+00:00"))
        < datetime.fromisoformat(context["phase_a_target"].replace("Z", "+00:00"))
        < datetime.fromisoformat(context["final_cutoff"].replace("Z", "+00:00"))
        < open_at
    ):
        raise IntegrityError("automation phase ordering differs")
    return context


def _current_acceptance_run(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    if not rows:
        raise ContractError("automation acceptance is not initialized")
    return dict(rows[-1])


def _start_successor(
    *, root: Path, policy: Mapping[str, object], failed: Mapping[str, object], recorded: datetime,
) -> dict[str, object]:
    successor_unsigned = {
        "policy_id": POLICY_ID,
        "predecessor_acceptance_run_id": failed["acceptance_run_id"],
        "remediation_commit": failed["remediation_commit"],
        "calendar_release_id": failed["calendar_release_id"],
        "started_after_failed_session": failed["session"],
        "started_at": iso_z(recorded),
        "required_consecutive_sessions": 2,
        "inherited_completed_session_credit": 0,
    }
    successor_run_id = sha256_bytes(canonical_json_bytes(successor_unsigned))
    return _append_event(
        _paths(root, policy)["acceptance_ledger"],
        {
            "schema_version": 1,
            "event_type": "ACCEPTANCE_SUCCESSOR_STARTED",
            "recorded_at": iso_z(recorded),
            "acceptance_run_id": successor_run_id,
            **successor_unsigned,
            "state": "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED",
            "completed_consecutive_sessions": 0,
            "latest_completed_session": None,
            "failure_classification": None,
            "prospective_capture_automation_accepted": False,
            "prospective_capture_operational": False,
            "background_reliability_monitoring_active": False,
            "next_phase_historical_exploratory_development_eligible": False,
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
        },
        id_field="event_id",
        allowed_root=root / "data",
    )


def record_acceptance_result(
    *, repository_root: Path, session: date, complete: bool,
    failure_classification: str | None, recorded_at: datetime,
) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    paths = _paths(root, policy)
    recorded = require_aware_utc(recorded_at, "acceptance result time")
    rows = _canonical_ledger(paths["acceptance_ledger"], id_field="event_id")
    current = _current_acceptance_run(rows)
    if current["state"] == "AUTOMATION_PAUSED_STRUCTURAL_FAILURE":
        return current
    if current["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED":
        if session <= date.fromisoformat(str(current["session"])):
            return current
        current = _start_successor(root=root, policy=policy, failed=current, recorded=recorded)
        rows = [*rows, current]
    run_id = str(current["acceptance_run_id"])
    duplicate = any(
        row.get("acceptance_run_id") == run_id and row.get("session") == session.isoformat()
        for row in rows
    )
    if duplicate:
        return current
    if current["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE":
        return current
    eligible_after = current.get("started_after_failed_session")
    if complete and eligible_after and session <= date.fromisoformat(str(eligible_after)):
        return current
    if not complete:
        if failure_classification not in STRUCTURAL_FAILURES | TRANSIENT_FAILURES:
            raise ContractError("automation failure classification is not registered")
        structural = failure_classification in STRUCTURAL_FAILURES
        failed = _append_event(
            paths["acceptance_ledger"],
            {
                **{k: v for k, v in current.items() if k not in {"event_id", "predecessor_event_id", "recorded_at", "event_type", "state", "failure_classification"}},
                "schema_version": 1,
                "event_type": "ACCEPTANCE_SESSION_FAILED",
                "recorded_at": iso_z(recorded),
                "session": session.isoformat(),
                "state": "AUTOMATION_PAUSED_STRUCTURAL_FAILURE" if structural else "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED",
                "failure_classification": failure_classification,
            },
            id_field="event_id",
            allowed_root=root / "data",
        )
        if structural:
            return failed
        return failed
    credit = int(current["completed_consecutive_sessions"])
    latest = current.get("latest_completed_session")
    if credit == 1 and latest:
        _, calendar_rows = _calendar_sessions(root)
        positions = {row["session"].isoformat(): index for index, row in enumerate(calendar_rows)}
        prior_index = positions.get(str(latest))
        if prior_index is None or prior_index + 1 >= len(calendar_rows):
            raise AutomationFailure(
                "QUALIFIED_CALENDAR_INTEGRITY_FAILURE",
                "acceptance calendar cannot establish the next session",
                structural=True,
            )
        expected_session = calendar_rows[prior_index + 1]["session"]
        if expected_session != session:
            record_acceptance_result(
                repository_root=root,
                session=expected_session,
                complete=False,
                failure_classification="LATE_CAPTURE",
                recorded_at=recorded,
            )
            return record_acceptance_result(
                repository_root=root,
                session=session,
                complete=True,
                failure_classification=None,
                recorded_at=recorded + timedelta(microseconds=1),
            )
    credit += 1
    accepted = credit >= 2
    return _append_event(
        paths["acceptance_ledger"],
        {
            **{k: v for k, v in current.items() if k not in {"event_id", "predecessor_event_id", "recorded_at", "event_type", "state", "failure_classification", "session"}},
            "schema_version": 1,
            "event_type": "ACCEPTANCE_SESSION_COMPLETE",
            "recorded_at": iso_z(recorded),
            "session": session.isoformat(),
            "state": "TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE" if accepted else "TWO_SESSION_AUTOMATION_ACCEPTANCE_IN_PROGRESS",
            "completed_consecutive_sessions": min(credit, 2),
            "latest_completed_session": session.isoformat(),
            "failure_classification": None,
            "prospective_capture_automation_accepted": accepted,
            "prospective_capture_operational": accepted,
            "background_reliability_monitoring_active": accepted,
            "next_phase_historical_exploratory_development_eligible": accepted,
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
        },
        id_field="event_id",
        allowed_root=root / "data",
    )


def _latest_json(directory: Path) -> Path:
    candidates = sorted(directory.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    if not candidates:
        raise IntegrityError(f"required local snapshot is missing: {directory.name}")
    return candidates[-1]


def _safe_runtime_validation(root: Path, policy: Mapping[str, object], *, require_clean: bool) -> dict[str, object]:
    if root != Path(r"C:\Users\donny\Desktop\US_stocks_swing_model_v2").resolve():
        raise AutomationFailure("CODE_COMMIT_MISMATCH", "repository root differs", structural=True)
    branch = _git(root, "branch", "--show-current")
    head = _git(root, "rev-parse", "HEAD")
    merge_base = _git(root, "merge-base", STRICT_REFERENCE, "HEAD")
    if branch != "alpaca-free-bounded-long-short" or merge_base != STRICT_REFERENCE:
        raise AutomationFailure("CODE_COMMIT_MISMATCH", "repository branch ancestry differs", structural=True)
    if require_clean and _git(root, "status", "--short"):
        raise AutomationFailure("CODE_COMMIT_MISMATCH", "scheduled capture requires a clean tree", structural=True)
    if _git(root, "check-ignore", "api.env") != "api.env" or _git(root, "ls-files", "--", "api.env"):
        raise AutomationFailure("SECRET_OR_RAW_DATA_GIT_EXPOSURE", "api.env Git safety differs", structural=True)
    for relative in ("data", str(policy["paths"]["automation_root"])):
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "--", relative], cwd=root, check=False
        ).returncode == 0
        if not ignored:
            raise AutomationFailure("SECRET_OR_RAW_DATA_GIT_EXPOSURE", "generated evidence root is not ignored", structural=True)
    loaded = load_qualified_profile_calendar(repository_root=root)
    if loaded.calendar.release_id != QUALIFIED_CALENDAR:
        raise AutomationFailure("QUALIFIED_CALENDAR_INTEGRITY_FAILURE", "qualified calendar differs", structural=True)
    acceptance = acceptance_status(repository_root=root)
    if acceptance.get("initialized") and not _git_commit_exists(root, str(acceptance["remediation_commit"])):
        raise AutomationFailure("CODE_COMMIT_MISMATCH", "acceptance commit is unavailable", structural=True)
    if acceptance.get("initialized"):
        ancestor = _git(root, "merge-base", str(acceptance["remediation_commit"]), head)
        if ancestor != acceptance["remediation_commit"]:
            raise AutomationFailure("CODE_COMMIT_MISMATCH", "HEAD does not descend from the acceptance commit", structural=True)
    return {"branch": branch, "head": head, "calendar_release_id": loaded.calendar.release_id}


def build_daily_liquidity_plans(
    *, repository_root: Path, candidate_snapshot_path: Path, completed_session: date,
) -> tuple[SourceRequestPlan, ...]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    snapshot = _load_snapshot(candidate_snapshot_path)
    eligible = sorted({
        str(row["symbol"])
        for row in snapshot["candidates"]
        if row.get("candidate_eligible") is True
    })
    if not eligible:
        raise IntegrityError("daily liquidity population is empty")
    batch_size = int(policy["rolling_liquidity"]["daily_batch_size"])
    return tuple(
        alpaca_bars_plan(
            repository_root=root,
            symbols=eligible[index : index + batch_size],
            start=completed_session,
            end_exclusive=completed_session + timedelta(days=1),
            evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
        )
        for index in range(0, len(eligible), batch_size)
    )


def _checkpoint(path: Path, plan_ids: Sequence[str]) -> dict[str, object]:
    if not path.exists():
        unsigned = {"schema_version": 1, "plan_ids": list(plan_ids), "completed": []}
        return {**unsigned, "checkpoint_id": sha256_bytes(canonical_json_bytes(unsigned))}
    payload = json.loads(path.read_text(encoding="utf-8"))
    claimed = require_sha256(payload.get("checkpoint_id"), "daily checkpoint ID")
    unsigned = {key: value for key, value in payload.items() if key != "checkpoint_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != claimed or payload.get("plan_ids") != list(plan_ids):
        raise AutomationFailure("LEDGER_CORRUPTION", "daily checkpoint differs from its plan", structural=True)
    completed = payload.get("completed")
    if not isinstance(completed, list) or [row["plan_index"] for row in completed] != list(range(len(completed))):
        raise AutomationFailure("LEDGER_CORRUPTION", "daily checkpoint sequence differs", structural=True)
    return payload


def _write_checkpoint(path: Path, plan_ids: Sequence[str], completed: Sequence[Mapping[str, object]]) -> dict[str, object]:
    unsigned = {"schema_version": 1, "plan_ids": list(plan_ids), "completed": list(completed)}
    payload = {**unsigned, "checkpoint_id": sha256_bytes(canonical_json_bytes(unsigned))}
    atomic_write(path, canonical_json_bytes(payload))
    return payload


def _execute_plans(
    *, plans: Sequence[SourceRequestPlan], checkpoint_path: Path, store: RawEvidenceStore,
    registry: NetworkAcquisitionRegistry, deadline: datetime, credentials: Mapping[str, str | None],
) -> tuple[str, ...]:
    checkpoint = _checkpoint(checkpoint_path, [plan.plan_id for plan in plans])
    completed = list(checkpoint["completed"])
    receipt_ids = [str(value) for row in completed for value in row["receipt_ids"]]
    for plan_index, plan in enumerate(plans[len(completed):], start=len(completed)):
        page_receipts: list[str] = []
        page_token = None
        parent = None
        for page_index in range(plan.maximum_pages):
            if TrustedClock.production().now() >= deadline:
                raise AutomationFailure("LATE_CAPTURE", "capture deadline elapsed", structural=False)
            try:
                result = execute_one_source_request(
                    plan=plan,
                    approved_plan_id=plan.plan_id,
                    evidence_store=store,
                    network_registry=registry,
                    clock=TrustedClock.production(),
                    network_enabled=True,
                    alpaca_key_id=credentials.get("APCA_API_KEY_ID"),
                    alpaca_secret_key=credentials.get("APCA_API_SECRET_KEY"),
                    alpha_vantage_key=credentials.get("ALPHA_VANTAGE_API_KEY"),
                    page_index=page_index,
                    requested_page_token=page_token,
                    parent_request_id=parent,
                )
            except NetworkGuardError as exc:
                raise AutomationFailure("TEMPORARY_NETWORK_LOSS", str(exc), structural=False) from exc
            page_receipts.append(result.receipt.receipt_id)
            if result.state != "PAGE_ACCEPTED":
                classification = "PROVIDER_RATE_LIMIT" if result.receipt.http_status == 429 else "PROVIDER_UNAVAILABLE"
                raise AutomationFailure(classification, "provider page was not accepted", structural=False)
            if result.terminal_page:
                break
            page_token = result.next_page_token
            parent = result.receipt.logical_request_id
        else:
            raise AutomationFailure("PROVIDER_PARTIAL_RESPONSE", "pagination exceeded the bounded plan", structural=False)
        completed.append({"plan_index": plan_index, "plan_id": plan.plan_id, "receipt_ids": page_receipts})
        _write_checkpoint(checkpoint_path, [item.plan_id for item in plans], completed)
        receipt_ids.extend(page_receipts)
    return tuple(receipt_ids)


def _wait_until(target: datetime, *, cutoff: datetime, disable_wait: bool) -> None:
    now = TrustedClock.production().now()
    if now >= cutoff:
        raise AutomationFailure("LATE_CAPTURE", "machine reached the workflow after its cutoff", structural=False)
    if disable_wait or now >= target:
        return
    while now < target:
        time.sleep(min(60.0, (target - now).total_seconds()))
        now = TrustedClock.production().now()
        if now >= cutoff:
            raise AutomationFailure("LATE_CAPTURE", "phase target wait crossed its cutoff", structural=False)


def _receipt_summary(store: RawEvidenceStore, receipt_ids: Iterable[str]) -> list[dict[str, object]]:
    rows = []
    for receipt_id in receipt_ids:
        receipt = store.receipt(receipt_id)
        rows.append({
            "receipt_id": receipt.receipt_id,
            "source": receipt.source,
            "content_hash": receipt.raw_sha256,
            "request_time": iso_z(receipt.requested_at),
            "receipt_time": iso_z(receipt.retrieved_at),
            "validation_state": receipt.validation_status,
            "pagination_terminal": receipt.next_page_token is None,
        })
    return rows


def _rolling_receipt_seed(root: Path) -> tuple[str, ...]:
    automation = root / "data/w/alpaca_free_bounded/automation/universe_snapshots"
    if automation.exists() and list(automation.glob("*.json")):
        payload = json.loads(_latest_json(automation).read_text(encoding="utf-8"))
        return tuple(str(value) for value in payload["rolling_receipt_ids"])
    legacy = root / "data/w/alpaca_free_bounded_v1/liquidity_universe"
    payload = json.loads(_latest_json(legacy).read_text(encoding="utf-8"))
    return tuple(str(value) for value in payload["warmup_receipt_ids"])


def _collect_bars(
    store: RawEvidenceStore, receipt_ids: Iterable[str], sessions: set[date]
) -> tuple[dict[str, list[dict[str, object]]], dict[str, set[str]]]:
    eastern = ZoneInfo("America/New_York")
    values: dict[tuple[str, date], tuple[float, float]] = {}
    hashes: dict[str, set[str]] = {}
    for receipt_id in receipt_ids:
        receipt = store.receipt(receipt_id)
        if (
            receipt.source != "alpaca_free_bounded_bars"
            or receipt.http_status != 200
            or receipt.parsing_status != "PARSED"
            or not receipt.validation_status.startswith("PASS")
        ):
            raise AutomationFailure("RECEIPT_CHAIN_CORRUPTION", "rolling receipt is not an accepted bars receipt", structural=True)
        query = dict(receipt.canonical_query)
        if query.get("feed") != "sip" or query.get("timeframe") != "1Day" or query.get("adjustment") != "raw" or "iex" in receipt.sanitized_url.lower():
            raise AutomationFailure("RECEIPT_CHAIN_CORRUPTION", "rolling bars receipt contract differs", structural=True)
        payload = json.loads(store.read_raw(receipt))
        for symbol, rows in payload["bars"].items():
            for row in rows:
                observed_session = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(eastern).date()
                if observed_session not in sessions:
                    continue
                key = (str(symbol), observed_session)
                observed = (float(row["c"]), float(row["v"]))
                if key in values and values[key] != observed:
                    raise AutomationFailure("INCONSISTENT_UNIVERSE_RECONSTRUCTION", "duplicate bars disagree", structural=True)
                values[key] = observed
                hashes.setdefault(str(symbol), set()).add(receipt.raw_sha256)
    observations: dict[str, list[dict[str, object]]] = {}
    for (symbol, session), (close, volume) in values.items():
        observations.setdefault(symbol, []).append({"session": session, "close": close, "volume": volume})
    for rows in observations.values():
        rows.sort(key=lambda row: row["session"])
    return observations, hashes


def _build_rolling_universe(
    *, root: Path, candidate_snapshot_path: Path, signal_session: date,
    sessions: Sequence[date], receipt_ids: Sequence[str], store: RawEvidenceStore,
    output_root: Path,
) -> dict[str, object]:
    source = _load_snapshot(candidate_snapshot_path)
    observations, hashes = _collect_bars(store, receipt_ids, set(sessions))
    prepared = []
    for source_row in source["candidates"]:
        row = dict(source_row)
        symbol = str(row["symbol"])
        bars = observations.get(symbol, [])
        reasons = list(row["inclusion_or_exclusion_reasons"])
        median = None
        previous_close = bars[-1]["close"] if bars else None
        if row.get("candidate_eligible") is True:
            reasons = []
            if len(bars) < 60:
                reasons.append("INSUFFICIENT_HISTORY")
            if previous_close is None or previous_close < 5:
                reasons.append("PREVIOUS_CLOSE_BELOW_5")
            if len(bars) >= 60:
                median = statistics.median(item["close"] * item["volume"] for item in bars[-60:])
        prepared.append({
            **row,
            "valid_prior_session_count": len(bars),
            "missing_expected_session_count": len(sessions) - len(bars),
            "previous_close": previous_close,
            "trailing_60_median_dollar_volume": median,
            "liquidity_rank": None,
            "selected": False,
            "final_exclusion_reasons": sorted(set(reasons)),
            "historical_liquidity_content_hashes": sorted(hashes.get(symbol, set())),
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
        "policy_id": POLICY_ID,
        "signal_session": signal_session.isoformat(),
        "information_cutoff_session": sessions[-1].isoformat(),
        "calendar_release_id": QUALIFIED_CALENDAR,
        "source_snapshot_id": source["universe_snapshot_id"],
        "rolling_sessions": [value.isoformat() for value in sessions],
        "rolling_receipt_ids": list(receipt_ids),
        "candidate_count": len(prepared),
        "liquidity_ready_count": len(ranked),
        "selected_count": len(selected),
        "rank_cutoff": selected[-1]["trailing_60_median_dollar_volume"] if selected else None,
        "evidence_class_composition": {
            "membership_and_identity": EvidenceClass.PROSPECTIVE_AS_OBSERVED.value,
            "rolling_liquidity_bars": "MIXED_PROSPECTIVE_AND_HISTORICAL_RECONSTRUCTED_WITH_RECEIPT_LINEAGE",
        },
        "rows": prepared,
        "prospective_research_ready": False,
        "training_authorized": False,
        "evaluation_authorized": False,
    }
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    payload = {**unsigned, "universe_snapshot_id": snapshot_id}
    destination = output_root / f"{snapshot_id}.json"
    if destination.exists() and destination.read_bytes() != canonical_json_bytes(payload):
        raise AutomationFailure("INCONSISTENT_UNIVERSE_RECONSTRUCTION", "universe snapshot collision", structural=True)
    if not destination.exists():
        atomic_write_new(destination, canonical_json_bytes(payload))
    return payload


def _onboarding_queue(
    *, root: Path, policy: Mapping[str, object], candidate_snapshot: Mapping[str, object],
    covered_symbols: set[str], recorded_at: datetime,
) -> dict[str, object]:
    eligible = sorted(
        (str(row["stable_asset_id"]), str(row["symbol"]))
        for row in candidate_snapshot["candidates"]
        if row.get("candidate_eligible") is True and str(row["symbol"]) not in covered_symbols
    )
    limit = int(policy["rolling_liquidity"]["new_candidate_queue_limit"])
    unsigned = {
        "schema_version": 1,
        "recorded_at": iso_z(recorded_at),
        "evidence_class": EvidenceClass.HISTORICAL_RECONSTRUCTED.value,
        "queue_limit": limit,
        "queued_count": len(eligible),
        "selected_for_bounded_warmup": [symbol for _, symbol in eligible[:limit]],
        "remaining_visible": [symbol for _, symbol in eligible[limit:]],
        "admission_rule": "REQUIRE_60_VALID_SESSIONS_BEFORE_TOP_500_ELIGIBILITY",
    }
    payload = {**unsigned, "queue_id": sha256_bytes(canonical_json_bytes(unsigned))}
    atomic_write(_paths(root, policy)["onboarding_queue"], canonical_json_bytes(payload))
    return payload


def _session_ledger_append(
    *, root: Path, policy: Mapping[str, object], payload: Mapping[str, object]
) -> dict[str, object]:
    return _append_event(
        _paths(root, policy)["session_ledger"], payload,
        id_field="session_event_id", allowed_root=root / "data",
    )


def reconcile_missed_sessions(
    *, repository_root: Path, current_session: date, recorded_at: datetime,
) -> tuple[dict[str, object], ...]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    paths = _paths(root, policy)
    recorded = require_aware_utc(recorded_at, "missed-session reconciliation time")
    ledger = _canonical_ledger(paths["session_ledger"], id_field="session_event_id")
    acceptance = acceptance_status(repository_root=root)
    if not acceptance.get("initialized"):
        raise ContractError("automation acceptance is not initialized")
    represented = {date.fromisoformat(str(row["session"])) for row in ledger}
    if ledger:
        lower_bound = max(represented)
    else:
        initialized = datetime.fromisoformat(str(acceptance["initialized_at"]).replace("Z", "+00:00"))
        lower_bound = initialized.astimezone(ZoneInfo("America/Los_Angeles")).date()
    _, calendar_rows = _calendar_sessions(root)
    missing = tuple(
        row["session"] for row in calendar_rows
        if lower_bound < row["session"] < current_session and row["session"] not in represented
    )
    appended = []
    for session in missing:
        acceptance_result = record_acceptance_result(
            repository_root=root,
            session=session,
            complete=False,
            failure_classification="MACHINE_WAKE_DELAY",
            recorded_at=recorded,
        )
        appended.append(_session_ledger_append(
            root=root,
            policy=policy,
            payload={
                "schema_version": 1,
                "task_name": TASK_NAME,
                "session": session.isoformat(),
                "started_at": None,
                "completed_at": iso_z(recorded),
                "final_status": "PARTIAL_FAIL_CLOSED",
                "failure_classification": "MACHINE_WAKE_DELAY",
                "retry_count": 0,
                "missing_symbol_count": 0,
                "pagination_failure_count": 0,
                "acceptance_run_id": acceptance_result["acceptance_run_id"],
                "acceptance_state": acceptance_result["state"],
                "acceptance_credit": acceptance_result["completed_consecutive_sessions"],
                "prospective_research_ready": False,
                "training_authorized": False,
                "evaluation_authorized": False,
                "orders": 0,
                "predictions": 0,
            },
        ))
    return tuple(appended)


def background_monitor_status(*, repository_root: Path) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    rows = _canonical_ledger(_paths(root, policy)["session_ledger"], id_field="session_event_id")[-20:]
    return {
        "policy_id": "NONBLOCKING_BACKGROUND_RELIABILITY_MONITOR",
        "blocking": False,
        "window_sessions": 20,
        "sessions_expected": len(rows),
        "sessions_complete": sum(row.get("final_status") == "COMPLETE" for row in rows),
        "sessions_partial": sum(row.get("final_status") == "PARTIAL_FAIL_CLOSED" for row in rows),
        "sessions_late": sum(row.get("failure_classification") == "LATE_CAPTURE" for row in rows),
        "provider_failures": sum(str(row.get("failure_classification", "")).startswith("PROVIDER_") for row in rows),
        "retry_count": sum(int(row.get("retry_count", 0)) for row in rows),
        "missing_symbol_count": sum(int(row.get("missing_symbol_count", 0)) for row in rows),
        "pagination_failures": sum(int(row.get("pagination_failure_count", 0)) for row in rows),
        "universe_determinism_failures": sum(row.get("failure_classification") == "INCONSISTENT_UNIVERSE_RECONSTRUCTION" for row in rows),
        "latest_top_500_count": rows[-1].get("selected_count") if rows else None,
    }


def _redacted_text(value: object, secrets: Iterable[str]) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "REDACTED")
    return text


def _write_status(root: Path, policy: Mapping[str, object], payload: Mapping[str, object], secrets: Iterable[str] = ()) -> None:
    safe = json.loads(_redacted_text(json.dumps(payload, sort_keys=True), secrets))
    forbidden = tuple(secret for secret in secrets if secret)
    encoded = canonical_json_bytes(safe)
    if any(secret.encode("utf-8") in encoded for secret in forbidden):
        raise AutomationFailure("CREDENTIAL_LEAKAGE_RISK", "status redaction failed", structural=True)
    atomic_write(_paths(root, policy)["latest_status"], encoded)


def _log(root: Path, policy: Mapping[str, object], message: str, *, secrets: Iterable[str] = ()) -> None:
    logs = _paths(root, policy)["logs"]
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"capture-{datetime.now(timezone.utc).date().isoformat()}.log"
    safe = _redacted_text(message, secrets)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{iso_z(datetime.now(timezone.utc))} {safe}\n")
    files = sorted(logs.glob("capture-*.log"), key=lambda item: item.stat().st_mtime_ns)
    for old in files[:-10]:
        old.unlink()


def automation_status(*, repository_root: Path) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    latest = _paths(root, policy)["latest_status"]
    return {
        "task_name": TASK_NAME,
        "acceptance": acceptance_status(repository_root=root),
        "background_monitor": background_monitor_status(repository_root=root),
        "latest_status": json.loads(latest.read_text(encoding="utf-8")) if latest.exists() else None,
        "network_requests": 0,
    }


def dry_run_daily_capture(*, repository_root: Path, local_date: date | None = None) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    runtime = _safe_runtime_validation(root, policy, require_clean=False)
    local = local_date or datetime.now(ZoneInfo("America/Los_Angeles")).date()
    context = session_context(repository_root=root, local_date=local)
    if context["state"] == "SKIP_NON_XNYS_SESSION":
        return {"state": "DRY_RUN_PASS", "runtime": runtime, "session_context": context, "network_requests": 0}
    source = _latest_json(root / "data/w/alpaca_free_bounded_v1/prospective_universe")
    plans = build_daily_liquidity_plans(
        repository_root=root,
        candidate_snapshot_path=source,
        completed_session=date.fromisoformat(str(context["previous_session"])),
    )
    return {
        "state": "DRY_RUN_PASS",
        "runtime": runtime,
        "session_context": context,
        "candidate_snapshot": str(source),
        "daily_liquidity_plan_count": len(plans),
        "daily_liquidity_symbol_count": len({symbol for plan in plans for symbol in dict(plan.canonical_query)["symbols"].split(",")}),
        "bar_contract": {"feed": "sip", "timeframe": "1Day", "adjustment": "raw", "iex_fallback": False},
        "acceptance": acceptance_status(repository_root=root),
        "network_requests": 0,
        "orders": 0,
        "predictions": 0,
        "training": False,
        "evaluation": False,
    }


def _record_failed_session(
    *, root: Path, policy: Mapping[str, object], session: date,
    context: Mapping[str, object], started: datetime, failure: AutomationFailure,
    secrets: Iterable[str],
) -> dict[str, object]:
    acceptance_result = record_acceptance_result(
        repository_root=root,
        session=session,
        complete=False,
        failure_classification=failure.classification,
        recorded_at=TrustedClock.production().now(),
    )
    session_event = _session_ledger_append(
        root=root,
        policy=policy,
        payload={
            "schema_version": 1,
            "task_name": TASK_NAME,
            "session": session.isoformat(),
            "previous_session": context["previous_session"],
            "started_at": iso_z(started),
            "completed_at": iso_z(TrustedClock.production().now()),
            "final_status": "PARTIAL_FAIL_CLOSED",
            "failure_classification": failure.classification,
            "retry_count": 0,
            "missing_symbol_count": 0,
            "pagination_failure_count": 0,
            "acceptance_run_id": acceptance_result["acceptance_run_id"],
            "acceptance_state": acceptance_result["state"],
            "acceptance_credit": acceptance_result["completed_consecutive_sessions"],
            "prospective_research_ready": False,
            "training_authorized": False,
            "evaluation_authorized": False,
            "orders": 0,
            "predictions": 0,
        },
    )
    status = {
        "task_name": TASK_NAME,
        "state": acceptance_result["state"],
        "session": session.isoformat(),
        "failure_classification": failure.classification,
        "acceptance_run_id": acceptance_result["acceptance_run_id"],
        "acceptance_credit": acceptance_result["completed_consecutive_sessions"],
        "session_event_id": session_event["session_event_id"],
        "structural_pause": failure.structural,
        "orders": 0,
        "predictions": 0,
        "training": False,
        "evaluation": False,
    }
    _write_status(root, policy, status, secrets)
    _log(
        root,
        policy,
        f"FAIL session={session.isoformat()} class={failure.classification}",
        secrets=secrets,
    )
    return status


def run_daily_capture(
    *, repository_root: Path, execute_network: bool, disable_wait: bool = False,
) -> dict[str, object]:
    root = Path(repository_root).resolve(strict=True)
    policy = load_automation_policy(root)
    paths = _paths(root, policy)
    if not execute_network:
        return dry_run_daily_capture(repository_root=root)
    started = TrustedClock.production().now()
    local_date = started.astimezone(ZoneInfo("America/Los_Angeles")).date()
    context = session_context(repository_root=root, local_date=local_date)
    if context["state"] == "SKIP_NON_XNYS_SESSION":
        status = {"task_name": TASK_NAME, "state": "SKIP_NON_XNYS_SESSION", "started_at": iso_z(started), **context}
        _write_status(root, policy, status)
        return status
    session = date.fromisoformat(str(context["session"]))
    lock = paths["automation_root"] / "daily_capture.lock"
    with ExclusiveFileLock(lock, allowed_root=root / "data"):
        secrets: tuple[str, ...] = ()
        try:
            credentials_result = load_local_api_env(root)
            if not all(credentials_result["presence"].values()):
                raise AutomationFailure(
                    "CREDENTIAL_UNAVAILABLE",
                    "one or more canonical credentials are unavailable",
                    structural=False,
                )
            secrets = tuple(
                os.environ.get(name, "")
                for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPHA_VANTAGE_API_KEY")
            )
            credentials = {
                name: os.environ.get(name)
                for name in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "ALPHA_VANTAGE_API_KEY")
            }
            acceptance = acceptance_status(repository_root=root)
            if not acceptance.get("initialized"):
                raise AutomationFailure(
                    "CODE_COMMIT_MISMATCH",
                    "automation acceptance is not initialized",
                    structural=True,
                )
            if acceptance["state"] == "AUTOMATION_PAUSED_STRUCTURAL_FAILURE":
                status = {
                    "task_name": TASK_NAME,
                    "state": "AUTOMATION_PAUSED_STRUCTURAL_FAILURE",
                    "network_requests": 0,
                    "acceptance_run_id": acceptance["acceptance_run_id"],
                }
                _write_status(root, policy, status, secrets)
                return status
            runtime = _safe_runtime_validation(root, policy, require_clean=True)
            missed_session_events = reconcile_missed_sessions(
                repository_root=root,
                current_session=session,
                recorded_at=TrustedClock.production().now(),
            )
        except AutomationFailure as exc:
            if acceptance_status(repository_root=root).get("initialized"):
                _record_failed_session(
                    root=root, policy=policy, session=session, context=context,
                    started=started, failure=exc, secrets=secrets,
                )
            raise
        except (ContractError, IntegrityError, UnicodeError) as exc:
            structural = AutomationFailure(
                "CREDENTIAL_LEAKAGE_RISK",
                "capture preflight validation failed",
                structural=True,
            )
            if acceptance_status(repository_root=root).get("initialized"):
                _record_failed_session(
                    root=root, policy=policy, session=session, context=context,
                    started=started, failure=structural, secrets=secrets,
                )
            raise structural from exc
        os.environ["FREE_SOURCE_QUALIFICATION_APPROVED"] = "YES"
        store = RawEvidenceStore((root / str(policy["paths"]["evidence_root"])).resolve(), allowed_root=(root / "data").resolve())
        registry = NetworkAcquisitionRegistry.load(root / "config/alpaca_free_bounded_network_registry.json", allowed_root=root)
        phase_b_target = datetime.fromisoformat(str(context["phase_b_target"]).replace("Z", "+00:00"))
        phase_b_deadline = datetime.fromisoformat(str(context["phase_b_validation_deadline"]).replace("Z", "+00:00"))
        phase_a_target = datetime.fromisoformat(str(context["phase_a_target"]).replace("Z", "+00:00"))
        final_cutoff = datetime.fromisoformat(str(context["final_cutoff"]).replace("Z", "+00:00"))
        try:
            _wait_until(phase_b_target, cutoff=phase_b_deadline, disable_wait=disable_wait)
            prior_snapshot = _latest_json(root / "data/w/alpaca_free_bounded_v1/prospective_universe")
            previous_session = date.fromisoformat(str(context["previous_session"]))
            bar_plans = build_daily_liquidity_plans(repository_root=root, candidate_snapshot_path=prior_snapshot, completed_session=previous_session)
            actions = next(
                plan for plan in prospective_source_plans(repository_root=root, observed_for=previous_session)
                if plan.source == "alpaca_free_bounded_corporate_actions"
            )
            phase_b_plans = (*bar_plans, actions)
            phase_b_receipts = _execute_plans(
                plans=phase_b_plans,
                checkpoint_path=paths["checkpoints"] / f"{session.isoformat()}-phase-b.json",
                store=store, registry=registry, deadline=phase_b_deadline, credentials=credentials,
            )
            _wait_until(phase_a_target, cutoff=final_cutoff, disable_wait=disable_wait)
            pre_plan = build_daily_capture_plan(repository_root=root, session=session, phase="PRE_DECISION")
            phase_a_receipts = _execute_plans(
                plans=pre_plan.source_plans,
                checkpoint_path=paths["checkpoints"] / f"{session.isoformat()}-phase-a.json",
                store=store, registry=registry, deadline=final_cutoff, credentials=credentials,
            )
            candidate_payload = build_prospective_universe_snapshot(
                plan=pre_plan,
                evidence_store=store,
                receipt_ids=phase_a_receipts,
                output_root=root / "data/w/alpaca_free_bounded_v1/prospective_universe",
            )
            _, calendar_rows = _calendar_sessions(root)
            position = {row["session"]: index for index, row in enumerate(calendar_rows)}[previous_session]
            sessions = tuple(row["session"] for row in calendar_rows[position - 89 : position + 1])
            seed_receipts = _rolling_receipt_seed(root)
            phase_b_bar_receipts = tuple(
                receipt_id
                for receipt_id in phase_b_receipts
                if store.receipt(receipt_id).source == "alpaca_free_bounded_bars"
            )
            rolling_receipts = tuple(dict.fromkeys((*seed_receipts, *phase_b_bar_receipts)))
            observations, _ = _collect_bars(store, rolling_receipts, set(sessions))
            queue = _onboarding_queue(
                root=root, policy=policy, candidate_snapshot=candidate_payload,
                covered_symbols=set(observations), recorded_at=TrustedClock.production().now(),
            )
            onboarding_symbols = tuple(queue["selected_for_bounded_warmup"])
            onboarding_receipts: tuple[str, ...] = ()
            if onboarding_symbols:
                onboarding_plan = alpaca_bars_plan(
                    repository_root=root,
                    symbols=onboarding_symbols,
                    start=sessions[0],
                    end_exclusive=sessions[-1] + timedelta(days=1),
                    evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
                )
                try:
                    onboarding_receipts = _execute_plans(
                        plans=(onboarding_plan,),
                        checkpoint_path=paths["checkpoints"] / f"{session.isoformat()}-onboarding.json",
                        store=store, registry=registry, deadline=final_cutoff, credentials=credentials,
                    )
                except AutomationFailure:
                    onboarding_receipts = ()
            rolling_receipts = tuple(dict.fromkeys((*rolling_receipts, *onboarding_receipts)))
            universe = _build_rolling_universe(
                root=root,
                candidate_snapshot_path=(root / "data/w/alpaca_free_bounded_v1/prospective_universe" / f"{candidate_payload['universe_snapshot_id']}.json"),
                signal_session=session, sessions=sessions, receipt_ids=rolling_receipts,
                store=store, output_root=paths["universe_snapshots"],
            )
            repeat = _build_rolling_universe(
                root=root,
                candidate_snapshot_path=(root / "data/w/alpaca_free_bounded_v1/prospective_universe" / f"{candidate_payload['universe_snapshot_id']}.json"),
                signal_session=session, sessions=sessions, receipt_ids=rolling_receipts,
                store=store, output_root=paths["universe_snapshots"],
            )
            if repeat["universe_snapshot_id"] != universe["universe_snapshot_id"]:
                raise AutomationFailure("INCONSISTENT_UNIVERSE_RECONSTRUCTION", "universe rerun differs", structural=True)
            if TrustedClock.production().now() >= final_cutoff:
                raise AutomationFailure("LATE_CAPTURE", "finalization missed the pre-decision cutoff", structural=False)
            acceptance_result = record_acceptance_result(
                repository_root=root, session=session, complete=True,
                failure_classification=None, recorded_at=TrustedClock.production().now(),
            )
            session_event = _session_ledger_append(
                root=root, policy=policy,
                payload={
                    "schema_version": 1,
                    "task_name": TASK_NAME,
                    "session": session.isoformat(),
                    "previous_session": previous_session.isoformat(),
                    "started_at": iso_z(started),
                    "completed_at": iso_z(TrustedClock.production().now()),
                    "final_status": "COMPLETE",
                    "failure_classification": None,
                    "phase_b_receipts": _receipt_summary(store, phase_b_receipts),
                    "phase_a_receipts": _receipt_summary(store, phase_a_receipts),
                    "onboarding_receipts": _receipt_summary(store, onboarding_receipts),
                    "rolling_manifest_receipt_ids": list(rolling_receipts),
                    "candidate_snapshot_id": candidate_payload["universe_snapshot_id"],
                    "universe_snapshot_id": universe["universe_snapshot_id"],
                    "candidate_count": universe["candidate_count"],
                    "selected_count": universe["selected_count"],
                    "missing_symbol_count": sum(row["missing_expected_session_count"] > 0 for row in universe["rows"]),
                    "pagination_failure_count": 0,
                    "retry_count": 0,
                    "acceptance_run_id": acceptance_result["acceptance_run_id"],
                    "acceptance_state": acceptance_result["state"],
                    "acceptance_credit": acceptance_result["completed_consecutive_sessions"],
                    "prospective_research_ready": False,
                    "training_authorized": False,
                    "evaluation_authorized": False,
                    "orders": 0,
                    "predictions": 0,
                },
            )
            status = {
                "task_name": TASK_NAME,
                "state": "COMPLETE",
                "session": session.isoformat(),
                "started_at": iso_z(started),
                "completed_at": iso_z(TrustedClock.production().now()),
                "acceptance_run_id": acceptance_result["acceptance_run_id"],
                "acceptance_state": acceptance_result["state"],
                "acceptance_credit": acceptance_result["completed_consecutive_sessions"],
                "universe_snapshot_id": universe["universe_snapshot_id"],
                "selected_count": universe["selected_count"],
                "session_event_id": session_event["session_event_id"],
                "background_monitor": background_monitor_status(repository_root=root),
                "reconciled_missed_session_event_ids": [
                    row["session_event_id"] for row in missed_session_events
                ],
                "structural_pause": False,
                "network_requests": len(phase_b_plans) + len(pre_plan.source_plans) + (1 if onboarding_receipts else 0),
                "orders": 0,
                "predictions": 0,
                "training": False,
                "evaluation": False,
            }
            _write_status(root, policy, status, secrets)
            _log(root, policy, f"COMPLETE session={session.isoformat()} snapshot={universe['universe_snapshot_id']}", secrets=secrets)
            return status
        except AutomationFailure as exc:
            _record_failed_session(
                root=root, policy=policy, session=session, context=context,
                started=started, failure=exc, secrets=secrets,
            )
            raise
        except (ContractError, IntegrityError, json.JSONDecodeError, UnicodeError) as exc:
            structural = AutomationFailure(
                "REPEATED_DETERMINISTIC_PARSER_FAILURE",
                "deterministic capture validation failed",
                structural=True,
            )
            _record_failed_session(
                root=root, policy=policy, session=session, context=context,
                started=started, failure=structural, secrets=secrets,
            )
            raise structural from exc
