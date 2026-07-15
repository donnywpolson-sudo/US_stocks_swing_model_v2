from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.bundle import (
    BLOCKED_EXTERNAL_ANCHOR_RECEIPT_ID,
    BLOCKED_READINESS_RECEIPT_ID,
    SealedBundleMetadata,
    build_metadata,
    load_bundle,
    prepare_bundle_candidate,
    seal_bundle,
)
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError, IntegrityError
from us_stocks_swing_model_v2.eligibility import (
    ELIGIBILITY_CENSUS_CONTRACT_ID,
    EligibilityCensus,
)
from us_stocks_swing_model_v2.gates import (
    GateState,
    IndependentGatePolicy,
    SleeveMetric,
    build_gate_receipt,
)
from us_stocks_swing_model_v2.governance import (
    AuthorizationAuthority,
    ReleaseBinding,
    release_bindings_hash,
    sign_authorization_receipt,
)
from us_stocks_swing_model_v2.inference import FitFreeInferenceEngine
from us_stocks_swing_model_v2.feature_release import load_feature_release
from us_stocks_swing_model_v2.ledger import (
    HashChainLedger,
    LedgerAnchorStore,
    OutcomeLedger,
    PredictionLedger,
)
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest, verify_release
from us_stocks_swing_model_v2.schemas import (
    FeatureRow,
    OutcomeRow,
    OutcomeStatus,
    SecurityType,
    assert_underlying_only_payload,
)
from us_stocks_swing_model_v2.trials import (
    TrialRegistry,
    TrialSpec,
    build_holdout_receipt,
)


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
AUTH_KEY = b"synthetic-test-authorization-key"


def _clock(at: datetime) -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        at,
        permit=SyntheticOnlyPermit.create(
            fixture_id=f"firewall-{at.isoformat()}",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _authority() -> AuthorizationAuthority:
    return AuthorizationAuthority.synthetic(
        key_id="synthetic-test-key",
        verification_key=AUTH_KEY,
        permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-authorization",
            scope="SYNTHETIC_AUTHORIZATION_AUTHORITY",
        ),
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
    hurdle: float = 0.001,
) -> SleeveMetric:
    return SleeveMetric(
        effective_sessions=sessions,
        after_cost_effect=effect,
        preregistered_economic_hurdle=hurdle,
        multiplicity_adjusted_confidence_lower=confidence_lower,
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
        negative_control_state="PASS",
    )


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


def _bundle(tmp_path: Path, *, model_overrides: dict[str, object] | None = None) -> Path:
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
    trial_id = registry.register(spec, verified_release_directories=releases.values())
    permit, _ = _outer_permit(registry, spec, trial_id)
    gate = registry.with_clock(
        _clock(datetime(2026, 7, 15, 2, tzinfo=timezone.utc))
    ).build_gate_receipt(
        permit,
        policy=_gate_policy(),
        metrics={name: _sleeve_metric() for name in ("stock_long", "stock_short", "etf_long", "etf_short")},
    )
    candidate = prepare_bundle_candidate(
        root,
        ["model.json"],
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
        sealed_at="2026-07-15T03:00:00Z",
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
        production_readiness_state="NOT_CONFIGURED_BLOCKS_PRODUCTION",
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
        neutral_band=0.005,
        maximum_uncertainty=0.05,
        maximum_feature_age_minutes=180,
        maximum_identity_age_minutes=1440,
        maximum_inference_latency_minutes=10,
    )
    authorization = sign_authorization_receipt(
        scope="AUTHORIZE_CANDIDATE_SEALING",
        subject_id=candidate.candidate_id,
        bindings=candidate.sealing_bindings(),
        issued_at="2026-07-15T02:30:00Z",
        expires_at="2026-07-15T04:00:00Z",
        authority=_authority(),
    )
    metadata = build_metadata(
        candidate,
        sealing_authorization=authorization,
        authorization_authority=_authority(),
        clock=_clock(datetime(2026, 7, 15, 3, tzinfo=timezone.utc)),
    )
    seal_bundle(
        root,
        metadata,
        authorization_authority=_authority(),
        clock=_clock(datetime(2026, 7, 15, 3, tzinfo=timezone.utc)),
    )
    assert load_bundle(root, authorization_authority=_authority()) == metadata
    return root


def _load_bundle(bundle_path: Path):
    return load_bundle(bundle_path, authorization_authority=_authority())


def _engine(bundle_path: Path, *, clock: TrustedClock) -> FitFreeInferenceEngine:
    return FitFreeInferenceEngine(
        bundle_path,
        authorization_authority=_authority(),
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


def _predict(
    engine: FitFreeInferenceEngine,
    rows: list[FeatureRow] | tuple[FeatureRow, ...],
):
    materialized = tuple(rows)
    return engine.predict(
        materialized,
        eligibility_census=_census(engine, materialized),
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="firewall-direct-prediction",
            scope="SYNTHETIC_DIRECT_PREDICTION_NOT_PUBLISHABLE",
        ),
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
            synthetic_permit=SyntheticOnlyPermit.create(
                fixture_id="firewall-incomplete-direct-prediction",
                scope="SYNTHETIC_DIRECT_PREDICTION_NOT_PUBLISHABLE",
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


def test_bundle_artifact_mutation_fails_closed(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    (root / "model.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="artifact mismatch"):
        _load_bundle(root)


def test_bundle_reload_rechecks_authority_and_exact_numeric_json_types(tmp_path: Path) -> None:
    root = _bundle(tmp_path)
    wrong_authority = AuthorizationAuthority.synthetic(
        key_id="wrong-synthetic-key",
        verification_key=b"wrong-synthetic-key",
        permit=SyntheticOnlyPermit.create(
            fixture_id="wrong-bundle-authority",
            scope="SYNTHETIC_AUTHORIZATION_AUTHORITY",
        ),
    )
    with pytest.raises(EvaluationAuthorizationError, match="pinned external authority"):
        load_bundle(root, authorization_authority=wrong_authority)

    payload = json.loads((root / "sealed_bundle.json").read_text(encoding="utf-8"))
    payload["maximum_feature_age_minutes"] = True
    with pytest.raises(ContractError, match="exact JSON integer"):
        SealedBundleMetadata.from_dict(payload)


def test_authorization_expiry_is_half_open() -> None:
    receipt = sign_authorization_receipt(
        scope="AUDIT_HALF_OPEN_EXPIRY",
        subject_id="1" * 64,
        bindings={"evidence": "2" * 64},
        issued_at="2026-07-15T19:00:00Z",
        expires_at="2026-07-15T20:00:00Z",
        authority=_authority(),
    )
    with pytest.raises(EvaluationAuthorizationError, match="not current"):
        receipt.validate(
            authority=_authority(),
            expected_scope="AUDIT_HALF_OPEN_EXPIRY",
            expected_subject_id="1" * 64,
            required_bindings={"evidence": "2" * 64},
            clock=_clock(datetime(2026, 7, 15, 20, tzinfo=timezone.utc)),
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

    assert metadata.trust_eligible is False


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
            synthetic_permit=SyntheticOnlyPermit.create(
                fixture_id="firewall-unsealed-feature",
                scope="SYNTHETIC_DIRECT_PREDICTION_NOT_PUBLISHABLE",
            ),
        )
    with pytest.raises(ContractError, match="eligibility census"):
        engine.predict(
            [replace(evidence_row, security_type_evidence_id="0" * 64)],
            eligibility_census=census,
            synthetic_permit=SyntheticOnlyPermit.create(
                fixture_id="firewall-unsealed-type",
                scope="SYNTHETIC_DIRECT_PREDICTION_NOT_PUBLISHABLE",
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


def test_accepted_feature_release_is_published_as_one_atomic_census(tmp_path: Path) -> None:
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

    commit = engine.predict_and_commit(
        feature_release_directory=feature_release,
        eligibility_census=census,
        prediction_ledger=ledger,
    )
    assert commit["feature_release_id"] == manifest.release_id
    assert commit["prediction_count"] == 2
    assert "predictions" not in commit
    anchor = Path(str(commit["anchor_path"]))
    assert len(ledger.verify(anchor)) == 2

    outcome_ledger = OutcomeLedger(
        tmp_path / "atomic-ledger" / "outcomes.jsonl",
        ledger,
        clock=_clock(NOW + timedelta(days=8)),
    )
    with pytest.raises(ContractError, match="caller-constructed outcomes"):
        outcome_ledger.append(None, prediction_anchor=anchor)  # type: ignore[arg-type]


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

    outcome_ledger.append_synthetic(
        outcome_for(predictions[0]),
        prediction_anchor=anchor,
        synthetic_permit=_outcome_append_permit("firewall-outcome-first"),
    )
    with pytest.raises(IntegrityError, match="exactly cover"):
        outcome_ledger.verify_expected_census(census, prediction_anchor=anchor)
    outcome_ledger.append_synthetic(
        outcome_for(predictions[1]),
        prediction_anchor=anchor,
        synthetic_permit=_outcome_append_permit("firewall-outcome-second"),
    )
    assert len(outcome_ledger.verify_expected_census(census, prediction_anchor=anchor)) == 2


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
        clock=_clock(NOW + timedelta(minutes=1)),
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
        clock=_clock(NOW + timedelta(minutes=1)),
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
        feature_schema_id="features-v1",
        outcome_schema_id="outcomes-v1",
        split_plan_id="nested-wfa-v1",
        model_family="linear-baseline",
        primary_metric="net_mean_return",
        primary_gate_id=sha256_bytes(canonical_json_bytes(_gate_policy().as_dict())),
        cost_policy_id="cost-v1",
        trial_family_id="family-v1",
        census_anchor_id="4" * 64,
        trial_family_anchor_id="5" * 64,
        evaluator_closure_hash="6" * 64,
        governance_contract_hash="7" * 64,
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
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
    bindings = {
        "trial_registry_binding_id": registry.registry_binding_id,
        "registration_hash": registration_hash,
        "evaluation_scope": "OUTER_SCREEN",
        "evaluation_input_hash": "8" * 64,
        "evaluator_code_hash": "9" * 64,
        "evaluator_closure_hash": spec.evaluator_closure_hash,
        "census_anchor_id": spec.census_anchor_id,
        "trial_family_anchor_id": spec.trial_family_anchor_id,
        "governance_contract_hash": spec.governance_contract_hash,
        "primary_gate_id": spec.primary_gate_id,
        "release_bindings_hash": release_bindings_hash(spec.release_bindings),
        "holdout_receipt_id": holdout.receipt_id,
    }
    authorization = sign_authorization_receipt(
        scope="AUTHORIZE_OUTER_SCREEN",
        subject_id=trial_id,
        bindings=bindings,
        issued_at="2026-07-15T00:30:00Z",
        expires_at="2026-07-15T04:00:00Z",
        authority=_authority(),
    )
    permit = registry.with_clock(_clock(permit_issued_at)).issue_permit(
        trial_id,
        evaluation_scope="OUTER_SCREEN",
        evaluation_input_hash="8" * 64,
        evaluator_code_hash="9" * 64,
        holdout_receipt=holdout,
        authorization=authorization,
        authorization_authority=_authority(),
    )
    return permit, holdout


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
    trial_id = registry.register(first, verified_release_directories=release_directories)
    assert registry.registry.read_verified()[0]["time_authority"] == (
        "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE"
    )
    assert registry.authorize(trial_id)["hypothesis_id"] == "h1"
    mutated = _trial(release_directories, "h2")
    assert mutated.trial_id != trial_id
    permit, holdout = _outer_permit(registry, first, trial_id)
    forged_unsigned = {**permit.unsigned_dict(), "evaluator_code_hash": "0" * 64}
    forged_permit = type(permit)(
        **forged_unsigned,
        permit_id=sha256_bytes(canonical_json_bytes(forged_unsigned)),
    )
    forged_permit.validate()
    with pytest.raises(EvaluationAuthorizationError, match="registry-issued"):
        registry.with_clock(
            _clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))
        ).record_evaluation(forged_permit, {})
    registry.with_clock(_clock(datetime(2026, 7, 15, 2, tzinfo=timezone.utc))).record_evaluation(
        permit,
        {
            "trial_id": trial_id,
            "evaluation_scope": "OUTER_SCREEN",
            "state": "INCONCLUSIVE",
            "evaluation_input_hash": permit.evaluation_input_hash,
            "evaluator_closure_hash": permit.evaluator_closure_hash,
            "authorization_receipt_id": permit.authorization_receipt_id,
            "holdout_receipt_id": holdout.receipt_id,
            "result_artifact_hash": "a" * 64,
            "evaluation_closed": True,
        },
    )
    with pytest.raises(IntegrityError, match="duplicate"):
        registry.with_clock(_clock(datetime(2026, 7, 15, 3, tzinfo=timezone.utc))).record_evaluation(
            permit,
            {
                "trial_id": trial_id,
                "evaluation_scope": "OUTER_SCREEN",
                "state": "PASS",
                "evaluation_input_hash": permit.evaluation_input_hash,
                "evaluator_closure_hash": permit.evaluator_closure_hash,
                "authorization_receipt_id": permit.authorization_receipt_id,
                "holdout_receipt_id": holdout.receipt_id,
                "result_artifact_hash": "b" * 64,
                "evaluation_closed": True,
            },
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
    trial_id = registry.register(spec, verified_release_directories=release_directories)
    assert registry.registry.read_verified()[0]["recorded_at"] == "2026-07-15T00:00:00Z"
    with pytest.raises(EvaluationAuthorizationError, match="after trial registration"):
        _outer_permit(
            registry,
            spec,
            trial_id,
            permit_issued_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        )
    permit, holdout = _outer_permit(registry, spec, trial_id)
    with pytest.raises(EvaluationAuthorizationError, match="fields"):
        registry.with_clock(_clock(datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc))).record_evaluation(
            permit,
            {"trial_id": trial_id, "evaluation_input_hash": permit.evaluation_input_hash},
        )
    with pytest.raises(EvaluationAuthorizationError, match="after permit"):
        registry.record_evaluation(
            permit,
            {
                "trial_id": trial_id,
                "evaluation_scope": "OUTER_SCREEN",
                "state": "PASS",
                "evaluation_input_hash": permit.evaluation_input_hash,
                "evaluator_closure_hash": permit.evaluator_closure_hash,
                "authorization_receipt_id": permit.authorization_receipt_id,
                "holdout_receipt_id": holdout.receipt_id,
                "result_artifact_hash": "a" * 64,
                "evaluation_closed": True,
            },
        )

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
    metrics = {sleeve: passing for sleeve in ("stock_long", "stock_short", "etf_long", "etf_short")}
    assert policy.aggregate(metrics) is GateState.PASS
    metrics["stock_short"] = _sleeve_metric(effect=-0.001, confidence_lower=-0.002)
    assert policy.aggregate(metrics) is GateState.FAIL
    del metrics["etf_short"]
    assert policy.evaluate(metrics)["etf_short"] is GateState.INCONCLUSIVE


def test_gate_distinguishes_invalid_power_pbo_and_strict_economic_confidence() -> None:
    policy = _gate_policy()
    sleeves = ("stock_long", "stock_short", "etf_long", "etf_short")
    base = {name: _sleeve_metric() for name in sleeves}

    equality = dict(base)
    equality["stock_long"] = _sleeve_metric(confidence_lower=0.001)
    assert policy.aggregate(equality) is GateState.FAIL

    invalid = dict(base)
    invalid["stock_long"] = replace(_sleeve_metric(), numerical_valid=False)
    assert policy.aggregate(invalid) is GateState.INVALID
    invalid["stock_long"] = replace(_sleeve_metric(), lineage_valid=False)
    assert policy.aggregate(invalid) is GateState.INVALID

    pbo_mid = dict(base)
    pbo_mid["stock_long"] = replace(_sleeve_metric(), conservative_pbo=0.35)
    assert policy.aggregate(pbo_mid) is GateState.INCONCLUSIVE
    pbo_high = dict(base)
    pbo_high["stock_long"] = replace(_sleeve_metric(), conservative_pbo=0.51)
    assert policy.aggregate(pbo_high) is GateState.FAIL

    single = dict(base)
    single["stock_long"] = replace(
        _sleeve_metric(),
        pbo_applicability="NOT_APPLICABLE_SINGLE_PREDECLARED_CONFIGURATION",
        conservative_pbo=None,
    )
    assert policy.aggregate(single) is GateState.PASS

    with pytest.raises(ContractError, match="Romano-Wolf alpha"):
        replace(policy, rw_alpha=0.0).validate()
