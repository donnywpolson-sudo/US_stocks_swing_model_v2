from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.governance import (
    AuthorizationAuthority,
    sign_authorization_receipt,
)
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.monitoring import (
    MonitoringObservation,
    MonitoringState,
    ProspectiveMonitoringLedger,
    assess_monitoring,
)
from us_stocks_swing_model_v2.research import (
    FoldEffect,
    RobustnessState,
    SourceEpochEffect,
    SourceEpochPolicy,
    StabilityPolicy,
    TemporalConcentrationPolicy,
    VariantEffect,
    deterministic_stability_seeds,
    evaluate_source_epoch_robustness,
    evaluate_temporal_concentration,
    evaluate_variant_stability,
    make_synthetic_permit,
    verify_deterministic_repeat,
)


AUTH_KEY = b"synthetic-monitoring-recovery-key"


def _clock(hour: int) -> TrustedClock:
    at = datetime(2026, 7, 15, hour, tzinfo=timezone.utc)
    return TrustedClock.synthetic_fixed(
        at,
        permit=SyntheticOnlyPermit.create(
            fixture_id=f"monitoring-clock-{hour}",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _authority() -> AuthorizationAuthority:
    return AuthorizationAuthority.synthetic(
        key_id="synthetic-monitoring-key",
        verification_key=AUTH_KEY,
        permit=SyntheticOnlyPermit.create(
            fixture_id="monitoring-recovery-authority",
            scope="SYNTHETIC_AUTHORIZATION_AUTHORITY",
        ),
    )


def _monitoring_ledger(tmp_path: Path, hour: int, *, bundle_id: str = "b" * 64):
    return ProspectiveMonitoringLedger(
        tmp_path / "ledger" / "monitoring.jsonl",
        tmp_path / "anchors",
        bundle_id=bundle_id,
        monitoring_policy_hash="c" * 64,
        monitoring_reference_hash="d" * 64,
        recovery_authority=_authority(),
        clock=_clock(hour),
    )


@pytest.fixture
def mechanics() -> tuple[np.ndarray, object]:
    fixture = np.asarray([1.0, 2.0, 3.0], dtype=np.float64)
    return fixture, make_synthetic_permit(fixture, generator_id="robustness-fixture", seed=7)


def _folds(values: tuple[float, ...]) -> tuple[FoldEffect, ...]:
    return tuple(FoldEffect(f"fold-{index}", 126, float(value)) for index, value in enumerate(values))


def test_temporal_and_variant_gates_are_binding_inconclusive(mechanics) -> None:
    fixture, permit = mechanics
    temporal = evaluate_temporal_concentration(
        folds=_folds((0.10, 0.09, 0.08, 0.07, 0.06, -0.01, -0.01, -0.01)),
        policy=TemporalConcentrationPolicy(), permit=permit, fixture=fixture,
    )
    assert temporal.state is RobustnessState.MECHANICS_READY
    assert min(temporal.leave_one_out_effects) > 0.0

    stable = evaluate_variant_stability(
        base_effect=1.0,
        variants=tuple(VariantEffect(f"variant-{i}", value) for i, value in enumerate((0.8, 0.7, 0.6, 0.5, -0.1))),
        policy=StabilityPolicy(), permit=permit, fixture=fixture,
    )
    assert stable.state is RobustnessState.MECHANICS_READY
    unstable = evaluate_variant_stability(
        base_effect=1.0,
        variants=tuple(VariantEffect(f"variant-{i}", value) for i, value in enumerate((0.4, 0.3, 0.2, -0.1, -0.2))),
        policy=StabilityPolicy(), permit=permit, fixture=fixture,
    )
    assert unstable.state is RobustnessState.MECHANICS_INCONCLUSIVE

    assert deterministic_stability_seeds("a" * 64) == deterministic_stability_seeds("a" * 64)
    assert verify_deterministic_repeat("1" * 64, "1" * 64)


def test_source_epochs_require_252_dates_and_positive_effect(mechanics) -> None:
    fixture, permit = mechanics
    policy = SourceEpochPolicy(("HFDL_IEX_ONLY", "HFDL_PITRADING_CONSOLIDATED"))
    passed = evaluate_source_epoch_robustness(
        effects=(
            SourceEpochEffect("HFDL_IEX_ONLY", 252, 0.01),
            SourceEpochEffect("HFDL_PITRADING_CONSOLIDATED", 252, 0.02),
        ),
        policy=policy, permit=permit, fixture=fixture,
    )
    assert passed.state is RobustnessState.MECHANICS_READY

    insufficient = evaluate_source_epoch_robustness(
        effects=(
            SourceEpochEffect("HFDL_IEX_ONLY", 251, 0.01),
            SourceEpochEffect("HFDL_PITRADING_CONSOLIDATED", 252, 0.0),
        ),
        policy=policy, permit=permit, fixture=fixture,
    )
    assert insufficient.state is RobustnessState.MECHANICS_INCONCLUSIVE
    assert set(insufficient.reasons) == {
        "HFDL_IEX_ONLY:INSUFFICIENT_OOS_DATES",
        "HFDL_PITRADING_CONSOLIDATED:NONPOSITIVE_STRESS_COST_EFFECT",
    }


def test_monitoring_boundaries_pause_and_abstain() -> None:
    pending = assess_monitoring(MonitoringObservation(30, 499, 0.0, 0.0, 1.0, 1.0, None))
    assert pending.state is MonitoringState.MONITORING_PENDING
    assert pending.requires_abstention
    low_window_pause = assess_monitoring(
        MonitoringObservation(1, 1, 0.25, 0.0, 1.0, 1.0, None)
    )
    assert low_window_pause.state is MonitoringState.MONITORING_PAUSED
    warning = assess_monitoring(MonitoringObservation(30, 500, 0.10, 0.05, 0.94, 1.0, None))
    assert warning.state is MonitoringState.MONITORING_WARNING
    paused = assess_monitoring(MonitoringObservation(30, 500, 0.25, 0.10, 0.89, 1.0, 0.10))
    assert paused.state is MonitoringState.MONITORING_PAUSED
    assert paused.requires_abstention
    invalid = assess_monitoring(MonitoringObservation(30, 500, 0.0, 0.0, -1.0, 1.0, None))
    assert invalid.state is MonitoringState.MONITORING_INVALID
    assert invalid.requires_abstention


def test_monitoring_ledger_requires_reviewed_recovery_and_exact_bindings(
    tmp_path: Path,
) -> None:
    paused_observation = MonitoringObservation(
        30, 500, 0.25, 0.0, 1.0, 1.0, None
    )
    first = _monitoring_ledger(tmp_path, 1).append(
        paused_observation,
        previous_anchor=None,
    )
    assert first["record"]["effective_state"] == "MONITORING_PAUSED"
    assert first["record"]["observation"] == paused_observation.as_dict()
    first_anchor = Path(first["anchor_path"])

    clean_observation = MonitoringObservation(
        31, 520, 0.0, 0.0, 1.0, 1.0, None
    )
    blocked_resume = _monitoring_ledger(tmp_path, 2).append(
        clean_observation,
        previous_anchor=first_anchor,
    )
    assert blocked_resume["record"]["assessed_state"] == "MONITORING_OK"
    assert blocked_resume["record"]["effective_state"] == "MONITORING_PAUSED"
    assert "RECOVERY_REVIEW_REQUIRED" in blocked_resume["record"]["reasons"]
    assert blocked_resume["record"]["abstention_required"] is True
    assert blocked_resume["record"]["automatic_actions"] == []
    blocked_anchor = Path(blocked_resume["anchor_path"])

    previous_record_id = str(blocked_resume["record"]["record_id"])
    bindings = {
        "bundle_id": "b" * 64,
        "monitoring_policy_hash": "c" * 64,
        "monitoring_reference_hash": "d" * 64,
        "previous_monitoring_record_id": previous_record_id,
        "observation_hash": clean_observation.observation_hash,
    }
    authorization = sign_authorization_receipt(
        scope="AUTHORIZE_MONITORING_RECOVERY",
        subject_id="b" * 64,
        bindings=bindings,
        issued_at="2026-07-15T02:30:00Z",
        expires_at="2026-07-15T04:00:00Z",
        authority=_authority(),
    )
    recovered = _monitoring_ledger(tmp_path, 3).append(
        clean_observation,
        previous_anchor=blocked_anchor,
        recovery_authorization=authorization,
    )
    assert recovered["record"]["effective_state"] == "MONITORING_OK"
    assert recovered["record"]["abstention_required"] is False
    assert recovered["record"]["automatic_actions"] == []
    final_anchor = Path(recovered["anchor_path"])
    verified = _monitoring_ledger(tmp_path, 3).verify(final_anchor)
    assert tuple(record.effective_state for record in verified) == (
        "MONITORING_PAUSED",
        "MONITORING_PAUSED",
        "MONITORING_OK",
    )

    with pytest.raises(IntegrityError, match="bundle, policy, reference, or predecessor"):
        _monitoring_ledger(tmp_path, 3, bundle_id="e" * 64).verify(final_anchor)
