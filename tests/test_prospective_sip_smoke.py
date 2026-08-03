from __future__ import annotations

from datetime import datetime, timezone
import json
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
    monkeypatch.setattr(smoke, "_clean_repository", lambda root: {"commit": "a" * 40, "tree": "b" * 40})
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


def test_smoke_candidate_requires_exact_two_bar_non_paginated_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_valid_inputs(monkeypatch)
    plan = smoke.build_prospective_sip_smoke_plan(
        identity_release_directory=REPO, calendar_release_directory=REPO,
        repository_root=REPO, clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
        allow_synthetic=True,
    )
    raw = json.dumps({"bars": {
        "AAPL": [{"t": "2026-08-03T04:00:00Z", "o": 1.0, "h": 1.1, "l": 0.9, "c": 1.0, "v": 1}],
        "SPY": [{"t": "2026-08-03T04:00:00Z", "o": 2.0, "h": 2.1, "l": 1.9, "c": 2.0, "v": 1}],
    }, "next_page_token": None}).encode()
    snapshot = SimpleNamespace(source=smoke.SMOKE_SOURCE, request_plan_id=plan["network_request_plan"]["plan_id"], url=plan["request"]["url"], http_status=200, trust_eligible=True, snapshot_id="a" * 64, raw_sha256="b" * 64, retrieved_at=datetime(2026, 8, 3, 20, 21, tzinfo=timezone.utc), read_verified_bytes=lambda: raw)
    candidate = smoke.build_prospective_sip_smoke_candidate(snapshot, plan=plan)
    assert len(candidate.bars) == 2
    assert candidate.bars[0]["session"] == "2026-08-03"
    assert smoke.build_prospective_sip_smoke_publication_plan(candidate, plan=plan, accepted_root=REPO / "data/vault/accepted", work_root=REPO / "data/w/smoke")["publication_authorized"] is False
    bad = SimpleNamespace(**{**snapshot.__dict__, "read_verified_bytes": lambda: raw.replace(b'"next_page_token": null', b'"next_page_token": "next"')})
    with pytest.raises(ContractError, match="response shape or pagination"):
        smoke.build_prospective_sip_smoke_candidate(bad, plan=plan)
    monkeypatch.setattr(smoke, "verify_accepted_release", lambda *args, **kwargs: SimpleNamespace(release_id="0" * 64, dataset="identity", role="prospective_as_received", quality_state="PASS", source_epoch="nasdaq_alpaca_active_us_equity_v1", row_count=2))
    with pytest.raises(IntegrityError, match="identity release differs"):
        smoke.build_prospective_sip_smoke_plan(
            identity_release_directory=REPO, calendar_release_directory=REPO,
            repository_root=REPO, clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
            allow_synthetic=True,
        )


def test_smoke_plan_package_reloads_exact_plan_and_rejects_altered_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_valid_inputs(monkeypatch)
    plan = smoke.build_prospective_sip_smoke_plan(
        identity_release_directory=REPO, calendar_release_directory=REPO,
        repository_root=REPO, clock=_clock(datetime(2026, 8, 3, 20, 20, tzinfo=timezone.utc)),
        allow_synthetic=True,
    )
    actual_contained = smoke.require_contained_path
    package_root = tmp_path / "packages"

    def contained(path: Path, allowed_root: Path, *, must_exist: bool = True) -> Path:
        candidate = Path(path)
        if candidate == REPO / smoke.PLAN_WORK_ROOT:
            return package_root
        if candidate.parent == package_root:
            return candidate
        return actual_contained(candidate, allowed_root, must_exist=must_exist)

    monkeypatch.setattr(smoke, "require_contained_path", contained)
    written = smoke.write_prospective_sip_smoke_plan_package(plan=plan, repository_root=REPO)
    package = Path(written["directory"])
    assert smoke.load_prospective_sip_smoke_plan_package(plan_package=package, repository_root=REPO) == plan
    (package / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="receipt differs"):
        smoke.load_prospective_sip_smoke_plan_package(plan_package=package, repository_root=REPO)


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
