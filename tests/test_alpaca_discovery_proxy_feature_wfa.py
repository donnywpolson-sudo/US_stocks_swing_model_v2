from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.alpaca_discovery_proxy_feature_wfa import build_price_only_proxy_features, load_feature_wfa_contract
from us_stocks_swing_model_v2.errors import ContractError


REPO = Path(__file__).resolve().parents[1]


def test_feature_contract_is_content_addressed_and_discovery_only() -> None:
    contract = load_feature_wfa_contract(REPO)
    assert len(contract["contract_id"]) == 64
    assert contract["features"]["may_read_outcomes"] is False
    assert contract["wfa"]["real_history_execution_authorized"] is False
    assert contract["claims"]["alpha_claim"] is False


def test_price_features_are_causal_and_preserve_an_unresolved_lookback() -> None:
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    bars = [{"symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0 + index} for index, session in enumerate(sessions)] + [{"symbol": "SPY", "session": session, "open": 200.0, "close": 200.0} for session in sessions[1:]]
    rows = build_price_only_proxy_features(sessions, bars)
    ready = next(row for row in rows if row["symbol"] == "AAPL" and row["decision_session"] == sessions[5])
    unresolved = next(row for row in rows if row["symbol"] == "SPY" and row["decision_session"] == sessions[5])
    assert ready["status"] == "READY_CAUSAL_RAW_PRICE_FEATURES"
    assert ready["d0_raw_intraday_return"] == pytest.approx(0.05)
    assert ready["trailing_5_session_raw_return"] == pytest.approx(0.05)
    assert unresolved["status"] == "UNRESOLVED_CAUSAL_LOOKBACK"
    assert unresolved["d0_raw_intraday_return"] is None


def test_price_features_reject_duplicate_sessions() -> None:
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    rows = [{"symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0} for session in sessions]
    rows.append(dict(rows[0]))
    with pytest.raises(ContractError, match="session"):
        build_price_only_proxy_features(sessions, rows)
