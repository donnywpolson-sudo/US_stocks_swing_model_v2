"""Fail-closed validation for the V1 historical-provider decision package.

This module validates research and acquisition-planning records only.  It has no
provider transport, credential, purchase, data-ingestion, outcome, training,
evaluation, or backtest capability.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError


PROJECT = "US_stocks_swing_model_v2"
PHASE = "HISTORICAL_DATA_PROVIDER_EVALUATION_ACQUISITION_DECISION_V1"
PROVIDERS = ("CRSP", "NORGATE", "SHARADAR", "TIINGO")
REQUIREMENTS = (
    "R01_STABLE_HISTORICAL_SECURITY_IDENTITY",
    "R02_HISTORICAL_TICKER_VALIDITY",
    "R03_HISTORICAL_EXCHANGE_LISTING_VALIDITY",
    "R04_HISTORICAL_SECURITY_TYPE_CLASSIFICATION",
    "R05_LISTING_INACTIVE_DELISTING_HISTORY",
    "R06_RAW_UNADJUSTED_DAILY_OHLCV",
    "R07_ACTIVE_SECURITY_COVERAGE",
    "R08_INACTIVE_DELISTED_SECURITY_COVERAGE",
    "R09_CORPORATE_ACTION_EVENTS",
    "R10_DELISTING_TERMINAL_EVENT_METADATA",
    "R11_DAILY_BAR_SESSION_TIMESTAMP_SEMANTICS",
    "R12_REVISION_VERSION_SEMANTICS",
    "R13_CAUSAL_AVAILABILITY_SEMANTICS",
    "R14_FULL_LINEAGE_PROVENANCE",
    "R15_LOCAL_RESEARCH_LICENSE",
)
CELL_STATUSES = {
    "FAIL",
    "NOT_APPLICABLE",
    "PASS_PRIMARY_EVIDENCE",
    "PASS_WITH_LIMITATION",
    "UNRESOLVED",
}
DECISION_STATUSES = {
    "BLOCKED_RESEARCH_ACCESS",
    "MULTI_PROVIDER_REQUIRED",
    "NO_QUALIFIED_OPTION_FOUND",
    "READY_PENDING_VENDOR_CONFIRMATION",
    "READY_TO_ACQUIRE",
}
PRIMARY_EVIDENCE_STATUSES = {
    "CURRENT_PRIMARY_EVIDENCE",
    "CURRENT_PRIMARY_EVIDENCE_WITH_LIMITATION",
}
LICENSE_FIELDS = (
    "individual_eligibility",
    "professional_or_commercial_eligibility",
    "local_storage_during_subscription",
    "automated_api_or_bulk_processing",
    "quantitative_research",
    "derived_data",
    "redistribution",
    "cloud_storage",
    "multiple_machine",
    "user_count",
    "post_cancellation_retention",
    "attribution",
    "additional_exchange_licenses",
)
LICENSE_STATUSES = {
    "CONFIRMED_ALLOWED",
    "CONFIRMED_RESTRICTED",
    "NOT_APPLICABLE",
    "UNRESOLVED_REQUIRES_PROVIDER_CONFIRMATION",
}
PRICING_STATUSES = {"PUBLIC_PRICE_CONFIRMED", "QUOTE_REQUIRED", "UNRESOLVED"}
OFFICIAL_HOST_SUFFIXES = (
    "crsp.org",
    "data.nasdaq.com",
    "docs.data.nasdaq.com",
    "indexes.morningstar.com",
    "norgatedata.com",
    "sharadar.com",
    "tiingo.com",
)
RECORDS = {
    "evidence": (
        "config/historical_provider_evidence_registry_v1.json",
        "HISTORICAL_PROVIDER_EVIDENCE_REGISTRY_V1",
        "registry_id",
    ),
    "evaluation": (
        "config/historical_provider_evaluation_v1.json",
        "HISTORICAL_PROVIDER_EVALUATION_V1",
        "evaluation_id",
    ),
    "comparison": (
        "config/historical_provider_comparison_v1.json",
        "HISTORICAL_PROVIDER_COMPARISON_V1",
        "comparison_id",
    ),
    "decision": (
        "config/historical_acquisition_decision_v1.json",
        "HISTORICAL_ACQUISITION_DECISION_V1",
        "decision_id",
    ),
    "plan": (
        "config/historical_acquisition_plan_v1.json",
        "HISTORICAL_ACQUISITION_PLAN_V1",
        "plan_id",
    ),
}


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError(f"{field} must be nonempty canonical text")
    return value


def _sorted_texts(value: object, field: str, *, empty: bool = False) -> tuple[str, ...]:
    if type(value) is not list or (not empty and not value):
        raise ContractError(f"{field} must be a canonical list")
    result = tuple(_text(item, field) for item in value)
    if result != tuple(sorted(set(result))):
        raise ContractError(f"{field} must be sorted and unique")
    return result


def _load_record(path: Path, record_type: str, id_field: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"provider decision record is unavailable: {path}") from exc
    if type(payload) is not dict:
        raise ContractError("provider decision record must be an object")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("phase") != PHASE
        or payload.get("record_type") != record_type
    ):
        raise ContractError(f"provider decision record identity differs: {path.name}")
    record_id = require_sha256(payload.get(id_field), id_field)
    unsigned = dict(payload)
    unsigned.pop(id_field)
    if record_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError(f"{id_field} differs from record content")
    return payload


def _official_url(value: object) -> str:
    url = _text(value, "evidence URL")
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in OFFICIAL_HOST_SUFFIXES
    ):
        raise ContractError(f"evidence URL is not an approved official host: {url}")
    return url


def _validate_boundaries(payload: Mapping[str, object], field: str) -> None:
    boundaries = payload.get(field)
    if type(boundaries) is not dict or not boundaries:
        raise ContractError(f"{field} must be explicit")
    if any(value is not False for value in boundaries.values()):
        raise ContractError(f"{field} cannot authorize acquisition or research")


def _validate_evidence(payload: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    evidence = payload.get("evidence")
    if type(evidence) is not list or not evidence:
        raise ContractError("evidence registry must contain evidence")
    result: dict[str, Mapping[str, object]] = {}
    urls: set[str] = set()
    ids: list[str] = []
    for item in evidence:
        if type(item) is not dict:
            raise ContractError("evidence entries must be objects")
        evidence_id = _text(item.get("evidence_id"), "evidence_id")
        ids.append(evidence_id)
        if evidence_id in result:
            raise ContractError("evidence IDs must be unique")
        provider = _text(item.get("provider_id"), "evidence provider_id")
        if provider not in PROVIDERS:
            raise ContractError("evidence provider is outside the evaluated census")
        url = _official_url(item.get("url"))
        if url in urls:
            raise ContractError("evidence URLs must not be duplicated")
        urls.add(url)
        requirement_ids = _sorted_texts(
            item.get("mandatory_requirement_ids"), "evidence requirement IDs"
        )
        if not set(requirement_ids) <= set(REQUIREMENTS):
            raise ContractError("evidence references an unknown requirement")
        if item.get("primary_source") is not True:
            raise ContractError("registered evidence must be primary source evidence")
        if item.get("status") not in PRIMARY_EVIDENCE_STATUSES:
            raise ContractError("evidence status is invalid")
        for field in (
            "product_id",
            "evidence_type",
            "title",
            "retrieved_at",
            "claim_supported",
            "evidence_summary",
        ):
            _text(item.get(field), f"evidence {field}")
        result[evidence_id] = item
    if ids != sorted(ids):
        raise ContractError("evidence entries must be ordered by evidence_id")
    _validate_boundaries(payload, "authorization_boundary")
    return result


def _validate_resolution(cell: Mapping[str, object], provider: str, requirement: str) -> None:
    resolution = cell.get("resolution")
    if type(resolution) is not dict:
        raise ContractError(f"{provider}/{requirement} requires an explicit resolution")
    for field in (
        "unknown_or_failure",
        "public_evidence_gap",
        "mandatory_impact",
        "vendor_question",
    ):
        _text(resolution.get(field), f"{provider}/{requirement}.{field}")
    if resolution.get("purchase_prohibited_until_resolved") is not True:
        raise ContractError(f"{provider}/{requirement} must prohibit premature purchase")


def _validate_evaluation(
    payload: Mapping[str, object], evidence: Mapping[str, Mapping[str, object]]
) -> dict[tuple[str, str], Mapping[str, object]]:
    catalog = payload.get("mandatory_requirements")
    if type(catalog) is not list:
        raise ContractError("mandatory requirement catalog is invalid")
    catalog_ids = tuple(
        item.get("requirement_id") for item in catalog if type(item) is dict
    )
    if catalog_ids != REQUIREMENTS:
        raise ContractError("mandatory requirement catalog differs from frozen V1")
    providers = payload.get("providers")
    if type(providers) is not list:
        raise ContractError("provider evaluation census is invalid")
    provider_ids = tuple(
        item.get("provider_id") for item in providers if type(item) is dict
    )
    if provider_ids != PROVIDERS:
        raise ContractError("required provider evaluation census differs")
    cells: dict[tuple[str, str], Mapping[str, object]] = {}
    for provider in providers:
        assert isinstance(provider, dict)
        provider_id = provider["provider_id"]
        license_assessment = provider.get("license")
        if type(license_assessment) is not dict or tuple(license_assessment) != LICENSE_FIELDS:
            raise ContractError(f"{provider_id} license assessment is incomplete")
        if any(value not in LICENSE_STATUSES for value in license_assessment.values()):
            raise ContractError(f"{provider_id} license status is outside the frozen taxonomy")
        pricing = provider.get("pricing")
        if type(pricing) is not dict or pricing.get("classification") not in PRICING_STATUSES:
            raise ContractError(f"{provider_id} pricing classification is invalid")
        evaluations = provider.get("mandatory_evaluations")
        if type(evaluations) is not list:
            raise ContractError("provider mandatory evaluations are invalid")
        cell_ids = tuple(
            item.get("requirement_id") for item in evaluations if type(item) is dict
        )
        if cell_ids != REQUIREMENTS:
            raise ContractError(f"{provider_id} lacks an exact mandatory evaluation")
        for cell in evaluations:
            assert isinstance(cell, dict)
            requirement = cell["requirement_id"]
            status = cell.get("status")
            if status not in CELL_STATUSES:
                raise ContractError("provider cell status is invalid")
            evidence_ids = _sorted_texts(
                cell.get("evidence_ids"),
                f"{provider_id}/{requirement}.evidence_ids",
                empty=status in {"FAIL", "UNRESOLVED", "NOT_APPLICABLE"},
            )
            if any(evidence_id not in evidence for evidence_id in evidence_ids):
                raise ContractError("provider cell references unknown evidence")
            if any(
                evidence[evidence_id]["provider_id"] != provider_id
                for evidence_id in evidence_ids
            ):
                raise ContractError("provider cell references another provider's evidence")
            if status == "PASS_PRIMARY_EVIDENCE" and not evidence_ids:
                raise ContractError("primary evidence PASS requires primary evidence")
            if status in {"FAIL", "UNRESOLVED"}:
                _validate_resolution(cell, provider_id, requirement)
            elif cell.get("resolution") is not None:
                raise ContractError("non-blocking provider cell cannot contain a resolution")
            _text(cell.get("assessment"), f"{provider_id}/{requirement}.assessment")
            cells[(provider_id, requirement)] = cell
        if provider.get("fully_qualified") is not False:
            raise ContractError("no evaluated provider may be marked fully qualified")
    _validate_boundaries(payload, "authorization_boundary")
    return cells


def _validate_comparison(
    payload: Mapping[str, object], cells: Mapping[tuple[str, str], Mapping[str, object]]
) -> None:
    rows = payload.get("rows")
    if type(rows) is not list:
        raise ContractError("comparison rows are invalid")
    row_ids = tuple(item.get("requirement_id") for item in rows if type(item) is dict)
    if row_ids != REQUIREMENTS:
        raise ContractError("comparison does not cover every mandatory requirement")
    for row in rows:
        assert isinstance(row, dict)
        requirement = row["requirement_id"]
        statuses = row.get("provider_statuses")
        if type(statuses) is not dict or tuple(statuses) != PROVIDERS:
            raise ContractError("comparison provider order differs")
        for provider, status in statuses.items():
            if status != cells[(provider, requirement)]["status"]:
                raise IntegrityError("comparison status differs from provider evaluation")
    if payload.get("fully_qualified_provider_ids") != []:
        raise ContractError("comparison cannot claim a fully qualified provider")
    declared_counts = payload.get("mandatory_gate_counts")
    if type(declared_counts) is not dict or tuple(declared_counts) != PROVIDERS:
        raise ContractError("comparison mandatory gate counts are incomplete")
    for provider in PROVIDERS:
        actual = Counter(
            cells[(provider, requirement)]["status"] for requirement in REQUIREMENTS
        )
        expected = declared_counts[provider]
        if type(expected) is not dict or expected != {
            status: actual.get(status, 0)
            for status in (
                "PASS_PRIMARY_EVIDENCE",
                "PASS_WITH_LIMITATION",
                "FAIL",
                "UNRESOLVED",
            )
        }:
            raise IntegrityError("comparison mandatory gate count differs")


def _validate_decision(
    payload: Mapping[str, object],
    cells: Mapping[tuple[str, str], Mapping[str, object]],
) -> None:
    status = payload.get("status")
    if status not in DECISION_STATUSES:
        raise ContractError("acquisition decision status is invalid")
    if status != "NO_QUALIFIED_OPTION_FOUND":
        raise ContractError("checked-in evidence does not support an acquisition-ready status")
    if payload.get("recommended_purchase") is not None:
        raise ContractError("no purchase may be recommended while mandatory gaps remain")
    unresolved = payload.get("mandatory_unresolved_items")
    if type(unresolved) is not list or not unresolved:
        raise ContractError("decision must preserve every mandatory unresolved item")
    question_ids = [item.get("question_id") for item in unresolved if type(item) is dict]
    if len(question_ids) != len(unresolved) or question_ids != sorted(set(question_ids)):
        raise ContractError("vendor questions must be ordered and unique")
    covered_pairs: set[tuple[str, str]] = set()
    for item in unresolved:
        assert isinstance(item, dict)
        _text(item.get("question"), "vendor confirmation question")
        provider = _text(item.get("provider_id"), "vendor question provider_id")
        if provider not in PROVIDERS:
            raise ContractError("vendor question provider is outside the evaluated census")
        requirement_ids = _sorted_texts(
            item.get("requirement_ids"), "vendor question requirement_ids"
        )
        if not set(requirement_ids) <= set(REQUIREMENTS):
            raise ContractError("vendor question references an unknown requirement")
        covered_pairs.update((provider, requirement) for requirement in requirement_ids)
        if item.get("mandatory") is not True or item.get("purchase_prohibited") is not True:
            raise ContractError("mandatory vendor question must prohibit purchase")
    unresolved_pairs = {
        pair for pair, cell in cells.items() if cell["status"] == "UNRESOLVED"
    }
    if not unresolved_pairs <= covered_pairs:
        raise ContractError("final decision omits an unresolved mandatory provider cell")
    _validate_boundaries(payload, "authorization_boundary")


def _validate_plan(payload: Mapping[str, object], decision_id: str) -> None:
    if payload.get("decision_id") != decision_id:
        raise IntegrityError("conditional acquisition plan is not bound to the decision")
    if payload.get("plan_status") != "CONDITIONAL_NOT_EXECUTABLE":
        raise ContractError("acquisition plan must remain conditional")
    if payload.get("provider_id") != "CRSP" or payload.get("product_code") != "C6Z":
        raise ContractError("conditional plan must name the smallest CRSP finalist")
    sequence = payload.get("subsequent_ingestion_sequence")
    if type(sequence) is not list or tuple(
        item.get("step") for item in sequence if type(item) is dict
    ) != tuple(range(1, 21)):
        raise ContractError("future ingestion sequence must contain exact steps 1 through 20")
    prerequisites = payload.get("purchase_prerequisites")
    if type(prerequisites) is not list or not prerequisites:
        raise ContractError("conditional plan must preserve purchase prerequisites")
    if any(item.get("status") != "UNRESOLVED" for item in prerequisites):
        raise ContractError("conditional plan cannot treat purchase prerequisites as resolved")
    _validate_boundaries(payload, "authorization_boundary")


def load_historical_provider_decision_package(root: Path) -> dict[str, object]:
    """Load and cross-check the complete provider decision package."""

    root = Path(root).resolve()
    loaded: dict[str, dict[str, object]] = {}
    for name, (relative, record_type, id_field) in RECORDS.items():
        loaded[name] = _load_record(root / relative, record_type, id_field)
    evidence = _validate_evidence(loaded["evidence"])
    cells = _validate_evaluation(loaded["evaluation"], evidence)
    if loaded["evaluation"].get("evidence_registry_id") != loaded["evidence"]["registry_id"]:
        raise IntegrityError("provider evaluation is not bound to its evidence registry")
    _validate_comparison(loaded["comparison"], cells)
    if loaded["comparison"].get("evaluation_id") != loaded["evaluation"]["evaluation_id"]:
        raise IntegrityError("provider comparison is not bound to its evaluation")
    _validate_decision(loaded["decision"], cells)
    if loaded["decision"].get("comparison_id") != loaded["comparison"]["comparison_id"]:
        raise IntegrityError("acquisition decision is not bound to its comparison")
    _validate_plan(loaded["plan"], loaded["decision"]["decision_id"])
    return {
        "status": loaded["decision"]["status"],
        "provider_count": len(PROVIDERS),
        "requirement_count": len(REQUIREMENTS),
        "matrix_cell_count": len(cells),
        "evidence_count": len(evidence),
        "record_ids": {
            "evidence": loaded["evidence"]["registry_id"],
            "evaluation": loaded["evaluation"]["evaluation_id"],
            "comparison": loaded["comparison"]["comparison_id"],
            "decision": loaded["decision"]["decision_id"],
            "plan": loaded["plan"]["plan_id"],
        },
    }
