from __future__ import annotations

import ast
import builtins
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import urllib.request

import numpy as np
import pytest
from scipy import io as scipy_io

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


def _fit_free_ast_violations(
    source: str,
    *,
    forbidden_import_markers: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Independently enforce the reviewed static fit-free source boundary."""

    tree = ast.parse(source)
    violations: set[str] = set()
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Import):
            origins = tuple(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = f"{'.' * node.level}{node.module or ''}"
            origins = tuple(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
        else:
            origins = ()
        for origin in origins:
            components = tuple(part for part in origin.casefold().split(".") if part)
            if "fit" in components:
                violations.add(f"fit_import:{origin}:{line}")
            if any(marker.casefold() in origin.casefold() for marker in forbidden_import_markers):
                violations.add(f"forbidden_import:{origin}:{line}")

        if isinstance(node, ast.Name) and node.id.casefold() == "fit":
            violations.add(f"fit_name:{line}")
        elif isinstance(node, ast.Attribute) and node.attr.casefold() == "fit":
            violations.add(f"fit_attribute:{line}")
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.casefold() == "fit"
        ):
            violations.add(f"fit_dynamic_literal:{line}")
    return tuple(sorted(violations))


@contextmanager
def _install_executor_io_guards(
    monkeypatch: pytest.MonkeyPatch,
    *,
    modules: tuple[object, ...],
) -> Iterator[None]:
    """Intercept reviewed runtime I/O capabilities and already-imported aliases."""

    targets = (
        ("builtins.open", builtins, "open"),
        ("os.open", os, "open"),
        ("os.fdopen", os, "fdopen"),
        ("Path.open", Path, "open"),
        ("Path.read_bytes", Path, "read_bytes"),
        ("Path.read_text", Path, "read_text"),
        ("Path.write_bytes", Path, "write_bytes"),
        ("Path.write_text", Path, "write_text"),
        ("socket.socket", socket, "socket"),
        ("socket.create_connection", socket, "create_connection"),
        ("subprocess.Popen", subprocess, "Popen"),
        ("subprocess.run", subprocess, "run"),
        ("subprocess.call", subprocess, "call"),
        ("subprocess.check_call", subprocess, "check_call"),
        ("subprocess.check_output", subprocess, "check_output"),
        ("urllib.request.urlopen", urllib.request, "urlopen"),
        ("scipy.io.loadmat", scipy_io, "loadmat"),
        ("scipy.io.savemat", scipy_io, "savemat"),
    )
    original_labels = {
        id(getattr(owner, attribute)): label
        for label, owner, attribute in targets
    }

    def guard(label: str):
        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError(
                f"synthetic public executor attempted prohibited I/O capability: {label}"
            )

        return forbidden

    for module in modules:
        for name, value in tuple(vars(module).items()):
            label = original_labels.get(id(value))
            if label is not None:
                monkeypatch.setattr(module, name, guard(f"imported_alias:{name}->{label}"))
    for label, owner, attribute in targets:
        monkeypatch.setattr(owner, attribute, guard(label))

    active = {"enabled": True}

    def audit_guard(event: str, args: tuple[object, ...]) -> None:
        if active["enabled"] and event == "open":
            raise AssertionError(
                "synthetic public executor attempted prohibited I/O capability: "
                "audit:open"
            )

    sys.addaudithook(audit_guard)
    try:
        yield
    finally:
        active["enabled"] = False


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
    signed_predictions = tuple(
        prediction
        for artifact in first.prediction_artifacts
        for prediction in artifact.predictions
    )
    positive = tuple(
        prediction
        for prediction in signed_predictions
        if prediction.expected_five_session_return > 0.0
    )
    negative = tuple(
        prediction
        for prediction in signed_predictions
        if prediction.expected_five_session_return < 0.0
    )
    assert positive
    assert negative
    assert all(prediction.p_up > prediction.p_down for prediction in positive)
    assert all(prediction.p_down > prediction.p_up for prediction in negative)

    for artifact in first.prediction_artifacts:
        assert artifact.registration.schema_version == 2
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


def test_legacy_schema_v1_registration_is_rejected_without_migration_or_relabeling() -> None:
    current = _registration()
    legacy_unsigned = {
        **current.unsigned_dict(),
        "schema_version": 1,
    }
    legacy_id = hashlib.sha256(
        json.dumps(
            legacy_unsigned,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    legacy = replace(
        current,
        schema_version=1,
        registration_id=legacy_id,
    )

    with pytest.raises(
        ResearchContractError,
        match="schema version 2.*cannot be migrated or relabeled",
    ):
        legacy.validate()


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

    module_policies = (
        (evaluator_module, ("builder", "sklearn")),
        (inference_module, ("sklearn", "research.evaluator", ".outcomes")),
    )
    for module, forbidden_import_markers in module_policies:
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert _fit_free_ast_violations(
            source,
            forbidden_import_markers=forbidden_import_markers,
        ) == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        ("fit(values)\n", "fit_name"),
        ("from estimator import fit as train\ntrain(values)\n", "fit_import"),
        ("train = model.fit\ntrain(values)\n", "fit_attribute"),
        (
            "def train(model, values):\n"
            "    return model.fit(values)\n",
            "fit_attribute",
        ),
        ("getattr(model, 'fit')(values)\n", "fit_dynamic_literal"),
        ("from sklearn.linear_model import Ridge\n", "forbidden_import"),
    ),
)
def test_fit_free_ast_oracle_rejects_representative_bypasses(
    source: str,
    expected: str,
) -> None:
    violations = _fit_free_ast_violations(
        source,
        forbidden_import_markers=("sklearn",),
    )
    assert any(item.startswith(expected) for item in violations)


def test_fit_free_ast_oracle_accepts_plain_scoring() -> None:
    assert _fit_free_ast_violations(
        "def score(weights, values):\n"
        "    return sum(weight * value for weight, value in zip(weights, values))\n"
    ) == ()


def test_cross_inner_fold_duplicate_audit_ids_fail_at_request_and_artifact_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import us_stocks_swing_model_v2.research.executor as executor_module

    original_builder = executor_module.build_frozen_outer_predictions
    captured = []

    def capture_request(request):
        captured.append(request)
        return original_builder(request)

    monkeypatch.setattr(
        executor_module,
        "build_frozen_outer_predictions",
        capture_request,
    )
    result = _execute(_dataset(), one_fold=True)
    request = captured[0]
    first_inner, second_inner = request.inner_folds
    duplicate_id = second_inner.audit_sample_ids[0]
    duplicate_position = request.fit_sample_ids.index(duplicate_id)
    duplicate_first = replace(
        first_inner,
        audit_sample_ids=(
            duplicate_id,
            *first_inner.audit_sample_ids[1:],
        ),
        audit_features=np.concatenate(
            (
                request.fit_features[duplicate_position : duplicate_position + 1],
                first_inner.audit_features[1:],
            ),
            axis=0,
        ),
        audit_targets=np.concatenate(
            (
                request.fit_targets[duplicate_position : duplicate_position + 1],
                first_inner.audit_targets[1:],
            )
        ),
    )
    duplicate_request = replace(
        request,
        inner_folds=(duplicate_first, second_inner),
    )
    with pytest.raises(ResearchContractError, match="overlap across folds"):
        original_builder(duplicate_request)

    artifact = result.prediction_artifacts[0]
    first_audit, second_audit = artifact.fit_audit.inner_folds
    duplicate_first_audit = replace(
        first_audit,
        audit_sample_ids=(
            second_audit.audit_sample_ids[0],
            *first_audit.audit_sample_ids[1:],
        ),
    )
    duplicate_fit_audit = replace(
        artifact.fit_audit,
        inner_folds=(duplicate_first_audit, second_audit),
    )
    duplicate_artifact = replace(
        artifact,
        fit_audit=duplicate_fit_audit,
    )
    with pytest.raises(ResearchContractError, match="overlap across folds"):
        duplicate_artifact.validate()


def test_execution_rejects_separately_consistent_evaluation_census_replacement() -> None:
    import us_stocks_swing_model_v2.research.executor as executor_module

    result = _execute(_dataset(), one_fold=True)
    evaluation = result.evaluations[0]
    replacement_ids = tuple(
        f"replacement-{index:04d}"
        for index in range(evaluation.evaluated_sample_count)
    )
    replacement = replace(
        evaluation,
        audit_sample_ids=replacement_ids,
        evaluation_id="",
    )
    replacement = replace(
        replacement,
        evaluation_id=hashlib.sha256(
            evaluator_module._canonical_bytes(replacement.unsigned_dict())
        ).hexdigest(),
    )
    replacement.validate()
    forged = replace(
        result,
        evaluations=(replacement,),
        execution_id="",
    )
    forged = replace(
        forged,
        execution_id=hashlib.sha256(
            executor_module._canonical_bytes(forged.unsigned_dict())
        ).hexdigest(),
    )
    with pytest.raises(ResearchContractError, match="fold bindings differ"):
        forged.validate()


def test_public_executor_runtime_capability_surface_excludes_outer_labels_and_io(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import us_stocks_swing_model_v2.research.executor as executor_module

    original_builder = executor_module.build_frozen_outer_predictions
    observed_fields: list[tuple[str, ...]] = []

    def inspect_dynamic_access(request):
        fields = tuple(vars(request))
        observed_fields.append(fields)
        assert "audit_targets" not in fields
        assert "outer_audit_targets" not in fields
        with pytest.raises(AttributeError):
            getattr(request, "audit_targets")
        with pytest.raises(AttributeError):
            getattr(request, "outer_audit_targets")
        return original_builder(request)

    monkeypatch.setattr(
        executor_module,
        "build_frozen_outer_predictions",
        inspect_dynamic_access,
    )
    imported_aliases = SimpleNamespace(
        file_alias=builtins.open,
        process_alias=subprocess.run,
        network_alias=urllib.request.urlopen,
    )
    scipy_path = tmp_path / "forbidden.mat"
    ndarray_path = tmp_path / "forbidden.bin"
    with _install_executor_io_guards(
        monkeypatch,
        modules=(
            executor_module,
            builder_module,
            evaluator_module,
            inference_module,
            imported_aliases,
        ),
    ):
        representative_attempts = (
            (lambda: builtins.open("forbidden"), "builtins.open"),
            (lambda: os.open("forbidden", os.O_RDONLY), "os.open"),
            (lambda: Path("forbidden").read_bytes(), "Path.read_bytes"),
            (lambda: subprocess.run(("forbidden",), check=False), "subprocess.run"),
            (lambda: socket.create_connection(("127.0.0.1", 1)), "socket.create_connection"),
            (lambda: urllib.request.urlopen("https://example.invalid"), "urllib.request.urlopen"),
            (lambda: imported_aliases.file_alias("forbidden"), "imported_alias:file_alias"),
            (
                lambda: imported_aliases.process_alias(("forbidden",), check=False),
                "imported_alias:process_alias",
            ),
            (
                lambda: imported_aliases.network_alias("https://example.invalid"),
                "imported_alias:network_alias",
            ),
            (
                lambda: scipy_io.savemat(scipy_path, {"x": np.asarray([1.0])}),
                "scipy.io.savemat",
            ),
            (
                lambda: np.asarray([1.0]).tofile(ndarray_path),
                "builtins.open|audit:open",
            ),
        )
        for attempt, diagnostic in representative_attempts:
            with pytest.raises(AssertionError, match=diagnostic):
                attempt()

        result = _execute(_dataset(), one_fold=True)
    assert not scipy_path.exists()
    assert not ndarray_path.exists()
    assert observed_fields
    assert result.state == "SYNTHETIC_MECHANICS_ONLY"
    assert result.alpha_evidence is False
    assert result.candidate_eligible is False


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
    assert "ready" not in readiness
    assert (
        readiness["mechanical_assessment_status"]
        == "PASS_NON_AUTHORIZING_LEGACY_DISCOVERY_ONLY"
    )
    assert readiness["historical_evidence_scope"] == "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    assert readiness["candidate_eligibility"] == "BLOCKED_PENDING_PROSPECTIVE_PIT"
    assert contract["production_evidence_anchors"]["trial_census_exact_status"] == (
        "INDETERMINATE_BLOCKS_TRUSTED_GATE"
    )
