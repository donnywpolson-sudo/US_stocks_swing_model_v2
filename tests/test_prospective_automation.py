from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from us_stocks_swing_model_v2 import prospective_automation as automation
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import LockHeldError
from us_stocks_swing_model_v2.locking import ExclusiveFileLock


ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def _policy() -> dict[str, object]:
    return json.loads((ROOT / automation.POLICY_PATH).read_text(encoding="utf-8"))


def _calendar() -> SimpleNamespace:
    sessions = [
        (date(2026, 3, 5), datetime(2026, 3, 5, 14, 30, tzinfo=UTC)),
        (date(2026, 3, 6), datetime(2026, 3, 6, 14, 30, tzinfo=UTC)),
        (date(2026, 3, 9), datetime(2026, 3, 9, 13, 30, tzinfo=UTC)),
        (date(2026, 3, 10), datetime(2026, 3, 10, 13, 30, tzinfo=UTC)),
        (date(2026, 3, 11), datetime(2026, 3, 11, 13, 30, tzinfo=UTC)),
    ]
    table = pa.Table.from_pylist([
        {"session": session, "open_at": open_at, "close_at": open_at + timedelta(hours=6, minutes=30)}
        for session, open_at in sessions
    ])
    return SimpleNamespace(
        calendar=SimpleNamespace(release_id=automation.QUALIFIED_CALENDAR),
        schedule=table,
    )


def _patch_acceptance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    policy = _policy()
    monkeypatch.setattr(automation, "load_automation_policy", lambda _root: policy)
    monkeypatch.setattr(automation, "load_qualified_profile_calendar", lambda **_kwargs: _calendar())
    monkeypatch.setattr(automation, "_git_commit_exists", lambda *_args: True)
    monkeypatch.setattr(automation, "_calendar_sessions", lambda _root: (_calendar(), _calendar().schedule.to_pylist()))
    (tmp_path / "data").mkdir()


def _initialize(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    _patch_acceptance(monkeypatch, tmp_path)
    return automation.supersede_legacy_soak_and_initialize_acceptance(
        repository_root=tmp_path,
        remediation_commit="a" * 40,
        initialized_at=datetime(2026, 3, 6, 1, tzinfo=UTC),
    )


def test_policy_replaces_blocking_twenty_with_two_and_nonblocking_monitor() -> None:
    policy = automation.load_automation_policy(ROOT)
    assert policy["policy_id"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_V1"
    assert policy["required_consecutive_sessions"] == 2
    assert policy["inherited_completed_session_credit"] == 0
    assert policy["background_monitor"] == {
        "policy_id": "NONBLOCKING_BACKGROUND_RELIABILITY_MONITOR",
        "rolling_session_window": 20,
        "blocking": False,
        "continues_after_acceptance": True,
    }
    assert all(value is False for value in policy["authorities"].values())


def test_calendar_runtime_gate_skips_holiday_and_derives_dst_safe_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(automation, "load_qualified_profile_calendar", lambda **_kwargs: _calendar())
    friday = automation.session_context(repository_root=ROOT, local_date=date(2026, 3, 6))
    monday = automation.session_context(repository_root=ROOT, local_date=date(2026, 3, 9))
    holiday = automation.session_context(repository_root=ROOT, local_date=date(2026, 3, 8))
    assert friday["open_at"] == "2026-03-06T14:30:00Z"
    assert monday["open_at"] == "2026-03-09T13:30:00Z"
    assert monday["previous_session"] == "2026-03-06"
    assert monday["phase_b_target"] == "2026-03-09T11:30:00Z"
    assert monday["phase_b_validation_deadline"] == "2026-03-09T12:00:00Z"
    assert monday["phase_a_target"] == "2026-03-09T12:30:00Z"
    assert monday["final_cutoff"] == "2026-03-09T13:15:00Z"
    assert holiday == {
        "state": "SKIP_NON_XNYS_SESSION",
        "local_date": "2026-03-08",
        "calendar_release_id": automation.QUALIFIED_CALENDAR,
        "provider_requests": 0,
        "acceptance_credit_change": 0,
    }


def test_one_session_is_in_progress_and_two_consecutive_sessions_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    initialized = _initialize(monkeypatch, tmp_path)
    assert initialized["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED"
    assert initialized["completed_consecutive_sessions"] == 0
    assert initialized["legacy_predecessor_soak_run_id"] == automation.LEGACY_VALID_SOAK_RUN
    assert initialized["supersession"]["new_state"] == "SUPERSEDED_BY_OWNER_ACCEPTANCE_POLICY_CHANGE"
    assert initialized["supersession"]["legacy_completed_session_credit"] == 0
    first = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    assert first["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_IN_PROGRESS"
    assert first["completed_consecutive_sessions"] == 1
    assert first["prospective_capture_automation_accepted"] is False
    second = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 10), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 10, 13, tzinfo=UTC),
    )
    assert second["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE"
    assert second["completed_consecutive_sessions"] == 2
    assert second["prospective_capture_automation_accepted"] is True
    assert second["prospective_capture_operational"] is True
    assert second["background_reliability_monitoring_active"] is True
    assert second["next_phase_historical_exploratory_development_eligible"] is True
    assert second["prospective_research_ready"] is False
    assert second["training_authorized"] is False
    assert second["evaluation_authorized"] is False


def test_duplicate_execution_cannot_award_duplicate_credit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    first = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    duplicate = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 14, tzinfo=UTC),
    )
    assert duplicate["event_id"] == first["event_id"]
    assert duplicate["completed_consecutive_sessions"] == 1


def test_transient_failure_preserves_failed_credit_and_starts_zero_credit_successor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    first = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    failed_result = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 10), complete=False,
        failure_classification="PROVIDER_UNAVAILABLE",
        recorded_at=datetime(2026, 3, 10, 13, tzinfo=UTC),
    )
    resumed = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 11), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 11, 13, tzinfo=UTC),
    )
    rows = automation._canonical_ledger(tmp_path / _policy()["paths"]["acceptance_ledger"], id_field="event_id")
    failed = rows[-3]
    successor = rows[-2]
    assert failed["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED"
    assert failed_result["event_id"] == failed["event_id"]
    assert failed["completed_consecutive_sessions"] == 1
    assert successor["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_NOT_STARTED"
    assert successor["completed_consecutive_sessions"] == 0
    assert successor["inherited_completed_session_credit"] == 0
    assert successor["predecessor_acceptance_run_id"] == first["acceptance_run_id"]
    assert resumed["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_IN_PROGRESS"
    assert resumed["completed_consecutive_sessions"] == 1


def test_transient_successor_cannot_claim_credit_from_failed_session_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    failed = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=False,
        failure_classification="TEMPORARY_NETWORK_LOSS",
        recorded_at=datetime(2026, 3, 9, 12, tzinfo=UTC),
    )
    same_session_restart = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    assert same_session_restart["event_id"] == failed["event_id"]
    assert same_session_restart["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED"
    assert same_session_restart["completed_consecutive_sessions"] == 0


def test_missed_xnys_session_is_materialized_and_starts_fail_closed_successor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    automation._session_ledger_append(
        root=tmp_path, policy=_policy(), payload={
            "schema_version": 1, "session": "2026-03-09", "final_status": "COMPLETE",
            "failure_classification": None, "retry_count": 0,
            "missing_symbol_count": 0, "pagination_failure_count": 0,
        },
    )
    reconciled = automation.reconcile_missed_sessions(
        repository_root=tmp_path,
        current_session=date(2026, 3, 11),
        recorded_at=datetime(2026, 3, 11, 11, tzinfo=UTC),
    )
    assert len(reconciled) == 1
    assert reconciled[0]["session"] == "2026-03-10"
    assert reconciled[0]["final_status"] == "PARTIAL_FAIL_CLOSED"
    assert reconciled[0]["failure_classification"] == "MACHINE_WAKE_DELAY"
    failed = automation.acceptance_status(repository_root=tmp_path)
    assert failed["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_FAILED"
    assert failed["completed_consecutive_sessions"] == 1


def test_structural_failure_pauses_without_successor_or_network_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    paused = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 9), complete=False,
        failure_classification="SCHEMA_DRIFT",
        recorded_at=datetime(2026, 3, 9, 13, tzinfo=UTC),
    )
    repeated = automation.record_acceptance_result(
        repository_root=tmp_path, session=date(2026, 3, 10), complete=True,
        failure_classification=None, recorded_at=datetime(2026, 3, 10, 13, tzinfo=UTC),
    )
    assert paused["state"] == "AUTOMATION_PAUSED_STRUCTURAL_FAILURE"
    assert repeated["event_id"] == paused["event_id"]
    assert paused["prospective_research_ready"] is False


def test_background_monitor_is_telemetry_and_does_not_revoke_completed_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _initialize(monkeypatch, tmp_path)
    for session in (date(2026, 3, 9), date(2026, 3, 10)):
        automation.record_acceptance_result(
            repository_root=tmp_path, session=session, complete=True,
            failure_classification=None, recorded_at=datetime.combine(session, datetime.min.time(), UTC),
        )
    automation._session_ledger_append(
        root=tmp_path, policy=_policy(), payload={
            "schema_version": 1, "session": "2026-03-11", "final_status": "PARTIAL_FAIL_CLOSED",
            "failure_classification": "PROVIDER_UNAVAILABLE", "retry_count": 1,
            "missing_symbol_count": 3, "pagination_failure_count": 0,
        },
    )
    monitor = automation.background_monitor_status(repository_root=tmp_path)
    accepted = automation.acceptance_status(repository_root=tmp_path)
    assert monitor["blocking"] is False
    assert monitor["sessions_partial"] == 1
    assert monitor["provider_failures"] == 1
    assert accepted["state"] == "TWO_SESSION_AUTOMATION_ACCEPTANCE_COMPLETE"


def test_daily_liquidity_plans_cover_full_candidate_pool_and_pin_sip_raw_daily(tmp_path: Path) -> None:
    candidates = [
        {"stable_asset_id": f"{index:064x}", "symbol": f"S{index:03d}", "candidate_eligible": True}
        for index in range(205)
    ] + [{"stable_asset_id": "f" * 64, "symbol": "OTCX", "candidate_eligible": False}]
    unsigned = {
        "schema_version": 1,
        "evidence_class": "PROSPECTIVE_AS_OBSERVED",
        "candidates": candidates,
    }
    snapshot = {**unsigned, "universe_snapshot_id": sha256_bytes(canonical_json_bytes(unsigned))}
    path = tmp_path / "candidates.json"
    path.write_bytes(canonical_json_bytes(snapshot))
    plans = automation.build_daily_liquidity_plans(
        repository_root=ROOT, candidate_snapshot_path=path, completed_session=date(2026, 8, 10),
    )
    symbols = [symbol for plan in plans for symbol in dict(plan.canonical_query)["symbols"].split(",")]
    assert len(plans) == 3
    assert len(symbols) == 205
    assert "OTCX" not in symbols
    for plan in plans:
        query = dict(plan.canonical_query)
        assert query["feed"] == "sip"
        assert query["timeframe"] == "1Day"
        assert query["adjustment"] == "raw"
        assert query["start"].endswith("Z") and query["end"].endswith("Z")
        assert "iex" not in plan.transport_url().lower()


def test_status_and_logs_redact_secret_values(tmp_path: Path) -> None:
    policy = _policy()
    (tmp_path / "data").mkdir()
    secret = "do-not-serialize-this-secret"
    automation._write_status(tmp_path, policy, {"message": secret}, (secret,))
    automation._log(tmp_path, policy, f"failure {secret}", secrets=(secret,))
    status = (tmp_path / policy["paths"]["latest_status"]).read_text(encoding="utf-8")
    logs = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / policy["paths"]["logs"]).glob("*.log"))
    assert secret not in status
    assert secret not in logs
    assert "REDACTED" in status and "REDACTED" in logs


def test_checkpoint_resume_is_idempotent_and_tampering_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "data" / "checkpoint.json"
    completed = [{"plan_index": 0, "plan_id": "a" * 64, "receipt_ids": ["b" * 64]}]
    written = automation._write_checkpoint(path, ["a" * 64, "c" * 64], completed)
    resumed = automation._checkpoint(path, ["a" * 64, "c" * 64])
    assert resumed == written
    resumed["completed"][0]["receipt_ids"] = ["d" * 64]
    path.write_bytes(canonical_json_bytes(resumed))
    with pytest.raises(automation.AutomationFailure, match="checkpoint differs"):
        automation._checkpoint(path, ["a" * 64, "c" * 64])


def test_late_phase_fails_before_network() -> None:
    now = datetime.now(UTC)
    with pytest.raises(automation.AutomationFailure, match="cutoff") as caught:
        automation._wait_until(now - timedelta(minutes=2), cutoff=now - timedelta(minutes=1), disable_wait=True)
    assert caught.value.classification == "LATE_CAPTURE"
    assert caught.value.structural is False


def test_exclusive_automation_lock_prevents_overlap(tmp_path: Path) -> None:
    data = (tmp_path / "data").resolve()
    lock_path = data / "automation" / "daily_capture.lock"
    with ExclusiveFileLock(lock_path, allowed_root=data):
        with pytest.raises(LockHeldError):
            ExclusiveFileLock(lock_path, allowed_root=data).acquire()


def test_new_candidate_queue_is_bounded_and_historically_reconstructed(tmp_path: Path) -> None:
    policy = _policy()
    (tmp_path / "data").mkdir()
    candidates = [
        {"stable_asset_id": f"{index:064x}", "symbol": f"NEW{index:02d}", "candidate_eligible": True}
        for index in range(25)
    ]
    queue = automation._onboarding_queue(
        root=tmp_path,
        policy=policy,
        candidate_snapshot={"candidates": candidates},
        covered_symbols={"NEW00"},
        recorded_at=datetime(2026, 3, 9, 12, tzinfo=UTC),
    )
    assert queue["evidence_class"] == "HISTORICAL_RECONSTRUCTED"
    assert queue["queue_limit"] == 20
    assert queue["queued_count"] == 24
    assert len(queue["selected_for_bounded_warmup"]) == 20
    assert len(queue["remaining_visible"]) == 4
    assert "NEW00" not in queue["selected_for_bounded_warmup"]


def test_live_entrypoint_skips_non_session_before_credentials_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        automation,
        "session_context",
        lambda **_kwargs: {
            "state": "SKIP_NON_XNYS_SESSION",
            "local_date": "2026-03-08",
            "calendar_release_id": automation.QUALIFIED_CALENDAR,
            "provider_requests": 0,
            "acceptance_credit_change": 0,
        },
    )
    monkeypatch.setattr(
        automation,
        "load_local_api_env",
        lambda *_args, **_kwargs: pytest.fail("credentials must not load on non-session"),
    )
    monkeypatch.setattr(
        automation,
        "_write_status",
        lambda _root, _policy, payload, *_args: captured.append(dict(payload)),
    )
    result = automation.run_daily_capture(repository_root=ROOT, execute_network=True)
    assert result["state"] == "SKIP_NON_XNYS_SESSION"
    assert result["provider_requests"] == 0
    assert result["acceptance_credit_change"] == 0
    assert captured == [result]
