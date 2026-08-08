from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import math
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.prospective_evidence import build_prospective_epoch_plan, load_prospective_evidence_policy
from us_stocks_swing_model_v2.prospective_price_features import CausalPriceBar, READY_STATUS, UNRESOLVED_STATUS, materialize_price_only_features
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, ReleaseFile, ReleaseManifest


REPO = Path(__file__).parents[1]


def _release(
    tmp_path: Path, accepted: Path, *, dataset: str, role: str, epoch: str,
    upstream: tuple[str, ...] = (), name: str,
) -> Path:
    stage = tmp_path / f"stage-{name}"
    stage.mkdir()
    payload = canonical_json_bytes({"name": name})
    (stage / "payload.json").write_bytes(payload)
    file = ReleaseFile("payload.json", len(payload), sha256_bytes(payload))
    manifest = ReleaseManifest(
        schema_version=1, project="US_stocks_swing_model_v2", dataset=dataset,
        source_epoch=epoch, role=role,
        quality_state="LEGACY_CAVEATED" if role == "legacy_discovery_only" else "PASS",
        created_at="2026-08-01T00:00:00Z", row_count=1,
        event_start="2026-08-01", event_end="2026-08-01",
        upstream_release_ids=tuple(sorted(upstream)), schema_fingerprint="1" * 64,
        code_hash="2" * 64, config_hash="3" * 64, environment_hash="4" * 64,
        files=(file,), release_id="",
    )
    manifest = replace(
        manifest,
        release_id=sha256_bytes(canonical_json_bytes(manifest.unsigned_dict())),
    )
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def _inputs(tmp_path: Path) -> dict[str, Path]:
    accepted = tmp_path / "accepted"
    accepted.mkdir()
    identity = _release(tmp_path, accepted, dataset="identity", role="prospective_as_received", epoch="nasdaq_alpaca_active_us_equity_v1", name="identity")
    calendar = _release(tmp_path, accepted, dataset="xnys_sessions", role="derived_causal", epoch="xnys_v1", name="calendar")
    bars = _release(tmp_path, accepted, dataset="alpaca_daily_bars", role="active_historical", epoch="alpaca_basic_sip_raw_v1", upstream=(identity.name, calendar.name), name="bars")
    actions = _release(tmp_path, accepted, dataset="corporate_actions", role="prospective_as_received", epoch="alpaca_corporate_actions_v1", name="actions")
    return {"accepted": accepted, "identity": identity, "calendar": calendar, "bars": bars, "actions": actions}


def test_policy_freezes_prospective_horizon_and_local_git_registry() -> None:
    policy = load_prospective_evidence_policy(REPO)
    assert policy["wfa_evidence_horizon"]["minimum_total_sessions"] == 2268
    assert policy["external_registry"]["configured_status"] == "CONFIGURED_LOCAL_GIT"
    assert policy["external_registry"]["requires_remote_backup"] is True
    assert policy["external_registry"]["independent_immutability"] is False
    assert policy["feature_contract"]["feature_names"] == [
        "d0_raw_intraday_return", "trailing_5_session_raw_return", "trailing_5_session_raw_volatility"
    ]


def test_price_only_features_are_causal_and_use_the_frozen_formulas() -> None:
    sessions = tuple(date(2026, 7, day) for day in range(20, 26))
    decision_at = datetime(2026, 7, 25, 21, tzinfo=timezone.utc)
    bars = tuple(
        CausalPriceBar("asset-a", session, 100.0 + index, 101.0 + index, datetime(2026, 7, 25, 20, tzinfo=timezone.utc))
        for index, session in enumerate(sessions)
    )
    result = materialize_price_only_features(bars, sessions=sessions, decision_session=sessions[-1], decision_at=decision_at, action_coverage_complete=True, action_or_delisting_sessions=frozenset())
    returns = [102 / 101 - 1, 103 / 102 - 1, 104 / 103 - 1, 105 / 104 - 1, 106 / 105 - 1]
    mean = sum(returns) / len(returns)
    assert result[0].status == READY_STATUS
    assert result[0].values == pytest.approx({
        "d0_raw_intraday_return": 106 / 105 - 1,
        "trailing_5_session_raw_return": 106 / 101 - 1,
        "trailing_5_session_raw_volatility": math.sqrt(sum((item - mean) ** 2 for item in returns) / len(returns)),
    })


@pytest.mark.parametrize(
    ("action_coverage_complete", "action_or_delisting_sessions", "reason"),
    [
        (False, frozenset(), "incomplete corporate-action or delisting coverage"),
        (True, frozenset({date(2026, 7, 23)}), "action or delisting event intersects feature lookback"),
    ],
)
def test_price_only_features_abstain_when_action_evidence_is_unresolved(
    action_coverage_complete: bool,
    action_or_delisting_sessions: frozenset[date],
    reason: str,
) -> None:
    sessions = tuple(date(2026, 7, day) for day in range(20, 26))
    bars = tuple(
        CausalPriceBar("asset-a", session, 100.0, 101.0, datetime(2026, 7, 25, 20, tzinfo=timezone.utc))
        for session in sessions
    )
    result = materialize_price_only_features(
        bars,
        sessions=sessions,
        decision_session=sessions[-1],
        decision_at=datetime(2026, 7, 25, 21, tzinfo=timezone.utc),
        action_coverage_complete=action_coverage_complete,
        action_or_delisting_sessions=action_or_delisting_sessions,
    )
    assert result[0].status == UNRESOLVED_STATUS
    assert result[0].values is None
    assert result[0].reason == reason


def test_price_only_features_abstain_when_bar_arrives_after_decision() -> None:
    sessions = tuple(date(2026, 7, day) for day in range(20, 26))
    bars = tuple(
        CausalPriceBar("asset-a", session, 100.0, 101.0, datetime(2026, 7, 25, 22, tzinfo=timezone.utc))
        for session in sessions
    )
    result = materialize_price_only_features(
        bars,
        sessions=sessions,
        decision_session=sessions[-1],
        decision_at=datetime(2026, 7, 25, 21, tzinfo=timezone.utc),
        action_coverage_complete=True,
        action_or_delisting_sessions=frozenset(),
    )
    assert result[0].status == UNRESOLVED_STATUS
    assert result[0].values is None
    assert result[0].reason == "required evidence was unavailable by decision time"


def test_epoch_plan_binds_only_trust_eligible_prospective_inputs(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    plan = build_prospective_epoch_plan(
        identity_release_directory=paths["identity"], bars_release_directory=paths["bars"],
        actions_release_directory=paths["actions"], calendar_release_directory=paths["calendar"],
        accepted_root=paths["accepted"], repository_root=REPO,
    )
    assert plan["mode"] == "PROSPECTIVE_EVIDENCE_EPOCH_PLAN_ONLY"
    assert plan["authorities"]["training"] is False
    assert plan["external_registry"]["configured_for_real_trial"] is True
    assert plan["external_registry"]["owner_controlled"] is True
    assert len(plan["prospective_epoch_plan_id"]) == 64


def test_epoch_plan_rejects_proxy_inputs(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    proxy = _release(tmp_path, paths["accepted"], dataset="alpaca_discovery_proxy_features", role="legacy_discovery_only", epoch="legacy_proxy_v1", name="proxy")
    with pytest.raises(ContractError, match="release dataset differs|prohibited legacy proxy"):
        build_prospective_epoch_plan(
            identity_release_directory=proxy, bars_release_directory=paths["bars"],
            actions_release_directory=paths["actions"], calendar_release_directory=paths["calendar"],
            accepted_root=paths["accepted"], repository_root=REPO,
        )


def test_epoch_plan_rejects_bars_without_identity_and_calendar_lineage(tmp_path: Path) -> None:
    paths = _inputs(tmp_path)
    bars = _release(tmp_path, paths["accepted"], dataset="alpaca_daily_bars", role="active_historical", epoch="alpaca_basic_sip_raw_v1", name="orphan-bars")
    with pytest.raises(ContractError, match="does not bind identity and calendar"):
        build_prospective_epoch_plan(
            identity_release_directory=paths["identity"], bars_release_directory=bars,
            actions_release_directory=paths["actions"], calendar_release_directory=paths["calendar"],
            accepted_root=paths["accepted"], repository_root=REPO,
        )
