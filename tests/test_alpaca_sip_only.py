from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.providers.alpaca import AlpacaBarsPolicy


REPO = Path(__file__).resolve().parents[1]


def test_sip_is_the_only_permitted_alpaca_bar_feed() -> None:
    AlpacaBarsPolicy(feed="sip").validate()
    with pytest.raises(ContractError, match="SIP"):
        AlpacaBarsPolicy(feed="iex").validate()


def test_source_policy_is_pending_single_feed_requalification() -> None:
    sources = json.loads((REPO / "config" / "sources.json").read_text(encoding="utf-8"))
    contract = sources["sources"]["alpaca_basic_delayed_sip"]
    assert contract["status"] == "pending_single_sip_requalification"
    assert contract["request_contract"] == {
        "qualified_feed": None,
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "minimum_end_lag_minutes": 20,
        "sort": "asc",
    }
