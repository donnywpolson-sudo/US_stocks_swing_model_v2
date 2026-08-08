from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.bundle import (
    BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID,
    BLOCKED_READINESS_RECEIPT_ID,
    BUNDLE_MANIFEST,
    PreparedBundleCandidate,
    SealedBundleMetadata,
    _require_reachable_sealing_time,
    build_metadata,
    load_bundle,
    prepare_bundle_candidate,
    seal_bundle,
    verify_production_bundle_readiness,
)
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    sha256_bytes,
)
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError, IntegrityError
from us_stocks_swing_model_v2.eligibility import (
    ELIGIBILITY_CENSUS_CONTRACT_ID,
    EligibilityCensus,
)
from us_stocks_swing_model_v2.gates import (
    GateEvaluationEvidence,
    GateReceipt,
    GateState,
    IndependentGatePolicy,
    SleeveMetric,
)
from us_stocks_swing_model_v2.monitoring import MonitoringPolicy
from us_stocks_swing_model_v2.governance import (
    ReleaseBinding,
    create_local_integrity_record,
    release_bindings_hash,
)
from us_stocks_swing_model_v2.inference import (
    FitFreeInferenceEngine,
    SYNTHETIC_DIRECT_PREDICTION_SCOPE,
    synthetic_direct_prediction_binding_id,
)
from us_stocks_swing_model_v2.feature_release import load_feature_release
from us_stocks_swing_model_v2.ledger import (
    HashChainLedger,
    LedgerAnchorStore,
    OutcomeLedger,
    PredictionLedger,
)
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest, verify_release
from us_stocks_swing_model_v2.git_trial_registry import GitBackedTrialRegistration
from us_stocks_swing_model_v2.schemas import (
    FeatureRow,
    OutcomeRow,
    OutcomeStatus,
    SecurityType,
    UnderlyingPrediction,
    assert_underlying_only_payload,
)
from us_stocks_swing_model_v2.trials import (
    EvaluationExecutionEvidence,
    GovernedHoldoutAccessReceipt,
    GovernedHoldoutAccessStore,
    TrialRegistry,
    TrialSpec,
    build_holdout_receipt,
    repository_trial_identity,
)


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[1]
_FIREWALL_CLOCK_PERMIT = SyntheticOnlyPermit.create(
    fixture_id="firewall-shared-clock-authority",
    scope="TRUSTED_CLOCK_FIXED_TIME",
)


def _clock(at: datetime) -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        at,
        permit=_FIREWALL_CLOCK_PERMIT,
    )


def _gate_policy(
    *,
    minimum_sessions: int = 20,
    hurdle: float = 0.001,
    confidence_lower: float = 0.0,
) -> IndependentGatePolicy:
    return IndependentGatePolicy(
        minimum_effective_sessions=minimum_sessions,
        sleeve_economic_hurdles={name: hurdle for name in ("stock_long", "stock_short", "etf_long", "etf_short")},
        minimum_confidence_lower=confidence_lower,
        rw_alpha=0.05,
        minimum_dsr_probability=0.95,
        maximum_conservative_pbo=0.20,
        pbo_failure_threshold=0.50,
    )


def _sleeve_metric(
    *,
    sessions: int = 30,
    effect: float = 0.002,
    confidence_lower: float = 0.0011,
    confidence_upper: float = 0.003,
    hurdle: float = 0.001,
) -> SleeveMetric:
    return SleeveMetric(
        effective_sessions=sessions,
        after_cost_effect=effect,
        preregistered_economic_hurdle=hurdle,
        multiplicity_adjusted_confidence_lower=confidence_lower,
        multiplicity_adjusted_confidence_upper=confidence_upper,
        rw_adjusted_p=0.01,
        rw_alpha=0.05,
        dsr_probability=0.99,
        minimum_dsr_probability=0.95,
        pbo_applicability="APPLICABLE_MULTIPLE_CONFIGURATIONS",
        conservative_pbo=0.10,
        maximum_conservative_pbo=0.20,
        pbo_failure_threshold=0.50,
        planned_power_pass=True,
        numerical_valid=True,
        lineage_valid=True,
        pit_identity_state="PASS",
        negative_control_state="PASS",
        robustness_state="PASS",
        robustness_evidence_hash="a" * 64,
    )


def _evaluation_input_commitments(scope: str) -> dict[str, dict[str, object]]:
    """Return pre-permit input commitments, never evaluator outcomes or metrics."""

    unsigned = {
        "schema_version": 1,
        "evaluation_scope": scope,
        "purpose": "SYNTHETIC_MECHANICS_INPUT_ONLY",
    }
    return {
        "evaluation_plan": {
            **unsigned,
            "commitment_hash": sha256_bytes(canonical_json_bytes(unsigned)),
        }
    }


def _seed_test_only_synthetic_gate_receipt_for_downstream_mechanics(
    registry: TrialRegistry,
    permit,
    *,
    state: GateState,
    evaluated_at: datetime,
) -> GateReceipt:
    """Privately seed a non-trust-eligible receipt for downstream mechanics tests."""

    rebound = registry.with_clock(_clock(evaluated_at))
    if rebound._clock.trust_eligible:
        raise AssertionError("test-only gate seeding requires a synthetic clock")
    policy_hash = sha256_bytes(canonical_json_bytes(_gate_policy().as_dict()))
    if policy_hash != permit.primary_gate_id:
        raise AssertionError("test-only gate seeding requires the permit's exact policy")
    descriptor = {
        "purpose": "TEST_ONLY_SYNTHETIC_DOWNSTREAM_MECHANICS",
        "evaluation_permit_id": permit.permit_id,
        "state": state.value,
    }
    unsigned = {
        "schema_version": 3,
        "trial_registry_binding_id": permit.trial_registry_binding_id,
        "trial_id": permit.trial_id,
        "evaluation_permit_id": permit.permit_id,
        "permit_payload_hash": sha256_bytes(canonical_json_bytes(permit.as_dict())),
        "registration_hash": permit.registration_hash,
        "evaluation_scope": permit.evaluation_scope,
        "evaluation_input_hash": permit.evaluation_input_hash,
        "evaluator_code_hash": permit.evaluator_code_hash,
        "evaluator_closure_hash": permit.evaluator_closure_hash,
        "census_anchor_id": permit.census_anchor_id,
        "trial_family_anchor_id": permit.trial_family_anchor_id,
        "governance_contract_hash": permit.governance_contract_hash,
        "release_bindings_hash": permit.release_bindings_hash,
        "holdout_receipt_id": permit.holdout_receipt_id,
        "authorization_receipt_id": permit.authorization_receipt_id,
        "permit_issued_at": permit.issued_at,
        "primary_gate_id": permit.primary_gate_id,
        "policy_hash": policy_hash,
        "robustness_policy_hash": permit.robustness_policy_id,
        "robustness_evidence_hash": sha256_bytes(
            canonical_json_bytes({**descriptor, "kind": "robustness_placeholder"})
        ),
        "metrics_hash": sha256_bytes(
            canonical_json_bytes({**descriptor, "kind": "metrics_placeholder"})
        ),
        "state": state.value,
        "evaluated_at": iso_z(rebound._clock.now()),
        "time_authority": rebound._clock.mode,
        "synthetic_clock_permit_id": rebound._clock.synthetic_permit_id,
    }
    receipt = GateReceipt(
        **unsigned,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    receipt.validate()
    history = rebound._gate_receipts.read_verified()
    rebound._gate_receipts.append(
        receipt.as_dict(),
        expected_record_count=len(history),
        expected_head_hash=history[-1]["record_hash"] if history else "0" * 64,
    )
    assert registry.verify_issued_gate_receipt(permit, receipt) == receipt.as_dict()
    return receipt


def _accepted_release(
    root: Path,
    dataset: str,
    role: str,
    epoch: str,
    *,
    payload_name: str = "fixture.bin",
    payload_bytes: bytes | None = None,
    row_count: int = 1,
) -> Path:
    stage = root / "stages" / dataset
    stage.mkdir(parents=True)
    (stage / payload_name).write_bytes(
        dataset.encode() if payload_bytes is None else payload_bytes
    )
    manifest = build_manifest(
        stage,
        [payload_name],
        project="US_stocks_swing_model_v2",
        dataset=dataset,
        source_epoch=epoch,
        role=role,
        quality_state="PASS",
        created_at="2026-06-30T00:00:00Z",
        row_count=row_count,
        event_start="2026-06-27",
        event_end="2026-06-27",
        schema_fingerprint="a" * 64,
        code_hash="b" * 64,
        config_hash="c" * 64,
        environment_hash="d" * 64,
    )
    return AtomicReleasePublisher(root / "accepted").publish(stage, manifest)


def _bundle(
    tmp_path: Path,
    *,
    model_overrides: dict[str, object] | None = None,
    production_sealing: bool = False,
    final_holdout_gate: bool = False,
    artifact_paths: tuple[str, ...] = ("model.json",),
) -> Path:
    root = tmp_path / "bundle"
    root.mkdir(parents=True)
    model = {
        "kind": "linear_distribution_v1",
        "feature_schema_id": "features-v1",
        "coefficients": {"momentum": 0.02, "volatility": -0.01},
        "bias": 0.001,
        "uncertainty": 0.02,
    }
    if model_overrides:
        model.update(model_overrides)
    (root / "model.json").write_text(json.dumps(model, sort_keys=True), encoding="utf-8")
    releases = {
        "bars": _accepted_release(tmp_path, "bars", "active_historical", "historical-v1"),
        "outcomes": _accepted_release(tmp_path, "outcomes", "outcome_only", "historical-v1"),
        "identity": _accepted_release(tmp_path, "identity", "prospective_as_received", "prospective-v1"),
        "security": _accepted_release(tmp_path, "security_types", "prospective_as_received", "prospective-v1"),
        "calendar": _accepted_release(tmp_path, "xnys_sessions", "derived_causal", "prospective-v1"),
        "actions": _accepted_release(tmp_path, "corporate_actions", "prospective_as_received", "prospective-v1"),
    }
    manifests = {name: json.loads((path / "release_manifest.json").read_text()) for name, path in releases.items()}
    feature_payload = {
        "schema_version": 1,
        "rows": [{
            "asset_id": "asset-ABC",
            "symbol": "ABC",
            "security_type": "STOCK",
            "decision_session": "2026-07-15",
            "decision_at": "2026-07-15T20:00:00Z",
            "available_at": "2026-07-15T19:55:00Z",
            "feature_schema_id": "features-v1",
            "identity_release_id": manifests["identity"]["release_id"],
            "security_type_evidence_id": manifests["security"]["release_id"],
            "calendar_release_id": manifests["calendar"]["release_id"],
            "action_release_id": manifests["actions"]["release_id"],
            "identity_known_at": "2026-07-15T19:30:00Z",
            "point_in_time_state": "PIT_CONFIRMED",
            "prediction_deadline_at": "2026-07-15T20:10:00Z",
            "information_barrier_at": "2026-07-22T20:00:00Z",
            "ordered_values": [["momentum", 1.0], ["volatility", 0.1]],
        }, {
            "asset_id": "asset-XYZ",
            "symbol": "XYZ",
            "security_type": "ETF",
            "decision_session": "2026-07-15",
            "decision_at": "2026-07-15T20:00:00Z",
            "available_at": "2026-07-15T19:55:00Z",
            "feature_schema_id": "features-v1",
            "identity_release_id": manifests["identity"]["release_id"],
            "security_type_evidence_id": manifests["security"]["release_id"],
            "calendar_release_id": manifests["calendar"]["release_id"],
            "action_release_id": manifests["actions"]["release_id"],
            "identity_known_at": "2026-07-15T19:30:00Z",
            "point_in_time_state": "PIT_CONFIRMED",
            "prediction_deadline_at": "2026-07-15T20:10:00Z",
            "information_barrier_at": "2026-07-22T20:00:00Z",
            "ordered_values": [["momentum", 0.5], ["volatility", 0.1]],
        }],
    }
    releases["features"] = _accepted_release(
        tmp_path,
        "features",
        "feature_only",
        "prospective-v1",
        payload_name="features.json",
        payload_bytes=json.dumps(feature_payload, sort_keys=True).encode(),
        row_count=2,
    )
    manifests = {name: json.loads((path / "release_manifest.json").read_text()) for name, path in releases.items()}
    governance_root = tmp_path / "governance"
    governance_root.mkdir()
    registry = TrialRegistry(
        governance_root / "trials.jsonl",
        governance_root / "evaluations.jsonl",
        accepted_release_root=tmp_path / "accepted",
        governance_root=governance_root,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="bundle-trial-registry",
            scope="SYNTHETIC_TRIAL_REGISTRY",
        ),
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    spec = _trial(tuple(releases.values()))
    trial_id = registry.register(
        spec,
        verified_release_directories=releases.values(),
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    permit, initial_holdout, _ = _outer_permit(registry, spec, trial_id)
    gate = _seed_test_only_synthetic_gate_receipt_for_downstream_mechanics(
        registry,
        permit,
        state=GateState.PASS_HISTORICAL_DISCOVERY_SCREEN,
        evaluated_at=datetime(2026, 7, 15, 2, tzinfo=timezone.utc),
    )
    if final_holdout_gate:
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 2, 15, tzinfo=timezone.utc))
        ).record_evaluation(
            permit,
            _evaluation_result(permit, initial_holdout, gate),
            gate_receipt=gate,
        )
        permit, _, _ = _final_permit(
            registry,
            spec,
            trial_id,
            initial_holdout=initial_holdout,
        )
        gate = _seed_test_only_synthetic_gate_receipt_for_downstream_mechanics(
            registry,
            permit,
            state=GateState.PASS_HISTORICAL_DISCOVERY_SCREEN,
            evaluated_at=datetime(2026, 7, 15, 4, tzinfo=timezone.utc),
        )
    if production_sealing:
        sealing_clock = TrustedClock.production()
        sealing_observed_at = sealing_clock.now()
    else:
        sealing_observed_at = datetime(
            2026,
            7,
            15,
            5 if final_holdout_gate else 3,
            tzinfo=timezone.utc,
        )
        sealing_clock = _clock(sealing_observed_at)
    candidate = prepare_bundle_candidate(
        root,
        artifact_paths,
        verified_release_directories=releases.values(),
        accepted_release_root=tmp_path / "accepted",
        expected_project="US_stocks_swing_model_v2",
        gate_receipt=gate,
        trial_registry=registry,
        trial_permit=permit,
        model_kind="linear_distribution_v1",
        feature_schema_id="features-v1",
        feature_names=("momentum", "volatility"),
        feature_types={"momentum": "float64", "volatility": "float64"},
        training_cutoff="2026-06-30T20:00:00Z",
        sealed_at=iso_z(sealing_observed_at),
        data_release_ids=tuple(sorted(value["release_id"] for value in manifests.values())),
        training_release_ids=tuple(
            sorted(
                manifests[name]["release_id"]
                for name in ("bars", "features", "outcomes")
            )
        ),
        allowed_feature_release_ids=(manifests["features"]["release_id"],),
        allowed_identity_release_ids=(manifests["identity"]["release_id"],),
        allowed_security_type_evidence_ids=(manifests["security"]["release_id"],),
        allowed_source_epochs=("historical-v1", "prospective-v1"),
        calendar_release_id=manifests["calendar"]["release_id"],
        action_release_id=manifests["actions"]["release_id"],
        readiness_receipt_id=BLOCKED_READINESS_RECEIPT_ID,
        eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
        external_anchor_receipt_id=BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID,
        production_readiness_state="GIT_REGISTRATION_NOT_VERIFIED_BLOCKS_PRODUCTION",
        monitoring_policy_hash=MonitoringPolicy().policy_hash,
        monitoring_reference_hash="9" * 64,
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
        neutral_band=0.005,
        maximum_uncertainty=0.05,
        maximum_feature_age_minutes=180,
        maximum_identity_age_minutes=1440,
        maximum_inference_latency_minutes=10,
    )
    authorization = create_local_integrity_record(
        scope="AUTHORIZE_CANDIDATE_SEALING",
        subject_id=candidate.candidate_id,
        bindings=candidate.sealing_bindings(),
        clock=sealing_clock,
    )
    metadata = build_metadata(
        candidate,
        sealing_authorization=authorization,
        clock=sealing_clock,
    )
    seal_bundle(
        root,
        metadata,
        clock=sealing_clock,
    )
    assert load_bundle(root) == metadata
    return root


def test_bundle_preparation_rejects_duplicate_artifact_declarations(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContractError, match="artifact declarations must be unique"):
        _bundle(
            tmp_path,
            artifact_paths=("model.json", "model.json"),
        )


def test_bundle_readiness_verifier_binds_external_gate_holdout_and_releases(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path, final_holdout_gate=True)
    metadata = load_bundle(bundle_path)
    assert metadata.gate_receipt.evaluation_scope == "FINAL_HOLDOUT"
    candidate = PreparedBundleCandidate(
        bundle_dir=bundle_path,
        candidate_json=canonical_json_bytes(metadata.candidate_dict()),
        candidate_id=metadata.candidate_id,
    )
    registry_envelope = json.loads(
        (tmp_path / "governance/trials.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    registered_payload = registry_envelope["payload"]
    registration_hash = sha256_bytes(canonical_json_bytes(registered_payload))
    assert registration_hash == metadata.registration_hash
    external_fields = {
        "schema_version": 2,
        "backend": "LOCAL_GIT_WITH_GITHUB_BACKUP",
        "policy_id": "a" * 64,
        "trial_id": metadata.trial_id,
        "trial_registry_binding_id": metadata.trial_registry_binding_id,
        "registration_hash": registration_hash,
        "registration_authorization_record_id": "b" * 64,
        "relative_path": f"research_registry/trials/{metadata.trial_id}.json",
        "object_sha256": registration_hash,
        "registered_at": registered_payload["registered_at"],
        "git_commit": "1" * 40,
        "remote_name": "origin",
        "remote_branch": "main",
        "remote_url_sha256": sha256_bytes(
            b"https://github.com/donnywpolson-sudo/US_stocks_swing_model_v2.git"
        ),
        "remote_tip_commit": "2" * 40,
        "backup_state": "GITHUB_REMOTE_TRACKING_REF_VERIFIED_OWNER_CONTROLLED",
        "registered_payload": registered_payload,
    }
    external_anchor_payload = {
        key: value
        for key, value in external_fields.items()
        if key != "registered_payload"
    }
    external = GitBackedTrialRegistration(
        **external_fields,
        external_anchor_receipt_id=sha256_bytes(
            canonical_json_bytes(external_anchor_payload)
        ),
    )
    external.validate()

    initial_holdout = build_holdout_receipt(
        trial_id=metadata.trial_id,
        state="LOCKED",
        clock=_clock(datetime(2026, 7, 15, 0, 15, tzinfo=timezone.utc)),
    )
    unlocked_holdout = build_holdout_receipt(
        trial_id=metadata.trial_id,
        state="UNLOCKED_ONCE",
        previous=initial_holdout,
        clock=_clock(datetime(2026, 7, 15, 2, 30, tzinfo=timezone.utc)),
    )
    assert unlocked_holdout.receipt_id == metadata.gate_receipt.holdout_receipt_id
    closed_holdout = build_holdout_receipt(
        trial_id=metadata.trial_id,
        state="CLOSED",
        previous=unlocked_holdout,
        clock=_clock(datetime(2026, 7, 15, 4, 15, tzinfo=timezone.utc)),
    )
    governed_store = GovernedHoldoutAccessStore(
        tmp_path / "governance/governed-holdout.jsonl",
        governance_root=tmp_path / "governance",
        clock=_clock(datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc)),
    )
    locked = governed_store.initialize(
        trial_registry_binding_id=metadata.trial_registry_binding_id,
        holdout_receipt=initial_holdout,
    )
    pre_unlock_head = "c" * 64
    unlock_bindings = {
        "trial_registry_binding_id": metadata.trial_registry_binding_id,
        "locked_governed_receipt_id": locked.receipt_id,
        "locked_holdout_state_receipt_id": locked.holdout_state_receipt_id,
        "unlocked_holdout_state_receipt_id": unlocked_holdout.receipt_id,
        "pre_unlock_trial_ledger_head": pre_unlock_head,
    }
    unlock_authorization = create_local_integrity_record(
        scope="AUTHORIZE_FINAL_HOLDOUT_ACCESS",
        subject_id=metadata.trial_id,
        bindings=unlock_bindings,
        clock=_clock(datetime(2026, 7, 15, 2, 35, tzinfo=timezone.utc)),
    )
    unlocked = governed_store.with_clock(
        _clock(datetime(2026, 7, 15, 2, 40, tzinfo=timezone.utc))
    ).unlock_once(
        locked_receipt=locked,
        unlocked_holdout_receipt=unlocked_holdout,
        pre_unlock_trial_ledger_head=pre_unlock_head,
        authorization=unlock_authorization,
    )
    close_bindings = {
        "trial_registry_binding_id": metadata.trial_registry_binding_id,
        "unlocked_governed_receipt_id": unlocked.receipt_id,
        "unlocked_holdout_state_receipt_id": unlocked.holdout_state_receipt_id,
        "closed_holdout_state_receipt_id": closed_holdout.receipt_id,
        "pre_unlock_trial_ledger_head": pre_unlock_head,
    }
    close_authorization = create_local_integrity_record(
        scope="CLOSE_FINAL_HOLDOUT_ACCESS",
        subject_id=metadata.trial_id,
        bindings=close_bindings,
        clock=_clock(datetime(2026, 7, 15, 4, 20, tzinfo=timezone.utc)),
    )
    closed = governed_store.with_clock(
        _clock(datetime(2026, 7, 15, 4, 25, tzinfo=timezone.utc))
    ).close(
        unlocked_receipt=unlocked,
        closed_holdout_receipt=closed_holdout,
        authorization=close_authorization,
    )
    holdout_chain = (locked, unlocked, closed)
    readiness_clock = _clock(datetime(2026, 7, 15, 6, tzinfo=timezone.utc))
    accepted_root = tmp_path / "accepted"
    release_directories = tuple(
        accepted_root / binding.dataset / binding.release_id
        for binding in metadata.release_bindings
    )
    readiness_bindings = {
        "candidate_id": candidate.candidate_id,
        "trial_registry_binding_id": metadata.trial_registry_binding_id,
        "trial_id": metadata.trial_id,
        "registration_hash": metadata.registration_hash,
        "external_anchor_receipt_id": external.external_anchor_receipt_id,
        "gate_receipt_id": metadata.gate_receipt.receipt_id,
        "closed_holdout_receipt_id": closed.receipt_id,
        "eligibility_census_contract_id": ELIGIBILITY_CENSUS_CONTRACT_ID,
        "release_bindings_hash": release_bindings_hash(metadata.release_bindings),
    }
    authorization = create_local_integrity_record(
        scope="VERIFY_PRODUCTION_BUNDLE_READINESS",
        subject_id=candidate.candidate_id,
        bindings=readiness_bindings,
        clock=readiness_clock,
    )
    receipt = verify_production_bundle_readiness(
        candidate,
        external_registration=external,
        holdout_access_chain=holdout_chain,
        unlock_authorization=unlock_authorization,
        close_authorization=close_authorization,
        eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
        verified_release_directories=release_directories,
        accepted_release_root=accepted_root,
        authorization=authorization,
        clock=readiness_clock,
    )
    assert receipt.evidence_state == "SYNTHETIC_MECHANICS_ONLY_NOT_TRUST_ELIGIBLE"
    assert receipt.trust_eligible is False
    outer_bundle_path = _bundle(tmp_path / "outer-only")
    outer_metadata = load_bundle(outer_bundle_path)
    outer_candidate = PreparedBundleCandidate(
        bundle_dir=outer_bundle_path,
        candidate_json=canonical_json_bytes(outer_metadata.candidate_dict()),
        candidate_id=outer_metadata.candidate_id,
    )
    with pytest.raises(ContractError, match="PASS final-holdout"):
        verify_production_bundle_readiness(
            outer_candidate,
            external_registration=external,
            holdout_access_chain=holdout_chain,
            unlock_authorization=unlock_authorization,
            close_authorization=close_authorization,
            eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
            verified_release_directories=release_directories,
            accepted_release_root=accepted_root,
            authorization=authorization,
            clock=readiness_clock,
        )
    with pytest.raises(ContractError, match="verified-ready bundle receipt"):
        EligibilityCensus.production_from_rows(
            metadata,
            (_row(bundle_path, "ABC", SecurityType.STOCK, 0.25),),
            readiness_receipt=receipt,
        )
    with pytest.raises(ContractError, match="eligibility contract differs"):
        verify_production_bundle_readiness(
            candidate,
            external_registration=external,
            holdout_access_chain=holdout_chain,
            unlock_authorization=unlock_authorization,
            close_authorization=close_authorization,
            eligibility_census_contract_id="e" * 64,
            verified_release_directories=release_directories,
            accepted_release_root=accepted_root,
            authorization=authorization,
            clock=readiness_clock,
        )
    forged_external = replace(external, registration_hash="f" * 64)
    with pytest.raises(EvaluationAuthorizationError, match="committed file"):
        verify_production_bundle_readiness(
            candidate,
            external_registration=forged_external,
            holdout_access_chain=holdout_chain,
            unlock_authorization=unlock_authorization,
            close_authorization=close_authorization,
            eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
            verified_release_directories=release_directories,
            accepted_release_root=accepted_root,
            authorization=authorization,
            clock=readiness_clock,
        )
    with pytest.raises(ContractError, match="exact governed holdout chain"):
        verify_production_bundle_readiness(
            candidate,
            external_registration=external,
            holdout_access_chain=(locked, closed),
            unlock_authorization=unlock_authorization,
            close_authorization=close_authorization,
            eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
            verified_release_directories=release_directories,
            accepted_release_root=accepted_root,
            authorization=authorization,
            clock=readiness_clock,
        )
    forged_closed_unsigned = {
        **closed.unsigned_dict(),
        "authorization_record_id": "f" * 64,
    }
    forged_closed = GovernedHoldoutAccessReceipt(
        **forged_closed_unsigned,
        receipt_id=sha256_bytes(canonical_json_bytes(forged_closed_unsigned)),
    )
    forged_bindings = {
        **readiness_bindings,
        "closed_holdout_receipt_id": forged_closed.receipt_id,
    }
    forged_readiness_authorization = create_local_integrity_record(
        scope="VERIFY_PRODUCTION_BUNDLE_READINESS",
        subject_id=candidate.candidate_id,
        bindings=forged_bindings,
        clock=readiness_clock,
    )
    with pytest.raises(ContractError, match="authorization receipt differs"):
        verify_production_bundle_readiness(
            candidate,
            external_registration=external,
            holdout_access_chain=(locked, unlocked, forged_closed),
            unlock_authorization=unlock_authorization,
            close_authorization=close_authorization,
            eligibility_census_contract_id=ELIGIBILITY_CENSUS_CONTRACT_ID,
            verified_release_directories=release_directories,
            accepted_release_root=accepted_root,
            authorization=forged_readiness_authorization,
            clock=readiness_clock,
        )


def _load_bundle(bundle_path: Path):
    return load_bundle(bundle_path)


def test_bundle_sealing_is_reachable_with_production_clock(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path, production_sealing=True)
    metadata = _load_bundle(bundle_path)
    assert parse_utc_z(metadata.sealed_at, "sealed_at") <= TrustedClock.production().now()


def test_bundle_sealing_window_rejects_future_and_stale_times() -> None:
    observed_at = datetime(2026, 7, 15, 3, 15, tzinfo=timezone.utc)
    with pytest.raises(ContractError, match="future"):
        _require_reachable_sealing_time(
            "2026-07-15T03:15:00.000001Z",
            observed_at,
        )
    with pytest.raises(ContractError, match="bounded sealing window"):
        _require_reachable_sealing_time(
            "2026-07-15T02:59:59.999999Z",
            observed_at,
        )
    _require_reachable_sealing_time("2026-07-15T03:00:00Z", observed_at)


def _engine(bundle_path: Path, *, clock: TrustedClock) -> FitFreeInferenceEngine:
    return FitFreeInferenceEngine(
        bundle_path,
        accepted_release_root=bundle_path.parent / "accepted",
        clock=clock,
    )


def _row(
    bundle_path: Path,
    symbol: str,
    security_type: SecurityType,
    momentum: float,
    *,
    stale: bool = False,
) -> FeatureRow:
    metadata = _load_bundle(bundle_path)
    return FeatureRow(
        asset_id=f"asset-{symbol}",
        symbol=symbol,
        security_type=security_type,
        decision_session=date(2026, 7, 15),
        decision_at=NOW,
        available_at=NOW - timedelta(minutes=181 if stale else 5),
        source_release_id=metadata.allowed_feature_release_ids[0],
        feature_schema_id="features-v1",
        identity_release_id=metadata.allowed_identity_release_ids[0],
        security_type_evidence_id=metadata.allowed_security_type_evidence_ids[0],
        calendar_release_id=metadata.calendar_release_id,
        action_release_id=metadata.action_release_id,
        source_epoch="prospective-v1",
        identity_known_at=NOW - timedelta(minutes=30),
        point_in_time_state="PIT_CONFIRMED",
        prediction_deadline_at=NOW + timedelta(minutes=10),
        information_barrier_at=NOW + timedelta(days=7),
        values={"momentum": momentum, "volatility": 0.10},
    )


def _census(engine: FitFreeInferenceEngine, rows: list[FeatureRow] | tuple[FeatureRow, ...]) -> EligibilityCensus:
    materialized = tuple(rows)
    return EligibilityCensus.synthetic_from_rows(
        engine.metadata,
        materialized,
        permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-eligibility-" + "-".join(sorted(row.asset_id for row in materialized)),
            scope="SYNTHETIC_ELIGIBILITY_CENSUS",
        ),
    )


def test_production_eligibility_materializer_requires_verified_ready_bundle(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path)
    metadata = _load_bundle(bundle_path)
    row = _row(bundle_path, "ABC", SecurityType.STOCK, 0.25)
    synthetic = EligibilityCensus.synthetic_from_rows(
        metadata,
        (row,),
        permit=SyntheticOnlyPermit.create(
            fixture_id="production-eligibility-blocker",
            scope="SYNTHETIC_ELIGIBILITY_CENSUS",
        ),
    )
    assert synthetic.readiness_receipt_id == metadata.readiness_receipt_id
    assert synthetic.external_anchor_receipt_id == metadata.external_anchor_receipt_id
    with pytest.raises(ContractError, match="exact readiness receipt"):
        EligibilityCensus.production_from_rows(
            metadata,
            (row,),
            readiness_receipt=object(),  # type: ignore[arg-type]
        )


def _predict(
    engine: FitFreeInferenceEngine,
    rows: list[FeatureRow] | tuple[FeatureRow, ...],
):
    materialized = tuple(rows)
    census = _census(engine, materialized)
    return engine.predict(
        materialized,
        eligibility_census=census,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id=synthetic_direct_prediction_binding_id(
                bundle_id=engine.metadata.bundle_id,
                eligibility_census_id=census.census_id,
                ordered_feature_row_hashes=(
                    row.row_hash for row in materialized
                ),
            ),
            scope=SYNTHETIC_DIRECT_PREDICTION_SCOPE,
        ),
    )


def _direct_prediction_permit(
    engine: FitFreeInferenceEngine,
    census: EligibilityCensus,
    rows: list[FeatureRow] | tuple[FeatureRow, ...],
    *,
    bundle_id: str | None = None,
    census_id: str | None = None,
) -> SyntheticOnlyPermit:
    materialized = tuple(rows)
    return SyntheticOnlyPermit.create(
        fixture_id=synthetic_direct_prediction_binding_id(
            bundle_id=(
                engine.metadata.bundle_id
                if bundle_id is None
                else bundle_id
            ),
            eligibility_census_id=(
                census.census_id if census_id is None else census_id
            ),
            ordered_feature_row_hashes=(
                row.row_hash for row in materialized
            ),
        ),
        scope=SYNTHETIC_DIRECT_PREDICTION_SCOPE,
    )


def _prediction_append_permit(fixture_id: str) -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id=fixture_id,
        scope="SYNTHETIC_SINGLE_PREDICTION_LEDGER_APPEND",
    )


def _outcome_append_permit(fixture_id: str) -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id=fixture_id,
        scope="SYNTHETIC_OUTCOME_LEDGER_APPEND",
    )


def _matured_outcome(prediction: UnderlyingPrediction, *, value: float = 0.01) -> OutcomeRow:
    return OutcomeRow.create(
        prediction_id=prediction.prediction_id,
        eligibility_census_id=prediction.eligibility_census_id,
        revision_number=1,
        prior_revision_id=None,
        asset_id=prediction.asset_id,
        decision_session=prediction.decision_session,
        entry_session=prediction.decision_session + timedelta(days=1),
        exit_session=prediction.decision_session + timedelta(days=7),
        status=OutcomeStatus.MATURED,
        split_normalized_price_return=value,
        reason=None,
        calendar_release_id=prediction.calendar_release_id,
        bar_release_id="f" * 64,
        action_release_id=prediction.action_release_id,
        source_epoch=prediction.source_epoch,
        action_view_as_of=NOW + timedelta(days=7),
    )


def _interrupted_outcome_anchor_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_prior_anchor: bool = False,
) -> tuple[OutcomeLedger, OutcomeRow, Path, TrustedClock, Path, Path | None]:
    bundle_path = _bundle(tmp_path / "bundle")
    inference = _engine(bundle_path, clock=_clock(NOW + timedelta(minutes=1)))
    prediction = _predict(
        inference,
        [_row(bundle_path, "ABC", SecurityType.STOCK, 1.0)],
    )[0]
    prediction_ledger = PredictionLedger(
        tmp_path / "ledger" / "predictions.jsonl",
        tmp_path / "prediction-anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    prediction_anchor = Path(
        prediction_ledger.append_synthetic(
            prediction,
            synthetic_permit=_prediction_append_permit(
                "outcome-anchor-recovery-prediction"
            ),
        )["anchor_path"]
    )
    recovery_clock = _clock(NOW + timedelta(days=8))
    outcome_anchor_root = tmp_path / "outcome-anchors"
    outcome_ledger = OutcomeLedger(
        tmp_path / "ledger" / "outcomes.jsonl",
        prediction_ledger,
        anchor_root=outcome_anchor_root,
        clock=recovery_clock,
    )
    intended = _matured_outcome(prediction)
    previous_anchor: Path | None = None
    if with_prior_anchor:
        previous_anchor = Path(
            outcome_ledger.append_synthetic(
                intended,
                prediction_anchor=prediction_anchor,
                synthetic_permit=_outcome_append_permit(
                    "outcome-anchor-recovery-prior-outcome"
                ),
            )["anchor_path"]
        )
        intended = OutcomeRow.create(
            prediction_id=intended.prediction_id,
            eligibility_census_id=intended.eligibility_census_id,
            revision_number=2,
            prior_revision_id=intended.revision_id,
            asset_id=intended.asset_id,
            decision_session=intended.decision_session,
            entry_session=intended.entry_session,
            exit_session=intended.exit_session,
            status=intended.status,
            split_normalized_price_return=0.02,
            reason=intended.reason,
            calendar_release_id=intended.calendar_release_id,
            bar_release_id=intended.bar_release_id,
            action_release_id=intended.action_release_id,
            source_epoch=intended.source_epoch,
            action_view_as_of=intended.action_view_as_of + timedelta(hours=1),
        )
    original_create = outcome_ledger._anchors.create

    def interrupt_after_commit(*_args: object, **_kwargs: object) -> Path:
        raise RuntimeError("simulated interruption before outcome anchor publication")

    monkeypatch.setattr(outcome_ledger._anchors, "create", interrupt_after_commit)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        outcome_ledger.append_synthetic(
            intended,
            prediction_anchor=prediction_anchor,
            synthetic_permit=_outcome_append_permit(
                "outcome-anchor-recovery-interrupted-append"
            ),
            previous_anchor=previous_anchor,
        )
    monkeypatch.setattr(outcome_ledger._anchors, "create", original_create)
    return (
        outcome_ledger,
        intended,
        prediction_anchor,
        recovery_clock,
        outcome_anchor_root,
        previous_anchor,
    )


def test_fit_free_underlying_only_inference_ranks_and_abstains(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path)
    engine = _engine(bundle_path, clock=_clock(NOW + timedelta(minutes=1)))
    assert not hasattr(engine, "fit")
    assert not hasattr(engine.model, "fit")
    rows = [
        _row(bundle_path, "ABC", SecurityType.STOCK, 1.0),
        _row(bundle_path, "SPY", SecurityType.ETF, -2.0),
        _row(bundle_path, "WRT", SecurityType.UNKNOWN, 1.0),
        _row(bundle_path, "OLD", SecurityType.STOCK, 1.0, stale=True),
    ]
    census = _census(engine, rows)
    predictions = _predict(engine, rows)
    with pytest.raises(ContractError, match="exactly cover"):
        engine.predict(
            rows[:-1],
            eligibility_census=census,
            synthetic_permit=_direct_prediction_permit(
                engine,
                census,
                rows[:-1],
            ),
        )
    by_symbol = {item.symbol: item for item in predictions}
    assert by_symbol["SPY"].rank == 1
    assert by_symbol["ABC"].rank == 2
    assert by_symbol["WRT"].abstention_reason == "unknown_security_type"
    assert by_symbol["OLD"].abstention_reason == "stale_feature"
    for prediction in predictions:
        payload = prediction.as_dict()
        assert_underlying_only_payload(payload)
        assert not ({"strike", "expiration", "option_symbol", "realized_return"} & payload.keys())


@pytest.mark.parametrize(
    ("binding_field", "replacement"),
    [
        ("bundle_id", "0" * 64),
        ("census_id", "1" * 64),
    ],
)
def test_direct_prediction_permit_binds_exact_bundle_and_census(
    tmp_path: Path,
    binding_field: str,
    replacement: str,
) -> None:
    bundle_path = _bundle(tmp_path)
    engine = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    rows = (_row(bundle_path, "ABC", SecurityType.STOCK, 1.0),)
    census = _census(engine, rows)
    permit = _direct_prediction_permit(
        engine,
        census,
        rows,
        **{binding_field: replacement},
    )
    with pytest.raises(ContractError, match="exact bundle, census"):
        engine.predict(
            rows,
            eligibility_census=census,
            synthetic_permit=permit,
        )


def test_direct_prediction_permit_binds_exact_ordered_feature_rows(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path)
    engine = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    rows = (
        _row(bundle_path, "ABC", SecurityType.STOCK, 1.0),
        _row(bundle_path, "SPY", SecurityType.ETF, -1.0),
    )
    census = _census(engine, rows)
    reversed_permit = _direct_prediction_permit(
        engine,
        census,
        tuple(reversed(rows)),
    )
    with pytest.raises(ContractError, match="ordered feature rows"):
        engine.predict(
            rows,
            eligibility_census=census,
            synthetic_permit=reversed_permit,
        )


def test_bundle_artifact_mutation_fails_closed(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "model.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="artifact mismatch"):
        _load_bundle(root)


def test_bundle_reload_rechecks_local_record_and_exact_numeric_json_types(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    poisoned_record = json.loads(
        (root / "sealed_bundle.json").read_text(encoding="utf-8")
    )
    poisoned_record["sealing_authorization"]["record_id"] = "f" * 64
    with pytest.raises(EvaluationAuthorizationError, match="record ID"):
        SealedBundleMetadata.from_dict(poisoned_record)

    payload = json.loads((root / "sealed_bundle.json").read_text(encoding="utf-8"))
    payload["maximum_feature_age_minutes"] = True
    with pytest.raises(ContractError, match="exact JSON integer"):
        SealedBundleMetadata.from_dict(payload)
    original = json.loads(
        (root / "sealed_bundle.json").read_text(encoding="utf-8")
    )
    for field in (
        "model_kind",
        "training_cutoff",
        "calendar_release_id",
        "governance_contract_hash",
        "candidate_id",
        "bundle_id",
    ):
        poisoned = dict(original)
        poisoned[field] = 123
        with pytest.raises(ContractError, match="exact JSON strings"):
            SealedBundleMetadata.from_dict(poisoned)
    nested_poison = json.loads(json.dumps(original))
    nested_poison["release_bindings"][0]["release_id"] = 123
    with pytest.raises(ContractError, match="release bindings"):
        SealedBundleMetadata.from_dict(nested_poison)
    artifact_poison = json.loads(json.dumps(original))
    artifact_poison["artifacts"][0]["size"] = True
    with pytest.raises(ContractError, match="nested evidence"):
        SealedBundleMetadata.from_dict(artifact_poison)


def test_prepared_bundle_rejects_non_string_signing_fields_before_bindings(
    tmp_path: Path,
) -> None:
    root = _bundle(tmp_path)
    sealed = json.loads((root / BUNDLE_MANIFEST).read_text(encoding="utf-8"))
    candidate_payload = {
        name: value
        for name, value in sealed.items()
        if name not in {"candidate_id", "sealing_authorization", "bundle_id"}
    }
    for field in (
        "trial_id",
        "trial_registry_binding_id",
        "readiness_receipt_id",
        "governance_contract_hash",
    ):
        poisoned_payload = {**candidate_payload, field: 7}
        candidate_json = canonical_json_bytes(poisoned_payload)
        candidate = PreparedBundleCandidate(
            bundle_dir=root,
            candidate_json=candidate_json,
            candidate_id=sha256_bytes(candidate_json),
        )
        with pytest.raises(ContractError, match="exact JSON strings"):
            candidate.sealing_bindings()


def test_local_integrity_record_rejects_observation_before_creation() -> None:
    record = create_local_integrity_record(
        scope="AUDIT_LOCAL_RECORD_TIME",
        subject_id="1" * 64,
        bindings={"evidence": "2" * 64},
        clock=_clock(datetime(2026, 7, 15, 20, tzinfo=timezone.utc)),
    )
    with pytest.raises(EvaluationAuthorizationError, match="before it was created"):
        record.validate(
            expected_scope="AUDIT_LOCAL_RECORD_TIME",
            expected_subject_id="1" * 64,
            required_bindings={"evidence": "2" * 64},
            clock=_clock(datetime(2026, 7, 15, 19, tzinfo=timezone.utc)),
        )


def test_bundle_release_slots_cutoffs_and_frozen_gate_are_binding(tmp_path: Path) -> None:
    metadata = _load_bundle(_bundle(tmp_path))
    with pytest.raises(ContractError, match="feature slot"):
        replace(
            metadata,
            allowed_feature_release_ids=metadata.allowed_identity_release_ids,
            allowed_identity_release_ids=metadata.allowed_feature_release_ids,
        ).validate()

    training_id = metadata.training_release_ids[0]
    event_poison = tuple(
        replace(binding, event_end="2026-07-02")
        if binding.release_id == training_id
        else binding
        for binding in metadata.release_bindings
    )
    with pytest.raises(ContractError, match="after the training cutoff"):
        replace(metadata, release_bindings=event_poison).validate()

    creation_poison = tuple(
        replace(binding, created_at="2026-07-16T00:00:00Z")
        if binding.release_id == training_id
        else binding
        for binding in metadata.release_bindings
    )
    with pytest.raises(ContractError, match="created after sealing"):
        replace(metadata, release_bindings=creation_poison).validate()

    with pytest.raises(ContractError, match="frozen-policy"):
        replace(metadata, primary_gate_id="0" * 64).validate()
    with pytest.raises(ContractError, match="frozen-policy"):
        replace(metadata, robustness_policy_hash="0" * 64).validate()
    with pytest.raises(ContractError, match="frozen-policy"):
        replace(metadata, robustness_evidence_hash="0" * 64).validate()

    assert metadata.trust_eligible is False
    with pytest.raises(ContractError, match="schema-v3 bundles cannot embed"):
        replace(
            metadata,
            production_readiness_state="VERIFIED_READY",
        ).validate()


@pytest.mark.parametrize(
    "poison",
    [
        {"coefficients": {"momentum": True, "volatility": -0.01}},
        {"bias": "0.001"},
        {"uncertainty": False},
    ],
)
def test_hash_consistent_model_rejects_non_numeric_json_scalar_poison(
    tmp_path: Path,
    poison: dict[str, object],
) -> None:
    root = _bundle(tmp_path, model_overrides=poison)
    with pytest.raises(ContractError, match="explicit JSON numeric scalar"):
        _engine(root, clock=_clock(NOW + timedelta(minutes=1)))


def test_inference_rejects_backdating_and_post_entry_and_abstains_untrusted_evidence(tmp_path: Path) -> None:
    actual_bundle = _bundle(tmp_path / "actual")
    row = _row(actual_bundle, "ABC", SecurityType.STOCK, 1.0)
    with pytest.raises(ContractError, match="production inference rejects"):
        _engine(actual_bundle, clock=TrustedClock.production())
    with pytest.raises(ContractError, match="repository-issued TrustedClock"):
        _engine(
            actual_bundle,
            clock=lambda: NOW + timedelta(minutes=1),  # type: ignore[arg-type]
        )
    with pytest.raises(ContractError, match="backdate"):
        backdate_bundle = _bundle(tmp_path / "backdate")
        _predict(_engine(
            backdate_bundle,
            clock=_clock(NOW - timedelta(seconds=1)),
        ), [_row(backdate_bundle, "ABC", SecurityType.STOCK, 1.0)])
    with pytest.raises(ContractError, match="entry/label-safe"):
        late_bundle = _bundle(tmp_path / "late")
        _predict(_engine(
            late_bundle,
            clock=_clock(NOW + timedelta(minutes=11)),
        ), [_row(late_bundle, "ABC", SecurityType.STOCK, 1.0)])

    evidence_bundle = _bundle(tmp_path / "evidence")
    engine = _engine(
        evidence_bundle,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    proxy = _predict(engine,
        [replace(_row(evidence_bundle, "ABC", SecurityType.STOCK, 1.0), point_in_time_state="HISTORICAL_PROXY")],
    )[0]
    evidence_row = _row(evidence_bundle, "ABC", SecurityType.STOCK, 1.0)
    census = _census(engine, [evidence_row])
    with pytest.raises(ContractError, match="eligibility census"):
        engine.predict(
            [replace(evidence_row, source_release_id="0" * 64)],
            eligibility_census=census,
            synthetic_permit=_direct_prediction_permit(
                engine,
                census,
                [replace(evidence_row, source_release_id="0" * 64)],
            ),
        )
    with pytest.raises(ContractError, match="eligibility census"):
        engine.predict(
            [replace(evidence_row, security_type_evidence_id="0" * 64)],
            eligibility_census=census,
            synthetic_permit=_direct_prediction_permit(
                engine,
                census,
                [replace(evidence_row, security_type_evidence_id="0" * 64)],
            ),
        )
    assert proxy.abstention_reason == "untrusted_point_in_time_state:historical_proxy"


def test_prediction_id_binds_recorded_time_and_entire_ordered_feature_row(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path)
    engine = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    first = _predict(engine,
        [_row(bundle_path, "ABC", SecurityType.STOCK, 1.0)],
    )[0]
    changed = _predict(engine,
        [_row(bundle_path, "ABC", SecurityType.STOCK, 1.1)],
    )[0]
    assert first.feature_row_hash != changed.feature_row_hash
    assert first.prediction_id != changed.prediction_id
    with pytest.raises(ContractError, match="prediction ID"):
        replace(first, prediction_id="0" * 64).validate()
    with pytest.raises(ContractError, match="exact date"):
        replace(
            first,
            decision_session=datetime(2026, 7, 15, tzinfo=timezone.utc),
        ).validate()
    coerced_payload = first.as_dict()
    coerced_payload["decision_session"] = 20260715
    with pytest.raises(ContractError, match="exact JSON strings"):
        UnderlyingPrediction.from_dict(coerced_payload)
    identity_poison = first.as_dict()
    identity_poison["asset_id"] = 123
    unsigned_poison = dict(identity_poison)
    unsigned_poison.pop("prediction_id")
    identity_poison["prediction_id"] = sha256_bytes(
        canonical_json_bytes(unsigned_poison)
    )
    with pytest.raises(ContractError, match="identity/enum/date/time"):
        UnderlyingPrediction.from_dict(identity_poison)
    direct_poison = replace(first, asset_id=123, prediction_id="")
    direct_poison = replace(
        direct_poison,
        prediction_id=direct_poison.computed_prediction_id,
    )
    with pytest.raises(ContractError, match="exact nonempty text"):
        direct_poison.validate()


def test_prediction_ledger_is_append_only_and_detects_tampering(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path)
    inference = _engine(
        bundle_path, clock=_clock(NOW + timedelta(minutes=1))
    )
    rows = [_row(bundle_path, "ABC", SecurityType.STOCK, 1.0)]
    census = _census(inference, rows)
    prediction = _predict(inference, rows)[0]
    ledger = PredictionLedger(
        tmp_path / "ledger" / "predictions.jsonl",
        tmp_path / "anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    append_receipt = ledger.append_synthetic(
        prediction, synthetic_permit=_prediction_append_permit("firewall-ledger-first")
    )
    anchor = Path(append_receipt["anchor_path"])
    assert len(ledger.verify(anchor)) == 1
    assert [row["asset_id"] for row in ledger.verify_expected_census(anchor, census)] == [
        "asset-ABC"
    ]
    with pytest.raises(IntegrityError, match="duplicate"):
        ledger.append_synthetic(
            prediction,
            previous_anchor=anchor,
            synthetic_permit=_prediction_append_permit("firewall-ledger-duplicate"),
        )
    path = tmp_path / "ledger" / "predictions.jsonl"
    path.write_bytes(path.read_bytes().replace(b'"symbol":"ABC"', b'"symbol":"XYZ"'))
    with pytest.raises(IntegrityError, match="hash mismatch"):
        ledger.verify(anchor)


def test_feature_release_rejects_additional_manifest_payloads(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path)
    metadata = _load_bundle(bundle_path)
    original_release = (
        tmp_path
        / "accepted"
        / "features"
        / metadata.allowed_feature_release_ids[0]
    )
    original_manifest, _ = load_feature_release(
        original_release,
        accepted_release_root=tmp_path / "accepted",
    )
    stage = tmp_path / "extra-feature-stage"
    stage.mkdir()
    (stage / "features.json").write_bytes(
        (original_release / "features.json").read_bytes()
    )
    (stage / "unexpected.json").write_bytes(b"{}")
    poisoned_manifest = build_manifest(
        stage,
        ["features.json", "unexpected.json"],
        project=original_manifest.project,
        dataset=original_manifest.dataset,
        source_epoch=original_manifest.source_epoch,
        role=original_manifest.role,
        quality_state=original_manifest.quality_state,
        created_at=original_manifest.created_at,
        row_count=original_manifest.row_count,
        event_start=original_manifest.event_start,
        event_end=original_manifest.event_end,
        schema_fingerprint=original_manifest.schema_fingerprint,
        code_hash=original_manifest.code_hash,
        config_hash=original_manifest.config_hash,
        environment_hash=original_manifest.environment_hash,
    )
    accepted = tmp_path / "extra-feature-accepted"
    poisoned = AtomicReleasePublisher(accepted).publish(
        stage,
        poisoned_manifest,
    )
    with pytest.raises(IntegrityError, match="payload census"):
        load_feature_release(
            poisoned,
            accepted_release_root=accepted,
        )


def test_production_prediction_commit_rejects_synthetic_census_before_mutation(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path)
    engine = _engine(bundle_path, clock=_clock(NOW + timedelta(minutes=1)))
    metadata = engine.metadata
    feature_release = (
        tmp_path
        / "accepted"
        / "features"
        / metadata.allowed_feature_release_ids[0]
    )
    manifest, rows = load_feature_release(
        feature_release,
        accepted_release_root=tmp_path / "accepted",
    )
    assert len(rows) == 2
    census = _census(engine, rows)
    ledger_path = tmp_path / "atomic-ledger" / "predictions.jsonl"
    ledger = PredictionLedger(
        ledger_path,
        tmp_path / "atomic-anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )

    with pytest.raises(ContractError, match="synthetic-only permit"):
        engine.predict(
            rows,
            eligibility_census=census,
            synthetic_permit=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ContractError, match="one-row prediction append"):
        ledger.append(None)  # type: ignore[arg-type]
    assert not ledger_path.exists()

    with pytest.raises(
        ContractError,
        match="production prediction commit rejects synthetic-only",
    ):
        engine.predict_and_commit(
            feature_release_directory=feature_release,
            eligibility_census=census,
            prediction_ledger=ledger,
        )
    assert not ledger_path.exists()
    assert not (tmp_path / "atomic-anchors").exists()

    predictions = _predict(engine, rows)
    with pytest.raises(
        ContractError,
        match="production prediction commit rejects synthetic-only",
    ):
        ledger._append_census_from_engine(
            predictions,
            census=census,
            bundle_id=metadata.bundle_id,
            feature_release_id=manifest.release_id,
        )
    assert not ledger_path.exists()
    assert not (tmp_path / "atomic-anchors").exists()


def test_prediction_and_outcome_ledgers_exactly_cover_the_eligibility_census(
    tmp_path: Path,
) -> None:
    bundle_path = _bundle(tmp_path / "bundle")
    inference = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    rows = [
        _row(bundle_path, "ABC", SecurityType.STOCK, 1.0),
        _row(bundle_path, "XYZ", SecurityType.ETF, 0.5),
    ]
    census = _census(inference, rows)
    predictions = _predict(inference, rows)
    prediction_ledger = PredictionLedger(
        tmp_path / "ledger" / "predictions.jsonl",
        tmp_path / "anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    anchor = Path(prediction_ledger.append_synthetic(
        predictions[0],
        synthetic_permit=_prediction_append_permit("firewall-census-first"),
    )["anchor_path"])
    with pytest.raises(IntegrityError, match="exactly cover"):
        prediction_ledger.verify_expected_census(anchor, census)
    anchor = Path(
        prediction_ledger.append_synthetic(
            predictions[1],
            previous_anchor=anchor,
            synthetic_permit=_prediction_append_permit("firewall-census-second"),
        )["anchor_path"]
    )
    prediction_ledger.verify_expected_census(anchor, census)

    outcome_ledger = OutcomeLedger(
        tmp_path / "ledger" / "outcomes.jsonl",
        prediction_ledger,
        anchor_root=tmp_path / "outcome-anchors",
        clock=_clock(NOW + timedelta(days=8)),
    )

    def outcome_for(prediction):
        return OutcomeRow.create(
            prediction_id=prediction.prediction_id,
            eligibility_census_id=prediction.eligibility_census_id,
            revision_number=1,
            prior_revision_id=None,
            asset_id=prediction.asset_id,
            decision_session=prediction.decision_session,
            entry_session=prediction.decision_session + timedelta(days=1),
            exit_session=prediction.decision_session + timedelta(days=7),
            status=OutcomeStatus.MATURED,
            split_normalized_price_return=0.01,
            reason=None,
            calendar_release_id=prediction.calendar_release_id,
            bar_release_id="f" * 64,
            action_release_id=prediction.action_release_id,
            source_epoch=prediction.source_epoch,
            action_view_as_of=NOW + timedelta(days=7),
        )

    first_outcome_receipt = outcome_ledger.append_synthetic(
        outcome_for(predictions[0]),
        prediction_anchor=anchor,
        synthetic_permit=_outcome_append_permit("firewall-outcome-first"),
    )
    outcome_anchor = Path(first_outcome_receipt["anchor_path"])
    with pytest.raises(IntegrityError, match="exactly cover"):
        outcome_ledger.verify_expected_census(
            census,
            prediction_anchor=anchor,
            outcome_anchor=outcome_anchor,
        )
    with pytest.raises(IntegrityError, match="retained prior local anchor"):
        outcome_ledger.append_synthetic(
            outcome_for(predictions[1]),
            prediction_anchor=anchor,
            synthetic_permit=_outcome_append_permit(
                "firewall-outcome-missing-prior-anchor"
            ),
        )
    second_outcome_receipt = outcome_ledger.append_synthetic(
        outcome_for(predictions[1]),
        prediction_anchor=anchor,
        synthetic_permit=_outcome_append_permit("firewall-outcome-second"),
        previous_anchor=outcome_anchor,
    )
    outcome_anchor = Path(second_outcome_receipt["anchor_path"])
    assert len(
        outcome_ledger.verify_expected_census(
            census,
            prediction_anchor=anchor,
            outcome_anchor=outcome_anchor,
        )
    ) == 2


def test_outcome_anchor_recovery_requires_exact_review_and_preserves_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        ledger,
        intended,
        prediction_anchor,
        recovery_clock,
        anchor_root,
        _,
    ) = _interrupted_outcome_anchor_fixture(tmp_path, monkeypatch)
    ledger_path = tmp_path / "ledger" / "outcomes.jsonl"
    committed = ledger_path.read_bytes()
    plan = ledger.build_unanchored_tail_recovery_plan(
        intended,
        prediction_anchor=prediction_anchor,
    )
    assert plan["mode"] == "OUTCOME_ANCHOR_RECOVERY_PLAN_ONLY_NO_WRITES"
    assert plan["execution_authorized"] is False
    assert plan["outcome_access_authorized"] is False
    assert not anchor_root.exists()

    authorization = create_local_integrity_record(
        scope=str(plan["scope"]),
        subject_id=str(plan["subject_id"]),
        bindings=plan["bindings"],
        clock=recovery_clock,
    )
    result = ledger.recover_unanchored_tail(
        intended,
        prediction_anchor=prediction_anchor,
        recovery_authorization=authorization,
    )
    anchor = Path(str(result["anchor_path"]))

    assert ledger_path.read_bytes() == committed
    assert result["recovery_plan_id"] == plan["recovery_plan_id"]
    assert result["recovery_record_id"] == authorization.record_id
    assert json.loads((anchor / "receipt.json").read_text(encoding="utf-8"))[
        "schema_version"
    ] == 2
    assert json.loads((anchor / "recovery.json").read_text(encoding="utf-8"))[
        "record_id"
    ] == authorization.record_id
    assert len(ledger.verify(anchor)) == 1
    with pytest.raises(IntegrityError, match="already anchored"):
        ledger.build_unanchored_tail_recovery_plan(
            intended,
            prediction_anchor=prediction_anchor,
        )


def test_outcome_anchor_recovery_rejects_substitution_stale_anchor_and_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        ledger,
        intended,
        prediction_anchor,
        recovery_clock,
        anchor_root,
        _,
    ) = _interrupted_outcome_anchor_fixture(tmp_path, monkeypatch)
    substitute = _matured_outcome(
        UnderlyingPrediction.from_dict(
            next(iter(ledger._predictions.verify(prediction_anchor)))["payload"]
        ),
        value=0.02,
    )
    with pytest.raises(IntegrityError, match="intended record differs"):
        ledger.build_unanchored_tail_recovery_plan(
            substitute,
            prediction_anchor=prediction_anchor,
        )
    with pytest.raises(IntegrityError, match="cannot claim a predecessor"):
        ledger.build_unanchored_tail_recovery_plan(
            intended,
            prediction_anchor=prediction_anchor,
            previous_anchor=prediction_anchor,
        )

    plan = ledger.build_unanchored_tail_recovery_plan(
        intended,
        prediction_anchor=prediction_anchor,
    )
    with pytest.raises(ContractError, match="exact local integrity record"):
        ledger.recover_unanchored_tail(
            intended,
            prediction_anchor=prediction_anchor,
            recovery_authorization=None,  # type: ignore[arg-type]
        )
    forged_bindings = dict(plan["bindings"])
    forged_bindings["head_hash"] = "0" * 64
    forged = create_local_integrity_record(
        scope=str(plan["scope"]),
        subject_id=str(plan["subject_id"]),
        bindings=forged_bindings,
        clock=recovery_clock,
    )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="bindings differ",
    ):
        ledger.recover_unanchored_tail(
            intended,
            prediction_anchor=prediction_anchor,
            recovery_authorization=forged,
        )
    assert not anchor_root.exists()


def test_outcome_anchor_recovery_requires_the_exact_prior_outcome_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        ledger,
        intended,
        prediction_anchor,
        recovery_clock,
        _,
        previous_anchor,
    ) = _interrupted_outcome_anchor_fixture(
        tmp_path,
        monkeypatch,
        with_prior_anchor=True,
    )
    assert previous_anchor is not None
    with pytest.raises(IntegrityError, match="approved root"):
        ledger.build_unanchored_tail_recovery_plan(
            intended,
            prediction_anchor=prediction_anchor,
            previous_anchor=prediction_anchor,
        )

    plan = ledger.build_unanchored_tail_recovery_plan(
        intended,
        prediction_anchor=prediction_anchor,
        previous_anchor=previous_anchor,
    )
    authorization = create_local_integrity_record(
        scope=str(plan["scope"]),
        subject_id=str(plan["subject_id"]),
        bindings=plan["bindings"],
        clock=recovery_clock,
    )
    result = ledger.recover_unanchored_tail(
        intended,
        prediction_anchor=prediction_anchor,
        previous_anchor=previous_anchor,
        recovery_authorization=authorization,
    )
    recovered_anchor = Path(str(result["anchor_path"]))
    receipt = json.loads(
        (recovered_anchor / "receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["previous_anchor_id"] == previous_anchor.name
    assert len(ledger.verify(recovered_anchor)) == 2


def test_outcome_anchor_recovery_tamper_and_interruption_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        ledger,
        intended,
        prediction_anchor,
        recovery_clock,
        anchor_root,
        _,
    ) = _interrupted_outcome_anchor_fixture(tmp_path, monkeypatch)
    ledger_path = tmp_path / "ledger" / "outcomes.jsonl"
    committed = ledger_path.read_bytes()
    plan = ledger.build_unanchored_tail_recovery_plan(
        intended,
        prediction_anchor=prediction_anchor,
    )
    authorization = create_local_integrity_record(
        scope=str(plan["scope"]),
        subject_id=str(plan["subject_id"]),
        bindings=plan["bindings"],
        clock=recovery_clock,
    )
    original_load = ledger._anchors.load

    def interrupt_pending_load(
        directory: Path,
        *,
        allow_pending: bool = False,
    ):
        if allow_pending:
            raise RuntimeError("simulated interrupted recovery publication")
        return original_load(directory, allow_pending=allow_pending)

    monkeypatch.setattr(ledger._anchors, "load", interrupt_pending_load)
    with pytest.raises(RuntimeError, match="interrupted recovery"):
        ledger.recover_unanchored_tail(
            intended,
            prediction_anchor=prediction_anchor,
            recovery_authorization=authorization,
        )
    monkeypatch.setattr(ledger._anchors, "load", original_load)

    pending = tuple(anchor_root.glob(".pending-*"))
    assert len(pending) == 1
    assert (pending[0] / "receipt.json").is_file()
    assert (pending[0] / "recovery.json").is_file()
    assert ledger_path.read_bytes() == committed
    with pytest.raises(IntegrityError, match="partial ledger anchor evidence"):
        ledger.build_unanchored_tail_recovery_plan(
            intended,
            prediction_anchor=prediction_anchor,
        )

    clean_root = tmp_path / "clean-retry"
    clean_ledger, clean_intended, clean_prediction_anchor, clean_clock, _, _ = (
        _interrupted_outcome_anchor_fixture(clean_root, monkeypatch)
    )
    clean_plan = clean_ledger.build_unanchored_tail_recovery_plan(
        clean_intended,
        prediction_anchor=clean_prediction_anchor,
    )
    clean_authorization = create_local_integrity_record(
        scope=str(clean_plan["scope"]),
        subject_id=str(clean_plan["subject_id"]),
        bindings=clean_plan["bindings"],
        clock=clean_clock,
    )
    clean_result = clean_ledger.recover_unanchored_tail(
        clean_intended,
        prediction_anchor=clean_prediction_anchor,
        recovery_authorization=clean_authorization,
    )
    recovery_path = Path(str(clean_result["anchor_path"])) / "recovery.json"
    recovery_path.write_bytes(recovery_path.read_bytes().replace(b'"record_id"', b'"record_ix"'))
    with pytest.raises(IntegrityError, match="recovery evidence"):
        clean_ledger.verify(Path(str(clean_result["anchor_path"])))


def test_local_tamper_evident_anchor_rejects_tail_truncation_rewrite_and_extra_tree_entry(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path / "bundle")
    engine = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    predictions = _predict(engine,
        [
            _row(bundle_path, "ABC", SecurityType.STOCK, 1.0),
            _row(bundle_path, "XYZ", SecurityType.STOCK, 0.5),
        ],
    )
    path = tmp_path / "ledger" / "predictions.jsonl"
    ledger = PredictionLedger(
        path,
        tmp_path / "anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    anchor1 = Path(ledger.append_synthetic(
        predictions[0],
        synthetic_permit=_prediction_append_permit("firewall-anchor-first"),
    )["anchor_path"])
    anchor2 = Path(ledger.append_synthetic(
        predictions[1],
        previous_anchor=anchor1,
        synthetic_permit=_prediction_append_permit("firewall-anchor-second"),
    )["anchor_path"])
    committed = path.read_bytes()
    path.write_bytes(committed.splitlines(keepends=True)[0])
    with pytest.raises(IntegrityError, match="local tamper-evident head anchor"):
        ledger.verify(anchor2)

    alternate_path = tmp_path / "alternate" / "predictions.jsonl"
    alternate = HashChainLedger(
        alternate_path,
        "underlying_prediction_v1",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    alternate.append(predictions[1].as_dict())
    path.write_bytes(alternate_path.read_bytes())
    with pytest.raises(IntegrityError, match="local tamper-evident head anchor"):
        ledger.verify(anchor1)

    path.write_bytes(committed)
    (anchor2 / "stray.txt").write_text("poison", encoding="utf-8")
    with pytest.raises(IntegrityError, match="anchor tree"):
        ledger.verify(anchor2)


def test_ledger_verification_recomputes_prediction_id_even_with_matching_anchor(tmp_path: Path) -> None:
    bundle_path = _bundle(tmp_path / "bundle")
    inference = _engine(
        bundle_path,
        clock=_clock(NOW + timedelta(minutes=1)),
    )
    prediction = _predict(inference,
        [_row(bundle_path, "ABC", SecurityType.STOCK, 1.0)],
    )[0]
    forged = prediction.as_dict()
    forged["prediction_id"] = "0" * 64
    path = tmp_path / "forged-ledger" / "predictions.jsonl"
    raw = HashChainLedger(
        path,
        "underlying_prediction_v1",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    raw.append(forged)
    anchors = LedgerAnchorStore(
        tmp_path / "forged-anchors",
        raw,
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    anchor = anchors.create(
        raw.read_verified(),
        previous_anchor=None,
    )
    ledger = PredictionLedger(
        path,
        tmp_path / "forged-anchors",
        clock=_clock(NOW + timedelta(minutes=2)),
    )
    with pytest.raises(ContractError, match="prediction ID"):
        ledger.verify(anchor)


def test_prediction_payload_rejects_option_or_outcome_poison() -> None:
    with pytest.raises(ContractError, match="forbidden"):
        assert_underlying_only_payload({"expected_five_session_return": 0.01, "strike": 100})
    with pytest.raises(ContractError, match="forbidden"):
        assert_underlying_only_payload({"expected_five_session_return": 0.01, "realized_return": 0.02})


def _trial(release_directories: tuple[Path, ...], hypothesis: str = "h1") -> TrialSpec:
    identity = repository_trial_identity(REPO)
    bindings = tuple(
        sorted(
            (ReleaseBinding.from_manifest(verify_release(path)) for path in release_directories),
            key=lambda binding: binding.release_id,
        )
    )
    return TrialSpec(
        hypothesis_id=hypothesis,
        evidence_class="REGISTERED_HISTORICAL_DISCOVERY",
        data_release_ids=tuple(binding.release_id for binding in bindings),
        release_bindings=bindings,
        feature_schema_id=identity.feature_schema_id,
        outcome_schema_id=identity.outcome_schema_id,
        split_plan_id=identity.split_plan_id,
        model_family=identity.model_family,
        primary_metric=identity.primary_metric,
        primary_gate_id=sha256_bytes(canonical_json_bytes(_gate_policy().as_dict())),
        robustness_policy_id=identity.robustness_policy_id,
        cost_policy_id=identity.cost_policy_id,
        trial_family_id="family-v1",
        census_anchor_id="4" * 64,
        trial_family_anchor_id="5" * 64,
        evaluator_closure_hash=identity.evaluator_closure_hash,
        governance_contract_hash=identity.governance_contract_hash,
        code_hash=identity.code_hash,
        config_hash=identity.config_hash,
        environment_hash=identity.environment_hash,
    )


def _outer_permit(
    registry: TrialRegistry,
    spec: TrialSpec,
    trial_id: str,
    *,
    permit_issued_at: datetime = datetime(2026, 7, 15, 1, tzinfo=timezone.utc),
):
    registration = registry.authorize(trial_id)
    registration_hash = sha256_bytes(canonical_json_bytes(registration))
    holdout = build_holdout_receipt(
        trial_id=trial_id,
        state="LOCKED",
        clock=_clock(datetime(2026, 7, 15, 0, 15, tzinfo=timezone.utc)),
    )
    execution_evidence = EvaluationExecutionEvidence.create(
        spec=spec,
        evaluation_scope="OUTER_SCREEN",
        artifacts=_evaluation_input_commitments("OUTER_SCREEN"),
        repository_root=REPO,
    )
    bindings = {
        "trial_registry_binding_id": registry.registry_binding_id,
        "registration_hash": registration_hash,
        "evaluation_scope": "OUTER_SCREEN",
        "evaluation_input_hash": execution_evidence.evaluation_input_hash,
        "evaluator_code_hash": execution_evidence.evaluator_code_hash,
        "evaluator_closure_hash": spec.evaluator_closure_hash,
        "census_anchor_id": spec.census_anchor_id,
        "trial_family_anchor_id": spec.trial_family_anchor_id,
        "governance_contract_hash": spec.governance_contract_hash,
        "primary_gate_id": spec.primary_gate_id,
        "robustness_policy_id": spec.robustness_policy_id,
        "release_bindings_hash": release_bindings_hash(spec.release_bindings),
        "holdout_receipt_id": holdout.receipt_id,
        "execution_evidence_id": execution_evidence.evidence_id,
        "repository_trial_identity_id": execution_evidence.repository_trial_identity_id,
    }
    authorization = create_local_integrity_record(
        scope="AUTHORIZE_OUTER_SCREEN",
        subject_id=trial_id,
        bindings=bindings,
        clock=_clock(datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc)),
    )
    permit = registry.with_clock(_clock(permit_issued_at)).issue_permit(
        trial_id,
        evaluation_scope="OUTER_SCREEN",
        execution_evidence=execution_evidence,
        holdout_receipt=holdout,
        action_record=authorization,
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    return permit, holdout, execution_evidence


def _final_permit(
    registry: TrialRegistry,
    spec: TrialSpec,
    trial_id: str,
    *,
    initial_holdout,
    permit_issued_at: datetime = datetime(2026, 7, 15, 3, tzinfo=timezone.utc),
    evaluation_input_hash: str = "a" * 64,
    evaluator_code_hash: str = "b" * 64,
):
    registration = registry.authorize(trial_id)
    registration_hash = sha256_bytes(canonical_json_bytes(registration))
    unlocked = build_holdout_receipt(
        trial_id=trial_id,
        state="UNLOCKED_ONCE",
        previous=initial_holdout,
        clock=_clock(datetime(2026, 7, 15, 2, 30, tzinfo=timezone.utc)),
    )
    execution_evidence = EvaluationExecutionEvidence.create(
        spec=spec,
        evaluation_scope="FINAL_HOLDOUT",
        artifacts=_evaluation_input_commitments("FINAL_HOLDOUT"),
        repository_root=REPO,
    )
    bindings = {
        "trial_registry_binding_id": registry.registry_binding_id,
        "registration_hash": registration_hash,
        "evaluation_scope": "FINAL_HOLDOUT",
        "evaluation_input_hash": execution_evidence.evaluation_input_hash,
        "evaluator_code_hash": execution_evidence.evaluator_code_hash,
        "evaluator_closure_hash": spec.evaluator_closure_hash,
        "census_anchor_id": spec.census_anchor_id,
        "trial_family_anchor_id": spec.trial_family_anchor_id,
        "governance_contract_hash": spec.governance_contract_hash,
        "primary_gate_id": spec.primary_gate_id,
        "robustness_policy_id": spec.robustness_policy_id,
        "release_bindings_hash": release_bindings_hash(spec.release_bindings),
        "holdout_receipt_id": unlocked.receipt_id,
        "execution_evidence_id": execution_evidence.evidence_id,
        "repository_trial_identity_id": execution_evidence.repository_trial_identity_id,
    }
    authorization = create_local_integrity_record(
        scope="AUTHORIZE_FINAL_HOLDOUT",
        subject_id=trial_id,
        bindings=bindings,
        clock=_clock(datetime(2026, 7, 15, 2, 45, tzinfo=timezone.utc)),
    )
    permit = registry.with_clock(_clock(permit_issued_at)).issue_permit(
        trial_id,
        evaluation_scope="FINAL_HOLDOUT",
        execution_evidence=execution_evidence,
        holdout_receipt=unlocked,
        initial_holdout_receipt=initial_holdout,
        action_record=authorization,
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    return permit, unlocked, execution_evidence


def _gate_for_state(
    registry: TrialRegistry,
    permit,
    *,
    state: GateState,
    evaluated_at: datetime = datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc),
):
    gate = _seed_test_only_synthetic_gate_receipt_for_downstream_mechanics(
        registry,
        permit,
        state=state,
        evaluated_at=evaluated_at,
    )
    assert gate.state == state.value
    return gate


def _evaluation_result(permit, holdout, gate) -> dict[str, object]:
    result: dict[str, object] = {
        "trial_id": permit.trial_id,
        "evaluation_scope": permit.evaluation_scope,
        "state": gate.state,
        "evaluation_input_hash": permit.evaluation_input_hash,
        "evaluator_closure_hash": permit.evaluator_closure_hash,
        "authorization_receipt_id": permit.authorization_receipt_id,
        "holdout_receipt_id": holdout.receipt_id,
        "gate_receipt_id": gate.receipt_id,
        "robustness_policy_id": permit.robustness_policy_id,
        "robustness_evidence_hash": gate.robustness_evidence_hash,
        "evaluation_closed": True,
    }
    result["result_artifact_hash"] = sha256_bytes(
        canonical_json_bytes(
            {name: result[name] for name in sorted(result)}
        )
    )
    return result


def test_trial_registration_and_execution_revalidate_live_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_directories = (
        _accepted_release(
            tmp_path,
            "identity_trial_bars",
            "active_historical",
            "historical-v1",
        ),
        _accepted_release(
            tmp_path,
            "identity_trial_features",
            "feature_only",
            "historical-v1",
        ),
    )
    governance_root = tmp_path / "governance"
    governance_root.mkdir()
    registry = TrialRegistry(
        governance_root / "trials.jsonl",
        governance_root / "evaluations.jsonl",
        accepted_release_root=tmp_path / "accepted",
        governance_root=governance_root,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="live-identity-trial-registry",
            scope="SYNTHETIC_TRIAL_REGISTRY",
        ),
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    spec = _trial(release_directories)
    alternate_checkout = tmp_path / "alternate_checkout"
    alternate_checkout.mkdir()
    with pytest.raises(ContractError, match="executing package checkout"):
        repository_trial_identity(alternate_checkout)
    with pytest.raises(ContractError, match="exact independent gate policy"):
        registry.register(
            spec,
            verified_release_directories=release_directories,
            repository_root=REPO,
            gate_policy=object(),
        )
    with pytest.raises(ContractError, match="live repository execution identity"):
        registry.register(
            replace(spec, code_hash="0" * 64),
            verified_release_directories=release_directories,
            repository_root=REPO,
            gate_policy=_gate_policy(),
        )
    trial_id = registry.register(
        spec,
        verified_release_directories=release_directories,
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    registration = registry.authorize(trial_id)
    for result_artifacts in (
        {"gate/policy": _gate_policy().as_dict()},
        {"evaluation_summary": {"conservative_pbo": 0.10}},
        {"innocent_name": {"favorable_value": 0.99}},
    ):
        with pytest.raises(
            ContractError,
            match="pre-permit evaluation input cannot contain gate or result artifacts",
        ):
            EvaluationExecutionEvidence.create(
                spec=spec,
                evaluation_scope="OUTER_SCREEN",
                artifacts=result_artifacts,
                repository_root=REPO,
            )
    evidence = EvaluationExecutionEvidence.create(
        spec=spec,
        evaluation_scope="OUTER_SCREEN",
        artifacts=_evaluation_input_commitments("OUTER_SCREEN"),
        repository_root=REPO,
    )
    with pytest.raises(TypeError):
        EvaluationExecutionEvidence.create(  # type: ignore[call-arg]
            spec=spec,
            evaluation_scope="OUTER_SCREEN",
            artifacts=_evaluation_input_commitments("OUTER_SCREEN"),
            repository_root=REPO,
            evaluator_source="caller-selected evaluator",
        )
    holdout = build_holdout_receipt(
        trial_id=trial_id,
        state="LOCKED",
        clock=_clock(datetime(2026, 7, 15, 0, 15, tzinfo=timezone.utc)),
    )
    bindings = {
        "trial_registry_binding_id": registry.registry_binding_id,
        "registration_hash": sha256_bytes(canonical_json_bytes(registration)),
        "evaluation_scope": "OUTER_SCREEN",
        "evaluation_input_hash": evidence.evaluation_input_hash,
        "evaluator_code_hash": evidence.evaluator_code_hash,
        "evaluator_closure_hash": spec.evaluator_closure_hash,
        "census_anchor_id": spec.census_anchor_id,
        "trial_family_anchor_id": spec.trial_family_anchor_id,
        "governance_contract_hash": spec.governance_contract_hash,
        "primary_gate_id": spec.primary_gate_id,
        "robustness_policy_id": spec.robustness_policy_id,
        "release_bindings_hash": release_bindings_hash(spec.release_bindings),
        "holdout_receipt_id": holdout.receipt_id,
        "execution_evidence_id": evidence.evidence_id,
        "repository_trial_identity_id": evidence.repository_trial_identity_id,
    }
    action = create_local_integrity_record(
        scope="AUTHORIZE_OUTER_SCREEN",
        subject_id=trial_id,
        bindings=bindings,
        clock=_clock(datetime(2026, 7, 15, 0, 30, tzinfo=timezone.utc)),
    )
    with pytest.raises(EvaluationAuthorizationError, match="execution evidence"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, tzinfo=timezone.utc))
        ).issue_permit(
            trial_id,
            evaluation_scope="OUTER_SCREEN",
            execution_evidence=replace(evidence, evaluator_code_hash="0" * 64),
            holdout_receipt=holdout,
            action_record=action,
            repository_root=REPO,
            gate_policy=_gate_policy(),
        )
    with pytest.raises(EvaluationAuthorizationError, match="execution contract"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, tzinfo=timezone.utc))
        ).issue_permit(
            trial_id,
            evaluation_scope="OUTER_SCREEN",
            execution_evidence=evidence,
            holdout_receipt=holdout,
            action_record=action,
            repository_root=REPO,
            gate_policy=_gate_policy(minimum_sessions=21),
        )
    live_identity = repository_trial_identity(REPO)
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.trials.repository_trial_identity",
        lambda _root: replace(live_identity, code_hash="0" * 64),
    )
    with pytest.raises(EvaluationAuthorizationError, match="live repository"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, tzinfo=timezone.utc))
        ).issue_permit(
            trial_id,
            evaluation_scope="OUTER_SCREEN",
            execution_evidence=evidence,
            holdout_receipt=holdout,
            action_record=action,
            repository_root=REPO,
            gate_policy=_gate_policy(),
        )
    assert registry.permits.read_verified() == []


def test_trial_registry_blocks_unregistered_and_semantic_mutation(tmp_path: Path) -> None:
    release_directories = (
        _accepted_release(tmp_path, "trial_bars", "active_historical", "historical-v1"),
        _accepted_release(tmp_path, "trial_features", "feature_only", "historical-v1"),
    )
    governance_root = tmp_path / "governance"
    governance_root.mkdir()
    registry = TrialRegistry(
        governance_root / "trials.jsonl",
        governance_root / "evaluations.jsonl",
        accepted_release_root=tmp_path / "accepted",
        governance_root=governance_root,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-trial-registry",
            scope="SYNTHETIC_TRIAL_REGISTRY",
        ),
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    with pytest.raises(EvaluationAuthorizationError):
        registry.authorize("missing")
    first = _trial(release_directories)
    trial_id = registry.register(
        first,
        verified_release_directories=release_directories,
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    assert registry.registry.read_verified()[0]["time_authority"] == (
        "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    )
    assert registry.authorize(trial_id)["hypothesis_id"] == "h1"
    mutated = _trial(release_directories, "h2")
    assert mutated.trial_id != trial_id
    permit, holdout, execution_evidence = _outer_permit(
        registry,
        first,
        trial_id,
    )
    assert registry._gate_receipts.read_verified() == []
    with pytest.raises(ContractError, match="predeclared trial policy"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).build_gate_receipt(
            permit,
            policy=_gate_policy(minimum_sessions=21),
            evidence={"state": GateState.PASS_HISTORICAL_DISCOVERY_SCREEN.value},
        )
    with pytest.raises(ContractError, match="exact post-permit evaluation evidence"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).build_gate_receipt(
            permit,
            policy=_gate_policy(),
            evidence={"state": GateState.PASS_HISTORICAL_DISCOVERY_SCREEN.value},
        )
    assert registry._gate_receipts.read_verified() == []
    wrong_permit_evidence = GateEvaluationEvidence.create(
        evaluation_permit_id="f" * 64,
        metrics={"stock_long": _sleeve_metric(sessions=10)},
    )
    with pytest.raises(ContractError, match="another permit"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).build_gate_receipt(
            permit,
            policy=_gate_policy(),
            evidence=wrong_permit_evidence,
        )
    inconclusive_gate = registry.with_clock(
        _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
    ).build_gate_receipt(
        permit,
        policy=_gate_policy(),
        evidence=GateEvaluationEvidence.create(
            evaluation_permit_id=permit.permit_id,
            metrics={"stock_long": _sleeve_metric(sessions=10)},
        ),
    )
    assert inconclusive_gate.state == GateState.INCONCLUSIVE_DATA_OR_POWER.value
    with pytest.raises(IntegrityError, match="duplicate"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).build_gate_receipt(
            permit,
            policy=_gate_policy(),
            evidence=GateEvaluationEvidence.create(
                evaluation_permit_id=permit.permit_id,
                metrics={"stock_long": _sleeve_metric(sessions=10)},
            ),
        )
    forged_unsigned = {**permit.unsigned_dict(), "evaluator_code_hash": "0" * 64}
    forged_permit = type(permit)(
        **forged_unsigned,
        permit_id=sha256_bytes(canonical_json_bytes(forged_unsigned)),
    )
    forged_permit.validate()
    with pytest.raises(EvaluationAuthorizationError, match="registry-issued"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).record_evaluation(
            forged_permit,
            {},
            gate_receipt=inconclusive_gate,
        )
    tampered_evidence = _evaluation_result(permit, holdout, inconclusive_gate)
    tampered_evidence["robustness_evidence_hash"] = "b" * 64
    tampered_evidence["result_artifact_hash"] = sha256_bytes(
        canonical_json_bytes(
            {
                name: tampered_evidence[name]
                for name in sorted(tampered_evidence)
                if name != "result_artifact_hash"
            }
        )
    )
    with pytest.raises(EvaluationAuthorizationError, match="gate or robustness"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 45, tzinfo=timezone.utc))
        ).record_evaluation(
            permit,
            tampered_evidence,
            gate_receipt=inconclusive_gate,
        )
    forged_gate = replace(
        inconclusive_gate,
        robustness_policy_hash="0" * 64,
        receipt_id="",
    )
    forged_gate = replace(
        forged_gate,
        receipt_id=sha256_bytes(canonical_json_bytes(forged_gate.unsigned_dict())),
    )
    forged_gate.validate()
    with pytest.raises(EvaluationAuthorizationError, match="gate or robustness"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 45, tzinfo=timezone.utc))
        ).record_evaluation(
            permit,
            _evaluation_result(permit, holdout, forged_gate),
            gate_receipt=forged_gate,
        )
    assert registry.verify_issued_gate_receipt(
        permit,
        inconclusive_gate,
    ) == inconclusive_gate.as_dict()
    directly_forged_gate = replace(
        inconclusive_gate,
        metrics_hash="0" * 64,
        receipt_id="",
    )
    directly_forged_gate = replace(
        directly_forged_gate,
        receipt_id=sha256_bytes(
            canonical_json_bytes(directly_forged_gate.unsigned_dict())
        ),
    )
    directly_forged_gate.validate()
    with pytest.raises(
        EvaluationAuthorizationError,
        match="exact registry-issued gate receipt",
    ):
        registry.verify_issued_gate_receipt(permit, directly_forged_gate)
    registry.with_clock(_clock(datetime(2026, 7, 15, 2, tzinfo=timezone.utc))).record_evaluation(
        permit,
        _evaluation_result(permit, holdout, inconclusive_gate),
        gate_receipt=inconclusive_gate,
    )
    with pytest.raises(IntegrityError, match="duplicate"):
        registry.with_clock(_clock(datetime(2026, 7, 15, 3, tzinfo=timezone.utc))).record_evaluation(
            permit,
            _evaluation_result(permit, holdout, inconclusive_gate),
            gate_receipt=inconclusive_gate,
        )


def test_trial_evaluation_chronology_and_malformed_registration_fail_closed(tmp_path: Path) -> None:
    release_directories = (
        _accepted_release(tmp_path, "trial_bars", "active_historical", "historical-v1"),
        _accepted_release(tmp_path, "trial_features", "feature_only", "historical-v1"),
    )
    governance_root = tmp_path / "governance"
    governance_root.mkdir()
    registry_path = governance_root / "trials.jsonl"
    evaluations_path = governance_root / "evaluations.jsonl"
    registry = TrialRegistry(
        registry_path,
        evaluations_path,
        accepted_release_root=tmp_path / "accepted",
        governance_root=governance_root,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-chronology-registry",
            scope="SYNTHETIC_TRIAL_REGISTRY",
        ),
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    spec = _trial(release_directories)
    trial_id = registry.register(
        spec,
        verified_release_directories=release_directories,
        repository_root=REPO,
        gate_policy=_gate_policy(),
    )
    assert registry.registry.read_verified()[0]["recorded_at"] == "2026-07-15T00:00:00Z"
    with pytest.raises(EvaluationAuthorizationError, match="after trial registration"):
        _outer_permit(
            registry,
            spec,
            trial_id,
            permit_issued_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
    permit, holdout, _ = _outer_permit(registry, spec, trial_id)
    gate = _gate_for_state(
        registry,
        permit,
        state=GateState.PASS_HISTORICAL_DISCOVERY_SCREEN,
    )
    for invalid_scope in ("", "UNREGISTERED_SCOPE"):
        payload = gate.unsigned_dict()
        payload["evaluation_scope"] = invalid_scope
        payload["receipt_id"] = sha256_bytes(canonical_json_bytes(payload))
        with pytest.raises(ContractError, match="scope"):
            GateReceipt.from_dict(payload)
    with pytest.raises(EvaluationAuthorizationError, match="fields"):
        registry.with_clock(_clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))).record_evaluation(
            permit,
            {"trial_id": trial_id, "evaluation_input_hash": permit.evaluation_input_hash},
            gate_receipt=gate,
        )
    with pytest.raises(EvaluationAuthorizationError, match="after permit"):
        registry.record_evaluation(
            permit,
            _evaluation_result(permit, holdout, gate),
            gate_receipt=gate,
        )
    with pytest.raises(
        EvaluationAuthorizationError,
        match="PASS_HISTORICAL_DISCOVERY_SCREEN",
    ):
        _final_permit(
            registry,
            spec,
            trial_id,
            initial_holdout=holdout,
        )
    registry.with_clock(
        _clock(datetime(2026, 7, 15, 2, tzinfo=timezone.utc))
    ).record_evaluation(
        permit,
        _evaluation_result(permit, holdout, gate),
        gate_receipt=gate,
    )
    final_permit, unlocked, _ = _final_permit(
        registry,
        spec,
        trial_id,
        initial_holdout=holdout,
    )
    assert final_permit.evaluation_scope == "FINAL_HOLDOUT"
    assert final_permit.holdout_receipt_id == unlocked.receipt_id
    with pytest.raises(EvaluationAuthorizationError, match="already has an issued permit"):
        _final_permit(
            registry,
            spec,
            trial_id,
            initial_holdout=holdout,
            permit_issued_at=datetime(2026, 7, 15, 4, tzinfo=timezone.utc),
            evaluation_input_hash="c" * 64,
            evaluator_code_hash="d" * 64,
        )
    assert len(registry.permits.read_verified()) == 2

    malformed_path = governance_root / "malformed-trials.jsonl"
    malformed = TrialRegistry(
        malformed_path,
        governance_root / "malformed-evaluations.jsonl",
        accepted_release_root=tmp_path / "accepted",
        governance_root=governance_root,
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-malformed-registry",
            scope="SYNTHETIC_TRIAL_REGISTRY",
        ),
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    malformed.registry.append(
        {"trial_id": "a" * 64},
    )
    with pytest.raises(EvaluationAuthorizationError, match="malformed"):
        malformed.authorize("a" * 64)


def test_all_four_sleeves_are_binding_and_underpowered_is_inconclusive() -> None:
    policy = _gate_policy()
    passing = _sleeve_metric()
    sleeves = ("stock_long", "stock_short", "etf_long", "etf_short")
    metrics = {sleeve: passing for sleeve in sleeves}
    assert policy.aggregate(metrics) is GateState.PASS_HISTORICAL_DISCOVERY_SCREEN

    missing = {sleeve: passing for sleeve in sleeves}
    del missing["etf_short"]
    assert (
        policy.evaluate(missing)["etf_short"]
        is GateState.INCONCLUSIVE_DATA_OR_POWER
    )
    assert policy.aggregate(missing) is GateState.INCONCLUSIVE_DATA_OR_POWER

    underpowered = {sleeve: passing for sleeve in sleeves}
    underpowered["etf_short"] = _sleeve_metric(sessions=19)
    assert policy.aggregate(underpowered) is GateState.INCONCLUSIVE_DATA_OR_POWER

    definite_failure = {sleeve: passing for sleeve in sleeves}
    definite_failure["stock_short"] = _sleeve_metric(
        effect=-0.001,
        confidence_lower=-0.002,
        confidence_upper=-0.0001,
    )
    definite_failure["etf_short"] = _sleeve_metric(sessions=19)
    assert policy.aggregate(definite_failure) is GateState.FAIL_NO_EDGE

    robustness = {sleeve: passing for sleeve in sleeves}
    robustness["stock_long"] = replace(
        passing,
        robustness_state="INCONCLUSIVE_ROBUSTNESS",
    )
    assert policy.aggregate(robustness) is GateState.INCONCLUSIVE_ROBUSTNESS
    robustness["stock_short"] = _sleeve_metric(
        effect=-0.001,
        confidence_lower=-0.002,
        confidence_upper=-0.0001,
    )
    assert policy.aggregate(robustness) is GateState.FAIL_NO_EDGE


def test_gate_distinguishes_invalid_power_pbo_and_strict_economic_confidence() -> None:
    policy = _gate_policy()
    sleeves = ("stock_long", "stock_short", "etf_long", "etf_short")
    base = {name: _sleeve_metric() for name in sleeves}

    equality = dict(base)
    equality["stock_long"] = _sleeve_metric(confidence_lower=0.001)
    assert policy.aggregate(equality) is GateState.INCONCLUSIVE_EFFECT

    invalid = dict(base)
    invalid["stock_long"] = replace(_sleeve_metric(), numerical_valid=False)
    assert policy.aggregate(invalid) is GateState.INVALID
    invalid["stock_long"] = replace(_sleeve_metric(), lineage_valid=False)
    assert policy.aggregate(invalid) is GateState.INVALID

    pbo_mid = dict(base)
    pbo_mid["stock_long"] = replace(_sleeve_metric(), conservative_pbo=0.35)
    assert policy.aggregate(pbo_mid) is GateState.INCONCLUSIVE_DATA_OR_POWER
    pbo_indeterminate_boundary = dict(base)
    pbo_indeterminate_boundary["stock_long"] = replace(
        _sleeve_metric(),
        conservative_pbo=policy.maximum_conservative_pbo,
    )
    assert (
        policy.aggregate(pbo_indeterminate_boundary)
        is GateState.INCONCLUSIVE_DATA_OR_POWER
    )
    pbo_failure_boundary = dict(base)
    pbo_failure_boundary["stock_long"] = replace(
        _sleeve_metric(),
        conservative_pbo=policy.pbo_failure_threshold,
    )
    assert (
        policy.aggregate(pbo_failure_boundary)
        is GateState.FAIL_MULTIPLICITY_OR_CONTROL
    )
    pbo_high = dict(base)
    pbo_high["stock_long"] = replace(_sleeve_metric(), conservative_pbo=0.51)
    assert policy.aggregate(pbo_high) is GateState.FAIL_MULTIPLICITY_OR_CONTROL

    single = dict(base)
    single["stock_long"] = replace(
        _sleeve_metric(),
        pbo_applicability="NOT_APPLICABLE_SINGLE_PREDECLARED_CONFIGURATION",
        conservative_pbo=None,
    )
    assert policy.aggregate(single) is GateState.PASS_HISTORICAL_DISCOVERY_SCREEN

    with pytest.raises(ContractError, match="Romano-Wolf alpha"):
        replace(policy, rw_alpha=0.0).validate()


def test_gate_exposes_every_documented_terminal_state_in_binding_order() -> None:
    policy = _gate_policy()
    sleeves = ("stock_long", "stock_short", "etf_long", "etf_short")
    passing = _sleeve_metric()

    cases = (
        (
            GateState.INVALID,
            replace(passing, numerical_valid=False),
        ),
        (
            GateState.INCONCLUSIVE_PIT_IDENTITY,
            replace(passing, pit_identity_state="INCONCLUSIVE_PIT_IDENTITY"),
        ),
        (
            GateState.FAIL_NO_EDGE,
            replace(
                passing,
                after_cost_effect=-0.0005,
                multiplicity_adjusted_confidence_lower=-0.001,
                multiplicity_adjusted_confidence_upper=0.0,
            ),
        ),
        (
            GateState.FAIL_NOT_ECONOMIC,
            replace(
                passing,
                after_cost_effect=0.0008,
                multiplicity_adjusted_confidence_lower=0.0005,
                multiplicity_adjusted_confidence_upper=0.001,
            ),
        ),
        (
            GateState.FAIL_MULTIPLICITY_OR_CONTROL,
            replace(passing, rw_adjusted_p=0.06),
        ),
        (
            GateState.INCONCLUSIVE_DATA_OR_POWER,
            replace(passing, planned_power_pass=False),
        ),
        (
            GateState.INCONCLUSIVE_EFFECT,
            replace(passing, multiplicity_adjusted_confidence_lower=0.001),
        ),
        (
            GateState.INCONCLUSIVE_ROBUSTNESS,
            replace(passing, robustness_state="INCONCLUSIVE_ROBUSTNESS"),
        ),
        (
            GateState.PASS_HISTORICAL_DISCOVERY_SCREEN,
            passing,
        ),
    )
    assert tuple(state for state, _ in cases) == tuple(GateState)
    for expected, metric in cases:
        metrics = {sleeve: passing for sleeve in sleeves}
        metrics["stock_long"] = metric
        assert policy.evaluate(metrics)["stock_long"] is expected
        assert policy.aggregate(metrics) is expected


@pytest.mark.parametrize(
    ("control_field", "control_value"),
    [
        ("negative_control_state", "FAIL"),
        ("robustness_state", "FAIL"),
    ],
)
@pytest.mark.parametrize(
    ("effect", "lower", "upper", "expected"),
    [
        (-0.0005, -0.0010, 0.0, GateState.FAIL_NO_EDGE),
        (0.0015, 0.0005, 0.0030, GateState.FAIL_MULTIPLICITY_OR_CONTROL),
        (0.0020, 0.0011, 0.0030, GateState.FAIL_MULTIPLICITY_OR_CONTROL),
    ],
)
def test_definite_control_failures_never_become_inconclusive(
    control_field: str,
    control_value: str,
    effect: float,
    lower: float,
    upper: float,
    expected: GateState,
) -> None:
    policy = _gate_policy()
    passing = _sleeve_metric()
    failed = replace(
        passing,
        after_cost_effect=effect,
        multiplicity_adjusted_confidence_lower=lower,
        multiplicity_adjusted_confidence_upper=upper,
        **{control_field: control_value},
    )
    metrics = {
        sleeve: failed if sleeve == "stock_long" else passing
        for sleeve in ("stock_long", "stock_short", "etf_long", "etf_short")
    }
    assert policy.evaluate(metrics)["stock_long"] is expected
    assert policy.aggregate(metrics) is expected


def test_governed_holdout_store_is_one_way_head_bound_and_replay_safe(
    tmp_path: Path,
) -> None:
    governance = tmp_path / "governance"
    governance.mkdir()
    trial_id = "1" * 64
    registry_binding_id = "2" * 64
    pre_unlock_head = "3" * 64
    locked_clock = _clock(datetime(2026, 7, 15, tzinfo=timezone.utc))
    store = GovernedHoldoutAccessStore(
        governance / "holdout_access.jsonl",
        governance_root=governance,
        clock=locked_clock,
    )
    locked_holdout = build_holdout_receipt(
        trial_id=trial_id,
        state="LOCKED",
        clock=locked_clock,
    )
    locked = store.initialize(
        trial_registry_binding_id=registry_binding_id,
        holdout_receipt=locked_holdout,
    )
    assert locked.state == "LOCKED"

    unlock_clock = _clock(datetime(2026, 7, 15, 1, tzinfo=timezone.utc))
    unlocked_holdout = build_holdout_receipt(
        trial_id=trial_id,
        state="UNLOCKED_ONCE",
        previous=locked_holdout,
        clock=unlock_clock,
    )
    unlock_bindings = {
        "trial_registry_binding_id": registry_binding_id,
        "locked_governed_receipt_id": locked.receipt_id,
        "locked_holdout_state_receipt_id": locked_holdout.receipt_id,
        "unlocked_holdout_state_receipt_id": unlocked_holdout.receipt_id,
        "pre_unlock_trial_ledger_head": pre_unlock_head,
    }
    unlock_record = create_local_integrity_record(
        scope="AUTHORIZE_FINAL_HOLDOUT_ACCESS",
        subject_id=trial_id,
        bindings=unlock_bindings,
        clock=unlock_clock,
    )
    unlocked_store = store.with_clock(unlock_clock)
    unlocked = unlocked_store.unlock_once(
        locked_receipt=locked,
        unlocked_holdout_receipt=unlocked_holdout,
        pre_unlock_trial_ledger_head=pre_unlock_head,
        authorization=unlock_record,
    )
    assert unlocked.state == "UNLOCKED_ONCE"
    assert unlocked.pre_unlock_trial_ledger_head == pre_unlock_head
    with pytest.raises(EvaluationAuthorizationError, match="stale or replayed"):
        unlocked_store.unlock_once(
            locked_receipt=locked,
            unlocked_holdout_receipt=unlocked_holdout,
            pre_unlock_trial_ledger_head=pre_unlock_head,
            authorization=unlock_record,
        )

    close_clock = _clock(datetime(2026, 7, 15, 2, tzinfo=timezone.utc))
    closed_holdout = build_holdout_receipt(
        trial_id=trial_id,
        state="CLOSED",
        previous=unlocked_holdout,
        clock=close_clock,
    )
    wrong_close = create_local_integrity_record(
        scope="CLOSE_FINAL_HOLDOUT_ACCESS",
        subject_id=trial_id,
        bindings={
            "trial_registry_binding_id": registry_binding_id,
            "unlocked_governed_receipt_id": unlocked.receipt_id,
            "unlocked_holdout_state_receipt_id": unlocked_holdout.receipt_id,
            "closed_holdout_state_receipt_id": closed_holdout.receipt_id,
            "pre_unlock_trial_ledger_head": "4" * 64,
        },
        clock=close_clock,
    )
    closing_store = unlocked_store.with_clock(close_clock)
    with pytest.raises(EvaluationAuthorizationError, match="bindings differ"):
        closing_store.close(
            unlocked_receipt=unlocked,
            closed_holdout_receipt=closed_holdout,
            authorization=wrong_close,
        )
    close_record = create_local_integrity_record(
        scope="CLOSE_FINAL_HOLDOUT_ACCESS",
        subject_id=trial_id,
        bindings={
            "trial_registry_binding_id": registry_binding_id,
            "unlocked_governed_receipt_id": unlocked.receipt_id,
            "unlocked_holdout_state_receipt_id": unlocked_holdout.receipt_id,
            "closed_holdout_state_receipt_id": closed_holdout.receipt_id,
            "pre_unlock_trial_ledger_head": pre_unlock_head,
        },
        clock=close_clock,
    )
    closed = closing_store.close(
        unlocked_receipt=unlocked,
        closed_holdout_receipt=closed_holdout,
        authorization=close_record,
    )
    assert closed.state == "CLOSED"
    assert closing_store.latest(trial_id).as_dict() == closed.as_dict()
    with pytest.raises(EvaluationAuthorizationError, match="stale or replayed"):
        closing_store.close(
            unlocked_receipt=unlocked,
            closed_holdout_receipt=closed_holdout,
            authorization=close_record,
        )


@pytest.mark.parametrize("unlock_count", [False, True, 0.0, 1.0, "0", "1"])
def test_holdout_receipt_rejects_non_integer_unlock_count(
    unlock_count: object,
) -> None:
    receipt = build_holdout_receipt(
        trial_id="1" * 64,
        state="LOCKED",
        clock=_clock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
    )
    forged = replace(receipt, unlock_count=unlock_count, receipt_id="")
    forged = replace(
        forged,
        receipt_id=sha256_bytes(canonical_json_bytes(forged.unsigned_dict())),
    )
    with pytest.raises(EvaluationAuthorizationError, match="transition shape"):
        forged.validate()
