from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

from ..common import parse_utc_z
from ..errors import ContractError


KNOWN_BAR_FIELDS = {"o", "h", "l", "c", "v", "n", "vw", "t"}


def _accept_native_bar(
    *,
    symbol: str,
    bar: object,
    eastern: ZoneInfo,
    seen_keys: set[tuple[str, object]],
) -> tuple[datetime, object, float, float, float, float, int, int | None, float | None]:
    if (
        not isinstance(bar, dict)
        or not {"o", "h", "l", "c", "v", "t"} <= set(bar)
        or set(bar) - KNOWN_BAR_FIELDS
    ):
        raise ContractError(
            "native Alpaca bar schema differs from the frozen contract"
        )
    event_at = parse_utc_z(bar["t"], "bar.t")
    local_event = event_at.astimezone(eastern)
    if any(
        (
            local_event.hour,
            local_event.minute,
            local_event.second,
            local_event.microsecond,
        )
    ):
        raise ContractError(
            "native Alpaca daily timestamp must be New York midnight"
        )
    session = local_event.date()
    if any(
        isinstance(bar[name], bool)
        or not isinstance(bar[name], (int, float))
        for name in ("o", "h", "l", "c")
    ):
        raise ContractError(
            "native Alpaca OHLC values must be exact JSON numbers"
        )
    if bar.get("vw") is not None and (
        isinstance(bar["vw"], bool)
        or not isinstance(bar["vw"], (int, float))
    ):
        raise ContractError(
            "native Alpaca VWAP must be an exact JSON number or null"
        )
    open_, high, low, close = (
        float(bar[name])
        for name in ("o", "h", "l", "c")
    )
    if type(bar["v"]) is not int:
        raise ContractError(
            "native Alpaca volume must be an exact JSON integer"
        )
    if bar.get("n") is not None and type(bar["n"]) is not int:
        raise ContractError(
            "native Alpaca trade count must be an exact JSON integer or null"
        )
    volume = bar["v"]
    trade_count = bar.get("n")
    vwap = None if bar.get("vw") is None else float(bar["vw"])
    if (
        not all(
            math.isfinite(value) and value > 0
            for value in (open_, high, low, close)
        )
        or high < max(open_, close)
        or low > min(open_, close)
        or high < low
        or volume < 0
        or trade_count is not None and trade_count < 0
        or vwap is not None and (not math.isfinite(vwap) or vwap <= 0)
    ):
        raise ContractError("native Alpaca bar violates OHLCV invariants")
    key = (symbol, session)
    if key in seen_keys:
        raise ContractError(
            f"duplicate Alpaca symbol/session: {symbol}/{session}"
        )
    seen_keys.add(key)
    return (
        event_at,
        session,
        open_,
        high,
        low,
        close,
        volume,
        trade_count,
        vwap,
    )
