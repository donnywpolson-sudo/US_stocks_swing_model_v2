from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.external_strategy_census import (
    SOURCE_KINDS,
    CensusSourceEvidence,
    HistoricalTrialCensusAssessment,
    build_historical_trial_census_assessment,
)
from us_stocks_swing_model_v2.external_strategy_intake import (
    COMPATIBLE_ADAPTER_ID,
    COMPATIBLE_ADAPTER_PARAMETERS,
    EXACT_FEATURE_NAMES,
    ExternalStrategySpec,
    ProjectTrialBindings,
    ProspectiveCandidateBindings,
    build_external_strategy_intake_plan,
    build_prospective_paper_test_plan,
    build_trial_spec_from_intake,
)
from us_stocks_swing_model_v2.governance import ReleaseBinding
from us_stocks_swing_model_v2.monitoring_policy import frozen_monitoring_policy_hash


def frozen_spec(
    *,
    holding_sessions: int = 5,
    model_family: str = "linear_distribution_v1",
    feature_schema: str = "prospective_price_only_v1",
    feature_names: list[str] | None = None,
    census_complete: bool = False,
) -> dict[str, object]:
    unsigned = {
        "schema_version": 1,
        "mode": "DISCOVERY_ONLY_FROZEN_STRATEGY_SPEC",
        "target_project": "US_stocks_swing_model_v2",
        "hypothesis": {
            "adapter_id": COMPATIBLE_ADAPTER_ID,
            "parameters": {**COMPATIBLE_ADAPTER_PARAMETERS, "holding_sessions": holding_sessions},
            "requested_model_family": model_family,
            "primary_metric": "multiclass_log_loss",
            "required_feature_schema_id": feature_schema,
            "required_feature_names": list(EXACT_FEATURE_NAMES) if feature_names is None else feature_names,
            "outcome_semantics": "D1_OPEN_TO_D5_CLOSE_SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN",
        },
        "timing": {
            "decision": "AFTER_D0_CLOSE",
            "entry": "D1_OPEN",
            "exit": "D5_CLOSE" if holding_sessions == 5 else "D10_CLOSE",
            "holding_sessions": holding_sessions,
        },
        "universe": {"project_eligible_universe_required": True, "scout_universe_hash": "a" * 64},
        "cost_policy": {"one_way_bps": [0, 10, 25, 50], "binding_bps": 25},
        "trial_family": {
            "trial_family_id": "breakout-family-v1",
            "run_configuration_count": 1,
            "evaluated_cost_curve_count": 4,
            "prior_exposure_floor": 64,
            "exact_census_complete": census_complete,
        },
        "discovery_lineage": {"screening_run_id": "b" * 64, "gate_receipt_id": "c" * 64},
        "claims": {
            "discovery_only": True,
            "contains_market_data": False,
            "contains_performance": False,
            "alpha_claim": False,
            "registration_authority": False,
            "prospective_authority": False,
        },
    }
    return {**unsigned, "spec_id": sha256_bytes(canonical_json_bytes(unsigned))}


def load(value: dict[str, object]) -> ExternalStrategySpec:
    return ExternalStrategySpec.from_bytes(canonical_json_bytes(value))


def census_for(spec: ExternalStrategySpec, *, complete: bool = True) -> HistoricalTrialCensusAssessment:
    sources = tuple(
        CensusSourceEvidence(
            source_kind=name,
            locator_sha256=f"{index:064x}",
            inspected=complete,
            outcome_informed_attempt_count=16 if complete else None,
        )
        for index, name in enumerate(SOURCE_KINDS, start=1)
    )
    return build_historical_trial_census_assessment(
        candidate_spec_id=spec.spec_id,
        trial_family_id=str(spec.trial_family["trial_family_id"]),
        evidence_cutoff_utc="2026-08-02T00:00:00Z",
        sources=sources,
        documented_prior_floor=int(spec.trial_family["prior_exposure_floor"]),
        unresolved_attempt_count=0 if complete else 1,
        exact_global_outcome_informed_attempt_count=64 if complete else None,
        exact_family_outcome_informed_attempt_count=1 if complete else None,
    )


def binding(dataset: str, number: int) -> ReleaseBinding:
    roles = {
        "identity": "prospective_as_received",
        "alpaca_daily_bars": "active_historical",
        "corporate_actions": "prospective_as_received",
        "xnys_sessions": "derived_causal",
        "eligible_universe": "derived_causal",
        "features": "feature_only",
        "outcomes": "outcome_only",
    }
    epochs = {
        "identity": "nasdaq_alpaca_active_us_equity_v1",
        "alpaca_daily_bars": "alpaca_basic_sip_raw_v1",
        "corporate_actions": "alpaca_corporate_actions_v1",
    }
    return ReleaseBinding(
        release_id=f"{number:064x}",
        project="US_stocks_swing_model_v2",
        dataset=dataset,
        source_epoch=epochs.get(dataset, "prospective-v1"),
        role=roles[dataset],
        quality_state="PASS",
        created_at="2026-01-01T00:00:00Z",
        event_start="2020-01-01",
        event_end="2025-12-31",
    )


def all_bindings() -> tuple[ReleaseBinding, ...]:
    names = ["identity", "alpaca_daily_bars", "corporate_actions", "xnys_sessions", "eligible_universe", "features", "outcomes"]
    return tuple(sorted((binding(name, index + 1) for index, name in enumerate(names)), key=lambda item: item.release_id))


def test_valid_spec_is_data_free_but_census_blocks_current_intake(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    plan = build_external_strategy_intake_plan(spec, repository_root=project_root)
    assert plan["status"] == "BLOCKED_EXACT_CENSUS"
    assert plan["authorities"] == {
        "registration": False,
        "outcome_access": False,
        "training": False,
        "evaluation": False,
        "candidate_sealing": False,
        "prospective_execution": False,
    }


def test_indeterminate_project_census_blocks_intake(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    plan = build_external_strategy_intake_plan(
        spec,
        repository_root=project_root,
        census_assessment=census_for(spec, complete=False),
    )
    assert plan["status"] == "BLOCKED_EXACT_CENSUS"


def test_project_census_is_content_addressed_and_candidate_bound(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    census = census_for(spec)
    tampered = dict(census.payload)
    tampered["counts"] = {**tampered["counts"], "exact_global_outcome_informed_attempt_count": 65}
    with pytest.raises(ContractError, match="anchor"):
        HistoricalTrialCensusAssessment.from_bytes(canonical_json_bytes(tampered))
    other = load(frozen_spec(census_complete=True))
    other.payload["trial_family"]["trial_family_id"] = "different-family"
    with pytest.raises(ContractError, match="does not bind"):
        build_external_strategy_intake_plan(other, repository_root=project_root, census_assessment=census)


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"holding_sessions": 10, "census_complete": True}, "REJECTED_NON_D5"),
        ({"model_family": "deterministic_rule_v1", "census_complete": True}, "SCOUT_ONLY_INCOMPATIBLE_MODEL"),
        ({"feature_schema": "breakout_20_200_v1", "census_complete": True}, "SCOUT_ONLY_INCOMPATIBLE_FEATURE_SCHEMA"),
    ],
)
def test_compatibility_statuses_are_explicit(project_root: Path, kwargs: dict[str, object], status: str) -> None:
    plan = build_external_strategy_intake_plan(load(frozen_spec(**kwargs)), repository_root=project_root)
    assert plan["status"] == status


def test_donchian_adapter_remains_scout_only(project_root: Path) -> None:
    value = frozen_spec(census_complete=True)
    value["hypothesis"]["adapter_id"] = "donchian_breakout_v1"
    value["hypothesis"]["parameters"] = {"breakout_lookback": 20, "trend_sma_sessions": 200, "holding_sessions": 5}
    value["spec_id"] = sha256_bytes(canonical_json_bytes({key: item for key, item in value.items() if key != "spec_id"}))
    assert build_external_strategy_intake_plan(load(value), repository_root=project_root)["status"] == "SCOUT_ONLY_INCOMPATIBLE_MODEL"


def test_compatible_complete_spec_still_requires_accepted_releases(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    plan = build_external_strategy_intake_plan(
        spec, repository_root=project_root, census_assessment=census_for(spec)
    )
    assert plan["status"] == "BLOCKED_ACCEPTED_RELEASES"
    assert plan["release_readiness"]["status"] == "WAITING_PROSPECTIVE_EVIDENCE_HORIZON"
    assert plan["release_readiness"]["minimum_total_sessions"] == 2268


def test_current_registry_blocks_after_release_verification(
    project_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.external_strategy_intake.verify_release_bindings",
        lambda *args, **kwargs: all_bindings(),
    )
    spec = load(frozen_spec(census_complete=True))
    plan = build_external_strategy_intake_plan(
        spec,
        repository_root=project_root,
        census_assessment=census_for(spec),
        accepted_release_directories=[project_root],
        accepted_release_root=project_root,
    )
    assert plan["status"] == "BLOCKED_EXTERNAL_REGISTRY"
    assert len(plan["accepted_release_ids"]) == 7


def test_release_role_mismatch_remains_blocked(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = load(frozen_spec(census_complete=True))
    values = list(all_bindings())
    values[0] = ReleaseBinding(**{**values[0].__dict__, "role": "legacy_discovery_only"})
    monkeypatch.setattr("us_stocks_swing_model_v2.external_strategy_intake.verify_release_bindings", lambda *args, **kwargs: tuple(values))
    plan = build_external_strategy_intake_plan(spec, repository_root=project_root, census_assessment=census_for(spec), accepted_release_directories=[project_root], accepted_release_root=project_root)
    assert plan["status"] == "BLOCKED_ACCEPTED_RELEASES"


def test_trial_spec_mapping_uses_project_contract_hashes(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    census = census_for(spec)
    readiness = json.loads((project_root / "config/research_readiness_contract.json").read_text(encoding="utf-8"))
    governance_hash = sha256_bytes(canonical_json_bytes(readiness))
    values = ProjectTrialBindings(
        census_anchor_id=census.census_anchor_id,
        trial_family_anchor_id=census.trial_family_anchor_id,
        evaluator_closure_hash="3" * 64,
        governance_contract_hash=governance_hash,
        code_hash="4" * 64,
        config_hash="5" * 64,
        environment_hash="6" * 64,
    )
    trial = build_trial_spec_from_intake(
        spec,
        release_bindings=all_bindings(),
        census_assessment=census,
        project_bindings=values,
        repository_root=project_root,
    )
    assert trial.hypothesis_id == spec.spec_id
    assert trial.model_family == "linear_distribution_v1"
    assert trial.evidence_class == "PROSPECTIVE_FINAL"


def test_prospective_plan_is_content_bound_and_non_authorizing(project_root: Path) -> None:
    spec = load(frozen_spec(census_complete=True))
    census = census_for(spec)
    releases = [item.release_id for item in all_bindings()]
    readiness = json.loads((project_root / "config/research_readiness_contract.json").read_text(encoding="utf-8"))
    governance_hash = sha256_bytes(canonical_json_bytes(readiness))
    trial = build_trial_spec_from_intake(
        spec,
        release_bindings=all_bindings(),
        census_assessment=census,
        project_bindings=ProjectTrialBindings(
            census_anchor_id=census.census_anchor_id,
            trial_family_anchor_id=census.trial_family_anchor_id,
            evaluator_closure_hash="3" * 64,
            governance_contract_hash=governance_hash,
            code_hash="4" * 64,
            config_hash="5" * 64,
            environment_hash="6" * 64,
        ),
        repository_root=project_root,
    )
    unsigned_intake = {
        "schema_version": 1,
        "mode": "STOCKS_V2_EXTERNAL_STRATEGY_INTAKE_PLAN_ONLY",
        "spec_id": spec.spec_id,
        "census_assessment_id": census.assessment_id,
        "status": "READY_FOR_SEPARATELY_AUTHORIZED_REGISTRATION",
        "accepted_release_ids": releases,
        "release_readiness": {"status": "COMPLETE", "minimum_total_sessions": 2268, "missing_datasets": [], "invalid_contracts": [], "legacy_or_proxy_substitution_allowed": False, "aapl_spy_smoke_satisfies_horizon": False},
        "registry_policy_id": "7" * 64,
        "project_contract": {
            "model_family": "linear_distribution_v1",
            "feature_schema_id": "prospective_price_only_v1",
            "feature_names": list(EXACT_FEATURE_NAMES),
            "outcome_schema_id": "d1_open_to_d5_close_split_normalized_v1",
            "primary_metric": "multiclass_log_loss",
        },
        "authorities": {
            "registration": False,
            "outcome_access": False,
            "training": False,
            "evaluation": False,
            "candidate_sealing": False,
            "prospective_execution": False,
        },
    }
    intake = {**unsigned_intake, "intake_plan_id": sha256_bytes(canonical_json_bytes(unsigned_intake))}
    bindings = ProspectiveCandidateBindings(
        trial_id=trial.trial_id,
        registration_hash="9" * 64,
        trial_registry_binding_id="a" * 64,
        candidate_id="b" * 64,
        bundle_id="c" * 64,
        monitoring_policy_hash=frozen_monitoring_policy_hash(),
        accepted_release_ids=tuple(releases),
    )
    plan = build_prospective_paper_test_plan(
        spec, intake_plan=intake, trial_spec=trial, bindings=bindings
    )
    assert plan["status"] == "BLOCKED_PENDING_SEPARATE_AUTHORITIES"
    assert not any(plan["authorities"].values())


def test_parser_rejects_results_paths_unknown_fields_and_oversize() -> None:
    value = frozen_spec()
    value["metrics"] = {"cagr": 1.0}
    with pytest.raises(ContractError):
        load(value)
    value = frozen_spec()
    value["hypothesis"]["parameters"]["source"] = "C:\\data\\bars.csv"
    value["spec_id"] = sha256_bytes(canonical_json_bytes({key: item for key, item in value.items() if key != "spec_id"}))
    with pytest.raises(ContractError, match="filesystem"):
        load(value)
    with pytest.raises(ContractError, match="bounded size"):
        ExternalStrategySpec.from_bytes(b"{" + b" " * (256 * 1024) + b"}")


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]
