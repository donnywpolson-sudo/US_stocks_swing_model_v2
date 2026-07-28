from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request

from ..common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from ..clock import TrustedClock, require_trusted_clock
from ..errors import ContractError, IntegrityError, NetworkGuardError
from .alpaca import (
    AUTH_ENVIRONMENT_TOKEN,
    MAX_TRUSTED_REQUEST_AGE_MINUTES,
    HttpResponseEvidence,
)
from .http import open_without_redirects
from .network_execution import (
    NetworkRequestAttempt,
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
PROCESS_DATE_ACQUISITION_COVERAGE = "PROVIDER_PROCESS_DATE_ACQUISITION_ONLY"


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

    def validate_against_trusted_time(self, observed_at: datetime) -> None:
        requested_at = require_aware_utc(self.requested_at, "requested_at")
        trusted_at = require_aware_utc(
            observed_at,
            "corporate_action.trusted_request_time",
        )
        if requested_at > trusted_at:
            raise ContractError(
                "corporate-action requested_at cannot be later than trusted request time"
            )
        if trusted_at - requested_at > timedelta(
            minutes=MAX_TRUSTED_REQUEST_AGE_MINUTES
        ):
            raise ContractError(
                "corporate-action requested_at is stale relative to trusted request time"
            )
        if self.end > trusted_at.date():
            raise ContractError(
                "corporate-action process-date end cannot follow trusted acquisition date"
            )


@dataclass(frozen=True)
class CorporateActionEvidence:
    provider_action_id: str
    provider_group: str
    symbol: str | None
    involved_symbols: tuple[str, ...]
    provider_process_date: date
    effective_session: date | None
    received_at: datetime
    snapshot_id: str
    raw_row_sha256: str
    acquisition_mode: str
    evidence_state: str
    acquisition_capability_id: str
    synthetic_permit_ids: tuple[str, ...]
    source_epoch: str = CORPORATE_ACTION_SOURCE_EPOCH
    provider_process_date_is_causal: bool = False

    def validate(self) -> None:
        if (
            not isinstance(self.provider_action_id, str)
            or not self.provider_action_id
            or not isinstance(self.provider_group, str)
            or self.provider_group not in ACTION_GROUPS
        ):
            raise ContractError("corporate-action evidence identity/group is invalid")
        if self.symbol is not None and (
            not isinstance(self.symbol, str)
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
        ):
            raise ContractError("corporate-action evidence symbol is not canonical")
        if (
            type(self.involved_symbols) is not tuple
            or not self.involved_symbols
            or self.involved_symbols
            != tuple(sorted(set(self.involved_symbols)))
            or any(
                type(value) is not str
                or not value
                or value != value.strip().upper()
                for value in self.involved_symbols
            )
            or (
                self.symbol is not None
                and self.symbol not in self.involved_symbols
            )
        ):
            raise ContractError(
                "corporate-action involved symbols are not exact canonical evidence"
            )
        if type(self.provider_process_date) is not date:
            raise ContractError("corporate-action process date must be an exact date")
        if self.effective_session is not None and type(self.effective_session) is not date:
            raise ContractError("corporate-action effective session must be an exact date")
        require_aware_utc(self.received_at, "corporate_action.received_at")
        require_sha256(self.snapshot_id, "corporate_action.snapshot_id")
        require_sha256(self.raw_row_sha256, "corporate_action.raw_row_sha256")
        require_sha256(
            self.acquisition_capability_id,
            "corporate_action.acquisition_capability_id",
        )
        if not isinstance(self.evidence_state, str):
            raise ContractError("corporate-action evidence state is invalid")
        if type(self.synthetic_permit_ids) is not tuple:
            raise ContractError("corporate-action synthetic permit IDs must be an exact tuple")
        if self.synthetic_permit_ids != tuple(sorted(set(self.synthetic_permit_ids))):
            raise ContractError("corporate-action synthetic permit IDs must be sorted and unique")
        for index, permit_id in enumerate(self.synthetic_permit_ids):
            require_sha256(permit_id, f"corporate_action.synthetic_permit_ids[{index}]")
        if self.acquisition_mode == "NETWORK_AS_RECEIVED":
            if self.evidence_state != "NETWORK_AS_RECEIVED" or self.synthetic_permit_ids:
                raise ContractError("network corporate-action evidence binding is inconsistent")
        elif self.acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED":
            if (
                self.evidence_state != "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
                or len(self.synthetic_permit_ids) != 1
                or self.synthetic_permit_ids[0] != self.acquisition_capability_id
            ):
                raise ContractError("synthetic corporate-action evidence binding is inconsistent")
        else:
            raise ContractError("corporate-action acquisition mode is invalid")
        if self.source_epoch != CORPORATE_ACTION_SOURCE_EPOCH:
            raise ContractError("corporate-action source epoch differs from the frozen contract")
        if type(self.provider_process_date_is_causal) is not bool or self.provider_process_date_is_causal:
            raise ContractError("provider process date cannot be asserted causal")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "provider_action_id": self.provider_action_id,
            "provider_group": self.provider_group,
            "symbol": self.symbol,
            "involved_symbols": list(self.involved_symbols),
            "provider_process_date": self.provider_process_date.isoformat(),
            "effective_session": (
                self.effective_session.isoformat()
                if self.effective_session is not None
                else None
            ),
            "received_at": iso_z(self.received_at),
            "snapshot_id": self.snapshot_id,
            "raw_row_sha256": self.raw_row_sha256,
            "acquisition_mode": self.acquisition_mode,
            "evidence_state": self.evidence_state,
            "acquisition_capability_id": self.acquisition_capability_id,
            "synthetic_permit_ids": list(self.synthetic_permit_ids),
            "source_epoch": self.source_epoch,
            "provider_process_date_is_causal": self.provider_process_date_is_causal,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CorporateActionEvidence":
        if type(payload) is not dict or set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("corporate-action evidence fields differ from the exact contract")
        if (
            type(payload["synthetic_permit_ids"]) is not list
            or type(payload["involved_symbols"]) is not list
        ):
            raise ContractError(
                "corporate-action symbol and permit collections require exact JSON arrays"
            )
        try:
            evidence = cls(
                provider_action_id=payload["provider_action_id"],
                provider_group=payload["provider_group"],
                symbol=payload["symbol"],
                involved_symbols=tuple(payload["involved_symbols"]),
                provider_process_date=date.fromisoformat(payload["provider_process_date"]),
                effective_session=(
                    date.fromisoformat(payload["effective_session"])
                    if payload["effective_session"] is not None
                    else None
                ),
                received_at=parse_utc_z(payload["received_at"], "corporate_action.received_at"),
                snapshot_id=payload["snapshot_id"],
                raw_row_sha256=payload["raw_row_sha256"],
                acquisition_mode=payload["acquisition_mode"],
                evidence_state=payload["evidence_state"],
                acquisition_capability_id=payload["acquisition_capability_id"],
                synthetic_permit_ids=tuple(payload["synthetic_permit_ids"]),
                source_epoch=payload["source_epoch"],
                provider_process_date_is_causal=payload["provider_process_date_is_causal"],
            )
        except (TypeError, ValueError) as exc:
            raise ContractError("corporate-action evidence values are invalid") from exc
        evidence.validate()
        return evidence


@dataclass(frozen=True)
class CorporateActionCoverageEvidence:
    schema_version: int
    coverage_semantics: str
    process_date_start: date
    process_date_end: date
    requested_at: datetime
    requested_symbols: tuple[str, ...]
    completed_at: datetime
    snapshot_ids: tuple[str, ...]
    acquisition_mode: str
    evidence_state: str
    acquisition_capability_ids: tuple[str, ...]
    synthetic_permit_ids: tuple[str, ...]
    source_epoch: str
    coverage_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "coverage_semantics": self.coverage_semantics,
            "process_date_start": self.process_date_start.isoformat(),
            "process_date_end": self.process_date_end.isoformat(),
            "requested_at": iso_z(self.requested_at),
            "requested_symbols": list(self.requested_symbols),
            "completed_at": iso_z(self.completed_at),
            "snapshot_ids": list(self.snapshot_ids),
            "acquisition_mode": self.acquisition_mode,
            "evidence_state": self.evidence_state,
            "acquisition_capability_ids": list(self.acquisition_capability_ids),
            "synthetic_permit_ids": list(self.synthetic_permit_ids),
            "source_epoch": self.source_epoch,
        }

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ContractError("corporate-action coverage schema is invalid")
        if self.coverage_semantics != PROCESS_DATE_ACQUISITION_COVERAGE:
            raise ContractError(
                "provider corporate-action coverage must remain process-date acquisition only"
            )
        if (
            type(self.process_date_start) is not date
            or type(self.process_date_end) is not date
            or self.process_date_start > self.process_date_end
        ):
            raise ContractError(
                "corporate-action process-date acquisition interval is invalid"
            )
        requested_at = require_aware_utc(
            self.requested_at, "corporate_action_coverage.requested_at"
        )
        completed_at = require_aware_utc(
            self.completed_at, "corporate_action_coverage.completed_at"
        )
        if completed_at < requested_at:
            raise ContractError("corporate-action coverage predates its request")
        if self.process_date_end > completed_at.date():
            raise ContractError(
                "corporate-action completed coverage cannot extend beyond acquisition date"
            )
        if (
            type(self.requested_symbols) is not tuple
            or self.requested_symbols
            != tuple(sorted(set(self.requested_symbols)))
            or any(
                not symbol or symbol != symbol.strip().upper()
                for symbol in self.requested_symbols
            )
        ):
            raise ContractError("corporate-action coverage symbol census is invalid")
        if (
            type(self.snapshot_ids) is not tuple
            or not self.snapshot_ids
            or len(self.snapshot_ids) != len(set(self.snapshot_ids))
        ):
            raise ContractError("corporate-action coverage page census is invalid")
        for index, snapshot_id in enumerate(self.snapshot_ids):
            require_sha256(snapshot_id, f"corporate_action_coverage.snapshot_ids[{index}]")
        for name, values in (
            ("acquisition_capability_ids", self.acquisition_capability_ids),
            ("synthetic_permit_ids", self.synthetic_permit_ids),
        ):
            if type(values) is not tuple or values != tuple(sorted(set(values))):
                raise ContractError(f"corporate-action coverage {name} is invalid")
            for index, value in enumerate(values):
                require_sha256(value, f"corporate_action_coverage.{name}[{index}]")
        if not self.acquisition_capability_ids:
            raise ContractError("corporate-action coverage lacks acquisition capability")
        if self.acquisition_mode == "NETWORK_AS_RECEIVED":
            if self.evidence_state != "NETWORK_AS_RECEIVED" or self.synthetic_permit_ids:
                raise ContractError("network corporate-action coverage binding is inconsistent")
        elif self.acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED":
            if (
                self.evidence_state != "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
                or self.synthetic_permit_ids != self.acquisition_capability_ids
            ):
                raise ContractError("synthetic corporate-action coverage binding is inconsistent")
        else:
            raise ContractError("corporate-action coverage acquisition mode is invalid")
        if self.source_epoch != CORPORATE_ACTION_SOURCE_EPOCH:
            raise ContractError("corporate-action coverage source epoch differs")
        require_sha256(self.coverage_id, "corporate_action_coverage.coverage_id")
        if self.coverage_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("corporate-action coverage ID differs from its content")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {**self.unsigned_dict(), "coverage_id": self.coverage_id}

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, object],
    ) -> "CorporateActionCoverageEvidence":
        if type(payload) is not dict or set(payload) != set(cls.__dataclass_fields__):
            raise ContractError(
                "provider corporate-action coverage fields differ"
            )
        if type(payload["schema_version"]) is not int:
            raise ContractError(
                "provider corporate-action coverage schema must be an exact integer"
            )
        text_fields = (
            "coverage_semantics",
            "process_date_start",
            "process_date_end",
            "requested_at",
            "completed_at",
            "acquisition_mode",
            "evidence_state",
            "source_epoch",
            "coverage_id",
        )
        if any(type(payload[name]) is not str for name in text_fields):
            raise ContractError(
                "provider corporate-action coverage scalar evidence must be exact text"
            )
        tuple_fields = (
            "requested_symbols",
            "snapshot_ids",
            "acquisition_capability_ids",
            "synthetic_permit_ids",
        )
        if any(
            type(payload[name]) is not list
            or any(type(value) is not str for value in payload[name])
            for name in tuple_fields
        ):
            raise ContractError(
                "provider corporate-action coverage censuses must be exact text arrays"
            )
        try:
            evidence = cls(
                schema_version=payload["schema_version"],
                coverage_semantics=payload["coverage_semantics"],
                process_date_start=date.fromisoformat(payload["process_date_start"]),
                process_date_end=date.fromisoformat(payload["process_date_end"]),
                requested_at=parse_utc_z(
                    payload["requested_at"],
                    "provider_coverage.requested_at",
                ),
                requested_symbols=tuple(payload["requested_symbols"]),
                completed_at=parse_utc_z(
                    payload["completed_at"],
                    "provider_coverage.completed_at",
                ),
                snapshot_ids=tuple(payload["snapshot_ids"]),
                acquisition_mode=payload["acquisition_mode"],
                evidence_state=payload["evidence_state"],
                acquisition_capability_ids=tuple(
                    payload["acquisition_capability_ids"]
                ),
                synthetic_permit_ids=tuple(payload["synthetic_permit_ids"]),
                source_epoch=payload["source_epoch"],
                coverage_id=payload["coverage_id"],
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "provider corporate-action coverage values are invalid"
            ) from exc
        evidence.validate()
        return evidence

    @classmethod
    def create(
        cls,
        *,
        initial: CorporateActionsRequest,
        pages: tuple[LandedSnapshot, ...],
        acquisition_mode: str,
        evidence_state: str,
        acquisition_capability_ids: tuple[str, ...],
        synthetic_permit_ids: tuple[str, ...],
    ) -> "CorporateActionCoverageEvidence":
        unsigned = {
            "schema_version": 2,
            "coverage_semantics": PROCESS_DATE_ACQUISITION_COVERAGE,
            "process_date_start": initial.start.isoformat(),
            "process_date_end": initial.end.isoformat(),
            "requested_at": iso_z(initial.requested_at),
            "requested_symbols": list(initial.symbols),
            "completed_at": iso_z(max(page.retrieved_at for page in pages)),
            "snapshot_ids": [page.snapshot_id for page in pages],
            "acquisition_mode": acquisition_mode,
            "evidence_state": evidence_state,
            "acquisition_capability_ids": list(acquisition_capability_ids),
            "synthetic_permit_ids": list(synthetic_permit_ids),
            "source_epoch": CORPORATE_ACTION_SOURCE_EPOCH,
        }
        receipt = cls(
            schema_version=2,
            coverage_semantics=PROCESS_DATE_ACQUISITION_COVERAGE,
            process_date_start=initial.start,
            process_date_end=initial.end,
            requested_at=initial.requested_at,
            requested_symbols=initial.symbols,
            completed_at=max(page.retrieved_at for page in pages),
            snapshot_ids=tuple(page.snapshot_id for page in pages),
            acquisition_mode=acquisition_mode,
            evidence_state=evidence_state,
            acquisition_capability_ids=acquisition_capability_ids,
            synthetic_permit_ids=synthetic_permit_ids,
            source_epoch=CORPORATE_ACTION_SOURCE_EPOCH,
            coverage_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        receipt.validate()
        return receipt


@dataclass(frozen=True)
class CorporateActionParseResult:
    actions: tuple[CorporateActionEvidence, ...]
    coverage: CorporateActionCoverageEvidence

    def validate(self) -> None:
        if type(self.actions) is not tuple:
            raise ContractError("corporate-action parse actions must be an exact tuple")
        for action in self.actions:
            if type(action) is not CorporateActionEvidence:
                raise ContractError("corporate-action parse result contains an invalid action")
            action.validate()
        if type(self.coverage) is not CorporateActionCoverageEvidence:
            raise ContractError("corporate-action parse result lacks exact coverage evidence")
        self.coverage.validate()

    def __iter__(self):
        return iter(self.actions)

    def __len__(self) -> int:
        return len(self.actions)

    def __getitem__(
        self, index: int | slice
    ) -> CorporateActionEvidence | tuple[CorporateActionEvidence, ...]:
        return self.actions[index]


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
    authorization_session: LocalNetworkExecutionSession | None = None,
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
    initial.validate_against_trusted_time(trusted_clock.now())
    request = initial
    seen_tokens: set[str] = set()
    for page_index in range(max_pages):
        request_attempt = assert_local_network_request(
            authorization_session,
            source="alpaca_corporate_actions",
            url=request.url(),
            timeout_seconds=timeout_seconds,
            max_response_bytes=MAX_RESPONSE_BYTES,
            page_index=page_index,
            expected_page_token=request.page_token,
            clock=trusted_clock,
        )
        evidence = _fetch_page(
            request,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            timeout_seconds=timeout_seconds,
            clock=trusted_clock,
            request_attempt=request_attempt,
        )
        source = "alpaca_corporate_actions"
        snapshot = snapshot_store._land_network_response(
            transport_evidence=evidence.transport_evidence,
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
) -> CorporateActionParseResult:
    page_list = tuple(pages)
    if not page_list:
        raise ContractError("corporate-action parse requires at least one landed page")
    if initial.page_token is not None:
        raise ContractError("corporate-action parse must begin with the initial request")
    initial_parameters = initial.parameters()
    expected_query = {
        name: [str(value)]
        for name, value in initial_parameters.items()
        if name != "page_token"
    }
    expected_token: str | None = None
    seen_next: set[str] = set()
    seen_actions: set[tuple[str, str]] = set()
    output: list[CorporateActionEvidence] = []
    acquisition_modes: set[str] = set()
    evidence_states: set[str] = set()
    acquisition_capability_ids: set[str] = set()
    all_synthetic_permit_ids: set[str] = set()
    for index, snapshot in enumerate(page_list):
        if snapshot.source != "alpaca_corporate_actions" or snapshot.http_status != 200:
            raise ContractError("corporate-action page source/status is not qualified")
        if snapshot.retrieved_at < initial.requested_at:
            raise ContractError("corporate-action receipt predates its request")
        query = parse_qs(urlparse(snapshot.url).query, keep_blank_values=True)
        actual_token = query.pop("page_token", [None])
        if query != expected_query:
            raise ContractError("corporate-action page request scope differs")
        if (index == 0 and actual_token[0] is not None) or (
            index > 0 and actual_token != [expected_token]
        ):
            raise ContractError("corporate-action page request token chain is broken")
        payload = _page_payload(snapshot)
        if snapshot.acquisition_mode == "NETWORK_AS_RECEIVED":
            if not snapshot.trust_eligible:
                raise ContractError("network corporate-action snapshot is not trust eligible")
            evidence_state = "NETWORK_AS_RECEIVED"
            synthetic_permit_ids: tuple[str, ...] = ()
        elif snapshot.acquisition_mode == "SYNTHETIC_DIRECT_NOT_AS_RECEIVED":
            if (
                snapshot.synthetic_permit_id is None
                or snapshot.acquisition_capability_id != snapshot.synthetic_permit_id
            ):
                raise ContractError("synthetic corporate-action snapshot binding is inconsistent")
            evidence_state = "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            synthetic_permit_ids = (snapshot.synthetic_permit_id,)
        else:
            raise ContractError("corporate-action snapshot acquisition mode is invalid")
        acquisition_modes.add(snapshot.acquisition_mode)
        evidence_states.add(evidence_state)
        acquisition_capability_ids.add(snapshot.acquisition_capability_id)
        all_synthetic_permit_ids.update(synthetic_permit_ids)
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
                symbol, involved_symbols = _symbols(row)
                if initial.symbols and not set(involved_symbols).intersection(
                    initial.symbols
                ):
                    raise ContractError(
                        "corporate-action response contains an unrequested symbol"
                    )
                evidence = CorporateActionEvidence(
                    provider_action_id=row["id"],
                    provider_group=group,
                    symbol=symbol,
                    involved_symbols=involved_symbols,
                    provider_process_date=process_date,
                    effective_session=_effective_session(row),
                    received_at=snapshot.retrieved_at,
                    snapshot_id=snapshot.snapshot_id,
                    raw_row_sha256=sha256_bytes(canonical_json_bytes(row)),
                    acquisition_mode=snapshot.acquisition_mode,
                    evidence_state=evidence_state,
                    acquisition_capability_id=snapshot.acquisition_capability_id,
                    synthetic_permit_ids=synthetic_permit_ids,
                    provider_process_date_is_causal=False,
                )
                evidence.validate()
                output.append(evidence)
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
    if len(acquisition_modes) != 1 or len(evidence_states) != 1:
        raise ContractError("corporate-action pages mix acquisition trust modes")
    coverage = CorporateActionCoverageEvidence.create(
        initial=initial,
        pages=page_list,
        acquisition_mode=next(iter(acquisition_modes)),
        evidence_state=next(iter(evidence_states)),
        acquisition_capability_ids=tuple(sorted(acquisition_capability_ids)),
        synthetic_permit_ids=tuple(sorted(all_synthetic_permit_ids)),
    )
    result = CorporateActionParseResult(actions=tuple(output), coverage=coverage)
    result.validate()
    return result


def _fetch_page(
    request: CorporateActionsRequest,
    *,
    api_key_id: str,
    api_secret_key: str,
    timeout_seconds: int,
    clock: TrustedClock,
    request_attempt: NetworkRequestAttempt,
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
        with open_without_redirects(
            http_request, timeout_seconds=timeout_seconds
        ) as response:
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
        transport_evidence=_bind_network_response(
            request_attempt,
            requested_url=url,
            response_url=response_url,
            http_status=status,
            raw=raw,
            headers=headers,
        ),
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


def _symbols(row: dict[str, object]) -> tuple[str | None, tuple[str, ...]]:
    primary: str | None = None
    symbols: list[str] = []
    for field in ("symbol", "old_symbol", "source_symbol", "acquiree_symbol", "initiating_symbol"):
        value = row.get(field)
        if value is None or value == "":
            continue
        if (
            type(value) is not str
            or not value
            or value != value.strip().upper()
        ):
            raise ContractError(
                f"corporate-action {field} must be exact canonical text"
            )
        symbols.append(value)
        if field == "symbol":
            primary = value
    distinct = tuple(sorted(set(symbols)))
    if primary is None and len(distinct) == 1:
        primary = distinct[0]
    return primary, distinct


def _effective_session(row: dict[str, object]) -> date | None:
    # Payable date is cash-distribution timing, not demonstrated economic
    # effect timing. Without an ex/effective date this evidence stays unresolved.
    sessions: list[date] = []
    for field in ("ex_date", "effective_date"):
        value = row.get(field)
        if value is not None:
            if type(value) is not str:
                raise ContractError(
                    f"corporate-action {field} must be exact text"
                )
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ContractError(f"corporate-action {field} is invalid") from exc
            if value != parsed.isoformat():
                raise ContractError(
                    f"corporate-action {field} must be canonical ISO date text"
                )
            sessions.append(parsed)
    if len(set(sessions)) > 1:
        raise ContractError(
            "corporate-action ex_date and effective_date conflict"
        )
    return sessions[0] if sessions else None
