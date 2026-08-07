from __future__ import annotations

import numpy as np
import pytest

from us_stocks_swing_model_v2.research import (
    ResearchArrayBinding,
    ResearchContractError,
    SessionWindow,
    SyntheticOnlyPermit,
    TemporalSamples,
    assert_disjoint_partitions,
    make_synthetic_permit,
    nested_chronological_splits,
    purge_and_post_embargo_indices,
    require_synthetic_permit,
)


def test_research_array_binding_freezes_trial_family_samples_and_arrays() -> None:
    arrays = {
        "book_returns": np.asarray([0.01, -0.02, 0.03], dtype=np.float64),
        "baseline_differentials": np.asarray(
            [[0.01, -0.01], [0.02, 0.00], [-0.01, 0.01]],
            dtype=np.float64,
        ),
    }
    sample_ids = ("sample-0001", "sample-0002", "sample-0003")
    binding = ResearchArrayBinding.create(
        trial_id="1" * 64,
        trial_family_id="absolute-direction-v2-family",
        trial_family_anchor_id="2" * 64,
        census_anchor_id="3" * 64,
        evaluator_closure_hash="4" * 64,
        data_release_ids=("6" * 64, "5" * 64),
        sample_ids=sample_ids,
        arrays=arrays,
    )
    binding.validate_inputs(sample_ids=sample_ids, arrays=arrays)
    assert binding.data_release_ids == ("5" * 64, "6" * 64)

    changed = {**arrays, "book_returns": arrays["book_returns"].copy()}
    changed["book_returns"][0] = 0.011
    with pytest.raises(ResearchContractError, match="statistical arrays differ"):
        binding.validate_inputs(sample_ids=sample_ids, arrays=changed)
    with pytest.raises(ResearchContractError, match="sample IDs differ"):
        binding.validate_inputs(sample_ids=tuple(reversed(sample_ids)), arrays=arrays)


def test_synthetic_permit_binds_exact_float64_fixture() -> None:
    fixture = np.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=np.float64)
    permit = make_synthetic_permit(fixture, generator_id="oracle-v1", seed=17)
    require_synthetic_permit(permit, fixture)

    changed = fixture.copy()
    changed[0, 0] = 9.0
    with pytest.raises(ResearchContractError, match="exact fixture"):
        require_synthetic_permit(permit, changed)


def test_synthetic_permit_rejects_real_history_and_sealing_claims() -> None:
    fixture = np.asarray([1.0, 2.0, 3.0], dtype=np.float64)
    with pytest.raises(ResearchContractError, match="SYNTHETIC"):
        make_synthetic_permit(
            fixture,
            generator_id="not-allowed",
            seed=0,
            source_kind="REAL_HISTORY",
        )
    forged = SyntheticOnlyPermit(
        purpose="MECHANICS_ONLY",
        source_kind="SYNTHETIC",
        generator_id="forged",
        seed=0,
        dataset_sha256="0" * 64,
        candidate_sealing_authorized=True,
    )
    with pytest.raises(ResearchContractError, match="cannot authorize"):
        require_synthetic_permit(forged)


def test_float_contract_never_casts_or_drops_nonfinite_rows() -> None:
    with pytest.raises(ResearchContractError, match="float64"):
        make_synthetic_permit(
            np.asarray([1.0, 2.0], dtype=np.float32),
            generator_id="wrong-dtype",
            seed=0,
        )
    with pytest.raises(ResearchContractError, match="NaN"):
        make_synthetic_permit(
            np.asarray([1.0, np.nan], dtype=np.float64),
            generator_id="nan",
            seed=0,
        )


def test_fit_and_audit_overlap_fails_closed() -> None:
    fit = np.asarray([0, 1, 2], dtype=np.int64)
    audit = np.asarray([2, 3], dtype=np.int64)
    with pytest.raises(ResearchContractError, match="overlap"):
        assert_disjoint_partitions(fit, audit)


def test_half_open_purge_and_post_embargo_oracle() -> None:
    samples = TemporalSamples(
        decision_session=np.asarray([0, 2, 4, 6, 8], dtype=np.int64),
        label_start=np.asarray([1, 3, 5, 7, 9], dtype=np.int64),
        label_end=np.asarray([2, 5, 6, 9, 10], dtype=np.int64),
        label_known_session=np.asarray([2, 5, 6, 9, 10], dtype=np.int64),
    )
    candidates = np.asarray([0, 1, 3, 4], dtype=np.int64)
    heldout = np.asarray([2], dtype=np.int64)

    # Outcome intervals [3,5) and [5,6) only touch, but their broader frozen
    # purge intervals [2,5) and [4,6) overlap from decision time. [6,9) starts
    # at the held-out end but is in post-embargo [6,8); decision 8 is safe.
    actual = purge_and_post_embargo_indices(
        samples,
        candidates,
        heldout,
        post_embargo_sessions=2,
    )
    np.testing.assert_array_equal(actual, np.asarray([0, 4], dtype=np.int64))


def test_nested_chronological_split_has_strict_known_before_and_session_gap() -> None:
    decisions = np.arange(10, dtype=np.int64)
    samples = TemporalSamples(
        decision_session=decisions,
        label_start=decisions + 1,
        label_end=decisions + 2,
        label_known_session=decisions + 2,
    )
    folds = nested_chronological_splits(
        samples,
        outer_test_windows=(SessionWindow(7, 9),),
        inner_validation_windows=((SessionWindow(4, 5),),),
        session_embargo=1,
        minimum_fit_samples=2,
        minimum_audit_samples=1,
    )

    outer = folds[0]
    np.testing.assert_array_equal(outer.audit_indices, np.asarray([7, 8], dtype=np.int64))
    np.testing.assert_array_equal(outer.fit_indices, np.asarray([0, 1, 2, 3, 4], dtype=np.int64))
    inner = outer.inner_folds[0]
    np.testing.assert_array_equal(inner.audit_indices, np.asarray([4], dtype=np.int64))
    np.testing.assert_array_equal(inner.fit_indices, np.asarray([0, 1], dtype=np.int64))
    # Observation 5 is inside the one-session gap, while observation 6 has a
    # label known exactly at test start and is not treated as known-before.
    assert 5 not in outer.fit_indices
    assert 6 not in outer.fit_indices


def test_inner_window_cannot_enter_outer_embargo() -> None:
    decisions = np.arange(10, dtype=np.int64)
    samples = TemporalSamples(decisions, decisions + 1, decisions + 2, decisions + 2)
    with pytest.raises(ResearchContractError, match="outer embargo"):
        nested_chronological_splits(
            samples,
            outer_test_windows=(SessionWindow(7, 9),),
            inner_validation_windows=((SessionWindow(6, 7),),),
            session_embargo=1,
            minimum_fit_samples=1,
            minimum_audit_samples=1,
        )


@pytest.mark.parametrize("offset", [0, -1])
def test_label_interval_cannot_begin_at_or_before_decision(offset: int) -> None:
    decisions = np.asarray([2, 4], dtype=np.int64)
    samples = TemporalSamples(
        decision_session=decisions,
        label_start=decisions + offset,
        label_end=decisions + 2,
        label_known_session=decisions + 2,
    )
    with pytest.raises(ResearchContractError, match="strictly after"):
        samples.validate()
