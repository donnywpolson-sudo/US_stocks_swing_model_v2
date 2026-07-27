from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import us_stocks_swing_model_v2.ledger as ledger_module
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.calendar import PinnedSessionCalendar
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.corporate_actions import (
    AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS,
    ActionType,
    BitemporalActionLedger,
    CorporateAction,
    CorporateActionCoverage,
    EFFECTIVE_EVENT_COMPLETENESS,
    GovernedEffectiveEventCoverage,
    authorize_effective_event_coverage,
    build_governed_corporate_action_release_payload,
    prepare_effective_event_coverage,
)
from us_stocks_swing_model_v2.errors import (
    ContractError,
    EvaluationAuthorizationError,
    IntegrityError,
)
from us_stocks_swing_model_v2.governance import create_local_integrity_record
from us_stocks_swing_model_v2.ledger import OutcomeLedger, _outcome_interval_bars
from us_stocks_swing_model_v2.outcomes import (
    DailyBar,
    build_outcome,
    load_daily_bar_release,
)
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest
from us_stocks_swing_model_v2.providers.corporate_actions import (
    CORPORATE_ACTION_SOURCE_EPOCH,
    PROCESS_DATE_ACQUISITION_COVERAGE,
    CorporateActionCoverageEvidence,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.schemas import (
    OutcomeStatus,
    SecurityType,
    UnderlyingPrediction,
)


SESSIONS = ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13"]
AS_OF = datetime(2026, 7, 14, tzinfo=timezone.utc)
BAR_RELEASE_ID = "e" * 64


def _action_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="calendar-actions-ledger",
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )


def _coverage_clock() -> TrustedClock:
    return TrustedClock.synthetic_fixed(
        AS_OF + timedelta(minutes=1),
        permit=SyntheticOnlyPermit.create(
            fixture_id="calendar-action-coverage-clock",
            scope="TRUSTED_CLOCK_FIXED_TIME",
        ),
    )


def _provider_coverage() -> CorporateActionCoverageEvidence:
    acquisition_permit = SyntheticOnlyPermit.create(
        fixture_id="calendar-action-provider-coverage",
        scope="SYNTHETIC_CORPORATE_ACTION_PROVIDER_COVERAGE",
    )
    unsigned = {
        "schema_version": 2,
        "coverage_semantics": PROCESS_DATE_ACQUISITION_COVERAGE,
        "process_date_start": "2026-07-01",
        "process_date_end": "2026-07-13",
        "requested_at": "2026-07-13T00:00:00Z",
        "requested_symbols": ["ABC"],
        "completed_at": "2026-07-13T01:00:00Z",
        "snapshot_ids": ["a" * 64],
        "acquisition_mode": "SYNTHETIC_DIRECT_NOT_AS_RECEIVED",
        "evidence_state": "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        "acquisition_capability_ids": [acquisition_permit.permit_id],
        "synthetic_permit_ids": [acquisition_permit.permit_id],
        "source_epoch": CORPORATE_ACTION_SOURCE_EPOCH,
    }
    return CorporateActionCoverageEvidence.from_dict(
        {
            **unsigned,
            "coverage_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
    )


def _governed_coverage() -> tuple[
    GovernedEffectiveEventCoverage,
    TrustedClock,
]:
    prepared = prepare_effective_event_coverage(
        _provider_coverage(),
        effective_start_session=date(2026, 7, 7),
        effective_end_session=date(2026, 7, 13),
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        reviewed_at=AS_OF,
        source_release_id="0" * 64,
        provider_contract_id="1" * 64,
        late_arrival_policy_id="2" * 64,
    )
    clock = _coverage_clock()
    authorization = create_local_integrity_record(
        scope=AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS,
        subject_id=prepared.coverage.coverage_content_id,
        bindings=prepared.authorization_bindings(),
        clock=clock,
    )
    governed = authorize_effective_event_coverage(
        prepared,
        authorization=authorization,
        clock=clock,
    )
    return governed, clock


def _coverage(permit: SyntheticOnlyPermit) -> CorporateActionCoverage:
    return CorporateActionCoverage.create(
        effective_start_session=date(2026, 7, 7),
        effective_end_session=date(2026, 7, 13),
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        received_at=AS_OF,
        source_snapshot_ids=("a" * 64,),
        provider_coverage_id="c" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
    )


def _action_ledger(actions=(), *, covered: bool = True) -> BitemporalActionLedger:
    permit = _action_permit()
    coverage = (_coverage(permit),) if covered else ()
    return BitemporalActionLedger(
        actions,
        synthetic_permit=permit,
        coverage=coverage,
    )


def _calendar() -> PinnedSessionCalendar:
    permit = SyntheticOnlyPermit.create(
        fixture_id="calendar-actions-outcomes",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    return PinnedSessionCalendar.from_iso_dates(
        permit.permit_id,
        SESSIONS,
        synthetic_permit=permit,
    )


@pytest.mark.parametrize("value", [None, 20260706])
def test_synthetic_calendar_normalizes_non_string_session_errors(
    value: object,
) -> None:
    permit = SyntheticOnlyPermit.create(
        fixture_id=f"calendar-invalid-session-{value!r}",
        scope="SYNTHETIC_SESSION_CALENDAR",
    )
    with pytest.raises(ContractError, match="invalid ISO session"):
        PinnedSessionCalendar.from_iso_dates(
            permit.permit_id,
            [value],
            synthetic_permit=permit,
        )


def _bars(exit_close: float = 55.0) -> dict[date, DailyBar]:
    result = {}
    for value in SESSIONS[1:]:
        session = date.fromisoformat(value)
        result[session] = DailyBar("asset-1", session, 100.0, exit_close, AS_OF)
    return result


def _action(action_type: ActionType, *, revision: int = 1, ratio: float | None = None, received: datetime = AS_OF) -> CorporateAction:
    permit = _action_permit()
    return CorporateAction(
        action_id="action-1",
        asset_id="asset-1",
        action_type=action_type,
        effective_session=date(2026, 7, 10),
        announced_at=None,
        received_at=received,
        revision=revision,
        source_snapshot_id="a" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
        raw_row_sha256="b" * 64,
        ratio_new_for_old=ratio,
    )


def test_split_normalized_next_open_to_fifth_close_uses_pinned_sessions() -> None:
    outcome = build_outcome(
        prediction_id="1" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=_action_ledger([_action(ActionType.SPLIT, ratio=2.0)]),
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is OutcomeStatus.MATURED
    assert outcome.entry_session == date(2026, 7, 7)
    assert outcome.exit_session == date(2026, 7, 13)
    assert outcome.split_normalized_price_return == pytest.approx(0.10)


def test_outcome_ledger_selects_only_the_exact_horizon_from_long_history() -> None:
    history = list(_bars().values())
    history.extend(
        (
            DailyBar(
                "asset-1",
                date(2026, 7, 6),
                90.0,
                91.0,
                AS_OF,
            ),
            DailyBar(
                "other-asset",
                date(2026, 7, 7),
                20.0,
                21.0,
                AS_OF,
            ),
        )
    )
    selected = _outcome_interval_bars(
        calendar=_calendar(),
        decision_session=date(2026, 7, 6),
        asset_id="asset-1",
        all_bars=history,
    )
    assert tuple(selected) == tuple(
        date.fromisoformat(value) for value in SESSIONS[1:]
    )


def test_corporate_action_revisions_are_bitemporal() -> None:
    early = datetime(2026, 7, 11, tzinfo=timezone.utc)
    late = datetime(2026, 7, 14, tzinfo=timezone.utc)
    ledger = _action_ledger(
        [
            _action(ActionType.SPLIT, revision=1, ratio=2.0, received=early),
            _action(ActionType.SPLIT, revision=2, ratio=3.0, received=late),
        ],
    )
    assert ledger.visible_as_of("asset-1", early)[0].ratio_new_for_old == 2.0
    assert ledger.visible_as_of("asset-1", late)[0].ratio_new_for_old == 3.0


@pytest.mark.parametrize(
    "action_type",
    [
        ActionType.DIVIDEND,
        ActionType.MERGER,
        ActionType.SPINOFF,
        ActionType.CONVERSION,
        ActionType.DELISTING,
        ActionType.OTHER,
    ],
)
def test_only_split_actions_accept_split_ratio(
    action_type: ActionType,
) -> None:
    action = _action(action_type, ratio=2.0)
    with pytest.raises(
        ContractError,
        match="only split actions may carry ratio_new_for_old",
    ):
        action.validate()
    _action(action_type, ratio=None).validate()
    _action(ActionType.SPLIT, ratio=2.0).validate()


def test_corporate_action_ledger_requires_verified_release_or_explicit_synthetic_scope(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    action_payload = {
        "schema_version": 1,
        "actions": [{
            "action_id": "verified-action",
            "asset_id": "asset-1",
            "action_type": "SPLIT",
            "effective_session": "2026-07-10",
            "announced_at": None,
            "received_at": "2026-07-14T00:00:00Z",
            "revision": 1,
            "source_snapshot_id": "a" * 64,
            "raw_row_sha256": "b" * 64,
            "ratio_new_for_old": 2.0,
            "voided": False,
        }],
    }
    (stage / "corporate_actions.json").write_text(
        json.dumps(action_payload, sort_keys=True), encoding="utf-8"
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch="alpaca_corporate_actions_v1",
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-10",
        event_end="2026-07-10",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    release = AtomicReleasePublisher(tmp_path / "accepted").publish(stage, manifest)
    ledger = BitemporalActionLedger(
        verified_release_directory=release,
        accepted_release_root=tmp_path / "accepted",
    )
    assert ledger.trust_eligible is False
    assert ledger.release_id == manifest.release_id
    assert ledger.covers_interval(
        "asset-1",
        date(2026, 7, 7),
        date(2026, 7, 13),
        AS_OF,
    ) is False
    with pytest.raises(ContractError, match="immutable payload views"):
        ledger.append(
            replace(
                ledger.visible_as_of("asset-1", AS_OF)[0],
                action_id="later-action",
            )
        )

    with pytest.raises(ContractError, match="must come only from release payload"):
        BitemporalActionLedger(
            [replace(ledger.visible_as_of("asset-1", AS_OF)[0], source_release_id="f" * 64)],
            verified_release_directory=release,
            accepted_release_root=tmp_path / "accepted",
        )


@pytest.mark.parametrize(
    ("field", "poison"),
    [
        ("action_id", 123),
        ("asset_id", 123),
        ("action_type", True),
        ("action_type", "UNKNOWN_ACTION"),
        ("effective_session", 20260710),
        ("effective_session", "not-a-date"),
        ("announced_at", 0),
        ("received_at", 0),
        ("source_snapshot_id", 123),
        ("raw_row_sha256", 123),
    ],
)
def test_verified_action_release_rejects_hash_consistent_coerced_text_fields(
    tmp_path: Path,
    field: str,
    poison: object,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    row = {
        "action_id": "verified-action",
        "asset_id": "asset-1",
        "action_type": "SPLIT",
        "effective_session": "2026-07-10",
        "announced_at": None,
        "received_at": "2026-07-14T00:00:00Z",
        "revision": 1,
        "source_snapshot_id": "a" * 64,
        "raw_row_sha256": "b" * 64,
        "ratio_new_for_old": 2.0,
        "voided": False,
    }
    row[field] = poison
    (stage / "corporate_actions.json").write_text(
        json.dumps(
            {"schema_version": 1, "actions": [row]},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch="alpaca_corporate_actions_v1",
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-10",
        event_end="2026-07-10",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    accepted = tmp_path / "accepted"
    release = AtomicReleasePublisher(accepted).publish(stage, manifest)
    expected = (
        "action type or effective session is invalid"
        if poison in {"UNKNOWN_ACTION", "not-a-date"}
        else "announced_at must be exact text or null"
        if field == "announced_at"
        else "identity/provenance fields must be exact text"
    )
    with pytest.raises(IntegrityError, match=expected):
        BitemporalActionLedger(
            verified_release_directory=release,
            accepted_release_root=accepted,
        )


@pytest.mark.parametrize(
    ("field", "poison"),
    [
        ("asset_id", 123),
        ("session", 20260707),
        ("available_at", 0),
    ],
)
def test_daily_bar_release_rejects_coerced_identity_and_time_fields(
    tmp_path: Path,
    field: str,
    poison: object,
) -> None:
    stage = tmp_path / "bar-stage"
    stage.mkdir()
    row = {
        "asset_id": "asset-1",
        "session": "2026-07-07",
        "open": 100.0,
        "close": 101.0,
        "available_at": "2026-07-07T21:00:00Z",
        "halted": False,
        "delisted": False,
    }
    row[field] = poison
    (stage / "daily_bars.json").write_text(
        json.dumps({"schema_version": 1, "rows": [row]}, sort_keys=True),
        encoding="utf-8",
    )
    manifest = build_manifest(
        stage,
        ["daily_bars.json"],
        project="US_stocks_swing_model_v2",
        dataset="bars",
        source_epoch="fixture_bars_v1",
        role="active_historical",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-07",
        event_end="2026-07-07",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    accepted = tmp_path / "bar-accepted"
    release = AtomicReleasePublisher(accepted).publish(stage, manifest)
    with pytest.raises(ContractError, match="exact JSON strings"):
        load_daily_bar_release(
            release,
            accepted_release_root=accepted,
        )


def test_daily_bar_release_rejects_extra_authenticated_payload(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "bar-stage-extra"
    stage.mkdir()
    (stage / "daily_bars.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rows": [
                    {
                        "asset_id": "asset-1",
                        "session": "2026-07-07",
                        "open": 100.0,
                        "close": 101.0,
                        "available_at": "2026-07-07T21:00:00Z",
                        "halted": False,
                        "delisted": False,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (stage / "unexpected.json").write_text("{}", encoding="utf-8")
    manifest = build_manifest(
        stage,
        ["daily_bars.json", "unexpected.json"],
        project="US_stocks_swing_model_v2",
        dataset="bars",
        source_epoch="fixture_bars_v1",
        role="active_historical",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-07",
        event_end="2026-07-07",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    accepted = tmp_path / "bar-accepted-extra"
    release = AtomicReleasePublisher(accepted).publish(stage, manifest)

    with pytest.raises(ContractError, match="payload census"):
        load_daily_bar_release(
            release,
            accepted_release_root=accepted,
        )


def test_explicit_zero_action_coverage_allows_mature_outcome() -> None:
    outcome = build_outcome(
        prediction_id="3" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=_action_ledger(),
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is OutcomeStatus.MATURED


def test_outcomes_require_explicit_effective_event_completeness() -> None:
    permit = _action_permit()
    complete = CorporateActionCoverage.create(
        effective_start_session=date(2026, 7, 7),
        effective_end_session=date(2026, 7, 13),
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        received_at=AS_OF,
        source_snapshot_ids=("a" * 64,),
        provider_coverage_id="c" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
    )
    assert complete.coverage_semantics == EFFECTIVE_EVENT_COMPLETENESS
    same_content_other_release = CorporateActionCoverage.create(
        effective_start_session=date(2026, 7, 7),
        effective_end_session=date(2026, 7, 13),
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        received_at=AS_OF,
        source_snapshot_ids=("a" * 64,),
        provider_coverage_id="c" * 64,
        source_release_id="e" * 64,
        source_epoch="SYNTHETIC_ONLY",
    )
    assert (
        same_content_other_release.coverage_content_id
        == complete.coverage_content_id
    )
    assert same_content_other_release.coverage_id != complete.coverage_id
    with pytest.raises(ContractError, match="effective-event completeness"):
        replace(
            complete,
            coverage_semantics="PROVIDER_PROCESS_DATE_ACQUISITION_ONLY",
        ).validate()
    with pytest.raises(ContractError, match="asset census"):
        replace(complete, asset_ids=(123,)).validate()
    with pytest.raises(ContractError, match="source epoch must be exact text"):
        replace(complete, source_epoch=123).validate()

    narrow = CorporateActionCoverage.create(
        effective_start_session=date(2026, 7, 8),
        effective_end_session=date(2026, 7, 13),
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        received_at=AS_OF,
        source_snapshot_ids=("a" * 64,),
        provider_coverage_id="d" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
    )
    ledger = BitemporalActionLedger(
        synthetic_permit=permit,
        coverage=(narrow,),
    )
    outcome = build_outcome(
        prediction_id="5" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=ledger,
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is OutcomeStatus.MISSING_SOURCE
    assert outcome.reason == "complete corporate-action coverage is unavailable"


def test_verified_zero_action_release_preserves_explicit_coverage(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    governed, clock = _governed_coverage()
    (stage / "corporate_actions.json").write_bytes(
        build_governed_corporate_action_release_payload(
            actions=(),
            governed_coverage=(governed,),
            clock=clock,
        )
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch="alpaca_corporate_actions_v1",
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=0,
        event_start="2026-07-07",
        event_end="2026-07-13",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    release = AtomicReleasePublisher(tmp_path / "accepted").publish(stage, manifest)
    with pytest.raises(ContractError, match="requires a trusted clock"):
        BitemporalActionLedger(
            verified_release_directory=release,
            accepted_release_root=tmp_path / "accepted",
        )
    ledger = BitemporalActionLedger(
        verified_release_directory=release,
        accepted_release_root=tmp_path / "accepted",
        clock=clock,
    )
    assert ledger.visible_as_of("asset-1", AS_OF) == ()
    assert ledger.covers_interval(
        "asset-1",
        date(2026, 7, 7),
        date(2026, 7, 13),
        AS_OF,
    )
    assert ledger.trust_eligible is True


def test_public_outcome_maturation_rejects_legacy_actions_and_accepts_governed_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def publish_actions(name: str, payload: bytes) -> tuple[Path, Path]:
        stage = tmp_path / f"{name}-stage"
        stage.mkdir()
        (stage / "corporate_actions.json").write_bytes(payload)
        manifest = build_manifest(
            stage,
            ["corporate_actions.json"],
            project="US_stocks_swing_model_v2",
            dataset="corporate_actions",
            source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
            role="prospective_as_received",
            quality_state="PASS",
            created_at="2026-07-15T12:05:00Z",
            row_count=0,
            event_start="2026-07-07",
            event_end="2026-07-13",
            schema_fingerprint="1" * 64,
            code_hash="2" * 64,
            config_hash="3" * 64,
            environment_hash="4" * 64,
        )
        accepted = tmp_path / f"{name}-accepted"
        return accepted, AtomicReleasePublisher(accepted).publish(stage, manifest)

    legacy_root, legacy_release = publish_actions(
        "legacy",
        canonical_json_bytes({"schema_version": 1, "actions": []}),
    )
    legacy_actions = BitemporalActionLedger(
        verified_release_directory=legacy_release,
        accepted_release_root=legacy_root,
    )
    assert legacy_actions.trust_eligible is False

    governed, clock = _governed_coverage()
    governed_root, governed_release = publish_actions(
        "governed",
        build_governed_corporate_action_release_payload(
            actions=(),
            governed_coverage=(governed,),
            clock=clock,
        ),
    )
    governed_actions = BitemporalActionLedger(
        verified_release_directory=governed_release,
        accepted_release_root=governed_root,
        clock=clock,
    )
    assert governed_actions.trust_eligible is True

    calendar = _calendar()
    bar_source_epoch = "fixture_bars_v1"

    def prediction_for(action_release_id: str) -> UnderlyingPrediction:
        return UnderlyingPrediction.create(
            asset_id="asset-1",
            symbol="ABC",
            security_type=SecurityType.STOCK,
            decision_session=date(2026, 7, 6),
            decision_at=datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc),
            recorded_at=datetime(2026, 7, 6, 20, 1, tzinfo=timezone.utc),
            time_authority=clock.mode,
            synthetic_clock_permit_id=clock.synthetic_permit_id,
            eligibility_census_id="c" * 64,
            bundle_id="1" * 64,
            feature_release_id="2" * 64,
            feature_row_hash="3" * 64,
            identity_release_id="4" * 64,
            security_type_evidence_id="5" * 64,
            calendar_release_id=calendar.release_id,
            action_release_id=action_release_id,
            source_epoch=bar_source_epoch,
            point_in_time_state="PIT_CONFIRMED",
            prediction_deadline_at=datetime(
                2026,
                7,
                6,
                20,
                5,
                tzinfo=timezone.utc,
            ),
            information_barrier_at=datetime(
                2026,
                7,
                7,
                20,
                0,
                tzinfo=timezone.utc,
            ),
            expected_five_session_return=0.01,
            p_up=0.6,
            p_down=0.2,
            p_neutral=0.2,
            uncertainty=0.05,
            rank=1,
            abstain=False,
            abstention_reason=None,
        )

    monkeypatch.setattr(
        ledger_module,
        "load_xnys_calendar_release",
        lambda *args, **kwargs: SimpleNamespace(calendar=calendar),
    )
    monkeypatch.setattr(
        ledger_module,
        "load_daily_bar_release",
        lambda *args, **kwargs: (
            SimpleNamespace(
                release_id=BAR_RELEASE_ID,
                source_epoch=bar_source_epoch,
            ),
            (),
        ),
    )

    legacy_prediction = prediction_for(legacy_actions.release_id)
    legacy_predictions = SimpleNamespace(
        verify=lambda _: [{"payload": legacy_prediction.as_dict()}]
    )
    monkeypatch.setattr(
        ledger_module,
        "BitemporalActionLedger",
        lambda *args, **kwargs: legacy_actions,
    )
    legacy_outcomes = OutcomeLedger(
        tmp_path / "legacy-outcomes" / "outcomes.jsonl",
        legacy_predictions,
        anchor_root=tmp_path / "legacy-outcome-anchors",
        clock=clock,
    )
    with pytest.raises(ContractError, match="trust-eligible corporate-action"):
        legacy_outcomes.mature_from_releases(
            legacy_prediction.prediction_id,
            prediction_anchor=tmp_path / "prediction-anchor",
            accepted_release_root=legacy_root,
            calendar_release_directory=tmp_path / "calendar-release",
            bar_release_directory=tmp_path / "bar-release",
            action_release_directory=legacy_release,
        )
    assert not legacy_outcomes._ledger.path.exists()

    class TrustedActionGateReached(Exception):
        pass

    def trusted_gate_reached(*args: object, **kwargs: object) -> object:
        raise TrustedActionGateReached

    governed_prediction = prediction_for(governed_actions.release_id)
    governed_predictions = SimpleNamespace(
        verify=lambda _: [{"payload": governed_prediction.as_dict()}]
    )
    monkeypatch.setattr(
        ledger_module,
        "BitemporalActionLedger",
        lambda *args, **kwargs: governed_actions,
    )
    monkeypatch.setattr(ledger_module, "build_outcome", trusted_gate_reached)
    governed_outcomes = OutcomeLedger(
        tmp_path / "governed-outcomes" / "outcomes.jsonl",
        governed_predictions,
        anchor_root=tmp_path / "governed-outcome-anchors",
        clock=clock,
    )
    with pytest.raises(TrustedActionGateReached):
        governed_outcomes.mature_from_releases(
            governed_prediction.prediction_id,
            prediction_anchor=tmp_path / "prediction-anchor",
            accepted_release_root=governed_root,
            calendar_release_directory=tmp_path / "calendar-release",
            bar_release_directory=tmp_path / "bar-release",
            action_release_directory=governed_release,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"asset_id": "asset-2"},
        {"effective_session": date(2026, 7, 14)},
    ],
)
def test_governed_release_binds_each_action_to_asset_and_session_coverage(
    mutation: dict[str, object],
) -> None:
    governed, clock = _governed_coverage()
    action = replace(
        _action(ActionType.SPLIT, ratio=2.0),
        source_release_id="0" * 64,
        source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
        **mutation,
    )
    with pytest.raises(ContractError, match="asset/session coverage"):
        build_governed_corporate_action_release_payload(
            actions=(action,),
            governed_coverage=(governed,),
            clock=clock,
        )


def test_verified_governed_release_rechecks_action_coverage_binding(
    tmp_path: Path,
) -> None:
    governed, clock = _governed_coverage()
    action = replace(
        _action(ActionType.SPLIT, ratio=2.0),
        source_release_id="0" * 64,
        source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
    )
    payload = json.loads(
        build_governed_corporate_action_release_payload(
            actions=(action,),
            governed_coverage=(governed,),
            clock=clock,
        )
    )
    payload["actions"][0]["asset_id"] = "asset-2"
    stage = tmp_path / "action-scope-stage"
    stage.mkdir()
    (stage / "corporate_actions.json").write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=1,
        event_start="2026-07-10",
        event_end="2026-07-10",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    accepted = tmp_path / "action-scope-accepted"
    release = AtomicReleasePublisher(accepted).publish(stage, manifest)
    with pytest.raises(IntegrityError, match="asset/session coverage"):
        BitemporalActionLedger(
            verified_release_directory=release,
            accepted_release_root=accepted,
            clock=clock,
        )


def test_verified_coverage_rejects_noncanonical_timestamp_and_missing_local_record(
    tmp_path: Path,
) -> None:
    governed, clock = _governed_coverage()
    governed_row = governed.payload_dict()
    for suffix, mutate, expected in (
        (
            "offset",
            lambda row: row["coverage"].__setitem__(
                "received_at",
                "2026-07-13T17:00:00-07:00",
            ),
            "canonical UTC Z",
        ),
        (
            "authorization",
            lambda row: row.pop("authorization"),
            "row fields differ",
        ),
        (
            "policy",
            lambda row: row.__setitem__(
                "late_arrival_policy_id",
                "3" * 64,
            ),
            "local integrity bindings differ",
        ),
    ):
        stage = tmp_path / f"{suffix}-stage"
        stage.mkdir()
        row = json.loads(json.dumps(governed_row))
        mutate(row)
        (stage / "corporate_actions.json").write_text(
            json.dumps(
                {
                    "schema_version": 5,
                    "actions": [],
                    "coverage": [row],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        manifest = build_manifest(
            stage,
            ["corporate_actions.json"],
            project="US_stocks_swing_model_v2",
            dataset="corporate_actions",
            source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
            role="prospective_as_received",
            quality_state="PASS",
            created_at="2026-07-15T12:05:00Z",
            row_count=0,
            event_start="2026-07-07",
            event_end="2026-07-13",
            schema_fingerprint="1" * 64,
            code_hash="2" * 64,
            config_hash="3" * 64,
            environment_hash="4" * 64,
        )
        accepted = tmp_path / f"{suffix}-accepted"
        release = AtomicReleasePublisher(accepted).publish(stage, manifest)
        with pytest.raises(
            (ContractError, EvaluationAuthorizationError, IntegrityError),
            match=expected,
        ):
            BitemporalActionLedger(
                verified_release_directory=release,
                accepted_release_root=accepted,
                clock=clock,
            )


def test_ambiguous_legacy_coverage_schema_is_not_trust_eligible(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "legacy-stage"
    stage.mkdir()
    (stage / "corporate_actions.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "actions": [],
                "coverage": [
                    {
                        "coverage_id": "a" * 64,
                        "start_session": "2026-07-07",
                        "end_session": "2026-07-13",
                        "asset_scope": "EXACT_ASSET_IDS",
                        "asset_ids": ["asset-1"],
                        "received_at": "2026-07-14T00:00:00Z",
                        "source_snapshot_ids": ["b" * 64],
                        "provider_coverage_id": "c" * 64,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest = build_manifest(
        stage,
        ["corporate_actions.json"],
        project="US_stocks_swing_model_v2",
        dataset="corporate_actions",
        source_epoch="alpaca_corporate_actions_v1",
        role="prospective_as_received",
        quality_state="PASS",
        created_at="2026-07-15T12:05:00Z",
        row_count=0,
        event_start="2026-07-07",
        event_end="2026-07-13",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )
    release = AtomicReleasePublisher(tmp_path / "legacy-accepted").publish(
        stage,
        manifest,
    )
    with pytest.raises(IntegrityError, match="lacks governed"):
        BitemporalActionLedger(
            verified_release_directory=release,
            accepted_release_root=tmp_path / "legacy-accepted",
        )


def test_absent_corporate_action_coverage_fails_closed() -> None:
    outcome = build_outcome(
        prediction_id="4" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=_action_ledger(covered=False),
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is OutcomeStatus.MISSING_SOURCE
    assert outcome.reason == "complete corporate-action coverage is unavailable"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("missing", OutcomeStatus.MISSING_SOURCE),
        ("halted", OutcomeStatus.HALTED),
        ("delisted", OutcomeStatus.DELISTED),
        ("merger", OutcomeStatus.ACTION_UNRESOLVED),
    ],
)
def test_unresolved_paths_are_retained_not_dropped(mutation: str, expected: OutcomeStatus) -> None:
    bars = _bars(exit_close=110.0)
    actions = _action_ledger()
    exit_session = date(2026, 7, 13)
    if mutation == "missing":
        del bars[exit_session]
    elif mutation == "halted":
        bars[exit_session] = DailyBar("asset-1", exit_session, None, None, AS_OF, halted=True)
    elif mutation == "delisted":
        middle = date(2026, 7, 10)
        bars[middle] = DailyBar("asset-1", middle, None, None, AS_OF, delisted=True)
    else:
        actions.append(_action(ActionType.MERGER))
    outcome = build_outcome(
        prediction_id="2" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=bars,
        bar_release_id=BAR_RELEASE_ID,
        actions=actions,
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    assert outcome.status is expected
    assert outcome.split_normalized_price_return is None
    assert outcome.reason
    if mutation == "merger":
        assert outcome.reason == (
            "unsupported corporate-action type prevents a comparable "
            "price-return outcome"
        )


@pytest.mark.parametrize("mutation", ["delisted", "halted", "missing_price"])
def test_unresolved_bar_state_cannot_be_observed_before_bar_availability(
    mutation: str,
) -> None:
    bars = _bars()
    if mutation == "delisted":
        session = date(2026, 7, 10)
        bars[session] = DailyBar(
            "asset-1",
            session,
            None,
            None,
            AS_OF + timedelta(minutes=1),
            delisted=True,
        )
    elif mutation == "halted":
        session = date(2026, 7, 13)
        bars[session] = DailyBar(
            "asset-1",
            session,
            None,
            None,
            AS_OF + timedelta(minutes=1),
            halted=True,
        )
    else:
        session = date(2026, 7, 13)
        bars[session] = DailyBar(
            "asset-1",
            session,
            None,
            None,
            AS_OF + timedelta(minutes=1),
        )
    with pytest.raises(
        ContractError,
        match="unified outcome evidence view cannot predate outcome-bar availability",
    ):
        build_outcome(
            prediction_id="2" * 64,
            eligibility_census_id="c" * 64,
            asset_id="asset-1",
            decision_session=date(2026, 7, 6),
            calendar=_calendar(),
            bars=bars,
            bar_release_id=BAR_RELEASE_ID,
            actions=_action_ledger(),
            action_view_as_of=AS_OF,
            source_epoch="fixture_epoch",
        )


@pytest.mark.parametrize(
    ("availability_delta", "raises"),
    [
        (timedelta(microseconds=-1), False),
        (timedelta(0), False),
        (timedelta(microseconds=1), True),
    ],
)
def test_unified_outcome_evidence_cutoff_applies_to_bar_availability(
    availability_delta: timedelta,
    raises: bool,
) -> None:
    bars = _bars()
    exit_session = date(2026, 7, 13)
    bars[exit_session] = replace(
        bars[exit_session],
        available_at=AS_OF + availability_delta,
    )
    arguments = {
        "prediction_id": "2" * 64,
        "eligibility_census_id": "c" * 64,
        "asset_id": "asset-1",
        "decision_session": date(2026, 7, 6),
        "calendar": _calendar(),
        "bars": bars,
        "bar_release_id": BAR_RELEASE_ID,
        "actions": _action_ledger(),
        "action_view_as_of": AS_OF,
        "source_epoch": "fixture_epoch",
    }
    if raises:
        with pytest.raises(
            ContractError,
            match="unified outcome evidence view",
        ):
            build_outcome(**arguments)
        return
    outcome = build_outcome(**arguments)
    assert outcome.status is OutcomeStatus.MATURED
    assert outcome.evidence_view_as_of == AS_OF


def test_unified_outcome_evidence_cutoff_controls_action_visibility() -> None:
    merger = _action(
        ActionType.MERGER,
        received=AS_OF + timedelta(microseconds=1),
    )
    actions = _action_ledger([merger])
    before = build_outcome(
        prediction_id="2" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=actions,
        action_view_as_of=AS_OF,
        source_epoch="fixture_epoch",
    )
    after = build_outcome(
        prediction_id="2" * 64,
        eligibility_census_id="c" * 64,
        asset_id="asset-1",
        decision_session=date(2026, 7, 6),
        calendar=_calendar(),
        bars=_bars(),
        bar_release_id=BAR_RELEASE_ID,
        actions=actions,
        action_view_as_of=AS_OF + timedelta(microseconds=1),
        source_epoch="fixture_epoch",
    )
    assert before.status is OutcomeStatus.MATURED
    assert after.status is OutcomeStatus.ACTION_UNRESOLVED
