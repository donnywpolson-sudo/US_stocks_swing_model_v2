from __future__ import annotations

import numpy as np
import pytest

from us_stocks_swing_model_v2.research import (
    ResearchArrayBinding,
    ResearchContractError,
    deflated_sharpe_ratio,
    exhaustive_cscv_pbo,
    training_only_mde,
)


def _dsr_binding(
    returns: np.ndarray,
    trial_sharpes: np.ndarray,
) -> tuple[ResearchArrayBinding, tuple[str, ...]]:
    sample_ids = tuple(f"sample-{index}" for index in range(len(returns)))
    binding = ResearchArrayBinding.create(
        trial_id="1" * 64,
        trial_family_id="synthetic-dsr-family",
        trial_family_anchor_id="2" * 64,
        census_anchor_id="3" * 64,
        evaluator_closure_hash="4" * 64,
        data_release_ids=("5" * 64,),
        sample_ids=sample_ids,
        arrays={
            "selected_returns": returns,
            "trial_sharpes": trial_sharpes,
        },
    )
    return binding, sample_ids


def test_deflated_sharpe_hand_computed_oracle_uses_raw_trial_census() -> None:
    returns = np.arange(5, dtype=np.float64)
    selected_sharpe = float(np.mean(returns) / np.std(returns, ddof=1))
    trial_sharpes = np.asarray([0.0, selected_sharpe], dtype=np.float64)
    binding, sample_ids = _dsr_binding(returns, trial_sharpes)
    result = deflated_sharpe_ratio(
        returns,
        trial_sharpes,
        raw_trial_count=2,
        selected_trial_index=1,
        sample_ids=sample_ids,
        binding=binding,
    )
    assert result.selected_sharpe_per_period == pytest.approx(1.2649110640673518)
    assert result.selected_trial_index == 1
    assert result.selection_rule == "MAX_SHARPE"
    assert result.trial_sharpe_mean == pytest.approx(0.6324555320336759)
    assert result.trial_sharpe_std == pytest.approx(0.8944271909999159)
    assert result.expected_maximum_sharpe == pytest.approx(1.0973388446257617)
    assert result.skewness == pytest.approx(0.0)
    assert result.kurtosis_non_excess == pytest.approx(1.7)
    assert result.probability == pytest.approx(0.6164722577174275)
    assert result.status == "MECHANICS_ONLY"


def test_dsr_rejects_incomplete_mismatched_or_ambiguous_trial_census() -> None:
    returns = np.arange(5, dtype=np.float64)
    selected_sharpe = float(np.mean(returns) / np.std(returns, ddof=1))
    incomplete = np.asarray([0.0, 1.0], dtype=np.float64)
    incomplete_binding, sample_ids = _dsr_binding(returns, incomplete)
    with pytest.raises(ResearchContractError, match="raw_trial_count"):
        deflated_sharpe_ratio(
            returns,
            incomplete,
            raw_trial_count=3,
            selected_trial_index=1,
            sample_ids=sample_ids,
            binding=incomplete_binding,
        )
    ambiguous = np.asarray(
        [selected_sharpe, selected_sharpe],
        dtype=np.float64,
    )
    ambiguous_binding, sample_ids = _dsr_binding(returns, ambiguous)
    with pytest.raises(ResearchContractError, match="unique deterministic"):
        deflated_sharpe_ratio(
            returns,
            ambiguous,
            raw_trial_count=2,
            selected_trial_index=1,
            sample_ids=sample_ids,
            binding=ambiguous_binding,
        )
    mismatched = np.asarray([0.0, 1.0], dtype=np.float64)
    mismatched_binding, sample_ids = _dsr_binding(returns, mismatched)
    with pytest.raises(ResearchContractError, match="do not match"):
        deflated_sharpe_ratio(
            returns,
            mismatched,
            raw_trial_count=2,
            selected_trial_index=1,
            sample_ids=sample_ids,
            binding=mismatched_binding,
        )


def test_dsr_rejects_unbound_and_tampered_non_selected_trial_sharpes() -> None:
    returns = np.arange(5, dtype=np.float64)
    selected_sharpe = float(np.mean(returns) / np.std(returns, ddof=1))
    trial_sharpes = np.asarray([0.0, selected_sharpe], dtype=np.float64)
    binding, sample_ids = _dsr_binding(returns, trial_sharpes)

    with pytest.raises(TypeError, match="sample_ids.*binding|binding.*sample_ids"):
        deflated_sharpe_ratio(
            returns,
            trial_sharpes,
            raw_trial_count=2,
            selected_trial_index=1,
        )

    tampered = trial_sharpes.copy()
    tampered[0] = -7.0
    with pytest.raises(
        ResearchContractError,
        match="statistical arrays differ",
    ):
        deflated_sharpe_ratio(
            returns,
            tampered,
            raw_trial_count=2,
            selected_trial_index=1,
            sample_ids=sample_ids,
            binding=binding,
        )

    with pytest.raises(
        ResearchContractError,
        match="sample IDs differ",
    ):
        deflated_sharpe_ratio(
            returns,
            trial_sharpes,
            raw_trial_count=2,
            selected_trial_index=1,
            sample_ids=tuple(reversed(sample_ids)),
            binding=binding,
        )


def test_exhaustive_cscv_pbo_oracle() -> None:
    # Strategies A and B dominate opposite halves.  Among all C(4,2)=6
    # symmetric splits, the IS winner ranks worst OOS exactly twice.
    returns = np.asarray(
        [
            [3.0, 0.0],
            [3.0, 0.0],
            [0.0, 2.0],
            [0.0, 2.0],
        ],
        dtype=np.float64,
    )
    result = exhaustive_cscv_pbo(
        returns,
        strategy_ids=("A", "B"),
        blocks=4,
        metric="mean",
    )
    assert result.combinations == 6
    assert np.count_nonzero(result.oos_rank_logits < 0.0) == 2
    assert result.pbo_strict == pytest.approx(1.0 / 3.0)
    assert result.pbo_conservative == pytest.approx(1.0 / 3.0)
    assert result.status == "MECHANICS_ONLY"


def test_cscv_never_samples_or_breaks_ties_silently() -> None:
    tied = np.ones((8, 2), dtype=np.float64)
    with pytest.raises(ResearchContractError, match="winner is tied"):
        exhaustive_cscv_pbo(
            tied,
            strategy_ids=("A", "B"),
            blocks=4,
            metric="mean",
        )
    with pytest.raises(ResearchContractError, match="combination cap"):
        exhaustive_cscv_pbo(
            np.column_stack((np.arange(8), -np.arange(8))).astype(np.float64),
            strategy_ids=("A", "B"),
            blocks=4,
            maximum_combinations=5,
        )
    with pytest.raises(ResearchContractError, match="explicit real"):
        exhaustive_cscv_pbo(
            np.column_stack((np.arange(8), -np.arange(8))).astype(np.float64),
            strategy_ids=("A", "B"),
            blocks=4,
            tie_tolerance=True,
        )


def test_training_only_mde_hand_computed_oracle() -> None:
    # Lag-zero LRV is exactly 4.  z_.95 + z_.80 = 2.4864748605243863.
    training = np.asarray([-2.0, 2.0, -2.0, 2.0], dtype=np.float64)
    result = training_only_mde(
        training,
        partition_role="TRAIN",
        hac_lag=0,
        planned_evaluation_observations=100,
        alpha=0.05,
        target_power=0.80,
        alternative="greater",
        economic_mean_hurdle=0.5,
    )
    assert result.long_run_variance == pytest.approx(4.0)
    assert result.minimum_detectable_mean == pytest.approx(0.4972949721048773)
    assert result.required_evaluation_observations == 99
    assert result.adequately_powered is True
    assert result.status == "TRAINING_PLAN_ONLY"


def test_power_planner_rejects_audit_data_and_nonpositive_economic_hurdle() -> None:
    training = np.asarray([-2.0, 2.0, -2.0, 2.0], dtype=np.float64)
    common = dict(
        hac_lag=0,
        planned_evaluation_observations=100,
        alpha=0.05,
        target_power=0.8,
        alternative="greater",
        economic_mean_hurdle=0.5,
    )
    with pytest.raises(ResearchContractError, match="TRAIN"):
        training_only_mde(training, partition_role="AUDIT", **common)
    common["economic_mean_hurdle"] = 0.0
    with pytest.raises(ResearchContractError, match="strictly positive"):
        training_only_mde(training, partition_role="TRAIN", **common)
    common["economic_mean_hurdle"] = 0.5
    common["alpha"] = True
    with pytest.raises(ResearchContractError, match="explicit real"):
        training_only_mde(training, partition_role="TRAIN", **common)
