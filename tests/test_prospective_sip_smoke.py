from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.calendar_successor import (
    CONFIRMATION_VALUE,
    build_calendar_successor_plan,
    publish_calendar_successor,
)
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2 import prospective_sip_smoke as smoke


REPO = Path(__file__).parents[1]


def _clock(value: datetime) -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        value,
        permit=SyntheticOnlyPermit.create(
            fixture_id="prospective-sip-smoke",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _patch_valid_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = SimpleNamespace(
        release_id="2c2898a6748dcd5b4d9f7875cd1549e050902c2f491005ed530a5899c685e115",
        dataset="identity", role="prospective_as_received", quality_state="PASS",
        source_epoch="nasdaq_alpaca_active_us_equity_v1", row_count=2,
    )
    rows = (
        SimpleNamespace(symbol="AAPL", asset_id="asset-aapl", active=True, eligible=True, membership_present=True, abstention_reason=None),
        SimpleNamespace(symbol="SPY", asset_id="asset-spy", active=True, eligible=True, membership_present=True, abstention_reason=None),
    )
    monkeypatch.setattr(smoke, "verify_accepted_release", lambda *args, **kwargs: manifest)
    monkeypatch.setattr(smoke, "_load_identity_release_payload", lambda *args, **kwargs: (SimpleNamespace(snapshot_id="identity-snapshot", rows=rows),))
    monkeypatch.setattr(
        smoke,
        "load_xnys_calendar_release",
        lambda *args, **kwargs: SimpleNamespace(
            calendar=SimpleNamespace(release_id="calendar-release"),
            schedule=SimpleNamespace(to_pylist=lambda: [{"session": datetime(2026, 8, 3, tzinfo=timezone.utc).date(), "close_at": datetime(2026, 8, 3, 20, tzinfo=timezone.utc)}]),
        ),
    )


def test_prospective_smoke_plan_binds_fresh_identity_calendar_and_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_valid_inputs(monkeypatch)
    plan = smoke.build_prospective_sip_smoke_plan(
        identity_release_directory=REPO / "data/vault/accepted/identity/ignored",
        calendar_release_directory=REPO / "data/vault/accepted/xnys_sessions/ignored",
        repository_root=REPO,
        clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
        allow_synthetic=True,
    )
    assert plan["identity"]["asset_ids"] == {"AAPL": "asset-aapl", "SPY": "asset-spy"}
    assert plan["calendar"]["session"] == "2026-08-03"
    assert plan["request"]["feed"] == "sip"
    assert plan["request"]["asof"] is None
    assert plan["network_request_plan"]["max_pages"] == 1


def test_prospective_smoke_plan_rejects_early_time_and_legacy_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_valid_inputs(monkeypatch)
    with pytest.raises(ContractError, match="not yet available"):
        smoke.build_prospective_sip_smoke_plan(
            identity_release_directory=REPO, calendar_release_directory=REPO,
            repository_root=REPO, clock=_clock(datetime(2026, 8, 3, 20, 19, tzinfo=timezone.utc)),
            allow_synthetic=True,
        )
    monkeypatch.setattr(smoke, "verify_accepted_release", lambda *args, **kwargs: SimpleNamespace(release_id="0" * 64, dataset="identity", role="prospective_as_received", quality_state="PASS", source_epoch="nasdaq_alpaca_active_us_equity_v1", row_count=2))
    with pytest.raises(IntegrityError, match="identity release differs"):
        smoke.build_prospective_sip_smoke_plan(
            identity_release_directory=REPO, calendar_release_directory=REPO,
            repository_root=REPO, clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
            allow_synthetic=True,
        )


def test_calendar_successor_plan_requires_clean_closure_and_production_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.calendar_successor._clean_repository",
        lambda root: {"commit": "a" * 40, "tree": "b" * 40},
    )
    plan = build_calendar_successor_plan(repository_root=REPO)
    assert plan["calendar"]["start"] == "2000-01-01"
    assert plan["calendar"]["end"] == "2035-12-31"
    with pytest.raises(ContractError, match="production system UTC"):
        publish_calendar_successor(
            approved_plan_id=plan["calendar_successor_plan_id"],
            owner_confirmation=CONFIRMATION_VALUE,
            clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
            repository_root=REPO,
        )
