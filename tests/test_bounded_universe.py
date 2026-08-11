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
    ProfileIdentityLedger,
    SENSITIVITY_PROFILE,
    UniverseCandidate,
    build_universe_snapshot,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError


DECISION = datetime(2026, 8, 10, 21, tzinfo=timezone.utc)
SIGNAL = date(2026, 8, 11)
CUTOFF = date(2026, 8, 10)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(symbol: str, *, asset_id: str | None = None, known_at: datetime = DECISION - timedelta(days=1), **overrides) -> IdentityEvidence:
    payload = dict(
        stable_asset_id=asset_id or f"asset-{symbol}",
        provider_asset_id=f"provider-{asset_id or symbol}",
        original_requested_ticker=symbol,
        returned_ticker=symbol,
        source_ticker=symbol,
        requested_as_of=CUTOFF,
        ticker_effective_from=date(2020, 1, 1),
        ticker_effective_through=None,
        listing_from=date(2020, 1, 1),
        delisting_through=None,
        exchange="NYSE",
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        known_at=known_at,
        mapping_evidence_id=_hash(f"mapping-{symbol}-{asset_id}"),
        mapping_status="CONFIRMED_CONTINUITY",
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    payload.update(overrides)
    return IdentityEvidence(**payload)


def _candidate(symbol: str, *, liquidity: float = 1_000_000, classification: str = AFFIRMATIVE_COMMON_STOCK, **overrides) -> UniverseCandidate:
    observations = tuple(
        LiquidityObservation(
            session=CUTOFF - timedelta(days=59 - index),
            close=10.0,
            volume=liquidity / 10.0,
            available_at=DECISION - timedelta(hours=1),
            source_hash=_hash(f"{symbol}-{index}"),
        )
        for index in range(60)
    )
    payload = dict(
        identity=_identity(symbol),
        ticker=symbol,
        security_classification=classification,
        exchange="NYSE",
        source_memberships=("ALPHA_VANTAGE_DATED_ACTIVE",),
        source_receipt_times=(DECISION - timedelta(days=1),),
        observations=observations,
        evidence_hashes=(_hash(f"evidence-{symbol}"),),
    )
    payload.update(overrides)
    return UniverseCandidate(**payload)


def _snapshot(candidates, *, profile=PRIMARY_PROFILE):
    return build_universe_snapshot(
        profile_id=profile,
        signal_session=SIGNAL,
        information_cutoff_session=CUTOFF,
        decision_at=DECISION,
        candidates=candidates,
    )


def test_t_minus_one_causality_ignores_signal_day_close() -> None:
    candidate = _candidate("AAA", liquidity=1000)
    leaked = LiquidityObservation(
        session=SIGNAL,
        close=1000,
        volume=1_000_000_000,
        available_at=DECISION - timedelta(minutes=1),
        source_hash=_hash("leak"),
    )
    snapshot = _snapshot([replace(candidate, observations=(*candidate.observations, leaked))])
    assert snapshot.selected[0].trailing_60_median_dollar_volume == pytest.approx(1000)
    assert snapshot.information_cutoff_session == CUTOFF


def test_top_500_and_isolated_top_1000_are_deterministic() -> None:
    candidates = [_candidate(f"S{index:04d}", liquidity=1000 + index) for index in range(1001)]
    primary = _snapshot(candidates)
    sensitivity = _snapshot(candidates, profile=SENSITIVITY_PROFILE)
    assert len(primary.selected) == 500
    assert len(sensitivity.selected) == 1000
    assert primary.target_size == 500
    assert sensitivity.target_size == 1000
    assert {row.stable_asset_id for row in primary.selected} < {row.stable_asset_id for row in sensitivity.selected}


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"classification": "UNKNOWN"}, "UNKNOWN_SECURITY_TYPE"),
        ({"is_etf_or_etp": True}, "ETF_OR_ETP_EXCLUDED"),
        ({"is_adr": True}, "ADR_EXCLUDED"),
        ({"is_preferred_warrant_right_or_unit": True}, "PREFERRED_WARRANT_RIGHT_UNIT_EXCLUDED"),
        ({"is_closed_end_or_mutual_fund": True}, "FUND_EXCLUDED"),
        ({"is_structured_product": True}, "STRUCTURED_PRODUCT_EXCLUDED"),
        ({"is_leveraged_or_inverse": True}, "LEVERAGED_OR_INVERSE_EXCLUDED"),
        ({"is_test_issue": True}, "TEST_ISSUE_EXCLUDED"),
        ({"is_otc": True, "exchange": "OTC"}, "OTC_EXCLUDED"),
    ],
)
def test_affirmative_security_type_and_instrument_exclusions(changes: dict[str, object], reason: str) -> None:
    candidate = _candidate("AAA", **changes)
    row = _snapshot([candidate]).rows[0]
    assert not row.included
    assert reason in row.exclusion_reasons


def test_lookback_previous_close_and_source_knowledge_checks() -> None:
    short = _candidate("SHORT")
    short = replace(short, observations=short.observations[:-1])
    cheap = _candidate("CHEAP")
    cheap_observations = list(cheap.observations)
    cheap_observations[-1] = replace(cheap_observations[-1], close=4.99)
    late = _candidate("LATE", source_receipt_times=(DECISION + timedelta(seconds=1),))
    rows = {row.ticker: row for row in _snapshot([short, replace(cheap, observations=tuple(cheap_observations)), late]).rows}
    assert "INSUFFICIENT_60_SESSION_LOOKBACK" in rows["SHORT"].exclusion_reasons
    assert "PREVIOUS_CLOSE_BELOW_5" in rows["CHEAP"].exclusion_reasons
    assert "SOURCE_NOT_KNOWN_BY_DECISION" in rows["LATE"].exclusion_reasons


def test_current_list_survivorship_shortcut_rejected_and_later_delisted_retained() -> None:
    current = _candidate("CURR", source_memberships=("CURRENT_ACTIVE_LIST_ONLY",))
    later_delisted_identity = _identity("GONE", delisting_through=date(2026, 9, 1))
    later_delisted = _candidate(
        "GONE",
        identity=later_delisted_identity,
        source_memberships=("ALPHA_VANTAGE_DATED_DELISTED",),
    )
    rows = {row.ticker: row for row in _snapshot([current, later_delisted]).rows}
    assert "SURVIVORSHIP_SHORTCUT_REJECTED" in rows["CURR"].exclusion_reasons
    assert rows["GONE"].included


def test_complete_candidate_snapshot_retains_selected_and_rejected_rows() -> None:
    selected = _candidate("GOOD")
    rejected = _candidate("BAD", classification="UNKNOWN")
    snapshot = _snapshot([selected, rejected])
    assert len(snapshot.rows) == 2
    assert len(snapshot.selected) == 1
    assert {row.ticker for row in snapshot.rows} == {"GOOD", "BAD"}
    assert snapshot.snapshot_id == _snapshot([selected, rejected]).snapshot_id


def test_identity_ledger_preserves_ticker_change_and_separates_nonoverlapping_reuse() -> None:
    ledger = ProfileIdentityLedger()
    fb = _identity(
        "FB", asset_id="meta-id", ticker_effective_through=date(2022, 6, 8),
        effective_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    meta = _identity(
        "META", asset_id="meta-id", ticker_effective_from=date(2022, 6, 9),
        effective_at=datetime(2022, 6, 9, tzinfo=timezone.utc),
        known_at=datetime(2022, 6, 9, 20, tzinfo=timezone.utc),
    )
    ledger.append(fb)
    ledger.append(meta)
    visible = ledger.visible_as_of(
        effective_as_of=datetime(2023, 1, 1, tzinfo=timezone.utc),
        known_as_of=datetime(2023, 1, 1, tzinfo=timezone.utc),
    )
    assert len(visible) == 1 and visible[0].stable_asset_id == "meta-id"
    old = _identity("XYZ", asset_id="old", ticker_effective_through=date(2020, 1, 1))
    new = _identity(
        "XYZ", asset_id="new", ticker_effective_from=date(2021, 1, 1),
        provider_asset_id="provider-new", mapping_status="CONFIRMED_DISTINCT",
    )
    reuse = ProfileIdentityLedger([old, new])
    assert len(reuse._rows) == 2


def test_identity_contradictions_fail_closed() -> None:
    first = _identity("XYZ", asset_id="one")
    overlapping = _identity("XYZ", asset_id="two", provider_asset_id="provider-two")
    with pytest.raises(IntegrityError, match="overlapping ticker reuse"):
        ProfileIdentityLedger([first, overlapping])
    reused_provider = _identity("ABC", asset_id="two", provider_asset_id=first.provider_asset_id)
    with pytest.raises(IntegrityError, match="provider asset ID"):
        ProfileIdentityLedger([first, reused_provider])


def test_universe_rejects_same_day_cutoff_and_duplicate_stable_identity() -> None:
    with pytest.raises(ContractError, match="T-1"):
        build_universe_snapshot(
            profile_id=PRIMARY_PROFILE, signal_session=SIGNAL,
            information_cutoff_session=SIGNAL, decision_at=DECISION,
            candidates=[_candidate("AAA")],
        )
    candidate = _candidate("AAA")
    with pytest.raises(IntegrityError, match="duplicate stable"):
        _snapshot([candidate, candidate])
