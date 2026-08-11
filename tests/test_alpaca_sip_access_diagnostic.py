from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from us_stocks_swing_model_v2.free_acquisition import _parse_success
from us_stocks_swing_model_v2.free_source_evidence import alpaca_sip_access_plan


ROOT = Path(__file__).resolve().parents[1]


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_sip_access_plans_pin_rfc3339_sip_raw_daily_and_endpoint_form() -> None:
    for endpoint_form in ("single", "multi"):
        plan = alpaca_sip_access_plan(
            repository_root=ROOT,
            start_at=_utc("2026-08-07T04:00:00Z"),
            end_at=_utc("2026-08-07T04:00:01Z"),
            endpoint_form=endpoint_form,
        )
        query = dict(plan.canonical_query)
        assert query["start"] == "2026-08-07T04:00:00Z"
        assert query["end"] == "2026-08-07T04:00:01Z"
        assert query["feed"] == "sip"
        assert query["timeframe"] == "1Day"
        assert query["adjustment"] == "raw"
        assert ("symbols" in query) is (endpoint_form == "multi")


def test_single_symbol_response_is_normalized_without_weakening_bar_validation() -> None:
    plan = alpaca_sip_access_plan(
        repository_root=ROOT,
        start_at=_utc("2026-08-07T04:00:00Z"),
        end_at=_utc("2026-08-07T04:00:01Z"),
        endpoint_form="single",
    )
    raw = json.dumps({
        "bars": [{"t": "2026-08-07T04:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 3}],
        "next_page_token": None,
    }).encode()
    parsed = _parse_success(plan, raw, retrieved_at=_utc("2026-08-11T02:00:00Z"))
    assert parsed[:4] == ("PARSED", "PASS", None, 1)
