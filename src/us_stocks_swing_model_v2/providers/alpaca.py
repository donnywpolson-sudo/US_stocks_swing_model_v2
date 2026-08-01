from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.error import HTTPError
from urllib.request import Request
from zoneinfo import ZoneInfo

from ..common import canonical_json_bytes, iso_z, require_aware_utc, require_sha256, sha256_bytes
from ..clock import TrustedClock, require_trusted_clock
from ..errors import ContractError, IntegrityError, NetworkGuardError
from ..exchange_calendar import load_xnys_calendar_release
from .http import open_without_redirects
from .network_execution import (
    NetworkResponseEvidence,
    LocalNetworkExecutionSession,
    _bind_network_response,
    assert_local_network_request,
)
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
MAX_TRUSTED_REQUEST_AGE_MINUTES = 15
NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class HttpResponseEvidence:
    url: str
    response_url: str
    status: int
    raw_bytes: bytes
    headers: dict[str, str]
    retrieved_at: datetime
    transport_evidence: NetworkResponseEvidence | None = None

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
        if self.feed != "sip":
            raise ContractError("Alpaca feed must be the explicitly qualified SIP feed")
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

    def validate_against_trusted_time(
        self,
        policy: AlpacaBarsPolicy,
        observed_at: datetime,
    ) -> None:
        policy.validate()
        requested_at = require_aware_utc(self.requested_at, "requested_at")
        trusted_at = require_aware_utc(observed_at, "trusted_request_time")
        if requested_at > trusted_at:
            raise ContractError(
                "Alpaca requested_at cannot be later than trusted request time"
            )
        if trusted_at - requested_at > timedelta(
            minutes=MAX_TRUSTED_REQUEST_AGE_MINUTES
        ):
            raise ContractError(
                "Alpaca requested_at is stale relative to trusted request time"
            )
        end = require_aware_utc(self.end, "end")
        if end > trusted_at - timedelta(
            minutes=policy.minimum_end_lag_minutes
        ):
            raise ContractError(
                "Alpaca qualification end is too recent for trusted request time"
            )

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
    authorization_session: LocalNetworkExecutionSession | None = None,
    page_index: int = 0,
    expected_page_token: str | None = None,
    source: str | None = None,
    max_response_bytes: int | None = None,
) -> HttpResponseEvidence:
    if not network_enabled or os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(
            f"network disabled; require explicit flag and {AUTH_ENVIRONMENT_TOKEN}=YES"
        )
    if not api_key_id or not api_secret_key:
        raise ContractError("Alpaca credentials must be supplied from the environment")
    trusted_clock = require_trusted_clock(clock)
    response_limit = (
        MAX_ALPACA_RESPONSE_BYTES
        if max_response_bytes is None
        else max_response_bytes
    )
    if (
        type(response_limit) is not int
        or not 1 <= response_limit <= MAX_ALPACA_RESPONSE_BYTES
    ):
        raise ContractError("Alpaca response limit is outside the allowed bound")
    request_source = source or f"alpaca_{policy.feed}_qualification"
    request.validate_against_trusted_time(
        policy,
        trusted_clock.now(),
    )
    url = request.url(policy)
    request_attempt = assert_local_network_request(
        authorization_session,
        source=request_source,
        url=url,
        timeout_seconds=timeout_seconds,
        max_response_bytes=response_limit,
        page_index=page_index,
        expected_page_token=expected_page_token,
        clock=trusted_clock,
    )
    http_request = Request(
        url,
        headers={"APCA-API-KEY-ID": api_key_id, "APCA-API-SECRET-KEY": api_secret_key},
        method="GET",
    )
    try:
        with open_without_redirects(
            http_request, timeout_seconds=timeout_seconds
        ) as response:
            payload_bytes = response.read(response_limit + 1)
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
        payload_bytes = response.read(response_limit + 1)
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
    if len(payload_bytes) > response_limit:
        raise ContractError("Alpaca response exceeded the bounded byte limit")
    evidence = HttpResponseEvidence(
        url=url,
        response_url=response_url,
        status=status,
        raw_bytes=payload_bytes,
        headers=headers,
        retrieved_at=trusted_clock.now(),
        transport_evidence=_bind_network_response(
            request_attempt,
            requested_url=url,
            response_url=response_url,
            http_status=status,
            raw=payload_bytes,
            headers=headers,
        ),
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
    authorization_session: LocalNetworkExecutionSession | None = None,
    source: str | None = None,
    timeout_seconds: int = 30,
    max_response_bytes: int | None = None,
) -> tuple[LandedSnapshot, ...]:
    if not 1 <= max_pages <= MAX_QUALIFICATION_PAGES:
        raise ContractError(f"max_pages must be in [1,{MAX_QUALIFICATION_PAGES}]")
    pages: list[LandedSnapshot] = []
    trusted_clock = require_trusted_clock(clock)
    response_limit = (
        MAX_ALPACA_RESPONSE_BYTES
        if max_response_bytes is None
        else max_response_bytes
    )
    request = initial
    seen_tokens: set[str] = set()
    for page_index in range(max_pages):
        evidence = guarded_fetch_json(
            request,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            policy=policy,
            network_enabled=network_enabled,
            clock=trusted_clock,
            authorization_session=authorization_session,
            page_index=page_index,
            expected_page_token=request.page_token,
            source=source,
            timeout_seconds=timeout_seconds,
            max_response_bytes=response_limit,
        )
        request_source = source or f"alpaca_{policy.feed}_qualification"
        snapshot = snapshot_store._land_network_response(
            transport_evidence=evidence.transport_evidence,
            source=request_source,
            requested_url=evidence.url,
            response_url=evidence.response_url,
            http_status=evidence.status,
            raw=evidence.raw_bytes,
            headers=evidence.headers,
            clock=trusted_clock,
            max_bytes=response_limit,
            requested_at=initial.requested_at if source is not None else None,
            request_plan_id=(
                authorization_session.plan.plan_id
                if source is not None and authorization_session is not None
                else None
            ),
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
                timestamps[symbol].append(timestamp)
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


def assess_landed_alpaca_sip(
    initial: AlpacaBarsRequest,
    *,
    sip_snapshot: LandedSnapshot,
    network_registry_id: str,
    calendar_release_directory: Path,
    accepted_release_root: Path,
    qualification_function: Callable[..., AlpacaQualificationResult] | None = None,
) -> dict[str, object]:
    """Build the canonical no-write SIP assessment from one immutable snapshot."""

    require_sha256(network_registry_id, "Alpaca SIP network_registry_id")
    qualify = qualification_function or qualify_landed_pages
    qualification = qualify(
        initial,
        AlpacaBarsPolicy(feed="sip", asof=None),
        (sip_snapshot,),
        calendar_release_directory=calendar_release_directory,
        accepted_release_root=accepted_release_root,
    )

    def qualification_result(value: AlpacaQualificationResult) -> dict[str, object]:
        return {
            "feed": value.feed,
            "state": value.state,
            "reasons": list(value.reasons),
            "snapshot_ids": list(value.snapshot_ids),
            "bar_count": value.bar_count,
            "calendar_release_id": value.calendar_release_id,
            "evidence_state": value.evidence_state,
            "trust_eligible": value.trust_eligible,
        }

    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_SIP_ASSESSMENT_NO_WRITES",
        "symbols": list(initial.symbols),
        "start": iso_z(initial.start),
        "end": iso_z(initial.end),
        "network_registry_id": network_registry_id,
        "snapshot": {
            "snapshot_id": sip_snapshot.snapshot_id,
            "raw_sha256": sip_snapshot.raw_sha256,
            "retrieved_at": iso_z(sip_snapshot.retrieved_at),
        },
        "qualification": qualification_result(qualification),
        "selected_feed_candidate": "sip" if qualification.eligible else None,
        "selection_reason": "sip_pass" if qualification.eligible else "sip_fail",
        "activation_authorized": False,
    }
    return {
        **unsigned,
        "assessment_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


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
        open_, high, low, close = (
            _explicit_json_number(row[name]) for name in ("o", "h", "l", "c")
        )
    except (TypeError, ValueError):
        return None
    volume = row["v"]
    if type(volume) is not int or volume < 0:
        return None
    if not request.start <= timestamp < request.end:
        return None
    if prior_timestamps and timestamp <= prior_timestamps[-1]:
        return None
    if not all(math.isfinite(value) for value in (open_, high, low, close)):
        return None
    if min(open_, high, low, close) <= 0 or high < max(open_, close, low) or low > min(open_, close, high):
        return None
    if "n" in row and (type(row["n"]) is not int or row["n"] < 0):
        return None
    if "vw" in row:
        try:
            value = _explicit_json_number(row["vw"])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0:
            return None
    return timestamp


def _explicit_json_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("provider numeric scalar is not explicit JSON numeric")
    return float(value)
