from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.bundle import prepare_bundle_candidate
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, safe_relative_path, sha256_bytes
from us_stocks_swing_model_v2.corporate_actions import ActionType, BitemporalActionLedger, CorporateAction
from us_stocks_swing_model_v2.environment import validate_environment_lock
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError, IntegrityError
from us_stocks_swing_model_v2.exchange_calendar import publish_xnys_calendar_release
from us_stocks_swing_model_v2.gates import IndependentGatePolicy, SleeveMetric
from us_stocks_swing_model_v2.governance import (
    AuthorizationAuthority,
    load_external_authority,
    sign_authorization_receipt,
)
from us_stocks_swing_model_v2.identity import BitemporalIdentityLedger, merge_identity_snapshot, parse_alpaca_assets
from us_stocks_swing_model_v2.ledger import PredictionLedger
from us_stocks_swing_model_v2.migration import execute_copy_plan, load_migration_config, plan_migration
from us_stocks_swing_model_v2.outcomes import DailyBar, build_outcome
from us_stocks_swing_model_v2.providers.alpaca import (
    AlpacaBarsPolicy,
    AlpacaBarsRequest,
    qualify_landed_pages,
)
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
    parse_nasdaq_traded,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore
from us_stocks_swing_model_v2.schemas import (
    FeatureRow,
    SecurityType,
    UnderlyingPrediction,
    assert_underlying_only_payload,
)
from us_stocks_swing_model_v2.trials import TrialPermit
from us_stocks_swing_model_v2.calendar import PinnedSessionCalendar


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc)
REPO = Path(__file__).parents[1]


def _clock(at: datetime) -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        at,
        permit=SyntheticOnlyPermit.create(
            fixture_id=f"m2a-{at.isoformat()}",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _nasdaq_policy() -> NasdaqCompletenessPolicy:
    return NasdaqCompletenessPolicy.synthetic_fixture(
        permit=SyntheticOnlyPermit.create(
            fixture_id="m2a-nasdaq",
            scope="NASDAQ_COMPLETENESS_FIXTURE",
        )
    )


def _action_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="m2a-actions",
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )


def _action_ledger(actions=()) -> BitemporalActionLedger:
    return BitemporalActionLedger(actions, synthetic_permit=_action_permit())


def _snapshot_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="m2a-snapshots",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _gate_policy(*, hurdle: float = 0.0) -> IndependentGatePolicy:
    return IndependentGatePolicy(
        minimum_effective_sessions=1,
        sleeve_economic_hurdles={name: hurdle for name in ("stock_long", "stock_short", "etf_long", "etf_short")},
        minimum_confidence_lower=0.0,
        rw_alpha=0.05,
        minimum_dsr_probability=0.95,
        maximum_conservative_pbo=0.20,
        pbo_failure_threshold=0.50,
    )


def _gate_metric(*, effect: float = 0.1) -> SleeveMetric:
    return SleeveMetric(
        effective_sessions=1,
        after_cost_effect=effect,
        preregistered_economic_hurdle=0.0,
        multiplicity_adjusted_confidence_lower=0.1,
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
        robustness_state="PASS",
        robustness_evidence_hash="a" * 64,
    )


def _nasdaq_raw(symbol: str = "ABC", name: str = "ABC COMMON STOCK") -> bytes:
    return (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
        f"Y|{symbol}|{name}|N|Q|N|100|N|N|{symbol}|{symbol}|N\n"
        "File Creation Time: 0715202601:30|||||||||||\n"
    ).encode()


def test_as_received_snapshot_lands_atomically_and_tampering_fails(tmp_path: Path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_nasdaq_raw(),
        headers={"ETag": "fixture", "Content-Type": "text/plain"},
        retrieved_at=NOW,
        synthetic_permit=_snapshot_permit(),
    )
    assert store.land(
        source="nasdaqtraded", url=NASDAQ_TRADED_URL, http_status=200, raw=_nasdaq_raw(),
        headers={"ETag": "fixture", "Content-Type": "text/plain"}, retrieved_at=NOW,
        synthetic_permit=_snapshot_permit(),
    ).snapshot_id == snapshot.snapshot_id
    snapshot.raw_path.write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="raw bytes"):
        store.load(snapshot.root)


def test_alpaca_and_nasdaq_identity_merge_is_bitemporal_and_unknown_abstains(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="verified release or synthetic"):
        BitemporalIdentityLedger()
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    nasdaq = parse_nasdaq_traded(
        store.land(source="nasdaqtraded", url=NASDAQ_TRADED_URL, http_status=200, raw=_nasdaq_raw(), headers={"etag": "n"}, retrieved_at=NOW, synthetic_permit=_snapshot_permit()),
        policy=_nasdaq_policy(),
    )
    assets_raw = json.dumps(
        [
            {"id": "id-abc", "symbol": "ABC", "class": "us_equity", "exchange": "NASDAQ", "status": "active", "tradable": True},
            {"id": "id-odd", "symbol": "ODD", "class": "us_equity", "exchange": "NYSE", "status": "active", "tradable": True},
        ]
    ).encode()
    assets = parse_alpaca_assets(
        store.land(source="alpaca_assets", url="https://paper-api.alpaca.markets/v2/assets", http_status=200, raw=assets_raw, headers={"etag": "a"}, retrieved_at=NOW, synthetic_permit=_snapshot_permit())
    )
    merged = merge_identity_snapshot(assets, nasdaq)
    by_symbol = {row.symbol: row for row in merged.rows}
    assert by_symbol["ABC"].eligible and by_symbol["ABC"].security_type is SecurityType.STOCK
    assert not by_symbol["ODD"].eligible and by_symbol["ODD"].security_type is SecurityType.UNKNOWN
    ledger = BitemporalIdentityLedger(
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="m2a-identity-ledger",
            scope="SYNTHETIC_IDENTITY_LEDGER",
        )
    )
    ledger.append_snapshot(merged)
    assert {
        row.symbol
        for row in ledger.visible_as_of(effective_as_of=NOW, known_as_of=NOW)
    } == {"ABC", "ODD"}


def test_feature_prefix_poison_and_abstention_uncertainty_fail() -> None:
    row = FeatureRow(
        asset_id="id",
        symbol="ABC",
        security_type=SecurityType.STOCK,
        decision_session=date(2026, 7, 15),
        decision_at=NOW,
        available_at=NOW,
        source_release_id="1" * 64,
        feature_schema_id="schema",
        identity_release_id="2" * 64,
        security_type_evidence_id="3" * 64,
        calendar_release_id="4" * 64,
        action_release_id="5" * 64,
        source_epoch="epoch",
        identity_known_at=NOW,
        point_in_time_state="PIT_CONFIRMED",
        prediction_deadline_at=NOW + timedelta(minutes=5),
        information_barrier_at=NOW + timedelta(days=7),
        values={"future_sneaky_return": 0.2},
    )
    with pytest.raises(ContractError, match="future/outcome"):
        row.validate()
    with pytest.raises(ContractError, match="actionable"):
        UnderlyingPrediction.create(
            asset_id="id",
            eligibility_census_id="c" * 64,
                symbol="ABC",
                security_type=SecurityType.UNKNOWN,
                decision_session=date(2026, 7, 15),
            decision_at=NOW,
            recorded_at=NOW,
            time_authority="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
            synthetic_clock_permit_id=_clock(NOW).synthetic_permit_id,
            bundle_id="6" * 64,
            feature_release_id="1" * 64,
            feature_row_hash="1" * 64,
            identity_release_id="2" * 64,
            security_type_evidence_id="3" * 64,
            calendar_release_id="4" * 64,
            action_release_id="5" * 64,
            source_epoch="epoch",
            point_in_time_state="PIT_CONFIRMED",
            prediction_deadline_at=NOW + timedelta(minutes=5),
            information_barrier_at=NOW + timedelta(days=7),
            expected_five_session_return=None,
            p_up=None,
            p_down=None,
            p_neutral=None,
            uncertainty=0.2,
            rank=None,
            abstain=True,
            abstention_reason="unknown",
        )


def test_schema_mapping_keys_must_be_strings_before_case_normalization() -> None:
    with pytest.raises(ContractError, match="mapping keys must be strings"):
        assert_underlying_only_payload({1: "not-a-string-key"})

    row = FeatureRow(
        asset_id="id",
        symbol="ABC",
        security_type=SecurityType.STOCK,
        decision_session=date(2026, 7, 15),
        decision_at=NOW,
        available_at=NOW,
        source_release_id="1" * 64,
        feature_schema_id="schema",
        identity_release_id="2" * 64,
        security_type_evidence_id="3" * 64,
        calendar_release_id="4" * 64,
        action_release_id="5" * 64,
        source_epoch="epoch",
        identity_known_at=NOW,
        point_in_time_state="PIT_CONFIRMED",
        prediction_deadline_at=NOW + timedelta(minutes=5),
        information_barrier_at=NOW + timedelta(days=7),
        values={1: 0.2},
    )
    with pytest.raises(ContractError, match="mapping keys must be strings"):
        row.validate()


def test_action_revisions_and_outcome_availability_are_monotone() -> None:
    action = CorporateAction(
        action_id="a",
        asset_id="id",
        action_type=ActionType.SPLIT,
        effective_session=date(2026, 7, 10),
        announced_at=None,
        received_at=NOW,
        revision=2,
        source_snapshot_id="a" * 64,
        source_release_id=_action_permit().permit_id,
        source_epoch="SYNTHETIC_ONLY",
        raw_row_sha256="b" * 64,
        ratio_new_for_old=2.0,
    )
    ledger = _action_ledger([action])
    with pytest.raises(IntegrityError, match="monotonically"):
        ledger.append(replace(action, revision=1, received_at=NOW + timedelta(minutes=1)))
    calendar_permit = SyntheticOnlyPermit.create(
        fixture_id="m2a-action-outcome",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    calendar = PinnedSessionCalendar.from_iso_dates(
        calendar_permit.permit_id,
        ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13"],
        synthetic_permit=calendar_permit,
    )
    bars = {
        session: DailyBar("id", session, 10.0, 11.0, NOW)
        for session in calendar.sessions[1:]
    }
    with pytest.raises(ContractError, match="predate"):
        build_outcome(
            prediction_id="1" * 64, asset_id="id", decision_session=calendar.sessions[0], calendar=calendar,
            eligibility_census_id="c" * 64,
            bars=bars, bar_release_id="2" * 64, actions=_action_ledger(), action_view_as_of=NOW-timedelta(seconds=1),
            source_epoch="fixture_epoch",
        )


def test_bundle_rejects_parent_path_before_hash_read(tmp_path: Path) -> None:
    root = tmp_path / "bundle"
    root.mkdir()
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ContractError, match="unsafe relative"):
        safe_relative_path("../outside.json")


def test_trial_permit_content_hash_cannot_be_forged(tmp_path: Path) -> None:
    unsigned = {
        "trial_registry_binding_id": "0" * 64,
        "trial_id": "1" * 64,
        "registration_hash": "2" * 64,
        "evaluation_scope": "OUTER_SCREEN",
        "evaluation_input_hash": "3" * 64,
        "evaluator_code_hash": "4" * 64,
        "evaluator_closure_hash": "5" * 64,
        "census_anchor_id": "6" * 64,
        "trial_family_anchor_id": "7" * 64,
        "governance_contract_hash": "8" * 64,
        "primary_gate_id": "c" * 64,
        "robustness_policy_id": "d" * 64,
        "release_bindings_hash": "9" * 64,
        "holdout_receipt_id": "a" * 64,
        "authorization_receipt_id": "b" * 64,
        "issued_at": "2026-07-15T01:00:00Z",
        "time_authority": "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        "synthetic_clock_permit_id": _clock(NOW).synthetic_permit_id,
    }
    permit = TrialPermit(**unsigned, permit_id=sha256_bytes(canonical_json_bytes(unsigned)))
    permit.validate()
    forged = replace(permit, evaluation_input_hash="6"*64)
    with pytest.raises(EvaluationAuthorizationError, match="hash"):
        forged.validate()


def test_environment_lock_matches_runtime() -> None:
    assert len(validate_environment_lock(REPO / "config" / "environment.lock.json")) == 64


def test_external_authority_is_fail_closed_and_repository_cannot_self_authorize(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvaluationAuthorizationError, match="not pinned and active"):
        load_external_authority(
            REPO / "config" / "authorization_authorities.json",
            key_id="user-key",
            verification_key=b"user-secret",
        )

    key = b"external-user-controlled-key"
    registry = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "status": "ACTIVE",
        "authorities": [
            {
                "key_id": "user-key",
                "key_sha256": sha256_bytes(key),
                "authorization_class": "EXTERNAL_USER_AUTHORITY",
            }
        ],
    }
    registry_path = tmp_path / "authorities.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    with pytest.raises(
        EvaluationAuthorizationError,
        match="entry fields differ",
    ):
        load_external_authority(
            registry_path,
            key_id="user-key",
            verification_key=key,
        )

    synthetic_authority = AuthorizationAuthority.synthetic(
        key_id="synthetic-key",
        verification_key=b"synthetic-key",
        permit=SyntheticOnlyPermit.create(
            fixture_id="m2a-self-authorization",
            scope="SYNTHETIC_AUTHORIZATION_AUTHORITY",
        ),
    )
    synthetic_receipt = sign_authorization_receipt(
        scope="AUTHORIZE_OUTER_SCREEN",
        subject_id="1" * 64,
        bindings={"evidence": "2" * 64},
        issued_at="2026-07-15T19:00:00Z",
        expires_at="2026-07-15T21:00:00Z",
        authority=synthetic_authority,
    )
    synthetic_receipt.validate(
        authority=synthetic_authority,
        expected_scope="AUTHORIZE_OUTER_SCREEN",
        expected_subject_id="1" * 64,
        required_bindings={"evidence": "2" * 64},
        clock=_clock(NOW),
    )


def test_gate_policy_and_metrics_reject_nonfinite_values() -> None:
    with pytest.raises(ContractError, match="finite"):
        _gate_policy(hurdle=float("nan")).validate()
    with pytest.raises(ContractError, match="metrics must be finite"):
        _gate_policy().aggregate({
            name: _gate_metric(effect=float("nan") if name == "stock_long" else 0.1)
            for name in ("stock_long", "stock_short", "etf_long", "etf_short")
        })


def test_legacy_trial_census_is_a_conservative_floor_not_a_false_exact_count() -> None:
    contract = json.loads(
        (REPO / "config" / "research_readiness_contract.json").read_text(encoding="utf-8")
    )
    census = contract["trial_ledger"]["legacy_trial_census"]
    assert census["documented_minimum_outcome_informed_attempts"] >= 62
    assert census["exact_count_state"] == "INDETERMINATE"
    assert census["effective_trial_count_reduction_allowed"] is False
    assert census["trusted_gate_blocked_until_exact_census"] is True
    assert census["unresolved_status"] == "INVALID_TRIAL_CENSUS_UNRESOLVED"


def test_unimplemented_external_anchors_and_census_materialization_block_production() -> None:
    contract = json.loads(
        (REPO / "config" / "research_readiness_contract.json").read_text(encoding="utf-8")
    )
    anchors = contract["production_evidence_anchors"]
    assert anchors["external_worm_anchor_status"] == "NOT_CONFIGURED_BLOCKS_PRODUCTION"
    assert anchors["trial_census_exact_status"] == "INDETERMINATE_BLOCKS_TRUSTED_GATE"
    assert anchors["eligibility_census_materializer_status"] == (
        "NOT_IMPLEMENTED_BLOCKS_PRODUCTION"
    )
    assert anchors["readiness_receipt_status"] == (
        "ISSUED_NON_AUTHORIZING_MECHANICAL_RECEIPT"
    )
    assert anchors["statistical_array_binding_required"] is True
    assert anchors["local_hash_chain_or_local_anchor_is_external_immutability_proof"] is False
    assert anchors["synthetic_fixture_evidence_is_production_evidence"] is False


def test_outcome_rejects_bar_stored_under_the_wrong_session_key() -> None:
    calendar_permit = SyntheticOnlyPermit.create(
        fixture_id="m2a-bar-mapping",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    calendar = PinnedSessionCalendar.from_iso_dates(
        calendar_permit.permit_id,
        ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13"],
        synthetic_permit=calendar_permit,
    )
    bars = {
        date(2026, 7, 7): DailyBar(
            "id", date(2026, 7, 8), 10.0, 10.5, NOW
        )
    }
    with pytest.raises(ContractError, match="mapping key"):
        build_outcome(
            prediction_id="1" * 64,
            eligibility_census_id="c" * 64,
            asset_id="id",
            decision_session=date(2026, 7, 6),
            calendar=calendar,
            bars=bars,
            bar_release_id="2" * 64,
            actions=_action_ledger(),
            action_view_as_of=NOW,
            source_epoch="fixture_epoch",
        )


def test_late_anchor_rejection_leaves_prediction_ledger_untouched(tmp_path: Path) -> None:
    prediction = UnderlyingPrediction.create(
        asset_id="id",
        eligibility_census_id="c" * 64,
        symbol="ABC",
        security_type=SecurityType.STOCK,
        decision_session=date(2026, 7, 15),
        decision_at=NOW,
        recorded_at=NOW + timedelta(minutes=1),
        time_authority="SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
        synthetic_clock_permit_id=_clock(NOW + timedelta(minutes=1)).synthetic_permit_id,
        bundle_id="1" * 64,
        feature_release_id="2" * 64,
        feature_row_hash="1" * 64,
        identity_release_id="3" * 64,
        security_type_evidence_id="4" * 64,
        calendar_release_id="5" * 64,
        action_release_id="6" * 64,
        source_epoch="epoch",
        point_in_time_state="PIT_CONFIRMED",
        prediction_deadline_at=NOW + timedelta(minutes=5),
        information_barrier_at=NOW + timedelta(days=7),
        expected_five_session_return=0.01,
        p_up=0.6,
        p_down=0.3,
        p_neutral=0.1,
        uncertainty=0.02,
        rank=1,
        abstain=False,
        abstention_reason=None,
    )
    ledger_path = tmp_path / "ledger" / "predictions.jsonl"
    anchor_root = tmp_path / "anchors"
    ledger = PredictionLedger(
        ledger_path,
        anchor_root,
        clock=_clock(NOW + timedelta(minutes=6)),
    )
    with pytest.raises(IntegrityError, match="anchor deadline"):
        ledger.append_synthetic(
            prediction,
            synthetic_permit=SyntheticOnlyPermit.create(
                fixture_id="m2a-late-anchor",
                scope="SYNTHETIC_SINGLE_PREDICTION_LEDGER_APPEND",
            ),
        )
    assert not ledger_path.exists()
    assert not anchor_root.exists()


def test_snapshot_store_rejects_relative_roots(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="must be absolute"):
        AsReceivedSnapshotStore(Path("snapshots"), allowed_root=tmp_path)


def test_alpaca_http_200_needs_complete_strict_bar_evidence(tmp_path: Path) -> None:
    calendar_release = publish_xnys_calendar_release(
        staging_root=tmp_path / "calendar-stage",
        release_root=tmp_path / "calendar-releases",
        start=date(2026, 7, 1),
        end=date(2026, 7, 2),
        created_at="2026-07-15T00:00:00Z",
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
    )
    request = AlpacaBarsRequest(
        symbols=("AAPL",),
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 3, tzinfo=timezone.utc),
        requested_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
    )
    policy = AlpacaBarsPolicy(feed="iex")
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    valid_page = store.land(
        source="alpaca_iex_qualification",
        url=request.url(policy),
        http_status=200,
        raw=json.dumps(
            {
                "bars": {
                    "AAPL": [
                        {
                            "t": "2026-07-01T04:00:00Z",
                            "o": 99.0,
                            "h": 101.0,
                            "l": 98.0,
                            "c": 100.0,
                            "v": 900.0,
                        },
                        {
                            "t": "2026-07-02T04:00:00Z",
                            "o": 100.0,
                            "h": 102.0,
                            "l": 99.0,
                            "c": 101.0,
                            "v": 1000.0,
                        }
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=NOW,
        synthetic_permit=_snapshot_permit(),
    )
    synthetic_result = qualify_landed_pages(
        request,
        policy,
        (valid_page,),
        calendar_release_directory=calendar_release,
        accepted_release_root=calendar_release.parents[1],
    )
    assert synthetic_result.state == "PASS"
    assert not synthetic_result.eligible
    assert synthetic_result.evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"

    false_positive_page = store.land(
        source="alpaca_iex_qualification",
        url=request.url(policy),
        http_status=200,
        raw=b'{"bars":{"AAPL":[{"t":"2026-07-02T04:00:00Z","o":100,"h":102,"l":99,"c":101,"v":1000}]},"next_page_token":null}',
        headers={"content-type": "application/json"},
        retrieved_at=NOW + timedelta(seconds=1),
        synthetic_permit=_snapshot_permit(),
    )
    result = qualify_landed_pages(
        request,
        policy,
        (false_positive_page,),
        calendar_release_directory=calendar_release,
        accepted_release_root=calendar_release.parents[1],
    )
    assert result.state == "FAIL"
    assert "AAPL_missing_sessions:2026-07-01" in result.reasons

    numeric_poison_page = store.land(
        source="alpaca_iex_qualification",
        url=request.url(policy),
        http_status=200,
        raw=json.dumps(
            {
                "bars": {
                    "AAPL": [
                        {"t": "2026-07-01T04:00:00Z", "o": "99", "h": 101, "l": 98, "c": 100, "v": 900},
                        {"t": "2026-07-02T04:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": True},
                    ]
                },
                "next_page_token": None,
            }
        ).encode(),
        headers={"content-type": "application/json"},
        retrieved_at=NOW + timedelta(seconds=2),
        synthetic_permit=_snapshot_permit(),
    )
    poisoned = qualify_landed_pages(
        request,
        policy,
        (numeric_poison_page,),
        calendar_release_directory=calendar_release,
        accepted_release_root=calendar_release.parents[1],
    )
    assert poisoned.state == "FAIL"
    assert "page_0_AAPL_invalid_bar" in poisoned.reasons

    drifted_url_page = store.land(
        source="alpaca_iex_qualification",
        url=request.url(policy) + "&unexpected=true",
        http_status=200,
        raw=valid_page.read_verified_bytes(),
        headers={"content-type": "application/json"},
        retrieved_at=NOW + timedelta(seconds=3),
        synthetic_permit=_snapshot_permit(),
    )
    drifted = qualify_landed_pages(
        request,
        policy,
        (drifted_url_page,),
        calendar_release_directory=calendar_release,
        accepted_release_root=calendar_release.parents[1],
    )
    assert "page_0_request_url_drift" in drifted.reasons


def test_alpaca_daily_bar_session_mapping_respects_new_york_dst(tmp_path: Path) -> None:
    calendar_release = publish_xnys_calendar_release(
        staging_root=tmp_path / "calendar-stage",
        release_root=tmp_path / "calendar-releases",
        start=date(2026, 11, 2),
        end=date(2026, 11, 3),
        created_at="2026-11-04T00:00:00Z",
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
    )
    request = AlpacaBarsRequest(
        symbols=("AAPL",),
        start=datetime(2026, 11, 2, tzinfo=timezone.utc),
        end=datetime(2026, 11, 4, tzinfo=timezone.utc),
        requested_at=datetime(2026, 11, 5, tzinfo=timezone.utc),
    )
    policy = AlpacaBarsPolicy(feed="iex")
    raw = json.dumps(
        {
            "bars": {
                "AAPL": [
                    {"t": "2026-11-02T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 100},
                    {"t": "2026-11-03T05:00:00Z", "o": 10, "h": 11, "l": 9, "c": 10, "v": 100},
                ]
            },
            "next_page_token": None,
        }
    ).encode()
    page = AsReceivedSnapshotStore(
        tmp_path / "snapshots",
        allowed_root=tmp_path,
    ).land(
        source="alpaca_iex_qualification",
        url=request.url(policy),
        http_status=200,
        raw=raw,
        headers={"content-type": "application/json"},
        retrieved_at=datetime(2026, 11, 5, 0, 1, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    result = qualify_landed_pages(
        request,
        policy,
        (page,),
        calendar_release_directory=calendar_release,
        accepted_release_root=calendar_release.parents[1],
    )
    assert result.state == "PASS"
    assert result.bar_count == 2
