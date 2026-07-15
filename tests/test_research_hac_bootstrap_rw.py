from __future__ import annotations

import numpy as np
import pytest

from us_stocks_swing_model_v2.research import (
    ResearchContractError,
    apply_shared_indices,
    hac_t_statistic,
    newey_west_mean,
    romano_wolf_from_differentials,
    romano_wolf_stepdown,
    stationary_bootstrap_index_kernel,
    stationary_bootstrap_index_rows,
    stationary_bootstrap_indices,
)


def test_newey_west_mean_hand_computed_oracle() -> None:
    values = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float64)
    result = newey_west_mean(values, lag=1)
    assert result.mean == pytest.approx(2.5)
    assert result.long_run_variance == pytest.approx(1.5625)
    assert result.variance_of_mean == pytest.approx(0.390625)
    assert result.standard_error == pytest.approx(0.625)
    assert result.status == "OK"


def test_hac_degenerate_and_implicit_lag_fail_closed() -> None:
    constant = np.ones(5, dtype=np.float64)
    result = newey_west_mean(constant, lag=0)
    assert result.status == "DEGENERATE"
    with pytest.raises(ResearchContractError, match="degenerate"):
        hac_t_statistic(constant, lag=0)
    with pytest.raises(TypeError):
        newey_west_mean(np.arange(5, dtype=np.float64))
    with pytest.raises(ResearchContractError, match="overflowed|non-finite"):
        newey_west_mean(
            np.asarray([1e308, -1e308, 1e308, -1e308], dtype=np.float64),
            lag=0,
        )
    with pytest.raises(ResearchContractError, match="explicit real"):
        hac_t_statistic(np.arange(5, dtype=np.float64), lag=0, null_mean=True)


def test_stationary_bootstrap_uniform_kernel_oracle() -> None:
    restart = np.asarray([[0.0, 0.9, 0.1, 0.8, 0.2]], dtype=np.float64)
    starts = np.asarray([[0.65, 0.2, 0.99, 0.4, 0.0]], dtype=np.float64)
    actual = stationary_bootstrap_index_kernel(
        n_observations=5,
        mean_block_length=2.0,
        restart_uniforms=restart,
        start_uniforms=starts,
    )
    np.testing.assert_array_equal(actual, np.asarray([[3, 4, 4, 0, 0]], dtype=np.int64))


def test_seeded_stationary_bootstrap_and_shared_application_are_deterministic() -> None:
    first = stationary_bootstrap_indices(
        n_observations=6,
        n_resamples=4,
        mean_block_length=2.0,
        seed=41,
    )
    second = stationary_bootstrap_indices(
        n_observations=6,
        n_resamples=4,
        mean_block_length=2.0,
        seed=41,
    )
    np.testing.assert_array_equal(first, second)
    streamed = np.stack(
        tuple(
            stationary_bootstrap_index_rows(
                n_observations=6,
                n_resamples=4,
                mean_block_length=2.0,
                seed=41,
            )
        )
    )
    np.testing.assert_array_equal(first, streamed)
    matrix = np.column_stack(
        (np.arange(6, dtype=np.float64), np.arange(6, dtype=np.float64) + 100.0)
    )
    resampled = apply_shared_indices(matrix, first)
    np.testing.assert_array_equal(resampled[:, :, 1] - resampled[:, :, 0], 100.0)
    with pytest.raises(ResearchContractError, match="memory cap"):
        stationary_bootstrap_indices(
            n_observations=100,
            n_resamples=100,
            mean_block_length=2.0,
            seed=1,
            maximum_materialized_bytes=100,
        )


def test_romano_wolf_plus_one_stepdown_oracle() -> None:
    observed = np.asarray([3.0, 2.0, 1.0], dtype=np.float64)
    null_bootstrap = np.asarray(
        [
            [2.5, 1.5, 0.5],
            [3.5, 0.5, 1.2],
            [0.0, 2.5, 0.8],
            [1.0, 1.0, 1.5],
        ],
        dtype=np.float64,
    )
    result = romano_wolf_stepdown(
        observed,
        null_bootstrap,
        hypothesis_ids=("A", "B", "C"),
        tail="greater",
        minimum_resamples=4,
    )
    np.testing.assert_array_equal(result.stepdown_order, np.asarray([0, 1, 2], dtype=np.int64))
    np.testing.assert_allclose(result.stage_p_values, np.asarray([0.4, 0.4, 0.6]))
    np.testing.assert_allclose(result.adjusted_p_values, np.asarray([0.4, 0.4, 0.6]))


def test_romano_wolf_default_resample_floor_fails_closed() -> None:
    with pytest.raises(ResearchContractError, match="too few"):
        romano_wolf_stepdown(
            np.asarray([1.0], dtype=np.float64),
            np.asarray([[0.0], [1.0]], dtype=np.float64),
            hypothesis_ids=("only",),
            tail="greater",
        )


def test_romano_wolf_full_path_centers_null_and_is_seed_deterministic() -> None:
    time = np.arange(64, dtype=np.float64)
    differentials = np.column_stack(
        (0.02 + np.sin(time * 0.37), 0.01 + np.cos(time * 0.23))
    )
    first = romano_wolf_from_differentials(
        differentials,
        hypothesis_ids=("sleeve-a", "sleeve-b"),
        hac_lag=2,
        mean_block_length=4.0,
        n_resamples=31,
        seed=9,
        minimum_resamples=31,
    )
    second = romano_wolf_from_differentials(
        differentials,
        hypothesis_ids=("sleeve-a", "sleeve-b"),
        hac_lag=2,
        mean_block_length=4.0,
        n_resamples=31,
        seed=9,
        minimum_resamples=31,
    )
    np.testing.assert_allclose(
        first.observed_statistics,
        np.asarray([0.44846469, 0.53446888]),
        rtol=1e-7,
    )
    np.testing.assert_allclose(first.adjusted_p_values, np.asarray([0.5625, 0.5625]))
    np.testing.assert_array_equal(first.adjusted_p_values, second.adjusted_p_values)
    assert first.null_centered is True
