from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .alpaca_free_bounded import retry_disposition, validate_bars_payload
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError, NetworkGuardError
from .free_source_evidence import (
    RawEvidenceReceipt,
    RawEvidenceStore,
    SourceRequestPlan,
    parse_alpha_vantage_listing_csv,
    parse_alpaca_asset_master,
    parse_corporate_action_groups,
    parse_nasdaq_symbol_directory,
)
from .providers.corporate_actions import ACTION_GROUPS
from .providers.http import open_without_redirects
from .providers.snapshots import (
    ALLOWED_RESPONSE_HEADERS,
    NetworkAcquisitionRegistry,
    normalize_response_headers,
)


AUTHORIZATION_ENVIRONMENT = "FREE_SOURCE_QUALIFICATION_APPROVED"


@dataclass(frozen=True)
class AcquisitionAttemptResult:
    state: str
    plan_id: str
    receipt: RawEvidenceReceipt
    terminal_page: bool
    next_page_token: str | None
    retry_state: str
    retry_next_attempt: int | None
    retry_delay_seconds: float | None
    accepted_records: int
    live_source_validation: str

    def summary(self) -> dict[str, object]:
        return {
            "state": self.state,
            "plan_id": self.plan_id,
            "receipt_id": self.receipt.receipt_id,
            "raw_sha256": self.receipt.raw_sha256,
            "http_status": self.receipt.http_status,
            "terminal_page": self.terminal_page,
            "next_page_token": self.next_page_token,
            "retry_state": self.retry_state,
            "retry_next_attempt": self.retry_next_attempt,
            "retry_delay_seconds": self.retry_delay_seconds,
            "accepted_records": self.accepted_records,
            "live_source_validation": self.live_source_validation,
            "training_authorized": False,
            "evaluation_authorized": False,
        }


def _parse_success(
    plan: SourceRequestPlan,
    raw: bytes,
    *,
    retrieved_at: datetime,
) -> tuple[str, str, str | None, int, datetime | None]:
    if plan.source == "alpaca_free_bounded_bars":
        payload = json.loads(raw)
        query = dict(plan.canonical_query)
        validation = validate_bars_payload(
            payload,
            expected_symbols=tuple(query["symbols"].split(",")),
        )
        if not validation["accepted"]:
            return (
                "PARSED_QUARANTINED",
                str(validation["validation_status"]),
                None,
                0,
                None,
            )
        return (
            "PARSED",
            "PASS",
            validation["next_page_token"],
            int(validation["row_count"]),
            None,
        )
    if plan.source == "alpha_vantage_listing_status":
        parsed = parse_alpha_vantage_listing_csv(raw)
        return "PARSED", "CANDIDATE_PENDING_LIVE_SEMANTICS_VALIDATION", None, int(parsed["row_count"]), None
    if plan.source == "alpaca_free_bounded_assets":
        assets = parse_alpaca_asset_master(raw)
        return "PARSED", "PASS", None, len(assets), None
    if plan.source == "alpaca_free_bounded_corporate_actions":
        parsed = parse_corporate_action_groups(raw, known_groups=ACTION_GROUPS)
        return (
            "PARSED",
            "PASS_WITH_UNKNOWN_PRESERVED" if parsed["unknown_actions"] else "PASS",
            parsed["next_page_token"],
            len(parsed["known_actions"]) + len(parsed["unknown_actions"]),
            None,
        )
    if plan.source in {"nasdaq_free_bounded_listed", "nasdaq_free_bounded_otherlisted"}:
        source_name = (
            "nasdaqlisted.txt"
            if plan.source == "nasdaq_free_bounded_listed"
            else "otherlisted.txt"
        )
        parsed = parse_nasdaq_symbol_directory(
            raw,
            source_name=source_name,
            retrieved_at=retrieved_at,
        )
        created = datetime.fromisoformat(parsed["file_created_at"].replace("Z", "+00:00"))
        return "PARSED", "PASS", None, int(parsed["row_count"]), created
    raise ContractError("source adapter is not implemented")


def execute_one_source_request(
    *,
    plan: SourceRequestPlan,
    approved_plan_id: str,
    evidence_store: RawEvidenceStore,
    network_registry: NetworkAcquisitionRegistry,
    clock: TrustedClock,
    network_enabled: bool,
    alpaca_key_id: str | None = None,
    alpaca_secret_key: str | None = None,
    alpha_vantage_key: str | None = None,
    page_index: int = 0,
    requested_page_token: str | None = None,
    retry_attempt: int = 1,
    parent_request_id: str | None = None,
) -> AcquisitionAttemptResult:
    plan.validate()
    network_registry.validate()
    if network_registry.allowed_origin_paths.get(plan.source) != plan.endpoint:
        raise PermissionError("source request is outside the checked network registry")
    if not network_enabled or os.environ.get(AUTHORIZATION_ENVIRONMENT) != "YES":
        raise NetworkGuardError(
            f"network disabled; require explicit flag and {AUTHORIZATION_ENVIRONMENT}=YES"
        )
    if approved_plan_id != plan.plan_id:
        raise PermissionError("approved plan ID differs from the exact source request plan")
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise PermissionError("free-source execution requires the production UTC clock")
    if plan.provider == "alpaca":
        missing = [
            name
            for name, value in (
                ("APCA_API_KEY_ID", alpaca_key_id),
                ("APCA_API_SECRET_KEY", alpaca_secret_key),
            )
            if not value
        ]
        if missing:
            raise ContractError("missing credential variable: " + ", ".join(missing))
        headers = {
            "APCA-API-KEY-ID": alpaca_key_id,
            "APCA-API-SECRET-KEY": alpaca_secret_key,
        }
        transport_url = plan.transport_url(page_token=requested_page_token)
    elif plan.provider == "alpha_vantage":
        if not alpha_vantage_key:
            raise ContractError("missing credential variable: ALPHA_VANTAGE_API_KEY")
        headers = {}
        transport_url = plan.transport_url(
            secret=alpha_vantage_key,
            page_token=requested_page_token,
        )
    else:
        headers = {}
        transport_url = plan.transport_url(page_token=requested_page_token)
    requested_at = trusted_clock.now()
    if plan.source == "alpaca_free_bounded_bars":
        query = dict(plan.canonical_query)
        try:
            requested_end = datetime.fromisoformat(query["end"]).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError) as exc:
            raise ContractError("Alpaca bars end is invalid") from exc
        if requested_end > requested_at - timedelta(minutes=20):
            raise ContractError("Alpaca bars end is inside the free SIP delay boundary")
    request = Request(transport_url, headers=headers, method="GET")
    try:
        with open_without_redirects(request, timeout_seconds=plan.timeout_seconds) as response:
            raw = response.read(plan.maximum_response_bytes + 1)
            response_url = str(response.geturl())
            http_status = int(response.status)
            response_headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in ALLOWED_RESPONSE_HEADERS
            }
    except HTTPError as response:
        raw = response.read(plan.maximum_response_bytes + 1)
        response_url = str(response.geturl())
        http_status = int(response.code)
        response_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in ALLOWED_RESPONSE_HEADERS
        }
    except URLError:
        raise ContractError(
            "provider transport failed before an HTTP response; no evidence was landed"
        ) from None
    if response_url != transport_url:
        raise ContractError("provider response redirected away from the approved origin")
    if not raw or len(raw) > plan.maximum_response_bytes:
        raise ContractError("provider response is empty or exceeds the bounded byte limit")
    sensitive_values = tuple(
        value.encode("utf-8")
        for value in (alpaca_key_id, alpaca_secret_key, alpha_vantage_key)
        if value
    )
    if any(value in raw for value in sensitive_values):
        raise ContractError("provider response contained credential material and was not landed")
    retrieved_at = trusted_clock.now()
    normalized_headers = normalize_response_headers(response_headers)
    parsed_status = "PROVIDER_ERROR_PRESERVED"
    validation_status = f"HTTP_{http_status}"
    next_page_token: str | None = None
    accepted_records = 0
    source_file_created_at = None
    parse_error = False
    if 200 <= http_status <= 299:
        try:
            (
                parsed_status,
                validation_status,
                next_page_token,
                accepted_records,
                source_file_created_at,
            ) = _parse_success(plan, raw, retrieved_at=retrieved_at)
        except (ContractError, json.JSONDecodeError, UnicodeDecodeError):
            parsed_status = "PARSE_FAILED_RAW_PRESERVED"
            validation_status = "QUARANTINED_UNRECOGNIZED_RESPONSE"
            parse_error = True
    receipt = evidence_store.append(
        plan=plan,
        raw=raw,
        requested_at=requested_at,
        retrieved_at=retrieved_at,
        response_headers=normalized_headers,
        http_status=http_status,
        page_index=page_index,
        requested_page_token=requested_page_token,
        next_page_token=next_page_token,
        retry_attempt=retry_attempt,
        parent_request_id=parent_request_id,
        parsing_status=parsed_status,
        validation_status=validation_status,
        source_file_created_at=source_file_created_at,
    )
    retry_after = None
    if "x-ratelimit-reset" in normalized_headers and http_status == 429:
        try:
            retry_after = max(
                0.0,
                float(normalized_headers["x-ratelimit-reset"]) - retrieved_at.timestamp(),
            )
        except ValueError:
            retry_after = None
    retry = retry_disposition(
        http_status=http_status,
        attempt_number=retry_attempt,
        request_id=plan.plan_id,
        retry_after_seconds=retry_after,
    )
    accepted = 200 <= http_status <= 299 and not parse_error and validation_status.startswith("PASS")
    terminal = accepted and next_page_token is None
    state = "PAGE_ACCEPTED" if accepted else "ATTEMPT_PRESERVED_NOT_ACCEPTED"
    return AcquisitionAttemptResult(
        state=state,
        plan_id=plan.plan_id,
        receipt=receipt,
        terminal_page=terminal,
        next_page_token=next_page_token,
        retry_state=retry.state,
        retry_next_attempt=retry.next_attempt,
        retry_delay_seconds=retry.delay_seconds,
        accepted_records=accepted_records if accepted else 0,
        live_source_validation=(
            "CANDIDATE_PENDING_SEMANTICS_VALIDATION"
            if plan.source == "alpha_vantage_listing_status" and accepted
            else "OBSERVED_RESPONSE_PRESERVED"
        ),
    )
