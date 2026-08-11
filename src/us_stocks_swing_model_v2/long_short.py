from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Iterable

from .alpaca_free_bounded import EvidenceClass, PositionSide
from .common import require_aware_utc
from .errors import ContractError


OUTCOME_NAME = "ALPACA_SIP_5_SESSION_LONG_SHORT_GROSS_RETURN_EX_BORROW_COSTS"
COST_LABEL = "GROSS OF STOCK-BORROW AND LOCATE COSTS"
FIXED_LAYER = "FIXED_HORIZON_SIGNAL_RETURN"
EXECUTED_LAYER = "BORROW_AWARE_EXECUTED_RETURN_EX_BORROW_COSTS"
HISTORICAL_SHORT_LABELS = (
    "SHORTABILITY_UNVERIFIED_HISTORICAL",
    "SHORT_EXECUTION_APPROXIMATE",
    "BORROW_COST_ASSUMPTION_ZERO",
)


class ProfileOutcomeStatus(str, Enum):
    ORDINARY_PRICE_OUTCOME = "ORDINARY_PRICE_OUTCOME"
    RESOLVED_CASH_MERGER = "RESOLVED_CASH_MERGER"
    RESOLVED_STOCK_MERGER = "RESOLVED_STOCK_MERGER"
    RESOLVED_STOCK_AND_CASH_MERGER = "RESOLVED_STOCK_AND_CASH_MERGER"
    RESOLVED_REDEMPTION = "RESOLVED_REDEMPTION"
    RESOLVED_WORTHLESS_REMOVAL = "RESOLVED_WORTHLESS_REMOVAL"
    TERMINAL_EVENT_UNRESOLVED = "TERMINAL_EVENT_UNRESOLVED"
    DATA_MISSING_NONTERMINAL = "DATA_MISSING_NONTERMINAL"


@dataclass(frozen=True)
class ProspectiveBorrowSnapshot:
    stable_asset_id: str
    status: str
    tradable: bool
    marginable: bool
    shortable: bool
    borrow_status: str | None
    easy_to_borrow: bool | None
    received_at: datetime
    expires_at: datetime
    raw_receipt_id: str

    def validate(self) -> None:
        received = require_aware_utc(self.received_at, "borrow.received_at")
        expires = require_aware_utc(self.expires_at, "borrow.expires_at")
        if expires <= received:
            raise ContractError("borrow snapshot expiry must follow receipt")
        if any(type(getattr(self, field)) is not bool for field in ("tradable", "marginable", "shortable")):
            raise ContractError("borrow eligibility flags must be exact booleans")
        if self.easy_to_borrow is not None and type(self.easy_to_borrow) is not bool:
            raise ContractError("legacy easy_to_borrow must be boolean or null")
        if self.borrow_status is not None and not isinstance(self.borrow_status, str):
            raise ContractError("borrow_status must be text or null")


@dataclass(frozen=True)
class ShortEligibilityDecision:
    eligible: bool
    reason_codes: tuple[str, ...]
    whole_share_quantity: int
    unused_capital: float
    canonical_borrow_status: str | None
    legacy_easy_to_borrow: bool | None


def prospective_short_eligibility(
    snapshot: ProspectiveBorrowSnapshot,
    *,
    proposed_order_at: datetime,
    risk_budget: float,
    reference_price: float,
) -> ShortEligibilityDecision:
    snapshot.validate()
    order_at = require_aware_utc(proposed_order_at, "proposed_order_at")
    if (
        isinstance(risk_budget, bool)
        or isinstance(reference_price, bool)
        or not isinstance(risk_budget, (int, float))
        or not isinstance(reference_price, (int, float))
        or not math.isfinite(float(risk_budget))
        or not math.isfinite(float(reference_price))
        or risk_budget < 0
        or reference_price <= 0
    ):
        raise ContractError("short risk budget and price must be finite and nonnegative")
    reasons: list[str] = []
    if snapshot.received_at >= order_at:
        reasons.append("SHORT_INELIGIBLE_POST_ORDER_SNAPSHOT")
    if order_at > snapshot.expires_at:
        reasons.append("SHORT_INELIGIBLE_STALE_SNAPSHOT")
    if snapshot.status != "active":
        reasons.append("SHORT_INELIGIBLE_INACTIVE")
    if not snapshot.tradable:
        reasons.append("SHORT_INELIGIBLE_NOT_TRADABLE")
    if not snapshot.marginable:
        reasons.append("SHORT_INELIGIBLE_NOT_MARGINABLE")
    if not snapshot.shortable:
        reasons.append("SHORT_INELIGIBLE_NOT_SHORTABLE")
    borrow = snapshot.borrow_status
    if borrow is None or borrow in {"", "unknown"}:
        reasons.append("SHORT_INELIGIBLE_UNKNOWN_BORROW")
    elif borrow == "hard_to_borrow":
        reasons.append("SHORT_INELIGIBLE_HTB")
    elif borrow != "easy_to_borrow":
        reasons.append("SHORT_INELIGIBLE_UNKNOWN_BORROW")
    if snapshot.easy_to_borrow is not None and borrow is not None:
        canonical_easy = borrow == "easy_to_borrow"
        if canonical_easy != snapshot.easy_to_borrow:
            reasons.append("SHORT_INELIGIBLE_CONTRADICTORY_EVIDENCE")
    quantity = int(float(risk_budget) // float(reference_price))
    unused = float(risk_budget) - quantity * float(reference_price)
    if quantity < 1:
        reasons.append("SHORT_INELIGIBLE_FRACTIONAL_ONLY")
    return ShortEligibilityDecision(
        eligible=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
        whole_share_quantity=quantity if not reasons else 0,
        unused_capital=unused if not reasons else float(risk_budget),
        canonical_borrow_status=borrow,
        legacy_easy_to_borrow=snapshot.easy_to_borrow,
    )


@dataclass(frozen=True)
class CorporateActionTerms:
    action_type: str
    verified: bool
    effective_session: date
    announcement_at: datetime | None = None
    provider_publication_at: datetime | None = None
    local_first_observed_at: datetime | None = None
    knowledge_time_status: str = "UNKNOWN_AVAILABILITY"
    split_ratio: float | None = None
    cash_consideration: float | None = None
    successor_ratio: float | None = None
    successor_price: float | None = None
    successor_asset_id: str | None = None
    additional_value: float | None = None
    explicit_zero_consideration: bool = False

    def validate(self) -> None:
        if self.knowledge_time_status not in {
            "PROVEN_AVAILABLE",
            "UNKNOWN_AVAILABILITY",
            "CONTRADICTED_AVAILABILITY",
        }:
            raise ContractError("corporate-action knowledge-time status is invalid")
        for name in ("announcement_at", "provider_publication_at", "local_first_observed_at"):
            value = getattr(self, name)
            if value is not None:
                require_aware_utc(value, f"corporate_action.{name}")
        for name in (
            "split_ratio",
            "cash_consideration",
            "successor_ratio",
            "successor_price",
            "additional_value",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ContractError(f"corporate-action {name} is invalid")


@dataclass(frozen=True)
class PositionOutcome:
    outcome_name: str
    layer: str
    side: PositionSide
    evidence_class: EvidenceClass
    entry_session: date
    exit_session: date
    status: ProfileOutcomeStatus
    entry_price: float
    terminal_value: float | None
    distributions: float
    gross_return_ex_borrow_costs: float | None
    stock_borrow_fee: float
    locate_fee: float
    configured_execution_costs: float
    cost_label: str
    labels: tuple[str, ...]
    reason: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome_name": self.outcome_name,
            "layer": self.layer,
            "side": self.side.value,
            "evidence_class": self.evidence_class.value,
            "entry_session": self.entry_session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "status": self.status.value,
            "entry_price": self.entry_price,
            "terminal_value": self.terminal_value,
            "distributions": self.distributions,
            "gross_return_ex_borrow_costs": self.gross_return_ex_borrow_costs,
            "stock_borrow_fee": self.stock_borrow_fee,
            "locate_fee": self.locate_fee,
            "configured_execution_costs": self.configured_execution_costs,
            "cost_label": self.cost_label,
            "labels": list(self.labels),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PositionOutcome":
        if set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("position outcome fields differ from the exact contract")
        outcome = cls(
            outcome_name=str(payload["outcome_name"]),
            layer=str(payload["layer"]),
            side=PositionSide(str(payload["side"])),
            evidence_class=EvidenceClass(str(payload["evidence_class"])),
            entry_session=date.fromisoformat(str(payload["entry_session"])),
            exit_session=date.fromisoformat(str(payload["exit_session"])),
            status=ProfileOutcomeStatus(str(payload["status"])),
            entry_price=float(payload["entry_price"]),
            terminal_value=(float(payload["terminal_value"]) if payload["terminal_value"] is not None else None),
            distributions=float(payload["distributions"]),
            gross_return_ex_borrow_costs=(
                float(payload["gross_return_ex_borrow_costs"])
                if payload["gross_return_ex_borrow_costs"] is not None else None
            ),
            stock_borrow_fee=float(payload["stock_borrow_fee"]),
            locate_fee=float(payload["locate_fee"]),
            configured_execution_costs=float(payload["configured_execution_costs"]),
            cost_label=str(payload["cost_label"]),
            labels=tuple(str(value) for value in payload["labels"]),
            reason=str(payload["reason"]) if payload["reason"] is not None else None,
        )
        outcome.validate()
        return outcome

    def validate(self) -> None:
        if self.outcome_name != OUTCOME_NAME or self.layer not in {FIXED_LAYER, EXECUTED_LAYER}:
            raise ContractError("long/short outcome identity is invalid")
        if type(self.side) is not PositionSide or type(self.status) is not ProfileOutcomeStatus:
            raise ContractError("long/short outcome enums are invalid")
        if self.exit_session < self.entry_session:
            raise ContractError("outcome exit session cannot precede entry")
        if self.stock_borrow_fee != 0.0 or self.locate_fee != 0.0 or self.cost_label != COST_LABEL:
            raise ContractError("borrow/locate cost contract drifted")
        if not math.isfinite(self.configured_execution_costs) or self.configured_execution_costs < 0:
            raise ContractError("configured execution costs must be finite and nonnegative")
        resolved = self.status not in {
            ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED,
            ProfileOutcomeStatus.DATA_MISSING_NONTERMINAL,
        }
        if resolved:
            if self.terminal_value is None or self.gross_return_ex_borrow_costs is None:
                raise ContractError("resolved outcome requires terminal value and return")
            if not math.isfinite(self.gross_return_ex_borrow_costs):
                raise ContractError("resolved outcome return must be finite")
        elif self.terminal_value is not None or self.gross_return_ex_borrow_costs is not None or not self.reason:
            raise ContractError("unresolved outcome must retain a reason without fabricated value")


def _gross_return(side: PositionSide, entry: float, terminal: float, distributions: float) -> float:
    return (
        (terminal + distributions - entry) / entry
        if side is PositionSide.LONG
        else (entry - terminal - distributions) / entry
    )


def resolve_fixed_horizon_outcome(
    *,
    side: PositionSide,
    evidence_class: EvidenceClass,
    entry_session: date,
    exit_session: date,
    entry_price: float,
    exit_price: float | None,
    distributions_per_initial_share: float = 0.0,
    split_ratios: Iterable[float] = (),
    terminal_action: CorporateActionTerms | None = None,
    nonterminal_missing: bool = False,
) -> PositionOutcome:
    if type(side) is not PositionSide or type(evidence_class) is not EvidenceClass:
        raise ContractError("outcome side/evidence class must use exact enums")
    if (
        isinstance(entry_price, bool)
        or not isinstance(entry_price, (int, float))
        or not math.isfinite(float(entry_price))
        or entry_price <= 0
        or isinstance(distributions_per_initial_share, bool)
        or not isinstance(distributions_per_initial_share, (int, float))
        or not math.isfinite(float(distributions_per_initial_share))
    ):
        raise ContractError("outcome entry/distribution values are invalid")
    labels = list(HISTORICAL_SHORT_LABELS) if (
        side is PositionSide.SHORT and evidence_class is EvidenceClass.HISTORICAL_RECONSTRUCTED
    ) else []
    quantity = 1.0
    for ratio in split_ratios:
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
            or ratio <= 0
        ):
            raise ContractError("split ratio must be finite and positive")
        quantity *= float(ratio)
    status: ProfileOutcomeStatus
    terminal: float | None
    reason: str | None = None
    if nonterminal_missing:
        status = ProfileOutcomeStatus.DATA_MISSING_NONTERMINAL
        terminal = None
        reason = "NONTERMINAL_PRICE_DATA_MISSING"
    elif terminal_action is None:
        if exit_price is None:
            status = ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
            terminal = None
            reason = "MISSING_TERMINAL_VALUE_NO_VERIFIED_EVENT"
        else:
            status = ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME
            terminal = quantity * float(exit_price)
    else:
        terminal_action.validate()
        action = terminal_action.action_type
        if not terminal_action.verified:
            status = ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
            terminal = None
            reason = "TERMINAL_EVENT_TERMS_UNVERIFIED"
        elif action in {"forward_split", "reverse_split", "unit_split", "name_change", "ticker_change"}:
            if exit_price is None:
                status = ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
                terminal = None
                reason = "POST_ACTION_EXIT_VALUE_UNAVAILABLE"
            else:
                ratio = terminal_action.split_ratio or 1.0
                terminal = quantity * ratio * float(exit_price)
                status = ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME
        elif action == "cash_merger" and terminal_action.cash_consideration is not None:
            terminal = quantity * terminal_action.cash_consideration
            status = ProfileOutcomeStatus.RESOLVED_CASH_MERGER
        elif (
            action == "stock_merger"
            and terminal_action.successor_ratio is not None
            and terminal_action.successor_price is not None
            and terminal_action.successor_asset_id
        ):
            terminal = quantity * terminal_action.successor_ratio * terminal_action.successor_price
            status = ProfileOutcomeStatus.RESOLVED_STOCK_MERGER
        elif (
            action == "stock_and_cash_merger"
            and terminal_action.cash_consideration is not None
            and terminal_action.successor_ratio is not None
            and terminal_action.successor_price is not None
            and terminal_action.successor_asset_id
        ):
            terminal = quantity * (
                terminal_action.cash_consideration
                + terminal_action.successor_ratio * terminal_action.successor_price
            )
            status = ProfileOutcomeStatus.RESOLVED_STOCK_AND_CASH_MERGER
        elif action == "redemption" and terminal_action.cash_consideration is not None:
            terminal = quantity * terminal_action.cash_consideration
            status = ProfileOutcomeStatus.RESOLVED_REDEMPTION
        elif action == "worthless_removal" and terminal_action.explicit_zero_consideration:
            terminal = 0.0
            status = ProfileOutcomeStatus.RESOLVED_WORTHLESS_REMOVAL
        elif action == "stock_dividend" and terminal_action.split_ratio is not None and exit_price is not None:
            terminal = quantity * terminal_action.split_ratio * float(exit_price)
            status = ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME
        elif (
            action in {"spin_off", "rights_distribution"}
            and terminal_action.additional_value is not None
            and exit_price is not None
        ):
            terminal = quantity * float(exit_price) + terminal_action.additional_value
            status = ProfileOutcomeStatus.ORDINARY_PRICE_OUTCOME
        else:
            terminal = None
            status = ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED
            reason = "TERMINAL_EVENT_ECONOMIC_TERMS_INCOMPLETE"
    gross = (
        _gross_return(side, float(entry_price), terminal, float(distributions_per_initial_share))
        if terminal is not None
        else None
    )
    outcome = PositionOutcome(
        outcome_name=OUTCOME_NAME,
        layer=FIXED_LAYER,
        side=side,
        evidence_class=evidence_class,
        entry_session=entry_session,
        exit_session=exit_session,
        status=status,
        entry_price=float(entry_price),
        terminal_value=terminal,
        distributions=float(distributions_per_initial_share),
        gross_return_ex_borrow_costs=gross,
        stock_borrow_fee=0.0,
        locate_fee=0.0,
        configured_execution_costs=0.0,
        cost_label=COST_LABEL,
        labels=tuple(labels),
        reason=reason,
    )
    outcome.validate()
    return outcome


def resolve_five_session_outcome(
    *,
    decision_session: date,
    pinned_market_sessions: Iterable[date],
    **outcome_fields: object,
) -> PositionOutcome:
    sessions = tuple(pinned_market_sessions)
    if len(sessions) != len(set(sessions)) or sessions != tuple(sorted(sessions)):
        raise ContractError("pinned market sessions must be unique and ascending")
    try:
        decision_index = sessions.index(decision_session)
    except ValueError as exc:
        raise ContractError("decision session is absent from the pinned market calendar") from exc
    if decision_index + 5 >= len(sessions):
        raise ContractError("pinned market calendar cannot resolve D1 through D5")
    expected_entry = sessions[decision_index + 1]
    expected_exit = sessions[decision_index + 5]
    if (
        outcome_fields.get("entry_session") != expected_entry
        or outcome_fields.get("exit_session") != expected_exit
    ):
        raise ContractError("outcome does not use D1 open through D5 close market sessions")
    return resolve_fixed_horizon_outcome(**outcome_fields)


def unresolved_stress(outcome: PositionOutcome) -> tuple[dict[str, object], ...]:
    outcome.validate()
    if outcome.status is not ProfileOutcomeStatus.TERMINAL_EVENT_UNRESOLVED:
        return ()
    if outcome.side is PositionSide.LONG:
        return (
            {
                "scenario": "LONG_UNRESOLVED_STRESS_NEGATIVE_100",
                "assumed_return": -1.0,
                "classification": "STRESS_NOT_ESTIMATE",
            },
        )
    return tuple(
        {
            "scenario": f"SHORT_UNRESOLVED_STRESS_{int(multiple)}X",
            "assumed_buy_in_multiple": multiple,
            "assumed_return": 1.0 - multiple,
            "classification": "SCENARIO_NOT_ESTIMATE_OR_LOWER_BOUND",
        }
        for multiple in (2.0, 3.0, 5.0)
    )


@dataclass(frozen=True)
class BuyToCoverInstruction:
    stable_asset_id: str
    generated_at: datetime
    earliest_session: date
    quantity: int
    reason: str
    submitted_order_id: str | None = None
    fill_price: float | None = None


def assess_borrow_degradation(
    snapshot: ProspectiveBorrowSnapshot,
    *,
    open_quantity: int,
    observed_at: datetime,
    next_executable_session: date,
) -> BuyToCoverInstruction | None:
    snapshot.validate()
    observed_at = require_aware_utc(observed_at, "observed_at")
    if type(open_quantity) is not int or open_quantity < 1:
        raise ContractError("open short quantity must be a positive whole share count")
    degraded = (
        snapshot.status != "active"
        or not snapshot.tradable
        or not snapshot.marginable
        or not snapshot.shortable
        or snapshot.borrow_status != "easy_to_borrow"
    )
    if not degraded:
        return None
    return BuyToCoverInstruction(
        stable_asset_id=snapshot.stable_asset_id,
        generated_at=observed_at,
        earliest_session=next_executable_session,
        quantity=open_quantity,
        reason="BORROW_STATUS_DEGRADED_DURING_HOLD",
        submitted_order_id=None,
        fill_price=None,
    )


def executed_return_from_fills(
    *,
    side: PositionSide,
    evidence_class: EvidenceClass,
    entry_session: date,
    exit_session: date,
    entry_fill: float | None,
    exit_fill: float | None,
    distributions: float,
    configured_execution_costs: float,
) -> PositionOutcome:
    if entry_fill is None or exit_fill is None:
        raise ContractError("executed return requires actual or explicitly simulated fill evidence")
    fixed = resolve_fixed_horizon_outcome(
        side=side,
        evidence_class=evidence_class,
        entry_session=entry_session,
        exit_session=exit_session,
        entry_price=entry_fill,
        exit_price=exit_fill,
        distributions_per_initial_share=distributions,
    )
    if not math.isfinite(configured_execution_costs) or configured_execution_costs < 0:
        raise ContractError("configured execution costs must be finite and nonnegative")
    adjusted = fixed.gross_return_ex_borrow_costs - configured_execution_costs / entry_fill
    result = PositionOutcome(
        **{
            **fixed.__dict__,
            "layer": EXECUTED_LAYER,
            "gross_return_ex_borrow_costs": adjusted,
            "configured_execution_costs": float(configured_execution_costs),
        }
    )
    result.validate()
    return result


def portfolio_return_on_starting_equity(
    *,
    trade_return_on_initial_notional: float,
    initial_trade_notional: float,
    starting_portfolio_equity: float,
) -> float:
    values = (
        trade_return_on_initial_notional,
        initial_trade_notional,
        starting_portfolio_equity,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        raise ContractError("portfolio return inputs must be finite numbers")
    if initial_trade_notional < 0 or starting_portfolio_equity <= 0:
        raise ContractError("portfolio return notionals are invalid")
    return (
        float(trade_return_on_initial_notional)
        * float(initial_trade_notional)
        / float(starting_portfolio_equity)
    )
