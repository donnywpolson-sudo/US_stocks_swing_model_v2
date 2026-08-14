"""Fail-closed validation for the CRSP vendor-response readiness package.

This module validates intake and purchase-decision metadata only.  It has no
vendor transport, credential, account, agreement, payment, download,
commercial-data ingestion, outcome, training, evaluation, or backtest path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError


PROJECT = "US_stocks_swing_model_v2"
PHASE = "CRSP_VENDOR_RESPONSE_ADJUDICATION_PURCHASE_AUTHORIZATION_DECISION_V1"
PROVIDER_EVALUATION_COMMIT = "2b2281119295d273ac0f868b20cf9d4aa29f3e24"
MISSING_REASON = "MISSING_VENDOR_RESPONSE_PACKAGE"
DECISION_STATUS = "REQUIRES_FOLLOW_UP"
PHASE_SPECIFICATION_SHA256 = "b1a333b1a5663dfd3d5a6c4d14d193263a580df33d5254f4c5fe0e8474307b30"
PHASE_SPECIFICATION_BYTES = 47433
EXPECTED_SEARCH_SCOPE = (
    "CRSP_ADJUDICATION_WORKTREE_TRACKED_PATHS",
    "CURRENT_CONVERSATION_ATTACHMENT_SET",
    "PROVIDER_EVALUATION_WORKTREE_TRACKED_PATHS",
    "REPOSITORY_DESIGNATED_INTAKE_DIRECTORIES",
)
EXPECTED_GATES = (
    "G01_STABLE_HISTORICAL_SECURITY_IDENTITY",
    "G02_HISTORICAL_TICKER_VALIDITY",
    "G03_HISTORICAL_EXCHANGE_LISTING_VALIDITY",
    "G04_HISTORICAL_SECURITY_TYPE",
    "G05_ACTIVE_INACTIVE_DELISTED_COVERAGE",
    "G06_RAW_DAILY_OHLCV",
    "G07_CORPORATE_ACTION_COVERAGE",
    "G08_TERMINAL_EVENT_METADATA",
    "G09_SESSION_TIMESTAMP_SEMANTICS",
    "G10_REVISION_VERSION_SEMANTICS",
    "G11_HISTORICAL_RELEASE_ARCHIVE",
    "G12_CAUSAL_AVAILABILITY_SEMANTICS",
    "G13_FULL_PROVENANCE",
    "G14_PROGRAMMATIC_LOCAL_USE",
    "G15_IMMUTABLE_LOCAL_LANDING",
    "G16_DERIVED_PANEL_RIGHTS",
    "G17_BACKUP_RIGHTS",
    "G18_POST_CANCELLATION_RETENTION",
    "G19_INDIVIDUAL_ELIGIBILITY",
    "G20_COMPLETE_ITEMIZED_PRICE",
    "G21_EXCHANGE_OBLIGATIONS",
    "G22_REDISTRIBUTION_RESTRICTIONS",
)
EXPECTED_MISSING_DOCUMENTS = (
    "CRSP_BINDING_LICENSE_OR_PROPOSED_AGREEMENT",
    "CRSP_C6Z_PRODUCT_SCHEDULE_OR_ENTITLEMENT",
    "CRSP_C6Z_TECHNICAL_RESPONSE_OR_SCHEMA_PACKAGE",
    "CRSP_DELIVERY_AND_ARCHIVE_ENTITLEMENT",
    "CRSP_ITEMIZED_QUOTE_OR_ORDER_FORM",
    "CRSP_WRITTEN_VENDOR_RESPONSE",
)
EVIDENCE_HIERARCHY = (
    "01_EXECUTABLE_OR_SIGNED_CONTRACT_LANGUAGE",
    "02_PRODUCT_SPECIFIC_ADDENDUM",
    "03_INCORPORATED_ORDER_FORM",
    "04_FORMAL_PRODUCT_SPECIFIC_VENDOR_RESPONSE",
    "05_CURRENT_OFFICIAL_SCHEMA_OR_DATA_DICTIONARY",
    "06_CURRENT_OFFICIAL_METHODOLOGY_GUIDE",
    "07_CURRENT_OFFICIAL_PRODUCT_DOCUMENTATION",
    "08_CURRENT_OFFICIAL_PRICING_DOCUMENT",
    "09_GENERAL_OFFICIAL_MARKETING_MATERIAL",
    "10_PRIOR_PUBLIC_EVIDENCE_REGISTRY",
)
EXPECTED_FUTURE_STEP_NAMES = (
    "VERIFY_SIGNED_ENTITLEMENT",
    "SECURE_CREDENTIAL_HANDLING",
    "IMMUTABLE_LANDING_ROOT",
    "AUTHORIZED_DOWNLOAD_CENSUS",
    "PRESERVE_ORIGINAL_FILES",
    "COMPUTE_SOURCE_HASHES",
    "CAPTURE_SCHEMA_VERSIONS",
    "CAPTURE_RELEASE_MANIFESTS",
    "CAPTURE_PUBLICATION_CORRECTION_METADATA",
    "IMPORT_PERMNO_PERMCO_IDENTITY",
    "IMPORT_HISTORICAL_TICKER_INTERVALS",
    "IMPORT_HISTORICAL_EXCHANGE_LISTING_INTERVALS",
    "IMPORT_SECURITY_TYPES",
    "IMPORT_RAW_OHLCV",
    "IMPORT_DISTRIBUTIONS_CORPORATE_ACTIONS",
    "IMPORT_DELISTING_TERMINAL_EVENTS",
    "QUARANTINE_OUTCOME_LIKE_FIELDS",
    "BIND_EXCHANGE_CALENDARS",
    "RUN_SOURCE_ADMISSION_GATEWAY",
    "RUN_FULL_CORPUS_VALIDATION",
    "QUARANTINE_UNRESOLVED_RECORDS",
    "BUILD_CANONICAL_PANEL",
    "RUN_REAL_SOURCE_PREFIX_INVARIANCE",
    "RUN_REAL_SOURCE_FUTURE_MUTATION_INVARIANCE",
    "RUN_CURRENT_SNAPSHOT_POISONING",
    "RUN_REVISION_ISOLATION",
    "RERUN_SOURCE_READINESS_GATE",
    "KEEP_REAL_OUTCOMES_DISABLED",
)
RECORDS = {
    "intake": (
        "config/crsp_vendor_response_intake_v1.json",
        "CRSP_VENDOR_RESPONSE_INTAKE_V1",
        "intake_id",
    ),
    "evidence": (
        "config/crsp_vendor_evidence_registry_v1.json",
        "CRSP_VENDOR_EVIDENCE_REGISTRY_V1",
        "registry_id",
    ),
    "questions": (
        "config/crsp_follow_up_questions_v1.json",
        "CRSP_FOLLOW_UP_QUESTIONS_V1",
        "question_package_id",
    ),
    "plan": (
        "config/crsp_future_ingestion_plan_v1.json",
        "CRSP_FUTURE_INGESTION_PLAN_V1",
        "plan_id",
    ),
    "decision": (
        "config/crsp_purchase_authorization_decision_v1.json",
        "CRSP_PURCHASE_AUTHORIZATION_DECISION_V1",
        "decision_id",
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
        raise IntegrityError(f"CRSP adjudication record is unavailable: {path}") from exc
    if type(payload) is not dict:
        raise ContractError("CRSP adjudication record must be an object")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("phase") != PHASE
        or payload.get("record_type") != record_type
    ):
        raise ContractError(f"CRSP adjudication record identity differs: {path.name}")
    record_id = require_sha256(payload.get(id_field), id_field)
    unsigned = dict(payload)
    unsigned.pop(id_field)
    if record_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError(f"{id_field} differs from record content")
    return payload


def _validate_boundaries(payload: Mapping[str, object]) -> None:
    boundaries = payload.get("authorization_boundary")
    if type(boundaries) is not dict or not boundaries:
        raise ContractError("authorization_boundary must be explicit")
    if any(value is not False for value in boundaries.values()):
        raise ContractError("CRSP readiness records cannot authorize purchase or research")


def _validate_intake(payload: Mapping[str, object]) -> dict[str, str]:
    if payload.get("provider_evaluation_commit") != PROVIDER_EVALUATION_COMMIT:
        raise IntegrityError("CRSP intake is not bound to the frozen provider checkpoint")
    require_sha256(
        payload.get("provider_evaluation_decision_id"),
        "provider_evaluation_decision_id",
    )
    _text(payload.get("intake_cutoff"), "intake_cutoff")
    if _sorted_texts(payload.get("search_scope"), "search_scope") != EXPECTED_SEARCH_SCOPE:
        raise ContractError("intake search scope differs from the bounded discovery")
    if payload.get("vendor_documents") != []:
        raise ContractError("missing-package intake cannot contain vendor documents")
    if payload.get("substantive_vendor_document_count") != 0:
        raise ContractError("missing-package intake vendor-document count must be zero")
    inputs = payload.get("non_vendor_inputs")
    if type(inputs) is not list or not inputs:
        raise ContractError("intake must preserve the supplied non-vendor instruction")
    ids: list[str] = []
    hashes: dict[str, str] = {}
    for item in inputs:
        if type(item) is not dict:
            raise ContractError("non-vendor input entries must be objects")
        input_id = _text(item.get("input_id"), "input_id")
        ids.append(input_id)
        if input_id in hashes:
            raise ContractError("non-vendor input IDs must be unique")
        content_hash = require_sha256(item.get("content_sha256"), "content_sha256")
        if content_hash in hashes.values():
            raise ContractError("duplicate non-vendor input bytes must be classified once")
        hashes[input_id] = content_hash
        if item.get("classification") != "PHASE_SPECIFICATION_NOT_VENDOR_EVIDENCE":
            raise ContractError("phase specification cannot be treated as vendor evidence")
        if item.get("binding_status") != "AUTHORITATIVE_USER_PHASE_INSTRUCTION":
            raise ContractError("non-vendor input binding status differs")
        if (
            input_id != "INPUT-001"
            or item.get("file_name") != "pasted-text.txt"
            or content_hash != PHASE_SPECIFICATION_SHA256
            or item.get("bytes") != PHASE_SPECIFICATION_BYTES
            or item.get("document_date") is not None
        ):
            raise IntegrityError("supplied phase specification binding differs")
        for field in (
            "file_name",
            "file_type",
            "source",
            "received_at",
            "version",
            "sender_class",
            "duplicate_status",
            "superseded_status",
        ):
            _text(item.get(field), f"non_vendor_inputs.{field}")
        _sorted_texts(
            item.get("requirement_categories"),
            "non_vendor_inputs.requirement_categories",
        )
    if ids != sorted(ids):
        raise ContractError("non-vendor inputs must be ordered by input_id")
    if len(inputs) != 1:
        raise ContractError("intake must contain exactly one supplied non-vendor instruction")
    if _sorted_texts(
        payload.get("missing_expected_documents"), "missing_expected_documents"
    ) != EXPECTED_MISSING_DOCUMENTS:
        raise ContractError("missing vendor-document census differs")
    if payload.get("duplicate_groups") != [] or payload.get("superseded_documents") != []:
        raise ContractError("absent vendor documents cannot have duplicate or superseded groups")
    handling = payload.get("sensitive_document_handling")
    if type(handling) is not dict or handling != {
        "committed_sensitive_vendor_documents": False,
        "originals_modified": False,
        "status": "NO_SENSITIVE_VENDOR_DOCUMENTS_RECEIVED",
        "tracked_content": "METADATA_AND_HASH_ONLY",
    }:
        raise ContractError("sensitive-document handling differs from the observed intake")
    if (
        payload.get("intake_inventory_complete_for_supplied_set") is not True
        or payload.get("readiness_status") != "NOT_READY"
        or payload.get("readiness_reason") != MISSING_REASON
    ):
        raise ContractError("intake must fail closed on the missing vendor package")
    _validate_boundaries(payload)
    return hashes


def _validate_evidence(payload: Mapping[str, object], intake: Mapping[str, object], hashes: Mapping[str, str]) -> None:
    if payload.get("intake_id") != intake.get("intake_id"):
        raise IntegrityError("CRSP evidence registry is not bound to intake")
    if payload.get("vendor_evidence") != []:
        raise ContractError("missing-package registry cannot contain vendor evidence")
    if (
        payload.get("vendor_evidence_status") != "NO_VENDOR_EVIDENCE_RECEIVED"
        or payload.get("adjudication_permitted") is not False
        or payload.get("adjudication_blocker") != MISSING_REASON
    ):
        raise ContractError("vendor-evidence registry must fail closed")
    refs = payload.get("non_vendor_input_refs")
    if type(refs) is not list or len(refs) != len(hashes):
        raise ContractError("non-vendor input references are incomplete")
    observed: dict[str, str] = {}
    for item in refs:
        if type(item) is not dict:
            raise ContractError("non-vendor input references must be objects")
        if item.get("classification") != "PHASE_SPECIFICATION_NOT_VENDOR_EVIDENCE":
            raise ContractError("input reference cannot be classified as vendor evidence")
        observed[_text(item.get("input_id"), "input reference ID")] = require_sha256(
            item.get("content_sha256"), "input reference hash"
        )
    if observed != dict(hashes):
        raise IntegrityError("non-vendor input references differ from intake")
    if tuple(payload.get("evidence_hierarchy", ())) != EVIDENCE_HIERARCHY:
        raise ContractError("CRSP evidence hierarchy differs")
    _validate_boundaries(payload)


def _validate_questions(payload: Mapping[str, object], intake: Mapping[str, object], evidence: Mapping[str, object]) -> set[str]:
    if (
        payload.get("intake_id") != intake.get("intake_id")
        or payload.get("evidence_registry_id") != evidence.get("registry_id")
    ):
        raise IntegrityError("follow-up questions are not bound to intake and evidence")
    if payload.get("status") != "NOT_SENT" or payload.get("do_not_send_automatically") is not True:
        raise ContractError("follow-up package must remain unsent")
    questions = payload.get("questions")
    if type(questions) is not list or not questions:
        raise ContractError("missing-package result requires follow-up questions")
    ids = [item.get("question_id") for item in questions if type(item) is dict]
    if ids != [f"Q-{index:02d}" for index in range(1, len(questions) + 1)]:
        raise ContractError("follow-up questions must be canonically ordered")
    covered: set[str] = set()
    for item in questions:
        assert isinstance(item, dict)
        gates = _sorted_texts(item.get("gate_ids"), "follow-up gate_ids")
        if not set(gates) <= set(EXPECTED_GATES):
            raise ContractError("follow-up question references an unknown gate")
        covered.update(gates)
        for field in (
            "question",
            "why_mandatory",
            "current_evidence",
            "exact_unresolved_point",
            "acceptable_answer_criteria",
            "sufficient_evidence_type",
        ):
            _text(item.get(field), f"follow-up.{field}")
        if type(item.get("binding_contract_language_required")) is not bool:
            raise ContractError("follow-up binding-language flag must be boolean")
        if type(item.get("product_schedule_or_schema_citation_sufficient")) is not bool:
            raise ContractError("follow-up schedule/schema flag must be boolean")
    if covered != set(EXPECTED_GATES):
        raise ContractError("follow-up package does not cover every unresolved gate")
    _validate_boundaries(payload)
    return covered


def _validate_plan(payload: Mapping[str, object], intake: Mapping[str, object], evidence: Mapping[str, object]) -> None:
    if (
        payload.get("intake_id") != intake.get("intake_id")
        or payload.get("evidence_registry_id") != evidence.get("registry_id")
    ):
        raise IntegrityError("future ingestion plan is not bound to intake and evidence")
    if payload.get("plan_status") != "NOT_EXECUTABLE_MISSING_VENDOR_RESPONSE_PACKAGE":
        raise ContractError("future ingestion plan must remain non-executable")
    if payload.get("future_phase_title") != "Pursue Goal — CRSP Licensed Source Landing, Admission, and Point-in-Time Canonical Panel Construction":
        raise ContractError("future phase title differs")
    sequence = payload.get("steps")
    if type(sequence) is not list or tuple(
        item.get("step") for item in sequence if type(item) is dict
    ) != tuple(range(1, 29)):
        raise ContractError("future ingestion plan must contain exact steps 1 through 28")
    if tuple(item.get("name") for item in sequence) != EXPECTED_FUTURE_STEP_NAMES:
        raise ContractError("future ingestion plan step names differ")
    if any(item.get("execution_authorized") is not False for item in sequence):
        raise ContractError("future ingestion steps cannot be executable")
    _validate_boundaries(payload)


def _validate_decision(
    payload: Mapping[str, object],
    intake: Mapping[str, object],
    evidence: Mapping[str, object],
    questions: Mapping[str, object],
    plan: Mapping[str, object],
) -> None:
    bindings = {
        "intake_id": intake.get("intake_id"),
        "evidence_registry_id": evidence.get("registry_id"),
        "question_package_id": questions.get("question_package_id"),
        "future_ingestion_plan_id": plan.get("plan_id"),
    }
    if any(payload.get(field) != value for field, value in bindings.items()):
        raise IntegrityError("purchase decision record bindings differ")
    if (
        payload.get("status") != DECISION_STATUS
        or payload.get("reason") != MISSING_REASON
        or payload.get("purchase_now") != "PROHIBITED"
        or payload.get("recommended_purchase") is not None
    ):
        raise ContractError("missing-package decision must require follow-up and prohibit purchase")
    if (
        payload.get("conditional_acquisition_manifest_created") is not False
        or payload.get("conditional_acquisition_manifest") is not None
    ):
        raise ContractError("missing vendor evidence cannot support an acquisition manifest")
    candidate = payload.get("expected_candidate")
    if type(candidate) is not dict or candidate != {
        "format": "CIZ_FLAT_FILE_FORMAT_2_0",
        "product": "CRSP 1962 US Stock",
        "product_code": "C6Z",
        "provider": "Morningstar/CRSP",
        "status": "PRIOR_EVALUATION_EXPECTATION_NOT_VENDOR_CONFIRMED",
    }:
        raise ContractError("expected candidate must remain explicitly unconfirmed")
    gates = payload.get("mandatory_gate_results")
    if type(gates) is not list:
        raise ContractError("mandatory gate results are unavailable")
    gate_ids = tuple(item.get("gate_id") for item in gates if type(item) is dict)
    if gate_ids != EXPECTED_GATES:
        raise ContractError("purchase decision does not cover every mandatory gate")
    question_ids = {
        item.get("question_id") for item in questions.get("questions", []) if type(item) is dict
    }
    for gate in gates:
        assert isinstance(gate, dict)
        if gate.get("status") != "UNRESOLVED" or gate.get("evidence_ids") != []:
            raise ContractError("a gate cannot pass without vendor evidence")
        _text(gate.get("limitation"), "mandatory gate limitation")
        actions = _sorted_texts(gate.get("required_question_ids"), "required_question_ids")
        if not set(actions) <= question_ids:
            raise ContractError("mandatory gate references an unknown follow-up question")
    if payload.get("technical_adjudication") != "NOT_PERFORMED_MISSING_VENDOR_RESPONSE_PACKAGE":
        raise ContractError("technical adjudication must remain unperformed")
    if payload.get("contract_adjudication") != "NOT_PERFORMED_MISSING_VENDOR_RESPONSE_PACKAGE":
        raise ContractError("contract adjudication must remain unperformed")
    if payload.get("pricing_adjudication") != {
        "archive_cost": None,
        "currency": None,
        "exchange_fees": None,
        "first_year_cost": None,
        "initial_cost": None,
        "minimum_term": None,
        "quote_expiration": None,
        "recurring_annual_cost": None,
        "setup_fees": None,
        "status": "UNRESOLVED",
    }:
        raise ContractError("missing quote cannot support price calculation")
    if payload.get("contradiction_analysis") != {
        "contradictions": [],
        "status": "NOT_EVALUABLE_MISSING_VENDOR_RESPONSE_PACKAGE",
    }:
        raise ContractError("contradictions cannot be inferred without vendor documents")
    attestation = payload.get("phase_execution_attestation")
    if type(attestation) is not dict or not attestation or any(value is not False for value in attestation.values()):
        raise ContractError("phase execution attestation must deny every prohibited action")
    _validate_boundaries(payload)


def load_crsp_vendor_response_adjudication_package(root: Path) -> dict[str, object]:
    """Load and cross-check the fail-closed CRSP readiness package."""

    root = Path(root).resolve()
    if (root / "config/crsp_conditional_acquisition_manifest_v1.json").exists():
        raise ContractError("missing vendor evidence cannot support an acquisition manifest")
    loaded: dict[str, dict[str, object]] = {}
    for name, (relative, record_type, id_field) in RECORDS.items():
        loaded[name] = _load_record(root / relative, record_type, id_field)
    hashes = _validate_intake(loaded["intake"])
    _validate_evidence(loaded["evidence"], loaded["intake"], hashes)
    _validate_questions(loaded["questions"], loaded["intake"], loaded["evidence"])
    _validate_plan(loaded["plan"], loaded["intake"], loaded["evidence"])
    _validate_decision(
        loaded["decision"],
        loaded["intake"],
        loaded["evidence"],
        loaded["questions"],
        loaded["plan"],
    )
    return {
        "status": DECISION_STATUS,
        "reason": MISSING_REASON,
        "substantive_vendor_document_count": 0,
        "mandatory_gate_count": len(EXPECTED_GATES),
        "follow_up_question_count": len(loaded["questions"]["questions"]),
        "future_ingestion_step_count": len(loaded["plan"]["steps"]),
        "record_ids": {
            "intake": loaded["intake"]["intake_id"],
            "evidence": loaded["evidence"]["registry_id"],
            "questions": loaded["questions"]["question_package_id"],
            "plan": loaded["plan"]["plan_id"],
            "decision": loaded["decision"]["decision_id"],
        },
    }
