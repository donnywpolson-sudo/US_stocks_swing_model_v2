"""Causal price-only feature materialization for prospective evidence.

The caller supplies already verified, as-of bars and action coverage.  This
module never fills a gap, reads outcomes, fits a transform, or publishes data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from .common import require_aware_utc
from .errors import ContractError


FEATURE_NAMES = (
    "d0_raw_intraday_return",
    "trailing_5_session_raw_return",
    "trailing_5_session_raw_volatility",
)
UNRESOLVED_STATUS = "ABSTAIN_UNRESOLVED_CAUSAL_LOOKBACK"
READY_STATUS = "READY_CAUSAL_PRICE_ONLY_V1"


@dataclass(frozen=True)
class CausalPriceBar:
    asset_id: str
    session: date
    open: float | None
    close: float | None
    available_at: datetime

    def validate(self) -> None:
        if type(self.asset_id) is not str or not self.asset_id or type(self.session) is not date:
            raise ContractError("causal price-bar identity differs")
        require_aware_utc(self.available_at, "price_bar.available_at")
        for value in (self.open, self.close):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(float(value)) or value <= 0
            ):
                raise ContractError("causal price-bar values must be positive finite numbers or null")


@dataclass(frozen=True)
class ProspectiveFeatureResult:
    asset_id: str
    decision_session: date
    status: str
    feature_available_at: datetime | None
    values: dict[str, float] | None
    reason: str | None


def materialize_price_only_features(
    bars: Iterable[CausalPriceBar],
    *,
    sessions: tuple[date, ...],
    decision_session: date,
    decision_at: datetime,
    action_coverage_complete: bool,
    action_or_delisting_sessions: frozenset[date],
) -> tuple[ProspectiveFeatureResult, ...]:
    """Materialize the frozen three-feature set or abstain per asset.

    `sessions` is the pinned XNYS sequence and must include six consecutive
    sessions ending in ``decision_session``.  An action/delisting event in that
    window deliberately invalidates the row instead of applying a retrospective
    price adjustment.
    """

    decision = require_aware_utc(decision_at, "decision_at")
    if not sessions or tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise ContractError("feature sessions must be strictly sorted and unique")
    if decision_session not in sessions:
        raise ContractError("decision session is absent from pinned sessions")
    if any(type(item) is not date for item in action_or_delisting_sessions):
        raise ContractError("action or delisting sessions must be exact dates")
    by_asset: dict[str, dict[date, CausalPriceBar]] = {}
    for bar in bars:
        if type(bar) is not CausalPriceBar:
            raise ContractError("feature bars must use CausalPriceBar")
        bar.validate()
        asset_bars = by_asset.setdefault(bar.asset_id, {})
        if bar.session in asset_bars:
            raise ContractError("feature bars contain a duplicate asset/session")
        asset_bars[bar.session] = bar
    position = sessions.index(decision_session)
    required_sessions = sessions[max(0, position - 5):position + 1]
    results: list[ProspectiveFeatureResult] = []
    for asset_id in sorted(by_asset):
        asset_bars = by_asset[asset_id]
        if not action_coverage_complete:
            results.append(ProspectiveFeatureResult(asset_id, decision_session, UNRESOLVED_STATUS, None, None, "incomplete corporate-action or delisting coverage"))
            continue
        if len(required_sessions) != 6:
            results.append(ProspectiveFeatureResult(asset_id, decision_session, UNRESOLVED_STATUS, None, None, "insufficient pinned-session lookback"))
            continue
        if any(session in action_or_delisting_sessions for session in required_sessions):
            results.append(ProspectiveFeatureResult(asset_id, decision_session, UNRESOLVED_STATUS, None, None, "action or delisting event intersects feature lookback"))
            continue
        window = [asset_bars.get(session) for session in required_sessions]
        if any(bar is None or bar.open is None or bar.close is None for bar in window):
            results.append(ProspectiveFeatureResult(asset_id, decision_session, UNRESOLVED_STATUS, None, None, "missing required causal OHLC evidence"))
            continue
        assert all(bar is not None for bar in window)
        available_at = max(require_aware_utc(bar.available_at, "price_bar.available_at") for bar in window)
        if available_at > decision:
            results.append(ProspectiveFeatureResult(asset_id, decision_session, UNRESOLVED_STATUS, available_at, None, "required evidence was unavailable by decision time"))
            continue
        closes = [float(bar.close) for bar in window]
        returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
        mean = sum(returns) / len(returns)
        values = {
            "d0_raw_intraday_return": float(window[-1].close) / float(window[-1].open) - 1.0,
            "trailing_5_session_raw_return": closes[-1] / closes[0] - 1.0,
            "trailing_5_session_raw_volatility": math.sqrt(sum((value - mean) ** 2 for value in returns) / len(returns)),
        }
        results.append(ProspectiveFeatureResult(asset_id, decision_session, READY_STATUS, available_at, values, None))
    return tuple(results)
