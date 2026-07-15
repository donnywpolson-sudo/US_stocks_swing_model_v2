from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from ..common import canonical_json_bytes, require_aware_utc, sha256_bytes
from ..clock import TrustedClock, require_trusted_clock
from ..errors import ContractError, IntegrityError, NetworkGuardError
from .alpaca import AUTH_ENVIRONMENT_TOKEN, HttpResponseEvidence
from .snapshots import (
    AsReceivedSnapshotStore,
    ALLOWED_RESPONSE_HEADERS,
    LandedSnapshot,
    normalize_response_headers,
)


CORPORATE_ACTIONS_ENDPOINT = "https://data.alpaca.markets/v1/corporate-actions"
MAX_PAGE_LIMIT = 1000
MAX_PAGES = 100
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
ACTION_GROUPS = {
    "forward_splits",
    "reverse_splits",
    "unit_splits",
    "cash_dividends",
    "stock_dividends",
    "spin_offs",
    "cash_mergers",
    "stock_mergers",
    "stock_and_cash_mergers",
    "redemptions",
    "name_changes",
    "worthless_removals",
    "rights_distributions",
    "partial_calls",
    "reorganizations",
}
CORPORATE_ACTION_SOURCE_EPOCH = "alpaca_corporate_actions_v1"


@dataclass(frozen=True)
class CorporateActionsRequest:
    start: date
    end: date
    requested_at: datetime
    symbols: tuple[str, ...] = ()
    page_token: str | None = None
    limit: int = MAX_PAGE_LIMIT

    def parameters(self) -> dict[str, str | int]:
        require_aware_utc(self.requested_at, "requested_at")
        if self.start > self.end:
            raise ContractError("corporate-action start cannot follow end")
        if self.limit != MAX_PAGE_LIMIT:
            raise ContractError("corporate-action qualification must pin the documented 1000 page limit")
        symbols = tuple(sorted(set(value.strip().upper() for value in self.symbols if value.strip())))
        if symbols != self.symbols:
            raise ContractError("corporate-action symbols must be uppercase, sorted, and unique")
        params: dict[str, str | int] = {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "region": "us",
            "sort": "asc",
            "limit": self.limit,
        }
        if symbols:
            params["symbols"] = ",".join(symbols)
        if self.page_token:
            params["page_token"] = self.page_token
        return params

    def url(self) -> str:
        return f"{CORPORATE_ACTIONS_ENDPOINT}?{urlencode(self.parameters())}"


@dataclass(frozen=True)
class CorporateActionEvidence:
    provider_action_id: str
    provider_group: str
    symbol: str | None
    provider_process_date: date
    effective_session: date | None
    received_at: datetime
    snapshot_id: str
    raw_row_sha256: str
    source_epoch: str = CORPORATE_ACTION_SOURCE_EPOCH
    provider_process_date_is_causal: bool = False


def guarded_fetch_corporate_action_pages(
    initial: CorporateActionsRequest,
    *,
    snapshot_store: AsReceivedSnapshotStore,
    api_key_id: str,
    api_secret_key: str,
    network_enabled: bool = False,
    max_pages: int = 10,
    timeout_seconds: int = 30,
    clock: TrustedClock | None = None,
) -> tuple[LandedSnapshot, ...]:
    if not network_enabled or os.environ.get(AUTH_ENVIRONMENT_TOKEN) != "YES":
        raise NetworkGuardError(
            f"network disabled; require explicit flag and {AUTH_ENVIRONMENT_TOKEN}=YES"
        )
    if not api_key_id or not api_secret_key:
        raise ContractError("Alpaca credentials must be supplied from the environment")
    if not 1 <= max_pages <= MAX_PAGES:
        raise ContractError(f"max_pages must be in [1,{MAX_PAGES}]")
    pages: list[LandedSnapshot] = []
    trusted_clock = require_trusted_clock(clock)
    request = initial
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        evidence = _fetch_page(
            request,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            timeout_seconds=timeout_seconds,
            clock=trusted_clock,
        )
        source = "alpaca_corporate_actions"
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
        # Parse pagination only after an atomic land and verified reread.
        payload = _page_payload(snapshot)
        token = payload["next_page_token"]
        if token is None:
            return tuple(pages)
        if not isinstance(token, str) or not token or token in seen_tokens:
            raise ContractError("corporate-action pagination token is malformed or repeated")
        seen_tokens.add(token)
        request = CorporateActionsRequest(
            start=initial.start,
            end=initial.end,
            requested_at=initial.requested_at,
            symbols=initial.symbols,
            page_token=token,
            limit=initial.limit,
        )
    raise ContractError("corporate-action qualification exceeded its bounded page limit")


def parse_landed_corporate_actions(
    initial: CorporateActionsRequest,
    pages: Iterable[LandedSnapshot],
) -> tuple[CorporateActionEvidence, ...]:
    page_list = tuple(pages)
    if not page_list:
        raise ContractError("corporate-action parse requires at least one landed page")
    expected_token: str | None = None
    seen_next: set[str] = set()
    seen_actions: set[tuple[str, str]] = set()
    output: list[CorporateActionEvidence] = []
    for index, snapshot in enumerate(page_list):
        if snapshot.source != "alpaca_corporate_actions" or snapshot.http_status != 200:
            raise ContractError("corporate-action page source/status is not qualified")
        if snapshot.retrieved_at < initial.requested_at:
            raise ContractError("corporate-action receipt predates its request")
        query = parse_qs(urlparse(snapshot.url).query)
        actual_token = query.get("page_token", [None])
        if (index == 0 and actual_token[0] is not None) or (
            index > 0 and actual_token != [expected_token]
        ):
            raise ContractError("corporate-action page request token chain is broken")
        payload = _page_payload(snapshot)
        actions = payload["corporate_actions"]
        for group, rows in actions.items():
            if group not in ACTION_GROUPS or not isinstance(rows, list):
                raise ContractError("corporate-action response group differs from the frozen contract")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ContractError("corporate-action row requires a provider ID")
                key = (group, row["id"])
                if key in seen_actions:
                    raise ContractError("corporate-action pages contain a duplicate action")
                seen_actions.add(key)
                try:
                    process_date = date.fromisoformat(str(row["process_date"]))
                except (KeyError, ValueError) as exc:
                    raise ContractError("corporate-action process_date is invalid") from exc
                if not initial.start <= process_date <= initial.end:
                    raise ContractError("corporate-action process_date escapes the request interval")
                output.append(
                    CorporateActionEvidence(
                        provider_action_id=row["id"],
                        provider_group=group,
                        symbol=_symbol(row),
                        provider_process_date=process_date,
                        effective_session=_effective_session(row),
                        received_at=snapshot.retrieved_at,
                        snapshot_id=snapshot.snapshot_id,
                        raw_row_sha256=sha256_bytes(canonical_json_bytes(row)),
                        provider_process_date_is_causal=False,
                    )
                )
        token = payload["next_page_token"]
        if token is not None:
            if not isinstance(token, str) or not token or token in seen_next:
                raise ContractError("corporate-action next-page token is malformed or repeated")
            seen_next.add(token)
        expected_token = token
        if index < len(page_list) - 1 and expected_token is None:
            raise ContractError("corporate-action pagination terminated before the final landed page")
    if expected_token is not None:
        raise ContractError("corporate-action landed pagination is incomplete")
    return tuple(output)


def _fetch_page(
    request: CorporateActionsRequest,
    *,
    api_key_id: str,
    api_secret_key: str,
    timeout_seconds: int,
    clock: TrustedClock,
) -> HttpResponseEvidence:
    url = request.url()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "data.alpaca.markets" or parsed.path != "/v1/corporate-actions":
        raise ContractError("corporate-action endpoint differs from the approved Alpaca endpoint")
    http_request = Request(
        url,
        headers={"APCA-API-KEY-ID": api_key_id, "APCA-API-SECRET-KEY": api_secret_key},
        method="GET",
    )
    try:
        with urlopen(http_request, timeout=timeout_seconds) as response:  # noqa: S310 - host is pinned above
            raw = response.read(MAX_RESPONSE_BYTES + 1)
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
        raw = response.read(MAX_RESPONSE_BYTES + 1)
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
        raise ContractError("corporate-action response redirected away from its exact request URL")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ContractError("corporate-action response exceeded the bounded byte limit")
    return HttpResponseEvidence(
        url=url,
        response_url=response_url,
        status=status,
        raw_bytes=raw,
        headers=headers,
        retrieved_at=require_trusted_clock(clock).now(),
    )


def _page_payload(snapshot: LandedSnapshot) -> dict[str, object]:
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except json.JSONDecodeError as exc:
        raise ContractError("corporate-action landed page is not JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"corporate_actions", "next_page_token"}:
        raise ContractError("corporate-action top-level response schema differs")
    if not isinstance(payload["corporate_actions"], dict):
        raise ContractError("corporate_actions must be an object")
    return payload


def _symbol(row: dict[str, object]) -> str | None:
    for field in ("symbol", "old_symbol", "source_symbol", "acquiree_symbol", "initiating_symbol"):
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _effective_session(row: dict[str, object]) -> date | None:
    for field in ("ex_date", "effective_date", "payable_date"):
        value = row.get(field)
        if value is not None:
            try:
                return date.fromisoformat(str(value))
            except ValueError as exc:
                raise ContractError(f"corporate-action {field} is invalid") from exc
    return None
