from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from typing import Iterable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from ..common import require_aware_utc, sha256_bytes
from ..clock import TrustedClock, require_trusted_clock
from ..errors import ContractError, IntegrityError, NetworkGuardError
from ..exchange_calendar import load_xnys_calendar_release
from .snapshots import (
    AsReceivedSnapshotStore,
    ALLOWED_RESPONSE_HEADERS,
    LandedSnapshot,
    normalize_response_headers,
)


ALPACA_BARS_ENDPOINT = "https://data.alpaca.markets/v2/stocks/bars"
AUTH_ENVIRONMENT_TOKEN = "FREE_SOURCE_QUALIFICATION_APPROVED"
MAX_QUALIFICATION_PAGES = 100
MAX_ALPACA_RESPONSE_BYTES = 64 * 1024 * 1024
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class HttpResponseEvidence:
    url: str
    response_url: str
    status: int
    raw_bytes: bytes
    headers: dict[str, str]
    retrieved_at: datetime

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.raw_bytes)

    def json_object(self) -> dict[str, object]:
        """Parse only transient evidence. Pagination workflows must land first."""
        try:
            payload = json.loads(self.raw_bytes)
        except json.JSONDecodeError as exc:
            raise ContractError("provider response is not JSON") from exc
        if not isinstance(payload, dict):
            raise ContractError("provider response root must be an object")
        return payload


@dataclass(frozen=True)
class AlpacaQualificationResult:
    state: str
    reasons: tuple[str, ...]
    feed: str
    snapshot_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    bar_count: int
    calendar_release_id: str
    evidence_state: str
    trust_eligible: bool

    @property
    def eligible(self) -> bool:
        return self.state == "PASS" and not self.reasons and self.trust_eligible


@dataclass(frozen=True)
class AlpacaBarsPolicy:
    feed: str | None = None
    timeframe: str = "1Day"
    adjustment: str = "raw"
    asof: str | None = None
    sort: str = "asc"
    minimum_end_lag_minutes: int = 20
    endpoint: str = ALPACA_BARS_ENDPOINT

    def validate(self) -> None:
        required = {
            "timeframe": (self.timeframe, "1Day"),
            "adjustment": (self.adjustment, "raw"),
            "sort": (self.sort, "asc"),
        }
        wrong = {name: actual for name, (actual, expected) in required.items() if actual != expected}
        if wrong:
            raise ContractError(f"Alpaca request policy drifted: {wrong}")
        if self.feed not in {"sip", "iex"}:
            raise ContractError("Alpaca feed must be an explicitly qualified SIP or IEX feed")
        if self.asof is not None:
            try:
                date.fromisoformat(self.asof)
            except ValueError as exc:
                raise ContractError("Alpaca asof must be omitted/null or a deliberate ISO date") from exc
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or parsed.netloc != "data.alpaca.markets" or parsed.path != "/v2/stocks/bars":
            raise ContractError("Alpaca endpoint is not the approved historical stock-bars endpoint")
        if self.minimum_end_lag_minutes < 20:
            raise ContractError("Alpaca qualification end lag cannot be weakened below 20 minutes")


@dataclass(frozen=True)
class AlpacaBarsRequest:
    symbols: tuple[str, ...]
    start: datetime
    end: datetime
    requested_at: datetime
    limit: int = 10000
    page_token: str | None = None

    def parameters(self, policy: AlpacaBarsPolicy) -> dict[str, str | int]:
        policy.validate()
        start = require_aware_utc(self.start, "start")
        end = require_aware_utc(self.end, "end")
        requested_at = require_aware_utc(self.requested_at, "requested_at")
        if start >= end:
            raise ContractError("Alpaca request start must precede end")
        if end > requested_at - timedelta(minutes=policy.minimum_end_lag_minutes):
            raise ContractError("Alpaca qualification end is too recent for the frozen policy")
        symbols = tuple(sorted(set(symbol.strip().upper() for symbol in self.symbols if symbol.strip())))
        if not symbols or symbols != self.symbols:
            raise ContractError("symbols must be nonempty, uppercase, sorted, and unique")
        if not 1 <= self.limit <= 10000:
            raise ContractError("Alpaca page limit must be in [1,10000]")
        params: dict[str, str | int] = {
            "symbols": ",".join(symbols),
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "timeframe": policy.timeframe,
            "adjustment": policy.adjustment,
            "feed": policy.feed,
            "sort": policy.sort,
            "limit": self.limit,
        }
        if policy.asof is not None:
            params["asof"] = policy.asof
        if self.page_token:
            params["page_token"] = self.page_token
        return params

    def url(self, policy: AlpacaBarsPolicy) -> str:
        return f"{policy.endpoint}?{urlencode(self.parameters(policy))}"


def guarded_fetch_json(
    request: AlpacaBarsRequest,
    *,
    api_key_id: str,
    api_secret_key: str,
    policy: AlpacaBarsPolicy,
    network_enabled: bool = False,
    timeout_seconds: int = 30,
    clock: TrustedClock | None = None,
) -> HttpResponseEvidence:
    if not network_enabled or os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(
            f"network disabled; require explicit flag and {AUTH_ENVIRONMENT_TOKEN}=YES"
        )
    if not api_key_id or not api_secret_key:
        raise ContractError("Alpaca credentials must be supplied from the environment")
    url = request.url(policy)
    http_request = Request(
        url,
        headers={"APCA-API-KEY-ID": api_key_id, "APCA-API-SECRET-KEY": api_secret_key},
        method="GET",
    )
    try:
        with urlopen(http_request, timeout=timeout_seconds) as response:  # noqa: S310 - host is policy pinned
            payload_bytes = response.read(MAX_ALPACA_RESPONSE_BYTES + 1)
            headers = normalize_response_headers(
                {
                    key.lower(): value
                    for key, value in response.headers.items()
                    if key.lower() in ALLOWED_RESPONSE_HEADERS
                }
            )
            status = int(response.status)
            response_url = str(response.geturl())
    except HTTPError as response:
        payload_bytes = response.read(MAX_ALPACA_RESPONSE_BYTES + 1)
        headers = normalize_response_headers(
            {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in ALLOWED_RESPONSE_HEADERS
            }
        )
        status = int(response.code)
        response_url = str(response.geturl())
    if response_url != url:
        raise ContractError("Alpaca response redirected away from the exact approved request URL")
    if len(payload_bytes) > MAX_ALPACA_RESPONSE_BYTES:
        raise ContractError("Alpaca response exceeded the bounded byte limit")
    evidence = HttpResponseEvidence(
        url=url,
        response_url=response_url,
        status=status,
        raw_bytes=payload_bytes,
        headers=headers,
        retrieved_at=require_trusted_clock(clock).now(),
    )
    return evidence


def guarded_fetch_landed_pages(
    initial: AlpacaBarsRequest,
    *,
    snapshot_store: AsReceivedSnapshotStore,
    api_key_id: str,
    api_secret_key: str,
    policy: AlpacaBarsPolicy,
    network_enabled: bool = False,
    max_pages: int = 10,
    clock: TrustedClock | None = None,
) -> tuple[LandedSnapshot, ...]:
    if not 1 <= max_pages <= MAX_QUALIFICATION_PAGES:
        raise ContractError(f"max_pages must be in [1,{MAX_QUALIFICATION_PAGES}]")
    pages: list[LandedSnapshot] = []
    trusted_clock = require_trusted_clock(clock)
    request = initial
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        evidence = guarded_fetch_json(
            request,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            policy=policy,
            network_enabled=network_enabled,
            clock=trusted_clock,
        )
        source = f"alpaca_{policy.feed}_qualification"
        snapshot = snapshot_store._land_network_response(
            source=source,
            requested_url=evidence.url,
            response_url=evidence.response_url,
            http_status=evidence.status,
            raw=evidence.raw_bytes,
            headers=evidence.headers,
            clock=trusted_clock,
        )
        pages.append(snapshot)
        # Pagination may be parsed only from re-verified bytes after the atomic
        # as-received snapshot has landed.
        payload = _json_object(snapshot.read_verified_bytes())
        token = payload.get("next_page_token")
        if token is None:
            return tuple(pages)
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ContractError("Alpaca pagination token is malformed or repeated")
        seen_tokens.add(token)
        request = AlpacaBarsRequest(
            symbols=initial.symbols,
            start=initial.start,
            end=initial.end,
            requested_at=initial.requested_at,
            limit=initial.limit,
            page_token=token,
        )
    raise ContractError("Alpaca qualification exceeded the bounded pagination limit")


def qualify_landed_pages(
    initial: AlpacaBarsRequest,
    policy: AlpacaBarsPolicy,
    pages: Iterable[LandedSnapshot],
    *,
    calendar_release_directory: Path,
    accepted_release_root: Path,
) -> AlpacaQualificationResult:
    """Strict qualification; HTTP 200 plus a bars object is deliberately insufficient."""
    policy.validate()
    initial.parameters(policy)
    loaded_calendar = load_xnys_calendar_release(
        calendar_release_directory,
        accepted_release_root=accepted_release_root,
    )
    calendar_rows = loaded_calendar.schedule.to_pylist()
    expected_sessions = tuple(
        row["session"]
        for row in calendar_rows
        if initial.start
        <= datetime.combine(row["session"], datetime.min.time(), tzinfo=NEW_YORK).astimezone(timezone.utc)
        < initial.end
    )
    expected_symbols = initial.symbols
    page_list = tuple(pages)
    reasons: list[str] = []
    seen_tokens: set[str] = set()
    expected_token: str | None = None
    timestamps: dict[str, list[datetime]] = {symbol: [] for symbol in expected_symbols}
    observed_sessions: dict[str, list[date]] = {symbol: [] for symbol in expected_symbols}
    bar_count = 0
    if not expected_sessions:
        reasons.append("calendar_has_no_expected_sessions")
    if not page_list:
        reasons.append("no_pages")
    for index, snapshot in enumerate(page_list):
        expected_request = AlpacaBarsRequest(
            symbols=initial.symbols,
            start=initial.start,
            end=initial.end,
            requested_at=initial.requested_at,
            limit=initial.limit,
            page_token=None if index == 0 else expected_token,
        )
        if snapshot.url != expected_request.url(policy):
            reasons.append(f"page_{index}_request_url_drift")
        if snapshot.source != f"alpaca_{policy.feed}_qualification":
            reasons.append(f"page_{index}_wrong_source")
        if snapshot.http_status != 200:
            reasons.append(f"page_{index}_http_{snapshot.http_status}")
        if snapshot.retrieved_at < initial.requested_at:
            reasons.append(f"page_{index}_retrieval_predates_request")
        try:
            payload = _json_object(snapshot.read_verified_bytes())
        except (ContractError, IntegrityError):
            reasons.append(f"page_{index}_invalid_landed_json")
            continue
        if set(payload) != {"bars", "next_page_token"}:
            reasons.append(f"page_{index}_top_level_schema")
            continue
        bars = payload.get("bars")
        if not isinstance(bars, dict):
            reasons.append(f"page_{index}_bars_not_object")
            continue
        if set(bars) - set(expected_symbols):
            reasons.append(f"page_{index}_unexpected_symbol")
        for symbol, rows in bars.items():
            if symbol not in timestamps:
                continue
            if not isinstance(rows, list) or not rows:
                reasons.append(f"page_{index}_{symbol}_empty_or_invalid")
                continue
            for row in rows:
                timestamp = _valid_bar(row, initial, timestamps[symbol])
                if timestamp is None:
                    reasons.append(f"page_{index}_{symbol}_invalid_bar")
                    break
                local = timestamp.astimezone(NEW_YORK)
                if local.timetz().replace(tzinfo=None) != datetime.min.time():
                    reasons.append(f"page_{index}_{symbol}_daily_timestamp_not_new_york_midnight")
                    break
                observed_sessions[symbol].append(local.date())
                bar_count += 1
        token = payload.get("next_page_token")
        request_query = parse_qs(urlparse(snapshot.url).query)
        actual_request_token = request_query.get("page_token", [None])
        if index == 0 and actual_request_token[0] is not None:
            reasons.append("first_page_has_token")
        if index > 0 and (expected_token is None or actual_request_token != [expected_token]):
            reasons.append(f"page_{index}_request_token_mismatch")
        if token is not None:
            if not isinstance(token, str) or not token or token in seen_tokens:
                reasons.append(f"page_{index}_invalid_or_repeated_next_token")
            else:
                seen_tokens.add(token)
        expected_token = token if isinstance(token, str) and token else None
        if index < len(page_list) - 1 and expected_token is None:
            reasons.append(f"page_{index}_premature_terminal")
    if page_list and expected_token is not None:
        reasons.append("pagination_not_terminal")
    missing = [symbol for symbol, values in timestamps.items() if not values]
    if missing:
        reasons.append("missing_symbols:" + ",".join(missing))
    expected_set = set(expected_sessions)
    for symbol in expected_symbols:
        actual = observed_sessions[symbol]
        actual_set = set(actual)
        if len(actual) != len(actual_set):
            reasons.append(f"{symbol}_duplicate_sessions")
        missing_sessions = sorted(expected_set - actual_set)
        extra_sessions = sorted(actual_set - expected_set)
        if missing_sessions:
            reasons.append(
                f"{symbol}_missing_sessions:" + ",".join(item.isoformat() for item in missing_sessions)
            )
        if extra_sessions:
            reasons.append(
                f"{symbol}_unexpected_sessions:" + ",".join(item.isoformat() for item in extra_sessions)
            )
    return AlpacaQualificationResult(
        state="PASS" if not reasons else "FAIL",
        reasons=tuple(sorted(set(reasons))),
        feed=str(policy.feed),
        snapshot_ids=tuple(snapshot.snapshot_id for snapshot in page_list),
        symbols=expected_symbols,
        bar_count=bar_count,
        calendar_release_id=loaded_calendar.calendar.release_id,
        evidence_state=(
            "NETWORK_AS_RECEIVED"
            if page_list and all(snapshot.trust_eligible for snapshot in page_list)
            else "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
        ),
        trust_eligible=bool(page_list) and all(snapshot.trust_eligible for snapshot in page_list),
    )


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError("provider response is not JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("provider response root must be an object")
    return payload


def _valid_bar(
    row: object,
    request: AlpacaBarsRequest,
    prior_timestamps: list[datetime],
) -> datetime | None:
    if not isinstance(row, Mapping):
        return None
    required = {"t", "o", "h", "l", "c", "v"}
    allowed = required | {"n", "vw"}
    if not required <= set(row) or set(row) - allowed:
        return None
    try:
        if not isinstance(row["t"], str):
            return None
        timestamp = datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(timezone.utc)
        open_, high, low, close, volume = (
            _explicit_json_number(row[name]) for name in ("o", "h", "l", "c", "v")
        )
    except (TypeError, ValueError):
        return None
    if not request.start <= timestamp < request.end:
        return None
    if prior_timestamps and timestamp <= prior_timestamps[-1]:
        return None
    if not all(math.isfinite(value) for value in (open_, high, low, close, volume)):
        return None
    if min(open_, high, low, close) <= 0 or volume < 0 or high < max(open_, close, low) or low > min(open_, close, high):
        return None
    for optional in ("n", "vw"):
        if optional in row:
            try:
                value = _explicit_json_number(row[optional])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(value) or value < 0:
                return None
    prior_timestamps.append(timestamp)
    return timestamp


def _explicit_json_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("provider numeric scalar is not explicit JSON numeric")
    return float(value)
