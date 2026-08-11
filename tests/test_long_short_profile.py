from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from us_stocks_swing_model_v2.alpaca_free_bounded import EvidenceClass, PositionSide
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.long_short import (
    COST_LABEL,
    EXECUTED_LAYER,
    FIXED_LAYER,
    CorporateActionTerms,
    ProfileOutcomeStatus,
    ProspectiveBorrowSnapshot,
    assess_borrow_degradation,
    executed_return_from_fills,
    prospective_short_eligibility,
    portfolio_return_on_starting_equity,
    resolve_fixed_horizon_outcome,
    resolve_five_session_outcome,
    unresolved_stress,
)


ENTRY = date(2026, 8, 3)
EXIT = date(2026, 8, 7)
ORDER = datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)


def _outcome(side: PositionSide, **kwargs):
    return resolve_fixed_horizon_outcome(
        side=side,
        evidence_class=kwargs.pop("evidence_class", EvidenceClass.HISTORICAL_RECONSTRUCTED),
        entry_session=ENTRY,
        exit_session=EXIT,
        entry_price=kwargs.pop("entry_price", 100.0),
        exit_price=kwargs.pop("exit_price", 110.0),
        **kwargs,
    )


def _borrow(**overrides) -> ProspectiveBorrowSnapshot:
    payload = dict(
        stable_asset_id="asset-a",
        status="active",
        tradable=True,
        marginable=True,
        shortable=True,
        borrow_status="easy_to_borrow",
        easy_to_borrow=True,
        received_at=ORDER - timedelta(minutes=5),
        expires_at=ORDER + timedelta(minutes=5),
        raw_receipt_id="r" * 64,
    )
    payload.update(overrides)
    return ProspectiveBorrowSnapshot(**payload)


def test_ordinary_long_short_signs_zero_cost_labels_and_historical_short_limitations() -> None:
    long = _outcome(PositionSide.LONG)
    short = _outcome(PositionSide.SHORT)
    assert long.gross_return_ex_borrow_costs == pytest.approx(0.10)
    assert short.gross_return_ex_borrow_costs == pytest.approx(-0.10)
    assert short.stock_borrow_fee == short.locate_fee == 0.0
    assert short.cost_label == COST_LABEL
    assert set(short.labels) == {
        "SHORTABILITY_UNVERIFIED_HISTORICAL",
        "SHORT_EXECUTION_APPROXIMATE",
        "BORROW_COST_ASSUMPTION_ZERO",
    }
    assert portfolio_return_on_starting_equity(
        trade_return_on_initial_notional=0.10,
        initial_trade_notional=1000,
        starting_portfolio_equity=10_000,
    ) == pytest.approx(0.01)


def test_five_session_wrapper_uses_market_sessions_not_calendar_days() -> None:
    sessions = (
        date(2026, 7, 31),
        date(2026, 8, 3),
        date(2026, 8, 4),
        date(2026, 8, 5),
        date(2026, 8, 6),
        date(2026, 8, 7),
    )
    outcome = resolve_five_session_outcome(
        decision_session=date(2026, 7, 31),
        pinned_market_sessions=sessions,
        side=PositionSide.LONG,
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
        entry_session=date(2026, 8, 3),
        exit_session=date(2026, 8, 7),
        entry_price=100,
        exit_price=110,
    )
    assert outcome.gross_return_ex_borrow_costs == pytest.approx(0.10)
    with pytest.raises(ContractError, match="D1 open through D5"):
        resolve_five_session_outcome(
            decision_session=date(2026, 7, 31),
            pinned_market_sessions=sessions,
            side=PositionSide.LONG,
            evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
            entry_session=date(2026, 8, 4),
            exit_session=date(2026, 8, 7),
            entry_price=100,
            exit_price=110,
        )


def test_short_loss_below_negative_100_is_preserved() -> None:
    short = _outcome(PositionSide.SHORT, entry_price=10, exit_price=30)
    assert short.gross_return_ex_borrow_costs == pytest.approx(-2.0)


def test_dividend_entitlement_and_liability_are_side_specific() -> None:
    long = _outcome(PositionSide.LONG, exit_price=100, distributions_per_initial_share=2)
    short = _outcome(PositionSide.SHORT, exit_price=100, distributions_per_initial_share=2)
    assert long.gross_return_ex_borrow_costs == pytest.approx(0.02)
    assert short.gross_return_ex_borrow_costs == pytest.approx(-0.02)


@pytest.mark.parametrize("side", [PositionSide.LONG, PositionSide.SHORT])
def test_split_is_economically_neutral_for_both_sides(side: PositionSide) -> None:
    action = CorporateActionTerms(
        action_type="forward_split", verified=True, effective_session=ENTRY,
        split_ratio=4.0,
    )
    outcome = _outcome(side, exit_price=25, terminal_action=action)
    assert outcome.gross_return_ex_borrow_costs == pytest.approx(0.0)


def test_ticker_change_follows_identity_without_close_and_reopen() -> None:
    action = CorporateActionTerms(
        action_type="ticker_change", verified=True, effective_session=ENTRY,
    )
    outcome = _outcome(PositionSide.LONG, exit_price=110, terminal_action=action)
    assert outcome.status is ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME
    assert outcome.gross_return_ex_borrow_costs == pytest.approx(0.10)


def test_verified_stock_dividend_and_spinoff_obligations_are_side_specific() -> None:
    stock_dividend = CorporateActionTerms(
        action_type="stock_dividend", verified=True, effective_session=ENTRY,
        split_ratio=1.1,
    )
    long = _outcome(PositionSide.LONG, exit_price=100, terminal_action=stock_dividend)
    short = _outcome(PositionSide.SHORT, exit_price=100, terminal_action=stock_dividend)
    assert long.gross_return_ex_borrow_costs == pytest.approx(0.10)
    assert short.gross_return_ex_borrow_costs == pytest.approx(-0.10)
    spinoff = CorporateActionTerms(
        action_type="spin_off", verified=True, effective_session=ENTRY,
        additional_value=5,
    )
    assert _outcome(PositionSide.LONG, exit_price=100, terminal_action=spinoff).gross_return_ex_borrow_costs == pytest.approx(0.05)
    assert _outcome(PositionSide.SHORT, exit_price=100, terminal_action=spinoff).gross_return_ex_borrow_costs == pytest.approx(-0.05)


@pytest.mark.parametrize(
    ("action", "expected_status", "terminal"),
    [
        (
            CorporateActionTerms(action_type="cash_merger", verified=True, effective_session=EXIT, cash_consideration=125),
            ProfileOutcomeStatus.RESOLVED_CASH_MERGER, 125,
        ),
        (
            CorporateActionTerms(
                action_type="stock_merger", verified=True, effective_session=EXIT,
                successor_ratio=0.5, successor_price=200, successor_asset_id="successor",
            ),
            ProfileOutcomeStatus.RESOLVED_STOCK_MERGER, 100,
        ),
        (
            CorporateActionTerms(
                action_type="stock_and_cash_merger", verified=True, effective_session=EXIT,
                cash_consideration=25, successor_ratio=0.5, successor_price=200,
                successor_asset_id="successor",
            ),
            ProfileOutcomeStatus.RESOLVED_STOCK_AND_CASH_MERGER, 125,
        ),
        (
            CorporateActionTerms(action_type="redemption", verified=True, effective_session=EXIT, cash_consideration=105),
            ProfileOutcomeStatus.RESOLVED_REDEMPTION, 105,
        ),
        (
            CorporateActionTerms(
                action_type="worthless_removal", verified=True, effective_session=EXIT,
                explicit_zero_consideration=True,
            ),
            ProfileOutcomeStatus.RESOLVED_WORTHLESS_REMOVAL, 0,
        ),
    ],
)
def test_verified_terminal_events_resolve_with_explicit_terms(action, expected_status, terminal) -> None:
    long = _outcome(PositionSide.LONG, exit_price=None, terminal_action=action)
    short = _outcome(PositionSide.SHORT, exit_price=None, terminal_action=action)
    assert long.status is expected_status and short.status is expected_status
    assert long.terminal_value == short.terminal_value == terminal
    assert long.gross_return_ex_borrow_costs == pytest.approx((terminal - 100) / 100)
    assert short.gross_return_ex_borrow_costs == pytest.approx((100 - terminal) / 100)


def test_zero_assignment_requires_explicit_evidence_and_last_price_is_not_substituted() -> None:
    unverified_zero = CorporateActionTerms(
        action_type="worthless_removal", verified=True, effective_session=EXIT,
        explicit_zero_consideration=False,
    )
    outcome = _outcome(PositionSide.LONG, exit_price=50, terminal_action=unverified_zero)
    assert outcome.status is ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
    assert outcome.terminal_value is None
    assert outcome.gross_return_ex_borrow_costs is None


@pytest.mark.parametrize(
    "action_type",
    ["delisting", "otc_continuation", "bankruptcy", "halt", "liquidation", "spin_off", "rights_distribution"],
)
def test_unresolved_terminal_events_remain_in_denominator(action_type: str) -> None:
    action = CorporateActionTerms(action_type=action_type, verified=False, effective_session=EXIT)
    outcome = _outcome(PositionSide.SHORT, exit_price=None, terminal_action=action)
    assert outcome.status is ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
    assert outcome.reason


def test_nonterminal_missing_data_is_distinct_from_terminal_event() -> None:
    outcome = _outcome(PositionSide.LONG, exit_price=None, nonterminal_missing=True)
    assert outcome.status is ProfileOutcomeStatus.DATA_MISSING_NONTERMINAL
    assert outcome.reason == "NONTERMINAL_PRICE_DATA_MISSING"


def test_unresolved_stress_contract_emits_long_and_complete_short_ladder() -> None:
    long = _outcome(PositionSide.LONG, exit_price=None)
    short = _outcome(PositionSide.SHORT, exit_price=None)
    assert unresolved_stress(long)[0]["assumed_return"] == -1.0
    ladder = unresolved_stress(short)
    assert [row["assumed_return"] for row in ladder] == [-1.0, -2.0, -4.0]
    assert all("NOT_ESTIMATE_OR_LOWER_BOUND" in row["classification"] for row in ladder)


def test_prospective_easy_to_borrow_whole_share_acceptance() -> None:
    decision = prospective_short_eligibility(
        _borrow(), proposed_order_at=ORDER, risk_budget=250, reference_price=100,
    )
    assert decision.eligible
    assert decision.whole_share_quantity == 2
    assert decision.unused_capital == pytest.approx(50)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"borrow_status": "hard_to_borrow", "easy_to_borrow": False}, "SHORT_INELIGIBLE_HTB"),
        ({"borrow_status": None}, "SHORT_INELIGIBLE_UNKNOWN_BORROW"),
        ({"borrow_status": "unknown", "easy_to_borrow": None}, "SHORT_INELIGIBLE_UNKNOWN_BORROW"),
        ({"shortable": False}, "SHORT_INELIGIBLE_NOT_SHORTABLE"),
        ({"marginable": False}, "SHORT_INELIGIBLE_NOT_MARGINABLE"),
        ({"tradable": False}, "SHORT_INELIGIBLE_NOT_TRADABLE"),
        ({"status": "inactive"}, "SHORT_INELIGIBLE_INACTIVE"),
        ({"borrow_status": "easy_to_borrow", "easy_to_borrow": False}, "SHORT_INELIGIBLE_CONTRADICTORY_EVIDENCE"),
    ],
)
def test_prospective_short_gate_rejects_ineligible_states(changes: dict[str, object], reason: str) -> None:
    decision = prospective_short_eligibility(
        _borrow(**changes), proposed_order_at=ORDER, risk_budget=250, reference_price=100,
    )
    assert not decision.eligible
    assert reason in decision.reason_codes


def test_stale_post_order_and_fractional_only_short_rejections() -> None:
    stale = prospective_short_eligibility(
        _borrow(expires_at=ORDER - timedelta(seconds=1)),
        proposed_order_at=ORDER, risk_budget=250, reference_price=100,
    )
    post = prospective_short_eligibility(
        _borrow(received_at=ORDER, expires_at=ORDER + timedelta(minutes=1)),
        proposed_order_at=ORDER, risk_budget=250, reference_price=100,
    )
    fractional = prospective_short_eligibility(
        _borrow(), proposed_order_at=ORDER, risk_budget=99, reference_price=100,
    )
    assert "SHORT_INELIGIBLE_STALE_SNAPSHOT" in stale.reason_codes
    assert "SHORT_INELIGIBLE_POST_ORDER_SNAPSHOT" in post.reason_codes
    assert "SHORT_INELIGIBLE_FRACTIONAL_ONLY" in fractional.reason_codes
    assert fractional.whole_share_quantity == 0


def test_legacy_easy_to_borrow_never_overrides_missing_canonical_field() -> None:
    decision = prospective_short_eligibility(
        _borrow(borrow_status=None, easy_to_borrow=True),
        proposed_order_at=ORDER, risk_budget=100, reference_price=50,
    )
    assert not decision.eligible
    assert "SHORT_INELIGIBLE_UNKNOWN_BORROW" in decision.reason_codes
    assert decision.legacy_easy_to_borrow is True


def test_borrow_degradation_generates_instruction_without_fabricated_fill() -> None:
    instruction = assess_borrow_degradation(
        _borrow(borrow_status="hard_to_borrow", easy_to_borrow=False),
        open_quantity=3,
        observed_at=ORDER + timedelta(days=1),
        next_executable_session=ENTRY + timedelta(days=2),
    )
    assert instruction is not None
    assert instruction.reason == "BORROW_STATUS_DEGRADED_DURING_HOLD"
    assert instruction.submitted_order_id is None
    assert instruction.fill_price is None
    assert assess_borrow_degradation(
        _borrow(), open_quantity=3, observed_at=ORDER,
        next_executable_session=ENTRY + timedelta(days=1),
    ) is None


def test_executed_layer_requires_fill_evidence_and_stays_separate() -> None:
    with pytest.raises(ContractError, match="fill evidence"):
        executed_return_from_fills(
            side=PositionSide.SHORT, evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
            entry_session=ENTRY, exit_session=EXIT, entry_fill=None, exit_fill=90,
            distributions=0, configured_execution_costs=0,
        )
    outcome = executed_return_from_fills(
        side=PositionSide.SHORT, evidence_class=EvidenceClass.PROSPECTIVE_AS_OBSERVED,
        entry_session=ENTRY, exit_session=EXIT, entry_fill=100, exit_fill=90,
        distributions=1, configured_execution_costs=0.5,
    )
    assert outcome.layer == EXECUTED_LAYER
    assert outcome.gross_return_ex_borrow_costs == pytest.approx(0.085)
    assert outcome.configured_execution_costs == pytest.approx(0.5)
    assert FIXED_LAYER != outcome.layer
