from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.alpaca import AlpacaBarsPolicy
from us_stocks_swing_model_v2.providers import alpaca_sip_single_feed_qualification as qualification


ROOT = Path(__file__).resolve().parents[1]


def _clean_binding(root: Path) -> dict[str, str]:
    return {
        "root": str(root.resolve()),
        "branch": "main",
        "commit": "a" * 40,
        "tree": "b" * 40,
    }


def _historical_pending_source(
    root: Path,
    _policy: dict[str, object],
) -> dict[str, object]:
    sources = json.loads((root / "config/sources.json").read_text(encoding="utf-8"))
    return {
        "path": "config/sources.json",
        "sha256": "0" * 64,
        "status": "pending_single_sip_requalification",
        "qualified_feed": None,
        "calendar_release_directory": sources["qualification_calendar_release"],
    }


def _deterministic_strict_calendar(
    _root: Path, directory: Path, sessions: list[str]
) -> dict[str, object]:
    """Unit-test the strict plan shape without blessing the host environment."""
    return {
        "release_id": "strict-fixture-calendar-release",
        "directory": str(directory.resolve()),
        "sessions": sessions,
    }


def test_policy_is_exactly_one_sip_request_and_no_iex() -> None:
    policy, _ = qualification.load_policy(ROOT)
    assert policy["symbols"] == ["AAPL", "SPY"]
    assert policy["window"]["sessions"] == [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
    ]
    assert policy["request_contract"] == {
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars",
        "feed": "sip",
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "sort": "asc",
        "limit": 10000,
        "minimum_end_lag_minutes": 20,
        "timeout_seconds": 30,
        "host_timeout_seconds": 120,
        "max_pages": 1,
        "max_response_bytes": 1048576,
    }
    with pytest.raises(ContractError, match="SIP"):
        AlpacaBarsPolicy(feed="iex").validate()


def test_plan_binds_calendar_registry_closure_and_one_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualification, "_repository_binding", _clean_binding)
    monkeypatch.setattr(qualification, "_source_binding", _historical_pending_source)
    monkeypatch.setattr(qualification, "_calendar_binding", _deterministic_strict_calendar)
    plan = qualification.build_qualification_plan(repo_root=ROOT, clock=TrustedClock.production())
    assert plan["network_request_plan"]["source"] == "alpaca_sip_qualification"
    assert plan["network_request_plan"]["max_pages"] == 1
    assert plan["network_request_plan"]["pagination_parameter"] == "none"
    assert plan["request_order"] == 1
    assert plan["calendar"]["sessions"] == [
        "2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31",
    ]
    assert plan["authorities"]["source_activation"] is False
    assert qualification._validate_plan(plan) == plan["qualification_plan_id"]


def test_altered_plan_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualification, "_repository_binding", _clean_binding)
    monkeypatch.setattr(qualification, "_source_binding", _historical_pending_source)
    monkeypatch.setattr(qualification, "_calendar_binding", _deterministic_strict_calendar)
    plan = qualification.build_qualification_plan(repo_root=ROOT, clock=TrustedClock.production())
    altered = json.loads(json.dumps(plan))
    altered["request"]["feed"] = "iex"
    with pytest.raises(IntegrityError, match="plan ID"):
        qualification._validate_plan(altered)


def test_strict_calendar_still_fails_closed_on_stale_environment() -> None:
    sources = json.loads((ROOT / "config/sources.json").read_text(encoding="utf-8"))
    configured_release = Path(sources["qualification_calendar_release"])
    local_release = (
        ROOT / "data/vault/accepted/xnys_sessions" / configured_release.name
    )
    with pytest.raises(IntegrityError, match="manifest closure differs"):
        qualification._calendar_binding(
            ROOT,
            local_release,
            ["2026-07-27", "2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31"],
        )


def test_synthetic_clock_cannot_plan_production_qualification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qualification, "_repository_binding", _clean_binding)
    # A generic non-clock object is also rejected before any plan can be made.
    with pytest.raises(ContractError, match="clock"):
        qualification.build_qualification_plan(repo_root=ROOT, clock=object())  # type: ignore[arg-type]


def test_current_qualified_source_rejects_repeat_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification, "_repository_binding", _clean_binding)
    with pytest.raises(ContractError, match="pending the exact SIP requalification"):
        qualification.build_qualification_plan(
            repo_root=ROOT,
            clock=TrustedClock.production(),
        )
