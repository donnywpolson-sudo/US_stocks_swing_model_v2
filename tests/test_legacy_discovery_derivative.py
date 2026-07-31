from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.governance import ReleaseBinding
from us_stocks_swing_model_v2.legacy_discovery_derivative import (
    DERIVATIVE_SCOPE,
    TARGET_SEMANTICS,
    InMemoryProxyDerivative,
    ProxySourceClosure,
    load_legacy_discovery_derivative_contract,
    materialize_synthetic_proxy_derivative,
    synthetic_derivative_fixture_id,
    verify_proxy_source_closure,
)
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    build_manifest,
)
from us_stocks_swing_model_v2.research.legacy_adapter import (
    ADAPTER_SCOPE,
    LegacyDiscoveryPreregistration,
    prepare_synthetic_legacy_discovery_adapter,
    synthetic_adapter_fixture_id,
)
from us_stocks_swing_model_v2.trials import TrialSpec


REPO = Path(__file__).resolve().parents[1]
EPOCH = "hfdl_pitrading_consolidated"
OTHER_EPOCH = "hfdl_iex_only"
SERIES_ID = sha256_bytes(b"synthetic-series")
COMMON_SHA = {
    "build_id": sha256_bytes(b"build"),
    "source_hfdl_release_id": sha256_bytes(b"hfdl"),
    "source_hfdl_set_release_id": sha256_bytes(b"hfdl-set"),
    "calendar_release_id": sha256_bytes(b"calendar"),
    "contract_id": sha256_bytes(b"foundation-contract"),
}


def _provenance(kind: str, causal_release_id: str | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "build_id": COMMON_SHA["build_id"],
        "source_epoch": EPOCH,
        "output_kind": kind,
        "source_hfdl_release_id": COMMON_SHA["source_hfdl_release_id"],
        "source_hfdl_set_release_id": COMMON_SHA["source_hfdl_set_release_id"],
        "calendar_release_id": COMMON_SHA["calendar_release_id"],
        "causal_bar_release_id": causal_release_id,
        "contract_id": COMMON_SHA["contract_id"],
        "quality_state": "LEGACY_CAVEATED",
        "role": "legacy_discovery_only",
        "evidence_class": "LEGACY_DISCOVERY",
        "point_in_time_safe": False,
        "point_in_time_state": "UNRESOLVED_NOT_AS_RECEIVED",
        "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
        "source_adjustment": "hfdl_clean_source_adjusted",
        "membership_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "security_type_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "action_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "delisting_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "source_series_id_is_persistent_asset_identity": False,
        "epochs_may_be_pooled": False,
        "model_or_evaluation_inputs_read": False,
        "real_history_hypothesis_executed": False,
        "matured_outcomes_emitted": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }


def _census(kind: str) -> dict[str, object]:
    statuses = {
        "causal_bars": {"OBSERVED": 2},
        "feature_inputs": {
            "MISSING_PREVIOUS_SOURCE_SESSION_OR_EPOCH_BOUNDARY": 1,
            "PRICE_INPUT_READY_PIT_UNRESOLVED": 1,
        },
        "outcome_inputs": {
            "BLOCKED_ACTION_AND_DELISTING_EVIDENCE": 1,
            "PENDING_OR_CROSS_EPOCH_HORIZON": 1,
        },
    }[kind]
    return {
        "schema_version": 1,
        "source_epoch": EPOCH,
        "output_kind": kind,
        "source_series_count": 1,
        "source_rows": 2,
        "calendar_sessions_in_epoch": 2,
        "calendar_symbol_session_denominator": 2,
        "noncalendar_source_rows": 0,
        "output_rows": 2,
        "status_counts": statuses,
        "missing_status_rows": sum(
            count for status, count in statuses.items() if status.startswith("MISSING_")
        ),
        "evidence_denominator_rows": 2,
        "membership_evidence_available_rows": 0,
        "membership_evidence_unknown_rows": 2,
        "security_type_evidence_available_rows": 0,
        "security_type_evidence_unknown_rows": 2,
        "action_evidence_available_rows": 0,
        "action_evidence_unavailable_rows": 2,
        "delisting_evidence_available_rows": 0,
        "delisting_evidence_unavailable_rows": 2,
        "outcome_evaluable_rows": 0,
        "matured_outcome_rows": 0,
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
    }


def _publish_component(
    tmp_path: Path,
    *,
    kind: str,
    causal_release_id: str | None,
    provenance_overrides: dict[str, object] | None = None,
    census_overrides: dict[str, object] | None = None,
) -> Path:
    stage = tmp_path / "stages" / kind
    (stage / "data").mkdir(parents=True)
    provenance = _provenance(kind, causal_release_id)
    census = _census(kind)
    if provenance_overrides:
        provenance.update(provenance_overrides)
    if census_overrides:
        census.update(census_overrides)
    (stage / "provenance.json").write_bytes(canonical_json_bytes(provenance))
    (stage / "census.json").write_bytes(canonical_json_bytes(census))
    (stage / "data" / "fixture.bin").write_bytes(f"{kind}-synthetic".encode())
    manifest = build_manifest(
        stage,
        ("census.json", "data/fixture.bin", "provenance.json"),
        project="US_stocks_swing_model_v2",
        dataset=f"{EPOCH}_{kind}",
        source_epoch=EPOCH,
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
        created_at="2026-07-30T00:00:00Z",
        row_count=2,
        event_start="2020-01-02",
        event_end="2020-01-03",
        upstream_release_ids=(() if causal_release_id is None else (causal_release_id,)),
        schema_fingerprint=sha256_bytes(f"{kind}-schema".encode()),
        code_hash=sha256_bytes(b"code"),
        config_hash=sha256_bytes(b"config"),
        environment_hash=sha256_bytes(b"environment"),
    )
    return AtomicReleasePublisher(tmp_path / "accepted").publish(stage, manifest)


def _foundation_binding(
    *,
    epoch: str,
    kind: str,
    release: Path | None,
) -> dict[str, object]:
    bounds = {
        EPOCH: ("2010-01-04", "2022-03-03"),
        OTHER_EPOCH: ("2022-03-04", "2026-06-26"),
    }
    release_id = (
        release.name
        if release is not None
        else sha256_bytes(f"{epoch}:{kind}:unselected".encode())
    )
    row_count = 2 if release is not None else 0
    return {
        "dataset": f"{epoch}_{kind}",
        "epoch": epoch,
        "event_end": "2020-01-03" if release is not None else bounds[epoch][1],
        "event_start": "2020-01-02" if release is not None else bounds[epoch][0],
        "kind": kind,
        "manifest_sha256": (
            sha256_bytes((release / "release_manifest.json").read_bytes())
            if release is not None
            else sha256_bytes(f"{epoch}:{kind}:manifest".encode())
        ),
        "phase": "bridge",
        "quality_state": "LEGACY_CAVEATED",
        "relative_directory": f"{epoch}_{kind}/{release_id}",
        "release_id": release_id,
        "role": "legacy_discovery_only",
        "row_count": row_count,
        "source_epoch": epoch,
    }


def _publish_foundation_set(
    tmp_path: Path,
    releases: dict[str, Path],
) -> Path:
    epochs = {
        EPOCH: {
            kind: _foundation_binding(
                epoch=EPOCH,
                kind=kind,
                release=releases[kind],
            )
            for kind in ("causal_bars", "feature_inputs", "outcome_inputs")
        },
        OTHER_EPOCH: {
            kind: _foundation_binding(
                epoch=OTHER_EPOCH,
                kind=kind,
                release=None,
            )
            for kind in ("causal_bars", "feature_inputs", "outcome_inputs")
        },
    }
    payload = {
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
        "point_in_time_safe": False,
        "epochs_may_be_pooled": False,
        "labels_emitted": False,
        "matured_outcomes_emitted": False,
        "model_or_evaluation_inputs_read": False,
        "wfa_executed": False,
        "real_history_hypothesis_executed": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
        "contract_id": COMMON_SHA["contract_id"],
        "contract": {
            "project": "US_stocks_swing_model_v2",
            "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
            "physical_hfdl_epochs": [EPOCH, OTHER_EPOCH],
            "historical_release_kinds": [
                "causal_bars",
                "feature_inputs",
                "outcome_inputs",
            ],
            "epochs_may_be_pooled": False,
            "labels_allowed": False,
            "models_allowed": False,
            "wfa_allowed": False,
        },
        "historical_foundation": {
            "bridge_set": {},
            "build_id": COMMON_SHA["build_id"],
            "epochs": epochs,
        },
        "calendar": {
            "release": {
                "release_id": COMMON_SHA["calendar_release_id"],
            }
        },
    }
    stage = tmp_path / "stages" / "foundation_set"
    stage.mkdir(parents=True)
    (stage / "foundation_set.json").write_bytes(canonical_json_bytes(payload))
    manifest = build_manifest(
        stage,
        ("foundation_set.json",),
        project="US_stocks_swing_model_v2",
        dataset="stock_historical_foundation_set",
        source_epoch="hfdl_two_epoch_legacy_discovery_no_pooling",
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
        created_at="2026-07-30T00:00:00Z",
        row_count=6,
        event_start="2010-01-04",
        event_end="2026-06-26",
        upstream_release_ids=tuple(sorted(path.name for path in releases.values())),
        schema_fingerprint=sha256_bytes(b"foundation-set-schema"),
        code_hash=sha256_bytes(b"code"),
        config_hash=sha256_bytes(b"config"),
        environment_hash=sha256_bytes(b"environment"),
    )
    return AtomicReleasePublisher(tmp_path / "accepted").publish(stage, manifest)


def _closure(
    tmp_path: Path,
    *,
    feature_provenance_overrides: dict[str, object] | None = None,
    outcome_census_overrides: dict[str, object] | None = None,
) -> ProxySourceClosure:
    causal = _publish_component(
        tmp_path,
        kind="causal_bars",
        causal_release_id=None,
    )
    causal_release_id = causal.name
    feature = _publish_component(
        tmp_path,
        kind="feature_inputs",
        causal_release_id=causal_release_id,
        provenance_overrides=feature_provenance_overrides,
    )
    outcome = _publish_component(
        tmp_path,
        kind="outcome_inputs",
        causal_release_id=causal_release_id,
        census_overrides=outcome_census_overrides,
    )
    releases = {
        "causal_bars": causal,
        "feature_inputs": feature,
        "outcome_inputs": outcome,
    }
    foundation_set = _publish_foundation_set(tmp_path, releases)
    return verify_proxy_source_closure(
        releases,
        foundation_set_directory=foundation_set,
        accepted_root=tmp_path / "accepted",
        expected_epoch=EPOCH,
        repository_root=REPO,
    )


def _caveats() -> dict[str, object]:
    return {
        "source_epoch": EPOCH,
        "source_adjustment": "hfdl_clean_source_adjusted",
        "evidence_class": "LEGACY_DISCOVERY",
        "point_in_time_state": "UNRESOLVED_NOT_AS_RECEIVED",
        "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
        "calendar_release_id": COMMON_SHA["calendar_release_id"],
        "membership_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "security_type_evidence_status": "UNKNOWN_NOT_AS_RECEIVED",
        "action_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
        "delisting_evidence_status": "UNAVAILABLE_NOT_AS_RECEIVED",
    }


def _rows() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    features = [
        {
            "source_series_id": SERIES_ID,
            "symbol": "AAPL",
            "decision_session": date(2020, 1, 2),
            "decision_at": datetime(2020, 1, 2, 21, tzinfo=timezone.utc),
            "feature_status": "PRICE_INPUT_READY_PIT_UNRESOLVED",
            "close_to_close_return_1": 0.01,
            "intraday_return": 0.02,
            "range_fraction": 0.03,
            "log1p_volume": 10.0,
            **_caveats(),
        },
        {
            "source_series_id": SERIES_ID,
            "symbol": "AAPL",
            "decision_session": date(2020, 1, 3),
            "decision_at": datetime(2020, 1, 3, 21, tzinfo=timezone.utc),
            "feature_status": "MISSING_PREVIOUS_SOURCE_SESSION_OR_EPOCH_BOUNDARY",
            "close_to_close_return_1": None,
            "intraday_return": None,
            "range_fraction": None,
            "log1p_volume": None,
            **_caveats(),
        },
    ]
    outcomes = [
        {
            "source_series_id": SERIES_ID,
            "symbol": "AAPL",
            "decision_session": date(2020, 1, 2),
            "entry_session": date(2020, 1, 3),
            "exit_session": date(2020, 1, 9),
            "entry_open": 100.0,
            "exit_close": 105.0,
            "split_normalized_price_return": None,
            "outcome_input_status": "BLOCKED_ACTION_AND_DELISTING_EVIDENCE",
            **_caveats(),
        },
        {
            "source_series_id": SERIES_ID,
            "symbol": "AAPL",
            "decision_session": date(2020, 1, 3),
            "entry_session": date(2020, 1, 6),
            "exit_session": None,
            "entry_open": 101.0,
            "exit_close": None,
            "split_normalized_price_return": None,
            "outcome_input_status": "PENDING_OR_CROSS_EPOCH_HORIZON",
            **_caveats(),
        },
    ]
    return features, outcomes


def _derivative(tmp_path: Path) -> InMemoryProxyDerivative:
    closure = _closure(tmp_path)
    features, outcomes = _rows()
    fixture_id = synthetic_derivative_fixture_id(closure, features, outcomes)
    permit = SyntheticOnlyPermit.create(
        fixture_id=fixture_id,
        scope=DERIVATIVE_SCOPE,
    )
    return materialize_synthetic_proxy_derivative(
        closure,
        features,
        outcomes,
        permit=permit,
    )


def _trial_spec(derivative: InMemoryProxyDerivative) -> TrialSpec:
    binding = ReleaseBinding(
        release_id=derivative.derivative_id,
        project="US_stocks_swing_model_v2",
        dataset="synthetic_proxy_derivative",
        source_epoch=derivative.source_epoch,
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
        created_at="2026-07-30T00:00:00Z",
        event_start="2020-01-02",
        event_end="2020-01-03",
    )
    return TrialSpec(
        hypothesis_id="synthetic-proxy-mechanics",
        evidence_class="REGISTERED_HISTORICAL_DISCOVERY",
        data_release_ids=(derivative.derivative_id,),
        release_bindings=(binding,),
        feature_schema_id=sha256_bytes(b"feature-spec"),
        outcome_schema_id=sha256_bytes(b"proxy-label-spec"),
        split_plan_id=sha256_bytes(b"split-spec"),
        model_family="synthetic-no-model",
        primary_metric="synthetic-no-metric",
        primary_gate_id=sha256_bytes(b"primary-gate"),
        robustness_policy_id=sha256_bytes(b"robustness"),
        cost_policy_id=sha256_bytes(b"cost"),
        trial_family_id="synthetic-proxy-family",
        census_anchor_id=sha256_bytes(b"census"),
        trial_family_anchor_id=sha256_bytes(b"family-anchor"),
        evaluator_closure_hash=sha256_bytes(b"evaluator-closure"),
        governance_contract_hash=sha256_bytes(b"governance"),
        code_hash=sha256_bytes(b"code"),
        config_hash=sha256_bytes(b"config"),
        environment_hash=sha256_bytes(b"environment"),
    )


def _registration(
    derivative: InMemoryProxyDerivative,
) -> LegacyDiscoveryPreregistration:
    return LegacyDiscoveryPreregistration.create(
        derivative=derivative,
        trial_spec=_trial_spec(derivative),
        trial_declaration_id=sha256_bytes(b"declaration"),
        trial_registry_binding_id=sha256_bytes(b"registry-binding"),
        trial_ledger_head_id=sha256_bytes(b"ledger-head"),
        charter_id=sha256_bytes(b"charter"),
        code_commit="a" * 40,
    )


def test_contract_is_content_addressed_and_non_authorizing() -> None:
    contract, contract_id = load_legacy_discovery_derivative_contract(REPO)
    unsigned = {key: value for key, value in contract.items() if key != "contract_id"}
    assert contract_id == sha256_bytes(canonical_json_bytes(unsigned))
    assert not any(contract["authorities"].values())
    assert contract["preregistered_adapter"]["executor_entrypoint"] is None
    assert contract["proxy_derivative"]["trusted_sleeves"] == []


def test_full_synthetic_release_closure_verifies_payload_and_provenance(
    tmp_path: Path,
) -> None:
    closure = _closure(tmp_path)
    assert tuple(item.kind for item in closure.components) == (
        "causal_bars",
        "feature_inputs",
        "outcome_inputs",
    )
    assert closure.source_epoch == EPOCH
    assert closure.components[1].causal_bar_release_id == closure.components[0].release_id
    assert closure.components[1].row_count == closure.components[2].row_count == 2


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"point_in_time_safe": True}, "discovery-only boundary"),
        ({"epochs_may_be_pooled": True}, "discovery-only boundary"),
        ({"model_or_evaluation_inputs_read": True}, "discovery-only boundary"),
        ({"source_adjustment": "raw"}, "discovery-only boundary"),
        ({"calendar_release_id": "f" * 64}, "closure differs"),
    ],
)
def test_source_closure_rejects_provenance_drift(
    tmp_path: Path,
    overrides: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(IntegrityError, match=match):
        _closure(tmp_path, feature_provenance_overrides=overrides)


def test_source_closure_rejects_false_evaluable_census(tmp_path: Path) -> None:
    with pytest.raises(IntegrityError, match="coverage"):
        _closure(
            tmp_path,
            outcome_census_overrides={"outcome_evaluable_rows": 1},
        )


def test_synthetic_derivative_is_deterministic_preserves_rows_and_labels_proxy(
    tmp_path: Path,
) -> None:
    derivative = _derivative(tmp_path)
    assert len(derivative.wfa_samples) == 2
    assert derivative.wfa_samples[0].proxy_return == pytest.approx(0.05)
    assert derivative.wfa_samples[0].mechanically_complete is True
    assert derivative.wfa_samples[1].proxy_return is None
    assert derivative.wfa_samples[1].mechanically_complete is False
    assert derivative.outcome_rows[0].target_semantics == TARGET_SEMANTICS
    assert derivative.outcome_rows[0].canonical_split_normalized_target_equivalent is False
    assert derivative.trusted_sleeves == ()
    assert derivative.real_history_execution_authorized is False
    assert derivative.candidate_eligible is False
    assert derivative.alpha_evidence is False


def test_derivative_rejects_schema_tampering_unsorted_rows_and_matured_target(
    tmp_path: Path,
) -> None:
    closure = _closure(tmp_path)
    features, outcomes = _rows()
    poisoned_features = deepcopy(features)
    poisoned_features[0]["future_return"] = 1.0
    permit = SyntheticOnlyPermit.create(
        fixture_id=synthetic_derivative_fixture_id(
            closure, poisoned_features, outcomes
        ),
        scope=DERIVATIVE_SCOPE,
    )
    with pytest.raises(ContractError, match="fields differ"):
        materialize_synthetic_proxy_derivative(
            closure, poisoned_features, outcomes, permit=permit
        )

    reversed_features = list(reversed(features))
    permit = SyntheticOnlyPermit.create(
        fixture_id=synthetic_derivative_fixture_id(
            closure, reversed_features, outcomes
        ),
        scope=DERIVATIVE_SCOPE,
    )
    with pytest.raises(ContractError, match="sorted unique"):
        materialize_synthetic_proxy_derivative(
            closure, reversed_features, outcomes, permit=permit
        )

    poisoned_outcomes = deepcopy(outcomes)
    poisoned_outcomes[0]["split_normalized_price_return"] = 0.05
    permit = SyntheticOnlyPermit.create(
        fixture_id=synthetic_derivative_fixture_id(
            closure, features, poisoned_outcomes
        ),
        scope=DERIVATIVE_SCOPE,
    )
    with pytest.raises(ContractError, match="matured target"):
        materialize_synthetic_proxy_derivative(
            closure, features, poisoned_outcomes, permit=permit
        )


def test_derivative_requires_exact_synthetic_permit(tmp_path: Path) -> None:
    closure = _closure(tmp_path)
    features, outcomes = _rows()
    with pytest.raises(ContractError, match="synthetic-only permit"):
        materialize_synthetic_proxy_derivative(
            closure, features, outcomes, permit=None
        )
    wrong = SyntheticOnlyPermit.create(
        fixture_id=sha256_bytes(b"other-fixture"),
        scope=DERIVATIVE_SCOPE,
    )
    with pytest.raises(ContractError, match="exact derivative fixture"):
        materialize_synthetic_proxy_derivative(
            closure, features, outcomes, permit=wrong
        )


def test_preregistered_adapter_binds_exact_trial_and_exposes_no_executor(
    tmp_path: Path,
) -> None:
    derivative = _derivative(tmp_path)
    registration = _registration(derivative)
    fixture_id = synthetic_adapter_fixture_id(derivative, registration)
    permit = SyntheticOnlyPermit.create(
        fixture_id=fixture_id,
        scope=ADAPTER_SCOPE,
    )
    adapter = prepare_synthetic_legacy_discovery_adapter(
        derivative,
        registration,
        permit=permit,
    )
    assert adapter.mode == "PREREGISTERED_INPUT_ADAPTER_ONLY"
    assert adapter.sample_count == 2
    assert adapter.mechanically_complete_sample_count == 1
    assert adapter.executor_entrypoint is None
    assert adapter.real_history_execution_authorized is False
    assert adapter.generated_evidence_eligible is False
    assert adapter.trusted_gate_eligible is False
    assert adapter.candidate_eligible is False
    assert adapter.alpha_evidence is False
    assert not hasattr(adapter, "execute")
    assert not hasattr(adapter, "fit")
    assert not hasattr(adapter, "evaluate")


def test_preregistration_rejects_nonregistered_or_wrong_derivative_trial(
    tmp_path: Path,
) -> None:
    derivative = _derivative(tmp_path)
    trial = _trial_spec(derivative)
    wrong_binding = ReleaseBinding(
        **{
            **trial.release_bindings[0].__dict__,
            "release_id": sha256_bytes(b"wrong-derivative"),
        }
    )
    wrong = TrialSpec(
        **{
            **trial.__dict__,
            "data_release_ids": (wrong_binding.release_id,),
            "release_bindings": (wrong_binding,),
        }
    )
    with pytest.raises(ContractError, match="does not bind"):
        LegacyDiscoveryPreregistration.create(
            derivative=derivative,
            trial_spec=wrong,
            trial_declaration_id=sha256_bytes(b"declaration"),
            trial_registry_binding_id=sha256_bytes(b"registry"),
            trial_ledger_head_id=sha256_bytes(b"ledger"),
            charter_id=sha256_bytes(b"charter"),
            code_commit="a" * 40,
        )


def test_adapter_requires_exact_synthetic_permit(tmp_path: Path) -> None:
    derivative = _derivative(tmp_path)
    registration = _registration(derivative)
    with pytest.raises(ContractError, match="synthetic-only permit"):
        prepare_synthetic_legacy_discovery_adapter(
            derivative,
            registration,
            permit=None,
        )


def test_checked_in_mechanics_surface_has_no_real_reader_writer_or_executor() -> None:
    derivative_source = (
        REPO / "src/us_stocks_swing_model_v2/legacy_discovery_derivative.py"
    ).read_text(encoding="utf-8")
    adapter_source = (
        REPO / "src/us_stocks_swing_model_v2/research/legacy_adapter.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "pyarrow",
        "read_table",
        "read_parquet",
        "AtomicReleasePublisher",
        "atomic_write",
        "open_without_redirects",
        ".fit(",
        "execute_synthetic_nested_wfa",
        "evaluate_frozen_predictions",
    ):
        assert forbidden not in derivative_source
        assert forbidden not in adapter_source
