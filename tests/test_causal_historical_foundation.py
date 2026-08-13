from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest

from us_stocks_swing_model_v2.alpaca_free_bounded import EvidenceClass
from us_stocks_swing_model_v2.bounded_universe import (
    AFFIRMATIVE_COMMON_STOCK,
    IdentityEvidence,
    LiquidityObservation,
    PRIMARY_PROFILE,
    UniverseCandidate,
    build_universe_snapshot,
)
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.causal_foundation import (
    AvailabilityStamp,
    CausalDailyBar,
    CausalInputKind,
    CausalInputStore,
    CausalInputVersion,
    SessionBoundary,
    assess_daily_bar_integrity,
    build_causal_stock_date_panel,
    require_inputs_usable_at,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.corporate_actions import (
    ActionType,
    BitemporalActionLedger,
    CorporateAction,
    CorporateActionCoverage,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.identity import (
    BitemporalIdentityLedger,
    IdentitySnapshot,
    IdentityVersion,
)
from us_stocks_swing_model_v2.prospective_price_features import (
    CausalPriceBar,
    materialize_price_only_features,
)
from us_stocks_swing_model_v2.schemas import SecurityType


UTC = timezone.utc
D0 = date(2026, 8, 11)
D1 = date(2026, 8, 12)
D0_OPEN = datetime(2026, 8, 11, 13, 30, tzinfo=UTC)
D0_CLOSE = datetime(2026, 8, 11, 20, 0, tzinfo=UTC)
SIGNAL_CUTOFF = D0_CLOSE + timedelta(minutes=10)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(
    name: str,
    *,
    effective: datetime,
    usable: datetime,
    revision: str = "v1",
) -> AvailabilityStamp:
    return AvailabilityStamp(
        effective_time=effective,
        published_time=usable - timedelta(minutes=2),
        received_time=usable - timedelta(minutes=1),
        usable_time=usable,
        source_revision=revision,
        source_identifier=f"synthetic-{name}",
        source_snapshot_id=_hash(f"snapshot-{name}-{revision}-{usable.isoformat()}"),
    )


def _input(
    kind: CausalInputKind,
    *,
    key: str,
    usable: datetime,
    payload: dict[str, object],
    revision: int = 1,
    predecessor: str | None = None,
) -> CausalInputVersion:
    return CausalInputVersion.create(
        logical_key=key,
        stable_security_id=(None if kind is CausalInputKind.SESSION else "asset-1"),
        input_kind=kind,
        availability=_stamp(
            key,
            effective=SIGNAL_CUTOFF - timedelta(days=1),
            usable=usable,
            revision=f"v{revision}",
        ),
        revision_number=revision,
        predecessor_record_id=predecessor,
        payload=payload,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )


@pytest.mark.parametrize(
    ("kind", "base_payload", "future_payload"),
    (
        (CausalInputKind.PRICE, {"close": 10.0}, {"close": 999.0}),
        (CausalInputKind.VOLUME, {"volume": 1000}, {"volume": 999999}),
        (CausalInputKind.CORPORATE_ACTION, {"action": "NONE"}, {"action": "SPLIT"}),
        (CausalInputKind.MEMBERSHIP, {"member": True}, {"member": False}),
        (CausalInputKind.IDENTITY, {"ticker": "AAA"}, {"ticker": "AAB"}),
        (CausalInputKind.DELISTING, {"state": "ACTIVE"}, {"state": "DELISTED"}),
        (CausalInputKind.SECTOR_CLASSIFICATION, {"sector": "TECH"}, {"sector": "OTHER"}),
        (CausalInputKind.FUNDAMENTAL, {"book_value": 10.0}, {"book_value": 20.0}),
        (CausalInputKind.EARNINGS_EVENT, {"event_state": "NONE"}, {"event_state": "REPORTED"}),
        (CausalInputKind.ANALYST_ESTIMATE, {"estimate": 1.0}, {"estimate": 2.0}),
    ),
)
def test_future_revision_mutations_cannot_change_cutoff_view(
    kind: CausalInputKind,
    base_payload: dict[str, object],
    future_payload: dict[str, object],
) -> None:
    base = _input(
        kind,
        key=f"{kind.value.casefold()}-asset-1",
        usable=SIGNAL_CUTOFF - timedelta(minutes=1),
        payload=base_payload,
    )
    store = CausalInputStore((base,))
    before = store.snapshot_id(SIGNAL_CUTOFF)
    visible_before = store.visible_as_of(SIGNAL_CUTOFF)
    future = _input(
        kind,
        key=base.logical_key,
        usable=SIGNAL_CUTOFF + timedelta(days=1),
        payload=future_payload,
        revision=2,
        predecessor=base.record_id,
    )
    store.append(future)

    assert store.snapshot_id(SIGNAL_CUTOFF) == before
    assert store.visible_as_of(SIGNAL_CUTOFF) == visible_before == (base,)
    assert store.visible_as_of(SIGNAL_CUTOFF + timedelta(days=2)) == (future,)


def test_availability_and_vintage_chains_fail_closed() -> None:
    base = _input(
        CausalInputKind.FUNDAMENTAL,
        key="fundamental-asset-1",
        usable=SIGNAL_CUTOFF - timedelta(minutes=1),
        payload={"book_value": 10.0},
    )
    future = _input(
        CausalInputKind.FUNDAMENTAL,
        key=base.logical_key,
        usable=SIGNAL_CUTOFF + timedelta(days=1),
        payload={"book_value": 20.0},
        revision=2,
        predecessor=base.record_id,
    )
    with pytest.raises(ContractError, match="unavailable"):
        require_inputs_usable_at((future,), SIGNAL_CUTOFF)
    wrong_predecessor = _input(
        CausalInputKind.FUNDAMENTAL,
        key=base.logical_key,
        usable=SIGNAL_CUTOFF + timedelta(days=1),
        payload={"book_value": 20.0},
        revision=2,
        predecessor="f" * 64,
    )
    with pytest.raises(IntegrityError, match="predecessor"):
        CausalInputStore((base, wrong_predecessor))
    with pytest.raises(ContractError, match="prohibited"):
        CausalInputVersion.create(
            logical_key="poison",
            stable_security_id="asset-1",
            input_kind=CausalInputKind.PRICE,
            availability=base.availability,
            revision_number=1,
            predecessor_record_id=None,
            payload={"forward_return": 0.5},
            evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        )


def test_premarket_and_after_hours_inputs_follow_actual_usable_time() -> None:
    opening_cutoff = D0_OPEN
    premarket = _input(
        CausalInputKind.EARNINGS_EVENT,
        key="premarket-earnings-asset-1",
        usable=D0_OPEN - timedelta(minutes=30),
        payload={"event_state": "PUBLISHED_PREMARKET"},
    )
    after_hours = _input(
        CausalInputKind.EARNINGS_EVENT,
        key="after-hours-earnings-asset-1",
        usable=D0_CLOSE + timedelta(hours=1),
        payload={"event_state": "PUBLISHED_AFTER_HOURS"},
    )
    assert require_inputs_usable_at((premarket,), opening_cutoff) == (premarket,)
    with pytest.raises(ContractError, match="unavailable"):
        require_inputs_usable_at((after_hours,), SIGNAL_CUTOFF)


def _identity_snapshot(
    permit: SyntheticOnlyPermit,
    *,
    symbol: str,
    at: datetime,
    alpaca_id: str,
    nasdaq_id: str,
) -> IdentitySnapshot:
    row = IdentityVersion(
        asset_id="asset-1",
        symbol=symbol,
        security_type=SecurityType.STOCK,
        listing_exchange="NYSE",
        active=True,
        eligible=True,
        membership_present=True,
        abstention_reason=None,
        effective_at=at,
        known_at=at,
        identity_snapshot_id="0" * 64,
        alpaca_snapshot_id=alpaca_id,
        nasdaq_snapshot_id=nasdaq_id,
        nasdaq_file_created_at=at,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        synthetic_permit_ids=(permit.permit_id,),
    )
    provisional = IdentitySnapshot(
        snapshot_id="0" * 64,
        effective_at=at,
        known_at=at,
        complete_membership=True,
        alpaca_snapshot_id=alpaca_id,
        nasdaq_snapshot_id=nasdaq_id,
        nasdaq_file_created_at=at,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        synthetic_permit_ids=(permit.permit_id,),
        rows=(row,),
    )
    unsigned = provisional.receipt_dict()
    unsigned.pop("snapshot_id")
    for item in unsigned["rows"]:
        item.pop("identity_snapshot_id")
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    return replace(
        provisional,
        snapshot_id=snapshot_id,
        rows=(replace(row, identity_snapshot_id=snapshot_id),),
    )


def _identity_ledger() -> tuple[BitemporalIdentityLedger, SyntheticOnlyPermit, str]:
    permit = SyntheticOnlyPermit.create(
        fixture_id="causal-foundation-identity",
        scope="SYNTHETIC_IDENTITY_LEDGER",
    )
    ledger = BitemporalIdentityLedger(synthetic_permit=permit)
    snapshot = _identity_snapshot(
        permit,
        symbol="AAA",
        at=D0_CLOSE - timedelta(hours=1),
        alpaca_id="1" * 64,
        nasdaq_id="2" * 64,
    )
    ledger.append_snapshot(snapshot)
    return ledger, permit, snapshot.snapshot_id


def _universe():
    decision_at = D0_CLOSE + timedelta(minutes=5)
    identity = IdentityEvidence(
        stable_asset_id="asset-1",
        provider_asset_id="provider-asset-1",
        original_requested_ticker="AAA",
        returned_ticker="AAA",
        source_ticker="AAA",
        requested_as_of=D0 - timedelta(days=1),
        ticker_effective_from=date(2020, 1, 1),
        ticker_effective_through=None,
        listing_from=date(2020, 1, 1),
        delisting_through=None,
        exchange="NYSE",
        effective_at=datetime(2020, 1, 1, tzinfo=UTC),
        known_at=D0_CLOSE - timedelta(hours=1),
        mapping_evidence_id=_hash("mapping-asset-1"),
        mapping_status="CONFIRMED_CONTINUITY",
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    observations = tuple(
        LiquidityObservation(
            session=D0 - timedelta(days=60 - index),
            close=10.0,
            volume=100_000.0,
            available_at=D0_CLOSE - timedelta(hours=2),
            source_hash=_hash(f"liquidity-{index}"),
        )
        for index in range(60)
    )
    candidate = UniverseCandidate(
        identity=identity,
        ticker="AAA",
        security_classification=AFFIRMATIVE_COMMON_STOCK,
        exchange="NYSE",
        source_memberships=("SYNTHETIC_DATED_MEMBERSHIP",),
        source_receipt_times=(D0_CLOSE - timedelta(hours=1),),
        observations=observations,
        evidence_hashes=(_hash("universe-evidence"),),
    )
    return build_universe_snapshot(
        profile_id=PRIMARY_PROFILE,
        signal_session=D0,
        information_cutoff_session=D0 - timedelta(days=1),
        decision_at=decision_at,
        candidates=(candidate,),
    )


def _action_ledger() -> tuple[BitemporalActionLedger, SyntheticOnlyPermit]:
    permit = SyntheticOnlyPermit.create(
        fixture_id="causal-foundation-actions",
        scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
    )
    coverage = CorporateActionCoverage.create(
        effective_start_session=D0,
        effective_end_session=D0,
        asset_scope="EXACT_ASSET_IDS",
        asset_ids=("asset-1",),
        received_at=D0_CLOSE - timedelta(hours=1),
        source_snapshot_ids=("3" * 64,),
        provider_coverage_id="4" * 64,
        source_release_id=permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
    )
    return (
        BitemporalActionLedger(
            synthetic_permit=permit,
            coverage=(coverage,),
        ),
        permit,
    )


def _session_pair() -> tuple[SessionBoundary, SessionBoundary]:
    calendar_id = _hash("synthetic-calendar")
    return (
        SessionBoundary.create(
            session=D0,
            open_at=D0_OPEN,
            close_at=D0_CLOSE,
            early_close=False,
            calendar_release_id=calendar_id,
        ),
        SessionBoundary.create(
            session=D1,
            open_at=datetime(2026, 8, 12, 13, 30, tzinfo=UTC),
            close_at=datetime(2026, 8, 12, 20, 0, tzinfo=UTC),
            early_close=False,
            calendar_release_id=calendar_id,
        ),
    )


def _bar(identity_snapshot_id: str, *, session: date = D0, price: float = 10.0) -> CausalDailyBar:
    close_at = D0_CLOSE if session == D0 else D0_CLOSE + timedelta(days=1)
    return CausalDailyBar.create(
        stable_security_id="asset-1",
        session=session,
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price + 0.5,
        volume=1000,
        trade_count=100,
        vwap=price + 0.25,
        availability=_stamp(
            f"bar-{session}",
            effective=close_at,
            usable=close_at + timedelta(minutes=5),
        ),
        source_release_id=_hash("synthetic-bar-release"),
        identity_snapshot_id=identity_snapshot_id,
        adjustment_state="RAW_OBSERVED",
        raw_source_bar_id=None,
        corporate_action_ids=(),
        quality_flags=(),
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )


def test_canonical_panel_is_stable_under_future_identity_and_action_mutation() -> None:
    identity_ledger, identity_permit, identity_snapshot_id = _identity_ledger()
    action_ledger, action_permit = _action_ledger()
    current, following = _session_pair()
    universe = _universe()
    bar = _bar(identity_snapshot_id)
    before = build_causal_stock_date_panel(
        session=current,
        next_session=following,
        signal_cutoff=SIGNAL_CUTOFF,
        identity_ledger=identity_ledger,
        universe_snapshot=universe,
        bars=(bar,),
        action_ledger=action_ledger,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )
    assert before.rows[0].causal_ready
    assert before.rows[0].stable_security_id == "asset-1"
    assert before.rows[0].symbol == "AAA"
    assert before.rows[0].earliest_execution_session == D1

    identity_ledger.append_snapshot(
        _identity_snapshot(
            identity_permit,
            symbol="AAB",
            at=SIGNAL_CUTOFF + timedelta(days=1),
            alpaca_id="5" * 64,
            nasdaq_id="6" * 64,
        )
    )
    action_ledger.append(
        CorporateAction(
            action_id="future-delisting-event",
            asset_id="asset-1",
            action_type=ActionType.DELISTING,
            effective_session=D1,
            announced_at=None,
            received_at=SIGNAL_CUTOFF + timedelta(days=1),
            revision=1,
            source_snapshot_id="7" * 64,
            source_release_id=action_permit.permit_id,
            source_epoch="SYNTHETIC_ONLY",
            raw_row_sha256="8" * 64,
        )
    )
    after = build_causal_stock_date_panel(
        session=current,
        next_session=following,
        signal_cutoff=SIGNAL_CUTOFF,
        identity_ledger=identity_ledger,
        universe_snapshot=universe,
        bars=(bar,),
        action_ledger=action_ledger,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )
    assert after == before


def test_canonical_panel_cannot_be_ready_from_an_unresolved_raw_bar_reference() -> None:
    identity_ledger, _, identity_snapshot_id = _identity_ledger()
    action_ledger, action_permit = _action_ledger()
    action = CorporateAction(
        action_id="synthetic-split",
        asset_id="asset-1",
        action_type=ActionType.SPLIT,
        effective_session=D0,
        announced_at=D0_CLOSE - timedelta(hours=2),
        received_at=D0_CLOSE - timedelta(hours=1),
        revision=1,
        source_snapshot_id="3" * 64,
        source_release_id=action_permit.permit_id,
        source_epoch="SYNTHETIC_ONLY",
        raw_row_sha256="9" * 64,
        ratio_new_for_old=2.0,
    )
    action_ledger.append(action)
    adjusted = CausalDailyBar.create(
        stable_security_id="asset-1",
        session=D0,
        open=5.0,
        high=5.5,
        low=4.5,
        close=5.25,
        volume=2000,
        trade_count=100,
        vwap=5.1,
        availability=_stamp(
            "adjusted-bar",
            effective=D0_CLOSE,
            usable=D0_CLOSE + timedelta(minutes=5),
        ),
        source_release_id=_hash("synthetic-bar-release"),
        identity_snapshot_id=identity_snapshot_id,
        adjustment_state="CAUSAL_ACTION_ADJUSTED",
        raw_source_bar_id=_hash("unresolved-raw-bar"),
        corporate_action_ids=(action.action_id,),
        quality_flags=(),
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )
    current, following = _session_pair()
    panel = build_causal_stock_date_panel(
        session=current,
        next_session=following,
        signal_cutoff=SIGNAL_CUTOFF,
        identity_ledger=identity_ledger,
        universe_snapshot=_universe(),
        bars=(adjusted,),
        action_ledger=action_ledger,
        evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
    )
    assert panel.rows[0].causal_ready is False
    assert "MISSING_RAW_OBSERVED_DAILY_BAR" in panel.rows[0].blocker_codes


def test_prefix_features_and_universe_ignore_strictly_future_rows() -> None:
    sessions = tuple(D0 - timedelta(days=value) for value in range(5, -1, -1))
    base_bars = tuple(
        CausalPriceBar(
            asset_id="asset-1",
            session=session,
            open=10.0 + index,
            close=10.5 + index,
            available_at=SIGNAL_CUTOFF - timedelta(minutes=1),
        )
        for index, session in enumerate(sessions)
    )
    future = CausalPriceBar(
        asset_id="asset-1",
        session=D1,
        open=999.0,
        close=1.0,
        available_at=SIGNAL_CUTOFF + timedelta(days=1),
    )
    truncated = materialize_price_only_features(
        base_bars,
        sessions=sessions,
        decision_session=D0,
        decision_at=SIGNAL_CUTOFF,
        action_coverage_complete=True,
        action_or_delisting_sessions=frozenset(),
    )
    full = materialize_price_only_features(
        (*base_bars, future),
        sessions=(*sessions, D1),
        decision_session=D0,
        decision_at=SIGNAL_CUTOFF,
        action_coverage_complete=True,
        action_or_delisting_sessions=frozenset(),
    )
    assert full == truncated

    identity = IdentityEvidence(
        stable_asset_id="asset-1",
        provider_asset_id="provider-asset-1",
        original_requested_ticker="AAA",
        returned_ticker="AAA",
        source_ticker="AAA",
        requested_as_of=D0 - timedelta(days=1),
        ticker_effective_from=date(2020, 1, 1),
        ticker_effective_through=None,
        listing_from=date(2020, 1, 1),
        delisting_through=None,
        exchange="NYSE",
        effective_at=datetime(2020, 1, 1, tzinfo=UTC),
        known_at=D0_CLOSE - timedelta(hours=1),
        mapping_evidence_id=_hash("mapping-asset-1"),
        mapping_status="CONFIRMED_CONTINUITY",
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    base_observations = tuple(
        LiquidityObservation(
            session=D0 - timedelta(days=60 - index),
            close=10.0,
            volume=100_000.0,
            available_at=D0_CLOSE - timedelta(hours=2),
            source_hash=_hash(f"prefix-liquidity-{index}"),
        )
        for index in range(60)
    )
    candidate = UniverseCandidate(
        identity=identity,
        ticker="AAA",
        security_classification=AFFIRMATIVE_COMMON_STOCK,
        exchange="NYSE",
        source_memberships=("SYNTHETIC_DATED_MEMBERSHIP",),
        source_receipt_times=(D0_CLOSE - timedelta(hours=1),),
        observations=base_observations,
        evidence_hashes=(_hash("prefix-universe"),),
    )
    baseline = build_universe_snapshot(
        profile_id=PRIMARY_PROFILE,
        signal_session=D0,
        information_cutoff_session=D0 - timedelta(days=1),
        decision_at=D0_CLOSE + timedelta(minutes=5),
        candidates=(candidate,),
    )
    future_liquidity = LiquidityObservation(
        session=D1,
        close=999.0,
        volume=999_999_999.0,
        available_at=SIGNAL_CUTOFF + timedelta(days=1),
        source_hash=_hash("future-liquidity"),
    )
    mutated = build_universe_snapshot(
        profile_id=PRIMARY_PROFILE,
        signal_session=D0,
        information_cutoff_session=D0 - timedelta(days=1),
        decision_at=D0_CLOSE + timedelta(minutes=5),
        candidates=(replace(candidate, observations=(*base_observations, future_liquidity)),),
    )
    assert mutated == baseline


def test_session_boundaries_capture_dst_and_early_close_without_inference() -> None:
    release_id = _hash("calendar")
    before_dst = SessionBoundary.create(
        session=date(2026, 3, 6),
        open_at=datetime(2026, 3, 6, 14, 30, tzinfo=UTC),
        close_at=datetime(2026, 3, 6, 21, 0, tzinfo=UTC),
        early_close=False,
        calendar_release_id=release_id,
    )
    after_dst = SessionBoundary.create(
        session=date(2026, 3, 9),
        open_at=datetime(2026, 3, 9, 13, 30, tzinfo=UTC),
        close_at=datetime(2026, 3, 9, 20, 0, tzinfo=UTC),
        early_close=False,
        calendar_release_id=release_id,
    )
    early = SessionBoundary.create(
        session=date(2026, 11, 27),
        open_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
        close_at=datetime(2026, 11, 27, 18, 0, tzinfo=UTC),
        early_close=True,
        calendar_release_id=release_id,
    )
    assert before_dst.open_at.hour == 14
    assert after_dst.open_at.hour == 13
    assert early.early_close
    with pytest.raises(ContractError, match="early-close"):
        replace(early, early_close=False).validate()


def test_bar_integrity_reports_missing_stale_zero_volume_and_action_gaps() -> None:
    _, _, identity_snapshot_id = _identity_ledger()
    sessions = (D0 - timedelta(days=2), D0 - timedelta(days=1), D0)
    first = _bar(identity_snapshot_id, session=sessions[0], price=100.0)
    second_source = _bar(identity_snapshot_id, session=sessions[1], price=100.0)
    second = CausalDailyBar.create(
        **{
            **{
                key: value
                for key, value in second_source.__dict__.items()
                if key != "bar_id"
            },
            "open": first.open,
            "high": first.high,
            "low": first.low,
            "close": first.close,
            "volume": 0,
            "trade_count": 0,
            "vwap": None,
            "quality_flags": ("STALE_ZERO_VOLUME_BAR", "ZERO_VOLUME"),
        }
    )
    missing_and_stale = assess_daily_bar_integrity(
        (first, second),
        expected_sessions=sessions,
        action_sessions=frozenset(),
        extreme_gap_threshold=0.5,
    )
    assert missing_and_stale.state == "FAIL_INTEGRITY"
    assert missing_and_stale.missing_sessions == (D0,)
    assert missing_and_stale.zero_volume_sessions == (sessions[1],)
    assert missing_and_stale.stale_zero_volume_sessions == (sessions[1],)

    gap = _bar(identity_snapshot_id, session=D0, price=160.0)
    action_gap = assess_daily_bar_integrity(
        (first, gap),
        expected_sessions=(sessions[0], D0),
        action_sessions=frozenset({D0}),
        extreme_gap_threshold=0.5,
    )
    assert action_gap.state == "PASS"
    assert action_gap.action_associated_extreme_gap_sessions == (D0,)
    unexplained = assess_daily_bar_integrity(
        (first, gap),
        expected_sessions=(sessions[0], D0),
        action_sessions=frozenset(),
        extreme_gap_threshold=0.5,
    )
    assert unexplained.state == "FAIL_INTEGRITY"
    assert unexplained.unexplained_extreme_gap_sessions == (D0,)
    unresolved = assess_daily_bar_integrity(
        (first, gap),
        expected_sessions=(sessions[0], D0),
        action_sessions=frozenset(),
        extreme_gap_threshold=None,
    )
    assert unresolved.state == "BLOCKED_EXTREME_GAP_POLICY_UNRESOLVED"


def test_bar_integrity_reports_unexpected_stale_halt_and_duplicate_records() -> None:
    _, _, identity_snapshot_id = _identity_ledger()
    first_session = D0 - timedelta(days=1)
    first = _bar(identity_snapshot_id, session=first_session, price=50.0)
    next_source = _bar(identity_snapshot_id, session=D0, price=50.0)
    stale_halt = CausalDailyBar.create(
        **{
            **{
                key: value
                for key, value in next_source.__dict__.items()
                if key != "bar_id"
            },
            "open": first.open,
            "high": first.high,
            "low": first.low,
            "close": first.close,
            "quality_flags": ("TRADING_HALT_SUSPECTED",),
        }
    )
    report = assess_daily_bar_integrity(
        (first, stale_halt),
        expected_sessions=(first_session,),
        action_sessions=frozenset(),
        extreme_gap_threshold=0.5,
    )
    assert report.state == "FAIL_INTEGRITY"
    assert report.unexpected_sessions == (D0,)
    assert report.stale_price_sessions == (D0,)
    assert report.halt_suspected_sessions == (D0,)
    with pytest.raises(IntegrityError, match="duplicate"):
        assess_daily_bar_integrity(
            (first, first),
            expected_sessions=(first_session,),
            action_sessions=frozenset(),
            extreme_gap_threshold=0.5,
        )


def test_invalid_ohlc_and_same_close_execution_fail_closed() -> None:
    _, _, identity_snapshot_id = _identity_ledger()
    valid = _bar(identity_snapshot_id)
    with pytest.raises(ContractError, match="OHLC"):
        CausalDailyBar.create(
            **{
                **{
                    key: value
                    for key, value in valid.__dict__.items()
                    if key != "bar_id"
                },
                "high": 9.0,
            }
        )
    identity_ledger, _, snapshot_id = _identity_ledger()
    actions, _ = _action_ledger()
    current, following = _session_pair()
    with pytest.raises(ContractError, match="post-close"):
        build_causal_stock_date_panel(
            session=current,
            next_session=following,
            signal_cutoff=D0_CLOSE - timedelta(seconds=1),
            identity_ledger=identity_ledger,
            universe_snapshot=_universe(),
            bars=(_bar(snapshot_id),),
            action_ledger=actions,
            evidence_state="SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
        )
