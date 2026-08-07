"""Half-open purging and nested chronological split mechanics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .contracts import (
    ResearchContractError,
    assert_disjoint_partitions,
    explicit_int,
    int64_vector,
)


@dataclass(frozen=True)
class TemporalSamples:
    """Causal ordering and label intervals for aligned observations.

    ``label_start``/``label_end`` are the outcome interval coordinates. Purging
    uses the broader half-open ``[decision_session, label_end)`` information
    interval required by the frozen contract. ``decision_session`` and
    ``label_known_session`` are integral session order keys. Equality at a
    held-out boundary is not treated as known-before; callers needing
    sub-session ordering must encode it in the order key.
    """

    decision_session: np.ndarray
    label_start: np.ndarray
    label_end: np.ndarray
    label_known_session: np.ndarray

    def validate(self) -> int:
        arrays = (
            int64_vector(self.decision_session, name="decision_session"),
            int64_vector(self.label_start, name="label_start"),
            int64_vector(self.label_end, name="label_end"),
            int64_vector(self.label_known_session, name="label_known_session"),
        )
        lengths = {len(value) for value in arrays}
        if len(lengths) != 1:
            raise ResearchContractError("temporal arrays must have equal length")
        if bool(np.any(self.label_end <= self.label_start)):
            raise ResearchContractError("every label interval must satisfy start < end")
        if bool(np.any(self.label_start <= self.decision_session)):
            raise ResearchContractError(
                "every label interval must begin strictly after its decision session"
            )
        if bool(np.any(np.diff(self.decision_session) < 0)):
            raise ResearchContractError("samples must be ordered by decision_session")
        if bool(np.any(self.label_known_session < self.decision_session)):
            raise ResearchContractError("a label cannot be known before its decision")
        if bool(np.any(self.label_known_session < self.label_end)):
            raise ResearchContractError("a label cannot be known before its interval ends")
        return len(self.decision_session)


@dataclass(frozen=True, order=True)
class SessionWindow:
    start: int
    stop: int

    def validate(self) -> None:
        if isinstance(self.start, bool) or isinstance(self.stop, bool):
            raise ResearchContractError("session boundaries must be integers")
        if not isinstance(self.start, int) or not isinstance(self.stop, int):
            raise ResearchContractError("session boundaries must be integers")
        if self.start >= self.stop:
            raise ResearchContractError("session windows are half-open with start < stop")


@dataclass(frozen=True)
class InnerFold:
    validation_window: SessionWindow
    fit_indices: np.ndarray
    audit_indices: np.ndarray


@dataclass(frozen=True)
class NestedFold:
    test_window: SessionWindow
    fit_indices: np.ndarray
    audit_indices: np.ndarray
    inner_folds: tuple[InnerFold, ...]


def _checked_indices(indices: np.ndarray, *, n: int, name: str) -> np.ndarray:
    values = int64_vector(indices, name=name)
    if bool(np.any(values < 0)) or bool(np.any(values >= n)):
        raise ResearchContractError(f"{name} contains an out-of-range index")
    if len(np.unique(values)) != len(values):
        raise ResearchContractError(f"{name} contains duplicate indices")
    return values


def purge_and_post_embargo_indices(
    samples: TemporalSamples,
    candidate_indices: np.ndarray,
    heldout_indices: np.ndarray,
    *,
    post_embargo_sessions: int,
) -> np.ndarray:
    """Remove overlapping labels and canonical post-heldout embargo samples.

    Two half-open intervals ``[a,b)`` and ``[c,d)`` overlap exactly when
    ``a < d and c < b``.  A candidate decision in ``[d,d+embargo)`` is also
    removed.  This generic primitive supports arbitrary candidate sets; the
    chronological splitter below additionally forbids all future training.
    """

    n = samples.validate()
    candidates = _checked_indices(candidate_indices, n=n, name="candidate_indices")
    heldout = _checked_indices(heldout_indices, n=n, name="heldout_indices")
    if isinstance(post_embargo_sessions, bool) or not isinstance(post_embargo_sessions, int):
        raise ResearchContractError("post_embargo_sessions must be an integer")
    if post_embargo_sessions < 0:
        raise ResearchContractError("post_embargo_sessions cannot be negative")
    if post_embargo_sessions and bool(
        np.any(
            samples.label_end[heldout]
            > np.iinfo(np.int64).max - post_embargo_sessions
        )
    ):
        raise ResearchContractError("post-embargo interval would overflow int64")

    keep = np.ones(len(candidates), dtype=np.bool_)
    for position, candidate in enumerate(candidates):
        c_start = samples.decision_session[candidate]
        c_end = samples.label_end[candidate]
        c_decision = samples.decision_session[candidate]
        overlap = np.any(
            (c_start < samples.label_end[heldout])
            & (samples.decision_session[heldout] < c_end)
        )
        embargoed = np.any(
            (samples.label_end[heldout] <= c_decision)
            & (
                c_decision
                < samples.label_end[heldout] + np.int64(post_embargo_sessions)
            )
        )
        keep[position] = not bool(overlap or embargoed)
    return candidates[keep]


def _window_indices(samples: TemporalSamples, window: SessionWindow) -> np.ndarray:
    window.validate()
    return np.flatnonzero(
        (samples.decision_session >= window.start)
        & (samples.decision_session < window.stop)
    ).astype(np.int64)


def _strict_past_fit(
    samples: TemporalSamples,
    heldout: np.ndarray,
    window: SessionWindow,
    *,
    session_embargo: int,
) -> np.ndarray:
    # In an expanding chronological split there is no future training on which
    # a post-test embargo could act.  The declared session embargo is therefore
    # a conservative pre-audit gap, separate from interval purging.
    cutoff = window.start - session_embargo
    candidates = np.flatnonzero(
        (samples.decision_session < cutoff)
        & (samples.label_known_session < window.start)
    ).astype(np.int64)
    return purge_and_post_embargo_indices(
        samples,
        candidates,
        heldout,
        post_embargo_sessions=0,
    )


def nested_chronological_splits(
    samples: TemporalSamples,
    outer_test_windows: Sequence[SessionWindow],
    inner_validation_windows: Sequence[Sequence[SessionWindow]],
    *,
    session_embargo: int,
    minimum_fit_samples: int,
    minimum_audit_samples: int,
) -> tuple[NestedFold, ...]:
    """Build deterministic expanding outer and inner chronological folds."""

    samples.validate()
    if isinstance(session_embargo, bool) or not isinstance(session_embargo, int):
        raise ResearchContractError("session_embargo must be an integer")
    if session_embargo < 0:
        raise ResearchContractError("session_embargo cannot be negative")
    checked_minimum_fit = explicit_int(
        minimum_fit_samples, name="minimum_fit_samples"
    )
    checked_minimum_audit = explicit_int(
        minimum_audit_samples, name="minimum_audit_samples"
    )
    if checked_minimum_fit < 1 or checked_minimum_audit < 1:
        raise ResearchContractError("minimum sample counts must be positive")
    if not outer_test_windows or len(outer_test_windows) != len(inner_validation_windows):
        raise ResearchContractError("each outer window needs its inner schedule")

    previous_stop: int | None = None
    result: list[NestedFold] = []
    for outer_window, inner_schedule in zip(
        outer_test_windows, inner_validation_windows, strict=True
    ):
        outer_window.validate()
        if previous_stop is not None and outer_window.start < previous_stop:
            raise ResearchContractError("outer audit windows must not overlap or reverse")
        previous_stop = outer_window.stop
        audit = _window_indices(samples, outer_window)
        fit = _strict_past_fit(
            samples,
            audit,
            outer_window,
            session_embargo=session_embargo,
        )
        if len(fit) < checked_minimum_fit or len(audit) < checked_minimum_audit:
            raise ResearchContractError("outer fold is underpowered by declared minima")
        assert_disjoint_partitions(fit, audit)

        inner_folds: list[InnerFold] = []
        prior_inner_stop: int | None = None
        fit_set = set(int(value) for value in fit.tolist())
        for validation_window in inner_schedule:
            validation_window.validate()
            if validation_window.stop > outer_window.start - session_embargo:
                raise ResearchContractError("inner validation enters the outer embargo/audit")
            if prior_inner_stop is not None and validation_window.start < prior_inner_stop:
                raise ResearchContractError("inner validation windows overlap or reverse")
            prior_inner_stop = validation_window.stop
            inner_audit = _window_indices(samples, validation_window)
            if not set(int(value) for value in inner_audit.tolist()).issubset(fit_set):
                raise ResearchContractError("inner audit must be inside outer fit data")
            inner_fit = _strict_past_fit(
                samples,
                inner_audit,
                validation_window,
                session_embargo=session_embargo,
            )
            if not set(int(value) for value in inner_fit.tolist()).issubset(fit_set):
                raise ResearchContractError("inner fit escapes the purged outer fit set")
            if len(inner_fit) < checked_minimum_fit or len(inner_audit) < checked_minimum_audit:
                raise ResearchContractError("inner fold is underpowered by declared minima")
            assert_disjoint_partitions(inner_fit, inner_audit, audit)
            inner_folds.append(
                InnerFold(
                    validation_window=validation_window,
                    fit_indices=inner_fit,
                    audit_indices=inner_audit,
                )
            )
        if not inner_folds:
            raise ResearchContractError("at least one inner fold is required")
        result.append(
            NestedFold(
                test_window=outer_window,
                fit_indices=fit,
                audit_indices=audit,
                inner_folds=tuple(inner_folds),
            )
        )
    return tuple(result)
