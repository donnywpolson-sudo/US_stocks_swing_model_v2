from __future__ import annotations

import ast
from dataclasses import replace
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import us_stocks_swing_model_v2.inference as inference_module
from us_stocks_swing_model_v2.inference import LinearDistributionArtifact
from us_stocks_swing_model_v2.research import (
    DIRECTION_SEMANTICS,
    ExecutorRegistration,
    ResearchContractError,
    SessionWindow,
    SyntheticNestedWfaPlan,
    SyntheticResearchDataset,
    TemporalSamples,
    execute_synthetic_nested_wfa,
    make_synthetic_permit,
    synthetic_fixture_vector,
)
from us_stocks_swing_model_v2.research import evaluator as evaluator_module
from us_stocks_swing_model_v2.research import builder as builder_module
from us_stocks_swing_model_v2.research.builder import build_frozen_outer_predictions


def _dataset(*, seed: int = 17, signal: bool = True, n: int = 110) -> SyntheticResearchDataset:
    rng = np.random.default_rng(seed)
    features = rng.normal(size=(n, 3)).astype(np.float64)
    if signal:
        targets = (
            0.035 * features[:, 0]
            - 0.018 * features[:, 1]
            + 0.006 * features[:, 2]
            + rng.normal(scale=0.002, size=n)
        ).astype(np.float64)
    else:
        targets = rng.normal(scale=0.02, size=n).astype(np.float64)
    decisions = np.arange(n, dtype=np.int64)
    return SyntheticResearchDataset(
        sample_ids=tuple(f"sample-{value:04d}" for value in range(n)),
        feature_names=("momentum", "reversal", "volatility"),
        features=features,
        targets=targets,
        temporal_samples=TemporalSamples(
            decision_session=decisions,
            label_start=decisions + 1,
            label_end=decisions + 6,
            label_known_session=decisions + 6,
        ),
    )


def _registration() -> ExecutorRegistration:
    return ExecutorRegistration.create(
        feature_schema_id="a" * 64,
        feature_names=("momentum", "reversal", "volatility"),
        ridge_alphas=(0.01, 0.1, 1.0, 10.0),
        neutral_band=0.005,
        uncertainty_floor=0.001,
    )


def _plan(*, one_fold: bool = False) -> SyntheticNestedWfaPlan:
    outer = (SessionWindow(70, 80),)
    inner = ((SessionWindow(35, 40), SessionWindow(50, 55)),)
    if not one_fold:
        outer += (SessionWindow(85, 95), SessionWindow(100, 110))
        inner += (
            (SessionWindow(45, 50), SessionWindow(65, 70)),
            (SessionWindow(60, 65), SessionWindow(80, 85)),
        )
    return SyntheticNestedWfaPlan(
        outer_test_windows=outer,
        inner_validation_windows=inner,
        session_embargo=5,
        minimum_fit_samples=20,
        minimum_audit_samples=5,
    )


def _execute(dataset: SyntheticResearchDataset, *, one_fold: bool = False):
    permit = make_synthetic_permit(
        synthetic_fixture_vector(dataset),
        generator_id="deterministic-linear-fixture-v1",
        seed=17,
    )
    return execute_synthetic_nested_wfa(
        dataset,
        permit=permit,
        registration=_registration(),
        plan=_plan(one_fold=one_fold),
    )


def _outer_targets(dataset: SyntheticResearchDataset, plan: SyntheticNestedWfaPlan) -> np.ndarray:
    masks = [
        (dataset.temporal_samples.decision_session >= window.start)
        & (dataset.temporal_samples.decision_session < window.stop)
        for window in plan.outer_test_windows
    ]
    return dataset.targets[np.logical_or.reduce(masks)]


def test_signal_executor_is_deterministic_absolute_and_linear_distribution_compatible(
    tmp_path: Path,
) -> None:
    dataset = _dataset()
    first = _execute(dataset)
    second = _execute(dataset)
    assert first == second
    assert first.state == "SYNTHETIC_MECHANICS_ONLY"
    assert first.real_history_authorized is False
    assert first.alpha_evidence is False
    assert first.candidate_eligible is False

    predicted = np.asarray(
        [
            prediction.expected_five_session_return
            for artifact in first.prediction_artifacts
            for prediction in artifact.predictions
        ],
        dtype=np.float64,
    )
    actual = _outer_targets(dataset, first.plan)
    assert np.corrcoef(predicted, actual)[0, 1] > 0.98
    assert np.mean(np.square(predicted - actual)) < 0.05 * np.var(actual)

    for artifact in first.prediction_artifacts:
        assert artifact.registration.direction_semantics == DIRECTION_SEMANTICS
        assert artifact.rank_used_as_direction is False
        assert not set(artifact.predictions[0].as_dict()) & {"rank", "direction", "bullish", "bearish"}
        assert not set(artifact.fit_audit.outer_fit_sample_ids) & set(
            artifact.fit_audit.outer_audit_sample_ids
        )
        for prediction in artifact.predictions:
            assert prediction.p_up + prediction.p_down + prediction.p_neutral == pytest.approx(1.0)
        model_path = tmp_path / f"model-{artifact.outer_fold_number}.json"
        model_path.write_text(json.dumps(artifact.model.as_dict()), encoding="utf-8")
        loaded = LinearDistributionArtifact.load(
            model_path,
            SimpleNamespace(feature_schema_id="a" * 64, feature_names=dataset.feature_names),
        )
        assert tuple(loaded.coefficients) == dataset.feature_names


def test_noise_fixture_never_becomes_alpha_or_candidate_evidence() -> None:
    dataset = _dataset(seed=211, signal=False)
    result = _execute(dataset)
    predicted = np.asarray(
        [
            prediction.expected_five_session_return
            for artifact in result.prediction_artifacts
            for prediction in artifact.predictions
        ],
        dtype=np.float64,
    )
    actual = _outer_targets(dataset, result.plan)
    assert abs(np.corrcoef(predicted, actual)[0, 1]) < 0.5
    assert result.state == "SYNTHETIC_MECHANICS_ONLY"
    assert result.alpha_evidence is False
    assert result.candidate_eligible is False
    assert all(evaluation.candidate_eligible is False for evaluation in result.evaluations)


def test_outer_label_poison_cannot_change_frozen_model_selection_or_predictions() -> None:
    baseline = _dataset()
    poisoned_targets = baseline.targets.copy()
    poisoned_targets[70:80] += 10.0
    poisoned = replace(baseline, targets=poisoned_targets)
    original = _execute(baseline, one_fold=True)
    attacked = _execute(poisoned, one_fold=True)
    assert attacked.prediction_artifacts == original.prediction_artifacts
    assert attacked.prediction_artifacts[0].artifact_id == original.prediction_artifacts[0].artifact_id
    assert attacked.evaluations[0].evaluation_id != original.evaluations[0].evaluation_id
    assert attacked.evaluations[0].mean_squared_error > original.evaluations[0].mean_squared_error


def test_outer_feature_poison_does_not_fit_scaler_or_model_on_audit_rows() -> None:
    baseline = _dataset()
    poisoned_features = baseline.features.copy()
    poisoned_features[70:80] += 1_000_000.0
    poisoned = replace(baseline, features=poisoned_features)
    original = _execute(baseline, one_fold=True).prediction_artifacts[0]
    attacked = _execute(poisoned, one_fold=True).prediction_artifacts[0]
    assert attacked.fit_audit == original.fit_audit
    assert attacked.model == original.model
    assert attacked.predictions != original.predictions


def test_role_isolation_overlap_artifact_tamper_and_fit_free_modules_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dataset = _dataset()
    captured = []
    import us_stocks_swing_model_v2.research.executor as executor_module

    original_builder = executor_module.build_frozen_outer_predictions
    original_phase_one = executor_module._build_phase_one_artifacts
    phase_plans = []

    def inspect_phase_one(plan):
        assert set(plan.__dataclass_fields__) == {"requests"}
        assert all(not hasattr(request, "audit_targets") for request in plan.requests)
        phase_plans.append(plan)
        return original_phase_one(plan)

    def inspect_request(request):
        assert not hasattr(request, "audit_targets")
        captured.append(request)
        return original_builder(request)

    monkeypatch.setattr(executor_module, "build_frozen_outer_predictions", inspect_request)
    monkeypatch.setattr(executor_module, "_build_phase_one_artifacts", inspect_phase_one)
    result = _execute(dataset, one_fold=True)
    assert len(phase_plans) == 1
    request = captured[0]
    overlapping = replace(
        request,
        audit_sample_ids=(request.fit_sample_ids[0], *request.audit_sample_ids[1:]),
    )
    with pytest.raises(ResearchContractError, match="overlap"):
        build_frozen_outer_predictions(overlapping)

    artifact = result.prediction_artifacts[0]
    changed_prediction = replace(
        artifact.predictions[0],
        expected_five_session_return=artifact.predictions[0].expected_five_session_return + 0.1,
    )
    tampered = replace(artifact, predictions=(changed_prediction, *artifact.predictions[1:]))
    with pytest.raises(ResearchContractError, match="artifact ID differs"):
        tampered.validate()

    for module in (evaluator_module, inference_module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        fit_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "fit"
        ]
        assert not fit_calls
        if module is evaluator_module:
            assert not any("builder" in name or "sklearn" in name for name in imports)


def test_ridge_solver_failure_is_a_controlled_research_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_solve(*args: object, **kwargs: object) -> np.ndarray:
        raise np.linalg.LinAlgError("synthetic singular system")

    monkeypatch.setattr(builder_module.np.linalg, "solve", fail_solve)
    with pytest.raises(ResearchContractError, match="system is not solvable"):
        _execute(_dataset(), one_fold=True)


def test_extreme_finite_research_inputs_fail_as_controlled_contract_errors() -> None:
    dataset = _dataset()
    extreme_features = dataset.features.copy()
    extreme_features[:70] = np.finfo(np.float64).max
    with pytest.raises(ResearchContractError, match="finite float64 bounds"):
        _execute(replace(dataset, features=extreme_features), one_fold=True)

    result = _execute(dataset, one_fold=True)
    artifact = result.prediction_artifacts[0]
    with pytest.raises(ResearchContractError, match="MSE exceeded finite float64 bounds"):
        evaluator_module.evaluate_frozen_predictions(
            artifact,
            audit_sample_ids=artifact.fit_audit.outer_audit_sample_ids,
            audit_targets=np.full(
                len(artifact.fit_audit.outer_audit_sample_ids),
                np.finfo(np.float64).max,
                dtype=np.float64,
            ),
        )


def test_registered_executor_status_is_mechanical_only_and_preserves_all_blockers() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (root / "config" / "research_readiness_contract.json").read_text(encoding="utf-8")
    )
    registered = contract["registered_mechanical_executor"]
    module_name, function_name = registered["entrypoint"].split(":", maxsplit=1)
    assert getattr(importlib.import_module(module_name), function_name) is execute_synthetic_nested_wfa
    assert registered["implementation_status"] == "IMPLEMENTED_SYNTHETIC_ADVERSARIAL_TESTED"
    assert registered["target_semantics"] == DIRECTION_SEMANTICS
    assert registered["rank_used_as_direction"] is False
    assert registered["real_history_authorized"] is False
    assert registered["alpha_evidence"] is False
    assert registered["candidate_eligible"] is False
    readiness = contract["readiness"]
    assert readiness["ready"] is True
    assert readiness["historical_evidence_scope"] == "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    assert readiness["candidate_eligibility"] == "BLOCKED_PENDING_PROSPECTIVE_PIT"
    assert contract["production_evidence_anchors"]["trial_census_exact_status"] == (
        "INDETERMINATE_BLOCKS_TRUSTED_GATE"
    )
