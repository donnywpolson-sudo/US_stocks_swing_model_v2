from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2 import prospective_corporate_action_raw_capture as raw
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.network_execution import NetworkRequestPlan


REPO = Path(__file__).parents[1]


def _inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(raw, "_clean_repository", lambda root: {"commit": "a" * 40, "tree": "b" * 40})
    manifests = iter((
        SimpleNamespace(release_id="1" * 64, project="US_stocks_swing_model_v2", dataset="identity", role="prospective_as_received", quality_state="PASS", upstream_release_ids=()),
        SimpleNamespace(release_id="2" * 64, project="US_stocks_swing_model_v2", dataset="alpaca_daily_bars", role="prospective_as_received", quality_state="PASS", upstream_release_ids=("1" * 64, "3" * 64)),
        SimpleNamespace(release_id="3" * 64, project="US_stocks_swing_model_v2", dataset="xnys_sessions", role="derived_causal", quality_state="PASS", upstream_release_ids=()),
    ))
    monkeypatch.setattr(raw, "verify_accepted_release", lambda *args, **kwargs: next(manifests))


def _plan(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    _inputs(monkeypatch)
    return raw.build_prospective_corporate_action_raw_capture_plan(repository_root=REPO, identity_release_directory=REPO, bars_release_directory=REPO, calendar_release_directory=REPO, symbols=("AAPL", "SPY"), process_date_start=date(2026, 7, 27), process_date_end=date(2026, 8, 10))


def test_raw_capture_plan_is_one_page_and_explicitly_blocks_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(monkeypatch)
    assert plan["replaces_capture_plan_id"] == "798e1777dfdc8caee5e62aca2285e1c35f9357b69f8b7a42b7e981ef5932a449"
    assert [item["path"] for item in plan["code_closure"]["files"]] == [
        "src/us_stocks_swing_model_v2/prospective_corporate_action_raw_capture.py",
        "src/us_stocks_swing_model_v2/cli/prospective_corporate_action_raw_capture.py",
        "src/us_stocks_swing_model_v2/providers/corporate_actions.py",
    ]
    assert plan["request"]["max_pages"] == 1
    assert plan["coverage"]["effective_event_completeness"] is False
    assert plan["coverage"]["delisting_evidence_available"] is False
    assert plan["coverage"]["outcomes_may_compute"] is False
    assert plan["authorities"]["network_calls"] == 0


def test_raw_capture_rejects_orphan_bars_and_tampered_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs(monkeypatch)
    manifests = iter((
        SimpleNamespace(release_id="1" * 64, project="US_stocks_swing_model_v2", dataset="identity", role="prospective_as_received", quality_state="PASS", upstream_release_ids=()),
        SimpleNamespace(release_id="2" * 64, project="US_stocks_swing_model_v2", dataset="alpaca_daily_bars", role="prospective_as_received", quality_state="PASS", upstream_release_ids=()),
        SimpleNamespace(release_id="3" * 64, project="US_stocks_swing_model_v2", dataset="xnys_sessions", role="derived_causal", quality_state="PASS", upstream_release_ids=()),
    ))
    monkeypatch.setattr(raw, "verify_accepted_release", lambda *args, **kwargs: next(manifests))
    with pytest.raises(ContractError, match="identity/calendar lineage"):
        raw.build_prospective_corporate_action_raw_capture_plan(repository_root=REPO, identity_release_directory=REPO, bars_release_directory=REPO, calendar_release_directory=REPO, symbols=("AAPL", "SPY"), process_date_start=date(2026, 7, 27), process_date_end=date(2026, 8, 10))
    plan = _plan(monkeypatch)
    plan["coverage"]["outcomes_may_compute"] = True
    with pytest.raises(IntegrityError, match="plan identity differs"):
        raw._validate_execution_plan(plan, repository_root=REPO)


def test_execution_stops_before_future_interval_or_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(monkeypatch)
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    monkeypatch.setattr(raw, "require_trusted_clock", lambda clock: SimpleNamespace(trust_eligible=True, now=lambda: datetime(2026, 8, 9, tzinfo=timezone.utc)))
    monkeypatch.setattr(raw, "_validate_execution_plan", lambda *args, **kwargs: (plan, raw.CorporateActionsRequest(start=date(2026, 7, 27), end=date(2026, 8, 10), symbols=("AAPL", "SPY"), requested_at=datetime(2026, 8, 9, tzinfo=timezone.utc)), NetworkRequestPlan(**plan["request"]["network_request_plan"])))
    with pytest.raises(ContractError, match="after its process-date end"):
        raw.execute_prospective_corporate_action_raw_capture(plan=plan, approved_plan_id=plan["capture_plan_id"], api_key_id="key", api_secret_key="secret", clock=object(), repository_root=REPO)


def test_execution_passes_the_one_mib_cap_to_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _plan(monkeypatch)
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    trusted = SimpleNamespace(trust_eligible=True, now=lambda: datetime(2026, 8, 11, tzinfo=timezone.utc))
    request = raw.CorporateActionsRequest(start=date(2026, 7, 27), end=date(2026, 8, 10), symbols=("AAPL", "SPY"), requested_at=datetime(2026, 8, 11, tzinfo=timezone.utc))
    network = NetworkRequestPlan(**plan["request"]["network_request_plan"])
    monkeypatch.setattr(raw, "require_trusted_clock", lambda clock: trusted)
    monkeypatch.setattr(raw, "_validate_execution_plan", lambda *args, **kwargs: (plan, request, network))
    monkeypatch.setattr(raw, "start_local_network_execution", lambda *args, **kwargs: object())
    observed: dict[str, object] = {}
    page = SimpleNamespace(snapshot_id="a" * 64, root=REPO, raw_sha256="b" * 64)
    def fetch(*args, **kwargs):
        observed.update(kwargs)
        return (page,)
    monkeypatch.setattr(raw, "guarded_fetch_corporate_action_pages", fetch)
    monkeypatch.setattr(raw, "parse_landed_corporate_actions", lambda *args: SimpleNamespace(actions=(), coverage=SimpleNamespace(coverage_id="c" * 64)))
    result = raw.execute_prospective_corporate_action_raw_capture(plan=plan, approved_plan_id=plan["capture_plan_id"], api_key_id="key", api_secret_key="secret", clock=object(), repository_root=REPO)
    assert observed["max_pages"] == 1
    assert observed["max_response_bytes"] == 1048576
    assert result["outcomes_may_compute"] is False
