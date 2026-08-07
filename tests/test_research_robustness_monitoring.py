from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints

import numpy as np
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.governance import create_local_integrity_record
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.monitoring import (
    MonitoringObservation,
    MonitoringPolicy,
    MonitoringState,
    ProspectiveMonitoringLedger,
    assess_monitoring,
)
from us_stocks_swing_model_v2.monitoring_policy import (
    MONITORING_POLICY_VERSION,
    MONITORING_STATE_PRECEDENCE,
)
from us_stocks_swing_model_v2.research import (
    FoldEffect,
    ResearchContractError,
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


def test_monitoring_constructor_public_annotations_resolve() -> None:
    hints = get_type_hints(ProspectiveMonitoringLedger.__init__)
    assert hints["synthetic_history_permit"] == SyntheticOnlyPermit | None


def _clock(hour: int) -> TrustedClock:
    at = datetime(2026, 7, 15, hour, tzinfo=timezone.utc)
    return TrustedClock.synthetic_fixed(
        at,
        permit=SyntheticOnlyPermit.create(
            fixture_id=f"monitoring-clock-{hour}",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _monitoring_ledger(tmp_path: Path, hour: int, *, bundle_id: str = "b" * 64):
    history_permit_ids = tuple(
        sorted(_clock(value).synthetic_permit_id for value in range(1, hour + 1))
    )
    return ProspectiveMonitoringLedger(
        tmp_path / "ledger" / "monitoring.jsonl",
        tmp_path / "anchors",
        bundle_id=bundle_id,
        monitoring_policy_hash=MonitoringPolicy().policy_hash,
        monitoring_reference_hash="d" * 64,
        clock=_clock(hour),
        synthetic_history_clock_permit_ids=history_permit_ids,
        synthetic_history_permit=SyntheticOnlyPermit.create(
            fixture_id=f"monitoring-history-through-{hour}",
            scope="SYNTHETIC_LEDGER_HISTORY_PERMITS",
        ),
    )


@pytest.mark.parametrize(
    "policy_hash",
    (
        "c" * 64,
        "203b1537230c378f59ba31ff4885d7e18456bdd722ed9e6caf99eb0c17acd049",
    ),
)
def test_monitoring_ledger_rejects_policy_hash_not_derived_from_policy(
    tmp_path: Path,
    policy_hash: str,
) -> None:
    with pytest.raises(ContractError, match="frozen evaluated policy"):
        ProspectiveMonitoringLedger(
            tmp_path / "ledger.jsonl",
            tmp_path / "anchors",
            bundle_id="b" * 64,
            monitoring_policy_hash=policy_hash,
            monitoring_reference_hash="d" * 64,
            clock=_clock(1),
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


@pytest.mark.parametrize("variant_count", (1, 4, 6))
def test_variant_stability_rejects_incomplete_or_expanded_census(
    mechanics,
    variant_count: int,
) -> None:
    fixture, permit = mechanics
    variants = tuple(
        VariantEffect(f"variant-{index}", 1.0)
        for index in range(variant_count)
    )

    with pytest.raises(ResearchContractError, match="census.*seed_count"):
        evaluate_variant_stability(
            base_effect=1.0,
            variants=variants,
            policy=StabilityPolicy(),
            permit=permit,
            fixture=fixture,
        )


def test_single_temporal_fold_is_controlled_inconclusive(mechanics) -> None:
    fixture, permit = mechanics
    result = evaluate_temporal_concentration(
        folds=_folds((0.10,)),
        policy=TemporalConcentrationPolicy(),
        permit=permit,
        fixture=fixture,
    )
    assert result.state is RobustnessState.MECHANICS_INCONCLUSIVE
    assert result.leave_one_out_effects == ()
    assert "INSUFFICIENT_OUTER_FOLDS" in result.reasons


def test_source_epochs_require_252_dates_and_positive_effect(mechanics) -> None:
    fixture, permit = mechanics
    policy = SourceEpochPolicy(("LEGACY_EPOCH_A", "LEGACY_EPOCH_B"))
    passed = evaluate_source_epoch_robustness(
        effects=(
            SourceEpochEffect("LEGACY_EPOCH_A", 252, 0.01),
            SourceEpochEffect("LEGACY_EPOCH_B", 252, 0.02),
        ),
        policy=policy, permit=permit, fixture=fixture,
    )
    assert passed.state is RobustnessState.MECHANICS_READY

    insufficient = evaluate_source_epoch_robustness(
        effects=(
            SourceEpochEffect("LEGACY_EPOCH_A", 251, 0.01),
            SourceEpochEffect("LEGACY_EPOCH_B", 252, 0.0),
        ),
        policy=policy, permit=permit, fixture=fixture,
    )
    assert insufficient.state is RobustnessState.MECHANICS_INCONCLUSIVE
    assert set(insufficient.reasons) == {
        "LEGACY_EPOCH_A:INSUFFICIENT_OOS_DATES",
        "LEGACY_EPOCH_B:NONPOSITIVE_STRESS_COST_EFFECT",
    }


def test_monitoring_boundaries_pause_and_abstain() -> None:
    payload = MonitoringPolicy().as_dict()
    assert payload["policy_version"] == MONITORING_POLICY_VERSION
    assert payload["state_precedence"] == list(MONITORING_STATE_PRECEDENCE)
    pending = assess_monitoring(MonitoringObservation(30, 499, 0.0, 0.0, 1.0, 1.0, None))
    assert pending.state is MonitoringState.MONITORING_PENDING
    assert pending.requires_abstention
    low_window_pause = assess_monitoring(
        MonitoringObservation(1, 1, 0.25, 0.0, 1.0, 1.0, None)
    )
    assert low_window_pause.state is MonitoringState.MONITORING_PAUSED
    assert low_window_pause.requires_abstention
    low_window_warning = assess_monitoring(
        MonitoringObservation(1, 1, 0.10, 0.05, 0.94, 1.0, None)
    )
    assert low_window_warning.state is MonitoringState.MONITORING_PENDING
    assert low_window_warning.reasons == ("MINIMUM_WINDOW_PENDING",)
    assert low_window_warning.requires_abstention
    warning = assess_monitoring(MonitoringObservation(30, 500, 0.10, 0.05, 0.94, 1.0, None))
    assert warning.state is MonitoringState.MONITORING_WARNING
    assert not warning.requires_abstention
    paused = assess_monitoring(MonitoringObservation(30, 500, 0.25, 0.10, 0.89, 1.0, 0.10))
    assert paused.state is MonitoringState.MONITORING_PAUSED
    assert paused.requires_abstention
    invalid = assess_monitoring(MonitoringObservation(30, 500, 0.0, 0.0, -1.0, 1.0, None))
    assert invalid.state is MonitoringState.MONITORING_INVALID
    assert invalid.requires_abstention


def test_monitoring_ledger_persists_pending_before_warning(
    tmp_path: Path,
) -> None:
    warning_but_undersized = MonitoringObservation(
        1,
        1,
        0.10,
        0.05,
        0.94,
        1.0,
        None,
    )
    appended = _monitoring_ledger(tmp_path, 1).append(
        warning_but_undersized,
        previous_anchor=None,
    )
    record = appended["record"]
    assert record["assessed_state"] == "MONITORING_PENDING"
    assert record["effective_state"] == "MONITORING_PENDING"
    assert record["reasons"] == ["MINIMUM_WINDOW_PENDING"]
    assert record["abstention_required"] is True
    assert record["automatic_actions"] == []

    verified = _monitoring_ledger(tmp_path, 1).verify(
        Path(appended["anchor_path"])
    )
    assert len(verified) == 1
    assert verified[0].effective_state == "MONITORING_PENDING"
    assert verified[0].abstention_required is True


@pytest.mark.parametrize(
    ("maximum_psi", "maximum_missingness_delta", "expected_reasons"),
    (
        (-0.01, 0.0, ("INVALID_PSI",)),
        (0.0, -0.01, ("INVALID_MISSINGNESS_DELTA",)),
        (-0.01, -0.02, ("INVALID_PSI", "INVALID_MISSINGNESS_DELTA")),
        (float("nan"), 0.0, ("INVALID_NUMERIC_EVIDENCE",)),
        (0.0, float("inf"), ("INVALID_NUMERIC_EVIDENCE",)),
    ),
)
def test_monitoring_rejects_invalid_drift_metric_domains(
    maximum_psi: float,
    maximum_missingness_delta: float,
    expected_reasons: tuple[str, ...],
) -> None:
    decision = assess_monitoring(
        MonitoringObservation(
            30,
            500,
            maximum_psi,
            maximum_missingness_delta,
            1.0,
            1.0,
            None,
        )
    )
    assert decision.state is MonitoringState.MONITORING_INVALID
    assert decision.reasons == expected_reasons
    assert decision.requires_abstention


def test_monitoring_accepts_zero_drift_metrics() -> None:
    decision = assess_monitoring(
        MonitoringObservation(30, 500, 0.0, 0.0, 1.0, 1.0, None)
    )
    assert decision.state is MonitoringState.MONITORING_OK


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
        "monitoring_policy_hash": MonitoringPolicy().policy_hash,
        "monitoring_reference_hash": "d" * 64,
        "previous_monitoring_record_id": previous_record_id,
        "observation_hash": clean_observation.observation_hash,
    }
    authorization = create_local_integrity_record(
        scope="AUTHORIZE_MONITORING_RECOVERY",
        subject_id="b" * 64,
        bindings=bindings,
        clock=_clock(3),
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
