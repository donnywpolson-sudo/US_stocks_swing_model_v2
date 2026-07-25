from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

from us_stocks_swing_model_v2.research import (
    NegativeControlOutcome,
    NegativeControlState,
    PortfolioCharter,
    PortfolioState,
    RobustnessState,
    SleeveState,
    SleeveThresholds,
    SyntheticSleeveMetrics,
    apply_negative_control_indices,
    circular_block_derangement_indices,
    evaluate_negative_controls,
    evaluate_portfolio_mechanics,
    evaluate_synthetic_sleeve,
    make_synthetic_permit,
    synthetic_noise_control,
)


def test_block_derangement_is_shared_deterministic_and_preserves_blocks() -> None:
    indices = circular_block_derangement_indices(
        n_observations=8,
        block_size=2,
        seed=7,
    )
    np.testing.assert_array_equal(
        indices,
        np.asarray([2, 3, 4, 5, 6, 7, 0, 1], dtype=np.int64),
    )
    matrix = np.column_stack(
        (np.arange(8, dtype=np.float64), np.arange(8, dtype=np.float64) + 10.0)
    )
    controlled = apply_negative_control_indices(matrix, indices)
    np.testing.assert_array_equal(controlled[:, 1] - controlled[:, 0], 10.0)


def test_noise_control_is_seeded_float64() -> None:
    first = synthetic_noise_control(shape=(5, 3), seed=91)
    second = synthetic_noise_control(shape=(5, 3), seed=91)
    assert first.dtype == np.float64
    np.testing.assert_array_equal(first, second)


def test_negative_controls_fail_on_leakage_or_incompleteness() -> None:
    clear = evaluate_negative_controls(
        (
            NegativeControlOutcome("block-shift", True, False),
            NegativeControlOutcome("noise", True, False),
        )
    )
    assert clear.state == NegativeControlState.CLEAR

    suspicious = evaluate_negative_controls(
        (NegativeControlOutcome("block-shift", True, True),)
    )
    assert suspicious.state == NegativeControlState.LEAKAGE_SUSPECTED
    assert suspicious.suspicious_controls == ("block-shift",)

    incomplete = evaluate_negative_controls(
        (NegativeControlOutcome("noise", False, False),)
    )
    assert incomplete.state == NegativeControlState.INVALID


def _metrics(*, adjusted_p: float) -> SyntheticSleeveMetrics:
    return SyntheticSleeveMetrics(
        mean_after_costs=0.020,
        confidence_lower_bound=0.015,
        minimum_economically_effective_mean=0.010,
        romano_wolf_adjusted_p=adjusted_p,
        dsr_probability=0.99,
        pbo_conservative=0.10,
        power_sufficient=True,
        negative_controls_clear=True,
        numerically_valid=True,
        robustness_state=RobustnessState.MECHANICS_READY,
    )


def test_sleeves_are_independent_and_portfolio_cannot_cross_subsidize() -> None:
    fixture = np.asarray([[0.0, 1.0], [1.0, 0.0]], dtype=np.float64)
    permit = make_synthetic_permit(fixture, generator_id="sleeve-oracle", seed=4)
    thresholds = SleeveThresholds(
        alpha=0.05,
        dsr_probability_minimum=0.95,
        pbo_conservative_maximum=0.20,
    )
    stock_long = evaluate_synthetic_sleeve(
        sleeve_id="stock-long",
        metrics=_metrics(adjusted_p=0.01),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    with np.testing.assert_raises_regex(ValueError, "exact fixture"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock-long",
            metrics=_metrics(adjusted_p=0.01),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture + 1.0,
        )
    etf_short = evaluate_synthetic_sleeve(
        sleeve_id="etf-short",
        metrics=_metrics(adjusted_p=0.06),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert stock_long.state == SleeveState.MECHANICS_READY
    assert etf_short.state == SleeveState.MECHANICS_FAIL_CLOSED
    assert etf_short.failed_gates == ("ROMANO_WOLF",)

    robustness_inconclusive = evaluate_synthetic_sleeve(
        sleeve_id="stock-long",
        metrics=replace(
            _metrics(adjusted_p=0.01),
            robustness_state=RobustnessState.MECHANICS_INCONCLUSIVE,
        ),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert (
        robustness_inconclusive.state
        is SleeveState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    )
    failure_precedes_robustness = evaluate_synthetic_sleeve(
        sleeve_id="etf-short",
        metrics=replace(
            _metrics(adjusted_p=0.06),
            robustness_state=RobustnessState.MECHANICS_INCONCLUSIVE,
        ),
        thresholds=thresholds,
        permit=permit,
        fixture=fixture,
    )
    assert failure_precedes_robustness.state is SleeveState.MECHANICS_FAIL_CLOSED

    with np.testing.assert_raises_regex(ValueError, "explicit real float"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock-long",
            metrics=replace(_metrics(adjusted_p=0.01), mean_after_costs=True),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture,
        )
    with np.testing.assert_raises_regex(ValueError, "exact bool"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock-long",
            metrics=replace(
                _metrics(adjusted_p=0.01), power_sufficient=np.bool_(True)
            ),
            thresholds=thresholds,
            permit=permit,
            fixture=fixture,
        )
    with np.testing.assert_raises_regex(ValueError, "explicit real"):
        evaluate_synthetic_sleeve(
            sleeve_id="stock-long",
            metrics=_metrics(adjusted_p=0.01),
            thresholds=replace(thresholds, alpha=True),
            permit=permit,
            fixture=fixture,
        )

    all_included = PortfolioCharter.create(
        registered_sleeves=("stock-long", "etf-short"),
        included_sleeves=("stock-long", "etf-short"),
    )
    assert (
        evaluate_portfolio_mechanics(all_included, (stock_long, etf_short))
        == PortfolioState.MECHANICS_FAIL_CLOSED
    )
    # An exclusion works only when it is already bound in the charter.
    preregistered_exclusion = PortfolioCharter.create(
        registered_sleeves=("stock-long", "etf-short"),
        included_sleeves=("stock-long",),
    )
    assert (
        evaluate_portfolio_mechanics(
            preregistered_exclusion,
            (stock_long, etf_short),
        )
        == PortfolioState.MECHANICS_READY
    )
    robustness_charter = PortfolioCharter.create(
        registered_sleeves=("stock-long", "etf-short"),
        included_sleeves=("stock-long",),
    )
    assert (
        evaluate_portfolio_mechanics(
            robustness_charter,
            (robustness_inconclusive, etf_short),
        )
        is PortfolioState.MECHANICS_INCONCLUSIVE_ROBUSTNESS
    )

    forged_charter = replace(all_included, charter_hash="0" * 64)
    with np.testing.assert_raises_regex(ValueError, "charter hash"):
        evaluate_portfolio_mechanics(forged_charter, (stock_long, etf_short))


def test_research_package_has_no_io_fit_network_or_alpha_transition() -> None:
    package = (
        Path(__file__).parents[1]
        / "src"
        / "us_stocks_swing_model_v2"
        / "research"
    )
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(package.glob("*.py"))
    ).lower()
    forbidden = (
        "requests.",
        "urllib",
        "http://",
        "https://",
        ".fit(",
        "read_csv(",
        "read_parquet(",
        "real_history_authorized=true",
        "candidate_sealing_authorized=true",
        "historical_pass",
        "alpha_pass",
    )
    for token in forbidden:
        assert token not in source
