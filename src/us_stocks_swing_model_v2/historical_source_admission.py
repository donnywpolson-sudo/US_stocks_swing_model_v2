"""Fail-closed V1 historical-source admission and structural-universe contracts.

This module contains no provider transport, outcome, label, training, evaluation,
or backtest capability.  It validates content-addressed source descriptors and
full-corpus audit summaries, quarantines unsafe evidence classes, and requires
all mandatory V1 source families before a panel can be called source qualified.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Mapping

from .causal_foundation import AvailabilityStamp, CausalDailyBar
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_timestamp,
    require_aware_utc,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError, IntegrityError


PROJECT = "US_stocks_swing_model_v2"
SOURCE_CONTRACT_PHASE = "HISTORICAL_SOURCE_CONTRACT_QUALIFICATION_PIT_DATA_V1"

REQUIREMENT_CLASSIFICATIONS = {
    "REQUIRED",
    "OPTIONAL",
    "OUT_OF_SCOPE_V1",
    "PROHIBITED",
    "UNRESOLVED",
}
MANDATORY_SOURCE_FAMILIES = (
    "CORPORATE_ACTIONS",
    "DELISTING_TERMINAL_EVENTS",
    "EXCHANGE_CALENDAR",
    "HISTORICAL_SECURITY_MASTER",
    "RAW_DAILY_OHLCV",
)
SOURCE_FAMILIES = set(MANDATORY_SOURCE_FAMILIES)
ADJUSTMENT_STATES = {
    "ADJUSTED_CONVENIENCE_ONLY",
    "NOT_APPLICABLE",
    "RAW_UNADJUSTED",
    "UNKNOWN",
}
EVIDENCE_CLASSES = {
    "CURRENT_STATE_ONLY",
    "EXTERNAL_AS_RECEIVED",
    "LEGACY_DISCOVERY",
    "SYNTHETIC_ONLY",
}
LICENSE_CLASSIFICATIONS = {
    "LOCAL_RESEARCH_PERMITTED",
    "PROHIBITED",
    "UNRESOLVED",
}
ADMISSION_STATUSES = {
    "ADMITTED",
    "BLOCKED",
    "QUARANTINED",
    "SYNTHETIC_TEST_ONLY",
}
SECURITY_TYPES = {
    "ADR",
    "CLOSED_END_FUND",
    "COMMON_STOCK",
    "CONVERTIBLE_INSTRUMENT",
    "ETF",
    "ETN",
    "MUTUAL_FUND",
    "OTC_SECURITY",
    "PREFERRED_STOCK",
    "RIGHT",
    "SPAC_UNIT",
    "UNIT",
    "UNKNOWN_AMBIGUOUS",
    "WARRANT",
}
V1_INCLUDED_SECURITY_TYPES = frozenset({"COMMON_STOCK"})
LISTING_STATES = {
    "ACQUIRED",
    "ACTIVE",
    "BANKRUPT",
    "DELISTED",
    "HALTED",
    "INACTIVE",
}
STRUCTURAL_REASON_CODES = {
    "AMBIGUOUS_SECURITY_ID",
    "DELISTED",
    "ELIGIBLE_STRUCTURAL",
    "HALTED",
    "INACTIVE",
    "INVALID_BAR",
    "MISSING_CAUSAL_PRICE",
    "MISSING_CAUSAL_VOLUME",
    "MISSING_REQUIRED_HISTORY",
    "NOT_YET_LISTED",
    "OUTSIDE_SUPPORTED_SESSION",
    "STALE_OBSERVATION",
    "UNQUALIFIED_SOURCE",
    "UNSUPPORTED_SECURITY_TYPE",
}
READINESS_RESULTS = {
    "BLOCKED",
    "FAIL",
    "OUT_OF_SCOPE_V1",
    "PASS",
    "PASS_WITH_CAVEATS",
}
SOURCE_READINESS_GATES = (
    "FOUNDATION_CHECKPOINT_PRESERVED",
    "SCHEDULER_WORKTREE_ISOLATED",
    "V1_RESEARCH_DATA_SCOPE_FROZEN",
    "LEGACY_HISTORICAL_DATA_QUARANTINED",
    "SOURCE_CONTRACT_FROZEN",
    "SOURCE_ADMISSION_GATEWAY",
    "RAW_OHLCV_SEMANTICS_QUALIFIED",
    "STABLE_HISTORICAL_SECURITY_IDS",
    "HISTORICAL_TICKER_INTERVALS",
    "HISTORICAL_EXCHANGE_LISTING_INTERVALS",
    "SECURITY_TYPE_CLASSIFICATION",
    "ACTIVE_AND_DELISTED_COVERAGE",
    "TICKER_REUSE_RECONCILIATION",
    "CORPORATE_ACTION_SOURCE_COVERAGE",
    "CORPORATE_ACTION_RECONCILIATION",
    "DELISTING_TERMINAL_EVENT_REPRESENTATION",
    "SESSION_DATE_SEMANTICS",
    "CAUSAL_STRUCTURAL_UNIVERSE",
    "CAUSAL_PRICE_VOLUME_ELIGIBILITY_INPUTS",
    "CANONICAL_PANEL_REBUILT_FROM_QUALIFIED_SOURCES",
    "FULL_LINEAGE_AND_CONTENT_HASHES",
    "FULL_CORPUS_IDENTITY_VALIDATION",
    "FULL_CORPUS_BAR_INTEGRITY_VALIDATION",
    "REAL_SOURCE_PREFIX_INVARIANCE",
    "REAL_SOURCE_FUTURE_MUTATION_INVARIANCE",
    "CURRENT_SNAPSHOT_POISONING_INVARIANCE",
    "REAL_SOURCE_UNIVERSE_INVARIANCE",
    "REVISION_ISOLATION",
    "OUTCOME_FIREWALL_REMAINS_CLOSED",
    "NO_REAL_OUTCOMES_CREATED",
    "CLEAN_REPRODUCIBLE_BUILD",
    "FINAL_WORKTREE_CLEAN",
)
SEMANTIC_CLAIM_KEYS = {
    "active_and_inactive_coverage",
    "corporate_action_effective_coverage",
    "corporate_action_publication_times",
    "corporate_action_revision_history",
    "delisted_coverage",
    "full_lineage",
    "historical_exchange_listing_validity",
    "historical_revisions_retained",
    "historical_ticker_validity",
    "no_current_state_join",
    "no_forward_fill",
    "no_interpolation",
    "no_synthetic_rows",
    "raw_bytes_immutable",
    "security_type_explicit",
    "session_date_qualified",
    "stable_security_identifier",
    "terminal_event_representation",
    "timestamp_publication_semantics",
}
VALIDATION_COUNT_KEYS = (
    "current_state_join_rows",
    "duplicate_key_rows",
    "forward_filled_rows",
    "future_availability_violations",
    "interpolated_rows",
    "invalid_bar_rows",
    "invalid_exchange_rows",
    "invalid_historical_ticker_rows",
    "invalid_rows",
    "missing_lineage_rows",
    "quarantined_rows",
    "silent_dropped_inactive_rows",
    "synthetic_rows",
    "unexpected_session_rows",
    "unknown_security_type_rows",
    "unresolved_corporate_action_rows",
    "unresolved_identity_rows",
)

FAMILY_REQUIRED_SEMANTICS = {
    "HISTORICAL_SECURITY_MASTER": frozenset(
        {
            "active_and_inactive_coverage",
            "delisted_coverage",
            "full_lineage",
            "historical_exchange_listing_validity",
            "historical_revisions_retained",
            "historical_ticker_validity",
            "no_current_state_join",
            "raw_bytes_immutable",
            "security_type_explicit",
            "stable_security_identifier",
            "timestamp_publication_semantics",
        }
    ),
    "RAW_DAILY_OHLCV": frozenset(
        {
            "full_lineage",
            "no_current_state_join",
            "no_forward_fill",
            "no_interpolation",
            "no_synthetic_rows",
            "raw_bytes_immutable",
            "session_date_qualified",
            "stable_security_identifier",
            "timestamp_publication_semantics",
        }
    ),
    "CORPORATE_ACTIONS": frozenset(
        {
            "corporate_action_effective_coverage",
            "corporate_action_publication_times",
            "corporate_action_revision_history",
            "full_lineage",
            "historical_revisions_retained",
            "raw_bytes_immutable",
            "stable_security_identifier",
            "timestamp_publication_semantics",
        }
    ),
    "DELISTING_TERMINAL_EVENTS": frozenset(
        {
            "corporate_action_publication_times",
            "delisted_coverage",
            "full_lineage",
            "historical_revisions_retained",
            "raw_bytes_immutable",
            "stable_security_identifier",
            "terminal_event_representation",
            "timestamp_publication_semantics",
        }
    ),
    "EXCHANGE_CALENDAR": frozenset(
        {
            "full_lineage",
            "historical_revisions_retained",
            "raw_bytes_immutable",
            "session_date_qualified",
            "timestamp_publication_semantics",
        }
    ),
}


def _canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError(f"{field} must be nonempty canonical text")
    return value


def _sorted_text_tuple(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(value) is not list or (not allow_empty and not value):
        raise ContractError(f"{field} must be a canonical JSON list")
    result = tuple(_canonical_text(item, field) for item in value)
    if result != tuple(sorted(set(result))):
        raise ContractError(f"{field} must be sorted and unique")
    return result


def _exact_date(value: object, field: str) -> date:
    if type(value) is not str:
        raise ContractError(f"{field} must be an exact ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError(f"{field} must be an exact ISO date") from exc
    if parsed.isoformat() != value:
        raise ContractError(f"{field} must use canonical ISO date encoding")
    return parsed


def load_content_addressed_source_record(
    path: Path,
    *,
    id_field: str,
) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"source record is missing or invalid JSON: {path}") from exc
    if type(payload) is not dict or id_field not in payload:
        raise ContractError(f"source record lacks {id_field}")
    record_id = require_sha256(payload[id_field], id_field)
    unsigned = dict(payload)
    unsigned.pop(id_field)
    if record_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError(f"{id_field} differs from source record content")
    return payload


def load_v1_source_contract(path: Path) -> dict[str, object]:
    payload = load_content_addressed_source_record(path, id_field="contract_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("record_type") != "HISTORICAL_SOURCE_CONTRACT_V1"
        or payload.get("phase") != SOURCE_CONTRACT_PHASE
        or payload.get("real_outcome_access") is not False
    ):
        raise ContractError("V1 historical source contract identity differs")
    domains = payload.get("source_domains")
    if type(domains) is not list or not domains:
        raise ContractError("V1 source contract requires source domains")
    classifications = {
        item.get("classification") for item in domains if type(item) is dict
    }
    if len(classifications) == 0 or not classifications <= REQUIREMENT_CLASSIFICATIONS:
        raise ContractError("V1 source-domain classifications are invalid")
    required = tuple(
        item.get("source_family")
        for item in domains
        if type(item) is dict and item.get("classification") == "REQUIRED"
    )
    if tuple(sorted(required)) != MANDATORY_SOURCE_FAMILIES:
        raise ContractError("V1 mandatory source families differ")
    if payload.get("admission_rule") != "FAIL_CLOSED_ALL_MANDATORY_FAMILIES":
        raise ContractError("V1 source contract admission rule differs")
    return payload


def load_v1_admission_policy(path: Path) -> dict[str, object]:
    payload = load_content_addressed_source_record(path, id_field="policy_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("record_type") != "HISTORICAL_SOURCE_ADMISSION_POLICY_V1"
        or payload.get("phase") != SOURCE_CONTRACT_PHASE
        or payload.get("unresolved_tolerance") != 0
        or payload.get("silent_drop_tolerance") != 0
        or payload.get("real_outcome_access") is not False
    ):
        raise ContractError("V1 historical source admission policy differs")
    if tuple(payload.get("required_source_families", ())) != MANDATORY_SOURCE_FAMILIES:
        raise ContractError("V1 admission source-family order differs")
    if payload.get("admittable_evidence_class") != "EXTERNAL_AS_RECEIVED":
        raise ContractError("V1 admission evidence class differs")
    if payload.get("required_license_classification") != "LOCAL_RESEARCH_PERMITTED":
        raise ContractError("V1 admission license policy differs")
    return payload


def load_historical_source_qualification_report(path: Path) -> dict[str, object]:
    payload = load_content_addressed_source_record(path, id_field="report_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("record_type") != "HISTORICAL_SOURCE_QUALIFICATION_REPORT_V1"
        or payload.get("phase") != SOURCE_CONTRACT_PHASE
        or payload.get("status") != "BLOCKED"
    ):
        raise ContractError("historical source qualification report identity differs")
    sources = payload.get("sources")
    if type(sources) is not list or not sources:
        raise ContractError("historical source qualification report requires sources")
    source_ids = [item.get("source_id") for item in sources if type(item) is dict]
    if len(source_ids) != len(sources) or len(source_ids) != len(set(source_ids)):
        raise ContractError("historical source qualification source IDs differ")
    canonical = payload.get("canonical_panel")
    if (
        type(canonical) is not dict
        or canonical.get("source_qualified_for_v1") is not False
        or canonical.get("build_status") != "NOT_BUILT_MANDATORY_SOURCES_BLOCKED"
        or canonical.get("row_count") != 0
    ):
        raise ContractError("source qualification report overstates canonical readiness")
    claims = payload.get("claims")
    if type(claims) is not dict or any(
        claims.get(name) is not False
        for name in (
            "real_outcomes_accessed",
            "real_outcomes_created",
            "real_labels_created",
            "training_performed",
            "evaluation_performed",
            "backtesting_performed",
            "source_readiness_passed",
        )
    ):
        raise ContractError("source qualification report crosses the phase boundary")
    return payload


def load_historical_source_qualification_audit_result(
    path: Path,
) -> dict[str, object]:
    payload = load_content_addressed_source_record(path, id_field="record_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != PROJECT
        or payload.get("record_type")
        != "HISTORICAL_SOURCE_QUALIFICATION_AUDIT_RESULT_V1"
        or payload.get("phase") != SOURCE_CONTRACT_PHASE
        or payload.get("exit_code") != 0
        or payload.get("attempt_index") != 1
        or payload.get("invocation_limit") != 1
    ):
        raise ContractError("historical source qualification audit result differs")
    claims = payload.get("claims")
    if (
        type(claims) is not dict
        or claims.get("read_only") is not True
        or claims.get("files_written") != 0
        or claims.get("network_requests") != 0
        or claims.get("automatic_retry_authorized") is not False
        or any(
            claims.get(name) is not False
            for name in (
                "credentials_accessed",
                "outcomes_accessed",
                "labels_accessed",
                "training_performed",
                "evaluation_performed",
                "backtesting_performed",
            )
        )
    ):
        raise ContractError("historical source audit crossed its authority boundary")
    bars = payload.get("historical_bar_census")
    if (
        type(bars) is not dict
        or bars.get("row_count") != 13724185
        or bars.get("evidence_class_counts") != {"LEGACY_DISCOVERY": 13724185}
        or bars.get("input_quality_state_counts")
        != {"CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED": 13724185}
        or bars.get("point_in_time_safe_true_rows") != 0
        or bars.get("historical_membership_proven_true_rows") != 0
        or not str(bars.get("admission_status", "")).startswith("QUARANTINED_")
    ):
        raise ContractError("historical source audit overstates legacy bar evidence")
    conclusion = payload.get("source_admission_conclusion")
    if (
        type(conclusion) is not dict
        or conclusion.get("status") != "BLOCKED"
        or conclusion.get("canonical_panel_build_authorized") is not False
    ):
        raise ContractError("historical source audit conclusion differs")
    return payload


def load_historical_source_readiness_gate(path: Path) -> dict[str, object]:
    payload = load_content_addressed_source_record(path, id_field="gate_id")
    if (
        payload.get("schema_version") != 2
        or payload.get("project") != PROJECT
        or payload.get("record_type") != "HISTORICAL_SOURCE_READINESS_GATE_V2"
        or payload.get("phase") != SOURCE_CONTRACT_PHASE
        or payload.get("overall_status") != "BLOCKED"
        or payload.get("automatic_outcome_unlock") is not False
        or payload.get("real_outcome_access_authorized") is not False
    ):
        raise ContractError("historical source readiness gate identity differs")
    gates = payload.get("gates")
    if type(gates) is not list:
        raise ContractError("historical source readiness gate census is invalid")
    names = tuple(item.get("gate") for item in gates if type(item) is dict)
    if names != SOURCE_READINESS_GATES:
        raise ContractError("historical source readiness gate order differs")
    if any(item.get("result") not in READINESS_RESULTS for item in gates):
        raise ContractError("historical source readiness result is invalid")
    blocked_names = {
        item["gate"] for item in gates if item.get("result") == "BLOCKED"
    }
    required_blockers = {
        "RAW_OHLCV_SEMANTICS_QUALIFIED",
        "STABLE_HISTORICAL_SECURITY_IDS",
        "ACTIVE_AND_DELISTED_COVERAGE",
        "CORPORATE_ACTION_SOURCE_COVERAGE",
        "CANONICAL_PANEL_REBUILT_FROM_QUALIFIED_SOURCES",
    }
    if not required_blockers <= blocked_names:
        raise ContractError("mandatory external-source blockers were lost")
    optional = payload.get("out_of_scope_v1_gates")
    if type(optional) is not list or any(
        item.get("result") != "OUT_OF_SCOPE_V1"
        for item in optional
        if type(item) is dict
    ):
        raise ContractError("V1 optional source gates differ")
    return payload


@dataclass(frozen=True)
class SourceFileEntry:
    path: str
    bytes: int
    sha256: str

    @classmethod
    def from_dict(cls, payload: object) -> "SourceFileEntry":
        if type(payload) is not dict or set(payload) != {"path", "bytes", "sha256"}:
            raise ContractError("source file manifest entry fields differ")
        value = cls(
            path=_canonical_text(payload["path"], "source file path"),
            bytes=payload["bytes"],
            sha256=payload["sha256"],
        )
        value.validate()
        return value

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}

    def validate(self) -> None:
        safe_relative_path(self.path)
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 1:
            raise ContractError("source file byte count must be positive")
        require_sha256(self.sha256, "source file sha256")


@dataclass(frozen=True)
class SourcePackageDescriptor:
    schema_version: int
    project: str
    record_type: str
    source_identifier: str
    source_family: str
    provider: str
    dataset_name: str
    dataset_version: str
    dataset_schema_version: str
    retrieved_at: datetime
    coverage_start: date
    coverage_end: date
    security_scope: str
    identifier_fields: tuple[str, ...]
    adjustment_state: str
    timezone: str
    timestamp_semantics: str
    revision_policy: str
    license_classification: str
    storage_location: str
    file_manifest: tuple[SourceFileEntry, ...]
    content_hashes: tuple[str, ...]
    schema_hash: str
    ingestion_code_version: str
    known_limitations: tuple[str, ...]
    evidence_class: str
    semantic_claims: tuple[tuple[str, bool], ...]
    descriptor_id: str

    @classmethod
    def from_dict(cls, payload: object) -> "SourcePackageDescriptor":
        expected = {
            "adjustment_state",
            "content_hashes",
            "coverage_end",
            "coverage_start",
            "dataset_name",
            "dataset_schema_version",
            "dataset_version",
            "descriptor_id",
            "evidence_class",
            "file_manifest",
            "identifier_fields",
            "ingestion_code_version",
            "known_limitations",
            "license_classification",
            "project",
            "provider",
            "record_type",
            "retrieved_at",
            "revision_policy",
            "schema_hash",
            "schema_version",
            "security_scope",
            "semantic_claims",
            "source_family",
            "source_identifier",
            "storage_location",
            "timestamp_semantics",
            "timezone",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ContractError("source package descriptor fields differ")
        claims = payload["semantic_claims"]
        if type(claims) is not dict or set(claims) != SEMANTIC_CLAIM_KEYS:
            raise ContractError("source package semantic claims differ")
        if any(type(value) is not bool for value in claims.values()):
            raise ContractError("source package semantic claims must be booleans")
        files = payload["file_manifest"]
        if type(files) is not list:
            raise ContractError("source package file manifest must be a list")
        value = cls(
            schema_version=payload["schema_version"],
            project=payload["project"],
            record_type=payload["record_type"],
            source_identifier=_canonical_text(
                payload["source_identifier"], "source identifier"
            ),
            source_family=_canonical_text(payload["source_family"], "source family"),
            provider=_canonical_text(payload["provider"], "source provider"),
            dataset_name=_canonical_text(payload["dataset_name"], "dataset name"),
            dataset_version=_canonical_text(
                payload["dataset_version"], "dataset version"
            ),
            dataset_schema_version=_canonical_text(
                payload["dataset_schema_version"], "dataset schema version"
            ),
            retrieved_at=parse_timestamp(payload["retrieved_at"], "retrieved_at"),
            coverage_start=_exact_date(payload["coverage_start"], "coverage_start"),
            coverage_end=_exact_date(payload["coverage_end"], "coverage_end"),
            security_scope=_canonical_text(payload["security_scope"], "security scope"),
            identifier_fields=_sorted_text_tuple(
                payload["identifier_fields"], "identifier fields"
            ),
            adjustment_state=_canonical_text(
                payload["adjustment_state"], "adjustment state"
            ),
            timezone=_canonical_text(payload["timezone"], "timezone"),
            timestamp_semantics=_canonical_text(
                payload["timestamp_semantics"], "timestamp semantics"
            ),
            revision_policy=_canonical_text(payload["revision_policy"], "revision policy"),
            license_classification=_canonical_text(
                payload["license_classification"], "license classification"
            ),
            storage_location=_canonical_text(
                payload["storage_location"], "storage location"
            ),
            file_manifest=tuple(SourceFileEntry.from_dict(item) for item in files),
            content_hashes=_sorted_text_tuple(payload["content_hashes"], "content hashes"),
            schema_hash=payload["schema_hash"],
            ingestion_code_version=_canonical_text(
                payload["ingestion_code_version"], "ingestion code version"
            ),
            known_limitations=_sorted_text_tuple(
                payload["known_limitations"], "known limitations", allow_empty=True
            ),
            evidence_class=_canonical_text(payload["evidence_class"], "evidence class"),
            semantic_claims=tuple(sorted(claims.items())),
            descriptor_id=payload["descriptor_id"],
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "record_type": self.record_type,
            "source_identifier": self.source_identifier,
            "source_family": self.source_family,
            "provider": self.provider,
            "dataset_name": self.dataset_name,
            "dataset_version": self.dataset_version,
            "dataset_schema_version": self.dataset_schema_version,
            "retrieved_at": iso_z(self.retrieved_at),
            "coverage_start": self.coverage_start.isoformat(),
            "coverage_end": self.coverage_end.isoformat(),
            "security_scope": self.security_scope,
            "identifier_fields": list(self.identifier_fields),
            "adjustment_state": self.adjustment_state,
            "timezone": self.timezone,
            "timestamp_semantics": self.timestamp_semantics,
            "revision_policy": self.revision_policy,
            "license_classification": self.license_classification,
            "storage_location": self.storage_location,
            "file_manifest": [item.as_dict() for item in self.file_manifest],
            "content_hashes": list(self.content_hashes),
            "schema_hash": self.schema_hash,
            "ingestion_code_version": self.ingestion_code_version,
            "known_limitations": list(self.known_limitations),
            "evidence_class": self.evidence_class,
            "semantic_claims": dict(self.semantic_claims),
        }

    def validate(self) -> None:
        if (
            self.schema_version != 1
            or self.project != PROJECT
            or self.record_type != "HISTORICAL_SOURCE_PACKAGE_DESCRIPTOR"
            or self.source_family not in SOURCE_FAMILIES
        ):
            raise ContractError("source package descriptor identity differs")
        require_aware_utc(self.retrieved_at, "source retrieved_at")
        if self.coverage_start > self.coverage_end:
            raise ContractError("source coverage interval is invalid")
        if self.adjustment_state not in ADJUSTMENT_STATES:
            raise ContractError("source adjustment state is invalid")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise ContractError("source evidence class is invalid")
        if self.license_classification not in LICENSE_CLASSIFICATIONS:
            raise ContractError("source license classification is invalid")
        safe_relative_path(self.storage_location)
        if not self.file_manifest:
            raise ContractError("source package file manifest is empty")
        paths = tuple(item.path for item in self.file_manifest)
        if paths != tuple(sorted(set(paths))):
            raise ContractError("source package file paths must be sorted and unique")
        for item in self.file_manifest:
            item.validate()
        expected_hashes = tuple(sorted({item.sha256 for item in self.file_manifest}))
        if self.content_hashes != expected_hashes:
            raise ContractError("source package content hashes differ from file manifest")
        require_sha256(self.schema_hash, "source schema hash")
        if set(dict(self.semantic_claims)) != SEMANTIC_CLAIM_KEYS:
            raise ContractError("source semantic claim census differs")
        require_sha256(self.descriptor_id, "source descriptor_id")
        if self.descriptor_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("source descriptor ID differs from its content")


@dataclass(frozen=True)
class CorpusValidation:
    total_rows: int
    validated_rows: int
    counts: tuple[tuple[str, int], ...]
    full_corpus: bool
    file_hashes_verified: bool
    schema_hash_verified: bool
    source_count_reconciled: bool
    validation_id: str

    @classmethod
    def create(
        cls,
        *,
        total_rows: int,
        validated_rows: int,
        counts: Mapping[str, int],
        full_corpus: bool,
        file_hashes_verified: bool,
        schema_hash_verified: bool,
        source_count_reconciled: bool,
    ) -> "CorpusValidation":
        normalized = tuple(sorted(counts.items()))
        provisional = cls(
            total_rows=total_rows,
            validated_rows=validated_rows,
            counts=normalized,
            full_corpus=full_corpus,
            file_hashes_verified=file_hashes_verified,
            schema_hash_verified=schema_hash_verified,
            source_count_reconciled=source_count_reconciled,
            validation_id="",
        )
        value = cls(
            **{
                **provisional.__dict__,
                "validation_id": sha256_bytes(
                    canonical_json_bytes(provisional.unsigned_dict())
                ),
            }
        )
        value.validate()
        return value

    @classmethod
    def from_dict(cls, payload: object) -> "CorpusValidation":
        expected = {
            "counts",
            "file_hashes_verified",
            "full_corpus",
            "schema_hash_verified",
            "source_count_reconciled",
            "total_rows",
            "validated_rows",
            "validation_id",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ContractError("source corpus validation fields differ")
        if type(payload["counts"]) is not dict:
            raise ContractError("source corpus validation counts must be an object")
        value = cls(
            total_rows=payload["total_rows"],
            validated_rows=payload["validated_rows"],
            counts=tuple(sorted(payload["counts"].items())),
            full_corpus=payload["full_corpus"],
            file_hashes_verified=payload["file_hashes_verified"],
            schema_hash_verified=payload["schema_hash_verified"],
            source_count_reconciled=payload["source_count_reconciled"],
            validation_id=payload["validation_id"],
        )
        value.validate()
        return value

    def count_dict(self) -> dict[str, int]:
        return dict(self.counts)

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "total_rows": self.total_rows,
            "validated_rows": self.validated_rows,
            "counts": self.count_dict(),
            "full_corpus": self.full_corpus,
            "file_hashes_verified": self.file_hashes_verified,
            "schema_hash_verified": self.schema_hash_verified,
            "source_count_reconciled": self.source_count_reconciled,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "validation_id": self.validation_id}

    def validate(self) -> None:
        if (
            isinstance(self.total_rows, bool)
            or not isinstance(self.total_rows, int)
            or self.total_rows < 1
            or isinstance(self.validated_rows, bool)
            or not isinstance(self.validated_rows, int)
            or not 0 <= self.validated_rows <= self.total_rows
        ):
            raise ContractError("source corpus row counts are invalid")
        counts = self.count_dict()
        if tuple(counts) != VALIDATION_COUNT_KEYS:
            raise ContractError("source corpus validation count census differs")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise ContractError("source corpus validation counts must be nonnegative")
        if counts["quarantined_rows"] != self.total_rows - self.validated_rows:
            raise ContractError("source quarantine count differs from validated rows")
        for value in (
            self.full_corpus,
            self.file_hashes_verified,
            self.schema_hash_verified,
            self.source_count_reconciled,
        ):
            if type(value) is not bool:
                raise ContractError("source corpus verification flags must be booleans")
        require_sha256(self.validation_id, "source validation_id")
        if self.validation_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("source validation ID differs from its content")


@dataclass(frozen=True)
class SourceAdmissionResult:
    source_identifier: str
    source_family: str
    storage_location: str
    descriptor_id: str
    validation_id: str
    contract_id: str
    policy_id: str
    evidence_class: str
    status: str
    research_eligible: bool
    reason_codes: tuple[str, ...]
    total_rows: int
    admitted_rows: int
    quarantined_rows: int
    content_hashes: tuple[str, ...]
    admission_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "source_identifier": self.source_identifier,
            "source_family": self.source_family,
            "storage_location": self.storage_location,
            "descriptor_id": self.descriptor_id,
            "validation_id": self.validation_id,
            "contract_id": self.contract_id,
            "policy_id": self.policy_id,
            "evidence_class": self.evidence_class,
            "status": self.status,
            "research_eligible": self.research_eligible,
            "reason_codes": list(self.reason_codes),
            "total_rows": self.total_rows,
            "admitted_rows": self.admitted_rows,
            "quarantined_rows": self.quarantined_rows,
            "content_hashes": list(self.content_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "admission_id": self.admission_id}

    @classmethod
    def from_dict(cls, payload: object) -> "SourceAdmissionResult":
        expected = {
            "admission_id",
            "admitted_rows",
            "content_hashes",
            "contract_id",
            "descriptor_id",
            "evidence_class",
            "policy_id",
            "quarantined_rows",
            "reason_codes",
            "research_eligible",
            "source_family",
            "source_identifier",
            "status",
            "storage_location",
            "total_rows",
            "validation_id",
        }
        if type(payload) is not dict or set(payload) != expected:
            raise ContractError("source admission result fields differ")
        value = cls(
            source_identifier=payload["source_identifier"],
            source_family=payload["source_family"],
            storage_location=payload["storage_location"],
            descriptor_id=payload["descriptor_id"],
            validation_id=payload["validation_id"],
            contract_id=payload["contract_id"],
            policy_id=payload["policy_id"],
            evidence_class=payload["evidence_class"],
            status=payload["status"],
            research_eligible=payload["research_eligible"],
            reason_codes=tuple(payload["reason_codes"]),
            total_rows=payload["total_rows"],
            admitted_rows=payload["admitted_rows"],
            quarantined_rows=payload["quarantined_rows"],
            content_hashes=tuple(payload["content_hashes"]),
            admission_id=payload["admission_id"],
        )
        value.validate()
        return value

    def validate(self) -> None:
        _canonical_text(self.source_identifier, "admission source identifier")
        if self.source_family not in SOURCE_FAMILIES:
            raise ContractError("admission source family is invalid")
        safe_relative_path(self.storage_location)
        for field in ("descriptor_id", "validation_id", "contract_id", "policy_id"):
            require_sha256(getattr(self, field), f"admission {field}")
        if self.evidence_class not in EVIDENCE_CLASSES:
            raise ContractError("admission evidence class is invalid")
        if self.status not in ADMISSION_STATUSES or type(self.research_eligible) is not bool:
            raise ContractError("source admission status is invalid")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ContractError("source admission reason codes must be sorted and unique")
        if self.research_eligible != (self.status == "ADMITTED"):
            raise ContractError("source research eligibility differs from admission status")
        if self.status == "ADMITTED" and (
            self.evidence_class != "EXTERNAL_AS_RECEIVED" or self.reason_codes
        ):
            raise ContractError("only clean external as-received evidence may be admitted")
        if self.status != "ADMITTED" and not self.reason_codes:
            raise ContractError("non-admitted source requires reason codes")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (self.total_rows, self.admitted_rows, self.quarantined_rows)
        ) or self.admitted_rows + self.quarantined_rows != self.total_rows:
            raise ContractError("source admission row counts are invalid")
        if self.status != "ADMITTED" and self.admitted_rows != 0:
            raise ContractError("non-admitted source cannot expose admitted rows")
        if self.content_hashes != tuple(sorted(set(self.content_hashes))):
            raise ContractError("source admission content hashes must be sorted and unique")
        for value in self.content_hashes:
            require_sha256(value, "source admission content hash")
        require_sha256(self.admission_id, "source admission_id")
        if self.admission_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("source admission ID differs from its content")


def assess_source_package(
    descriptor: SourcePackageDescriptor,
    validation: CorpusValidation,
    *,
    contract_id: str,
    policy_id: str,
) -> SourceAdmissionResult:
    """Assess one complete package without repairing or dropping any source row."""

    descriptor.validate()
    validation.validate()
    require_sha256(contract_id, "source contract_id")
    require_sha256(policy_id, "source policy_id")
    claims = dict(descriptor.semantic_claims)
    reasons: set[str] = set()

    if descriptor.evidence_class == "LEGACY_DISCOVERY":
        reasons.add("LEGACY_DISCOVERY_NOT_RESEARCH_ELIGIBLE")
    elif descriptor.evidence_class == "CURRENT_STATE_ONLY":
        reasons.add("CURRENT_STATE_ONLY_NOT_HISTORICAL")
    elif descriptor.evidence_class == "SYNTHETIC_ONLY":
        reasons.add("SYNTHETIC_SOURCE_NOT_REAL_RESEARCH_ELIGIBLE")
    if descriptor.license_classification != "LOCAL_RESEARCH_PERMITTED":
        reasons.add("LICENSE_CLASSIFICATION_NOT_QUALIFIED")
    if descriptor.adjustment_state == "UNKNOWN":
        reasons.add("ADJUSTMENT_STATE_UNKNOWN")
    if descriptor.adjustment_state == "ADJUSTED_CONVENIENCE_ONLY":
        reasons.add("ADJUSTED_ONLY_SOURCE_DENIED")
    if descriptor.source_family == "RAW_DAILY_OHLCV" and (
        descriptor.adjustment_state != "RAW_UNADJUSTED"
    ):
        reasons.add("RAW_OHLCV_REQUIRED")
    if descriptor.source_family != "RAW_DAILY_OHLCV" and (
        descriptor.adjustment_state != "NOT_APPLICABLE"
    ):
        reasons.add("NON_PRICE_ADJUSTMENT_STATE_INVALID")
    for required in FAMILY_REQUIRED_SEMANTICS[descriptor.source_family]:
        if claims[required] is not True:
            reasons.add(f"MISSING_SEMANTIC_{required.upper()}")
    if descriptor.timestamp_semantics.casefold() in {"unknown", "unqualified"}:
        reasons.add("TIMESTAMP_SEMANTICS_UNQUALIFIED")
    if descriptor.revision_policy.casefold() in {"unknown", "unqualified"}:
        reasons.add("REVISION_POLICY_UNQUALIFIED")

    counts = validation.count_dict()
    if not validation.full_corpus:
        reasons.add("FULL_CORPUS_VALIDATION_REQUIRED")
    if not validation.file_hashes_verified:
        reasons.add("SOURCE_FILE_HASHES_UNVERIFIED")
    if not validation.schema_hash_verified:
        reasons.add("SOURCE_SCHEMA_HASH_UNVERIFIED")
    if not validation.source_count_reconciled:
        reasons.add("SOURCE_COUNT_NOT_RECONCILED")
    for name, count in counts.items():
        if count:
            reasons.add(f"NONZERO_{name.upper()}")

    if descriptor.evidence_class == "SYNTHETIC_ONLY":
        status = "SYNTHETIC_TEST_ONLY"
    elif descriptor.evidence_class in {"LEGACY_DISCOVERY", "CURRENT_STATE_ONLY"}:
        status = "QUARANTINED"
    elif reasons:
        status = "BLOCKED"
    else:
        status = "ADMITTED"
    research_eligible = status == "ADMITTED"
    unsigned = {
        "source_identifier": descriptor.source_identifier,
        "source_family": descriptor.source_family,
        "storage_location": descriptor.storage_location,
        "descriptor_id": descriptor.descriptor_id,
        "validation_id": validation.validation_id,
        "contract_id": contract_id,
        "policy_id": policy_id,
        "evidence_class": descriptor.evidence_class,
        "status": status,
        "research_eligible": research_eligible,
        "reason_codes": sorted(reasons),
        "total_rows": validation.total_rows,
        "admitted_rows": validation.total_rows if research_eligible else 0,
        "quarantined_rows": 0 if research_eligible else validation.total_rows,
        "content_hashes": list(descriptor.content_hashes),
    }
    result = SourceAdmissionResult(
        source_identifier=descriptor.source_identifier,
        source_family=descriptor.source_family,
        storage_location=descriptor.storage_location,
        descriptor_id=descriptor.descriptor_id,
        validation_id=validation.validation_id,
        contract_id=contract_id,
        policy_id=policy_id,
        evidence_class=descriptor.evidence_class,
        status=status,
        research_eligible=research_eligible,
        reason_codes=tuple(sorted(reasons)),
        total_rows=validation.total_rows,
        admitted_rows=validation.total_rows if research_eligible else 0,
        quarantined_rows=0 if research_eligible else validation.total_rows,
        content_hashes=descriptor.content_hashes,
        admission_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    result.validate()
    return result


@dataclass(frozen=True)
class QualifiedSourceBundle:
    contract_id: str
    policy_id: str
    admissions: tuple[SourceAdmissionResult, ...]
    bundle_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "policy_id": self.policy_id,
            "admission_ids": [item.admission_id for item in self.admissions],
        }

    def validate(self) -> None:
        require_sha256(self.contract_id, "qualified bundle contract_id")
        require_sha256(self.policy_id, "qualified bundle policy_id")
        if self.admissions != tuple(
            sorted(self.admissions, key=lambda item: item.source_family)
        ):
            raise ContractError("qualified source admissions must be family ordered")
        if tuple(item.source_family for item in self.admissions) != MANDATORY_SOURCE_FAMILIES:
            raise ContractError("qualified source bundle lacks a mandatory family")
        for item in self.admissions:
            item.validate()
            if (
                not item.research_eligible
                or item.status != "ADMITTED"
                or item.contract_id != self.contract_id
                or item.policy_id != self.policy_id
            ):
                raise ContractError("qualified source bundle contains an unqualified source")
        require_sha256(self.bundle_id, "qualified source bundle_id")
        if self.bundle_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("qualified source bundle ID differs from its content")


def require_qualified_source_bundle(
    admissions: Iterable[SourceAdmissionResult],
) -> QualifiedSourceBundle:
    materialized = tuple(sorted(admissions, key=lambda item: item.source_family))
    if not materialized:
        raise ContractError("qualified source bundle requires admissions")
    contract_ids = {item.contract_id for item in materialized}
    policy_ids = {item.policy_id for item in materialized}
    if len(contract_ids) != 1 or len(policy_ids) != 1:
        raise ContractError("qualified source admissions mix contract or policy IDs")
    unsigned = {
        "contract_id": next(iter(contract_ids)),
        "policy_id": next(iter(policy_ids)),
        "admission_ids": [item.admission_id for item in materialized],
    }
    bundle = QualifiedSourceBundle(
        contract_id=next(iter(contract_ids)),
        policy_id=next(iter(policy_ids)),
        admissions=materialized,
        bundle_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    bundle.validate()
    return bundle


@dataclass(frozen=True)
class HistoricalIdentityInterval:
    stable_security_id: str
    vendor_instrument_id: str
    issuer_id: str | None
    share_class_id: str | None
    ticker: str
    mic: str
    security_type: str
    listing_state: str
    effective_start: date
    effective_end: date | None
    availability: AvailabilityStamp
    revision_number: int
    predecessor_row_id: str | None
    source_row_hash: str
    row_id: str

    @classmethod
    def create(cls, **fields: object) -> "HistoricalIdentityInterval":
        provisional = cls(**fields, row_id="")
        value = cls(
            **{
                **provisional.__dict__,
                "row_id": sha256_bytes(canonical_json_bytes(provisional.unsigned_dict())),
            }
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "vendor_instrument_id": self.vendor_instrument_id,
            "issuer_id": self.issuer_id,
            "share_class_id": self.share_class_id,
            "ticker": self.ticker,
            "mic": self.mic,
            "security_type": self.security_type,
            "listing_state": self.listing_state,
            "effective_start": self.effective_start.isoformat(),
            "effective_end": (
                self.effective_end.isoformat() if self.effective_end is not None else None
            ),
            "availability": self.availability.as_dict(),
            "revision_number": self.revision_number,
            "predecessor_row_id": self.predecessor_row_id,
            "source_row_hash": self.source_row_hash,
        }

    def validate(self) -> None:
        for name in ("stable_security_id", "vendor_instrument_id", "ticker", "mic"):
            _canonical_text(getattr(self, name), f"identity interval {name}")
        if self.ticker != self.ticker.upper() or self.mic != self.mic.upper():
            raise ContractError("historical ticker and MIC must be uppercase")
        for name in ("issuer_id", "share_class_id"):
            value = getattr(self, name)
            if value is not None:
                _canonical_text(value, f"identity interval {name}")
        if self.security_type not in SECURITY_TYPES:
            raise ContractError("historical security type is invalid")
        if self.listing_state not in LISTING_STATES:
            raise ContractError("historical listing state is invalid")
        if type(self.effective_start) is not date or (
            self.effective_end is not None and type(self.effective_end) is not date
        ):
            raise ContractError("identity effective bounds must be exact dates")
        if self.effective_end is not None and self.effective_end < self.effective_start:
            raise ContractError("identity effective interval is inverted")
        self.availability.validate()
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise ContractError("identity interval revision must be positive")
        if self.revision_number == 1 and self.predecessor_row_id is not None:
            raise ContractError("first identity interval revision cannot name a predecessor")
        if self.revision_number > 1:
            require_sha256(self.predecessor_row_id, "identity predecessor_row_id")
        require_sha256(self.source_row_hash, "identity source_row_hash")
        require_sha256(self.row_id, "identity interval row_id")
        if self.row_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("identity interval ID differs from its content")


def visible_identity_as_of(
    rows: Iterable[HistoricalIdentityInterval],
    *,
    session: date,
    signal_cutoff: datetime,
) -> tuple[HistoricalIdentityInterval, ...]:
    if type(session) is not date:
        raise ContractError("identity view session must be an exact date")
    cutoff = require_aware_utc(signal_cutoff, "identity signal_cutoff")
    candidates: dict[tuple[str, date], HistoricalIdentityInterval] = {}
    for row in rows:
        row.validate()
        if row.availability.usable_time > cutoff or row.effective_start > session:
            continue
        if row.effective_end is not None and session > row.effective_end:
            continue
        key = (row.stable_security_id, row.effective_start)
        prior = candidates.get(key)
        if prior is None or (
            row.revision_number,
            row.availability.usable_time,
            row.row_id,
        ) > (
            prior.revision_number,
            prior.availability.usable_time,
            prior.row_id,
        ):
            candidates[key] = row
    by_security: dict[str, list[HistoricalIdentityInterval]] = defaultdict(list)
    for row in candidates.values():
        by_security[row.stable_security_id].append(row)
    resolved: list[HistoricalIdentityInterval] = []
    for stable_id, versions in by_security.items():
        if len(versions) != 1:
            raise ContractError(
                f"ambiguous historical identity intervals for stable security {stable_id}"
            )
        resolved.append(versions[0])
    venue_tickers = [(row.mic, row.ticker) for row in resolved]
    if len(venue_tickers) != len(set(venue_tickers)):
        raise ContractError("historical ticker mapping is ambiguous at the cutoff")
    return tuple(sorted(resolved, key=lambda row: row.stable_security_id))


@dataclass(frozen=True)
class IdentityIntervalAudit:
    row_count: int
    stable_security_count: int
    ticker_change_count: int
    ticker_reuse_count: int
    inactive_or_delisted_count: int
    unknown_security_type_count: int
    ambiguous_overlap_count: int
    revision_chain_error_count: int
    audit_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "row_count": self.row_count,
            "stable_security_count": self.stable_security_count,
            "ticker_change_count": self.ticker_change_count,
            "ticker_reuse_count": self.ticker_reuse_count,
            "inactive_or_delisted_count": self.inactive_or_delisted_count,
            "unknown_security_type_count": self.unknown_security_type_count,
            "ambiguous_overlap_count": self.ambiguous_overlap_count,
            "revision_chain_error_count": self.revision_chain_error_count,
        }


def _intervals_overlap(
    left: HistoricalIdentityInterval,
    right: HistoricalIdentityInterval,
) -> bool:
    left_end = left.effective_end or date.max
    right_end = right.effective_end or date.max
    return left.effective_start <= right_end and right.effective_start <= left_end


def audit_identity_intervals(
    rows: Iterable[HistoricalIdentityInterval],
) -> IdentityIntervalAudit:
    materialized = tuple(rows)
    if not materialized:
        raise ContractError("historical identity audit requires rows")
    for row in materialized:
        row.validate()
    if len({row.row_id for row in materialized}) != len(materialized):
        raise ContractError("historical identity audit contains duplicate rows")

    chain_errors = 0
    revisions: dict[tuple[str, date], list[HistoricalIdentityInterval]] = defaultdict(list)
    for row in materialized:
        revisions[(row.stable_security_id, row.effective_start)].append(row)
    for versions in revisions.values():
        ordered = sorted(versions, key=lambda row: row.revision_number)
        if [row.revision_number for row in ordered] != list(range(1, len(ordered) + 1)):
            chain_errors += 1
            continue
        for prior, current in zip(ordered, ordered[1:]):
            if current.predecessor_row_id != prior.row_id:
                chain_errors += 1

    latest = [
        max(versions, key=lambda row: (row.revision_number, row.availability.usable_time))
        for versions in revisions.values()
    ]
    overlaps = 0
    for index, left in enumerate(latest):
        for right in latest[index + 1 :]:
            same_security = left.stable_security_id == right.stable_security_id
            same_venue_ticker = left.mic == right.mic and left.ticker == right.ticker
            if (same_security or same_venue_ticker) and _intervals_overlap(left, right):
                overlaps += 1

    tickers_by_security: dict[str, set[tuple[str, str]]] = defaultdict(set)
    securities_by_ticker: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in latest:
        tickers_by_security[row.stable_security_id].add((row.mic, row.ticker))
        securities_by_ticker[(row.mic, row.ticker)].add(row.stable_security_id)
    ticker_changes = sum(max(0, len(values) - 1) for values in tickers_by_security.values())
    ticker_reuse = sum(
        1 for values in securities_by_ticker.values() if len(values) > 1
    )
    unsigned = {
        "row_count": len(materialized),
        "stable_security_count": len(tickers_by_security),
        "ticker_change_count": ticker_changes,
        "ticker_reuse_count": ticker_reuse,
        "inactive_or_delisted_count": len(
            {
                row.stable_security_id
                for row in latest
                if row.listing_state in {"ACQUIRED", "BANKRUPT", "DELISTED", "INACTIVE"}
            }
        ),
        "unknown_security_type_count": sum(
            row.security_type == "UNKNOWN_AMBIGUOUS" for row in latest
        ),
        "ambiguous_overlap_count": overlaps,
        "revision_chain_error_count": chain_errors,
    }
    return IdentityIntervalAudit(
        **unsigned,
        audit_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


@dataclass(frozen=True)
class HistoricalRawDailyBar:
    stable_security_id: str
    source_symbol: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    availability: AvailabilityStamp
    source_partition_hash: str
    source_row_hash: str
    synthetic: bool
    interpolated: bool
    forward_filled: bool
    halted: bool
    row_id: str

    @classmethod
    def create(cls, **fields: object) -> "HistoricalRawDailyBar":
        provisional = cls(**fields, row_id="")
        value = cls(
            **{
                **provisional.__dict__,
                "row_id": sha256_bytes(canonical_json_bytes(provisional.unsigned_dict())),
            }
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "source_symbol": self.source_symbol,
            "session": self.session.isoformat(),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": self.volume,
            "availability": self.availability.as_dict(),
            "source_partition_hash": self.source_partition_hash,
            "source_row_hash": self.source_row_hash,
            "synthetic": self.synthetic,
            "interpolated": self.interpolated,
            "forward_filled": self.forward_filled,
            "halted": self.halted,
        }

    def validate(self) -> None:
        _canonical_text(self.stable_security_id, "raw bar stable_security_id")
        _canonical_text(self.source_symbol, "raw bar source_symbol")
        if self.source_symbol != self.source_symbol.upper():
            raise ContractError("raw bar source symbol must be uppercase")
        if type(self.session) is not date:
            raise ContractError("raw bar session must be an exact date")
        prices = (self.open, self.high, self.low, self.close)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in prices
        ):
            raise ContractError("raw bar OHLC must be positive finite values")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ) or self.low > self.high:
            raise ContractError("raw bar violates OHLC relationships")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ContractError("raw bar volume must be a nonnegative integer")
        self.availability.validate()
        require_sha256(self.source_partition_hash, "raw bar source_partition_hash")
        require_sha256(self.source_row_hash, "raw bar source_row_hash")
        for field in ("synthetic", "interpolated", "forward_filled", "halted"):
            if type(getattr(self, field)) is not bool:
                raise ContractError(f"raw bar {field} must be boolean")
        require_sha256(self.row_id, "raw bar row_id")
        if self.row_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("raw bar ID differs from its content")


@dataclass(frozen=True)
class RawBarAudit:
    total_rows: int
    admitted_rows: int
    quarantined_rows: int
    reason_counts: tuple[tuple[str, int], ...]
    admitted_row_ids: tuple[str, ...]
    audit_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "total_rows": self.total_rows,
            "admitted_rows": self.admitted_rows,
            "quarantined_rows": self.quarantined_rows,
            "reason_counts": dict(self.reason_counts),
            "admitted_row_ids": list(self.admitted_row_ids),
        }


def audit_raw_daily_bars(
    rows: Iterable[HistoricalRawDailyBar],
    *,
    identities: Iterable[HistoricalIdentityInterval],
    session_closes: Mapping[date, datetime],
) -> RawBarAudit:
    """Classify each row exactly once; never repair, fill, or silently drop it."""

    materialized = tuple(rows)
    identity_rows = tuple(identities)
    if not materialized or not identity_rows or not session_closes:
        raise ContractError("raw bar audit requires bars, identities, and sessions")
    seen: Counter[tuple[str, date]] = Counter(
        (row.stable_security_id, row.session) for row in materialized
    )
    reasons: Counter[str] = Counter()
    admitted: list[str] = []
    for row in materialized:
        row_reasons: set[str] = set()
        try:
            row.validate()
        except (ContractError, IntegrityError):
            row_reasons.add("INVALID_BAR")
        if seen[(row.stable_security_id, row.session)] > 1:
            row_reasons.add("DUPLICATE_SECURITY_DATE")
        close_at = session_closes.get(row.session)
        if close_at is None:
            row_reasons.add("OUTSIDE_SUPPORTED_SESSION")
        else:
            close_at = require_aware_utc(close_at, "session close")
            if row.availability.effective_time < close_at:
                row_reasons.add("DAILY_BAR_EFFECTIVE_BEFORE_SESSION_CLOSE")
        if row.synthetic:
            row_reasons.add("SYNTHETIC_BAR")
        if row.interpolated:
            row_reasons.add("INTERPOLATED_BAR")
        if row.forward_filled:
            row_reasons.add("FORWARD_FILLED_BAR")
        try:
            visible = visible_identity_as_of(
                identity_rows,
                session=row.session,
                signal_cutoff=row.availability.usable_time,
            )
        except ContractError:
            visible = ()
            row_reasons.add("AMBIGUOUS_SECURITY_ID")
        match = [
            item for item in visible if item.stable_security_id == row.stable_security_id
        ]
        if len(match) != 1:
            row_reasons.add("UNRESOLVED_SECURITY_ID")
        elif match[0].ticker != row.source_symbol:
            row_reasons.add("INVALID_HISTORICAL_TICKER")
        if row_reasons:
            for reason in row_reasons:
                reasons[reason] += 1
        else:
            admitted.append(row.row_id)
    unsigned = {
        "total_rows": len(materialized),
        "admitted_rows": len(admitted),
        "quarantined_rows": len(materialized) - len(admitted),
        "reason_counts": dict(sorted(reasons.items())),
        "admitted_row_ids": sorted(admitted),
    }
    return RawBarAudit(
        total_rows=len(materialized),
        admitted_rows=len(admitted),
        quarantined_rows=len(materialized) - len(admitted),
        reason_counts=tuple(sorted(reasons.items())),
        admitted_row_ids=tuple(sorted(admitted)),
        audit_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


@dataclass(frozen=True)
class StructuralUniverseRow:
    stable_security_id: str
    ticker: str
    mic: str
    security_type: str
    session: date
    structural_eligible: bool
    completed_session_close: float | None
    completed_session_volume: int | None
    completed_session_dollar_volume: float | None
    valid_observation_count: int
    halted: bool
    reason_codes: tuple[str, ...]
    usable_at: datetime
    row_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "ticker": self.ticker,
            "mic": self.mic,
            "security_type": self.security_type,
            "session": self.session.isoformat(),
            "structural_eligible": self.structural_eligible,
            "completed_session_close": self.completed_session_close,
            "completed_session_volume": self.completed_session_volume,
            "completed_session_dollar_volume": self.completed_session_dollar_volume,
            "valid_observation_count": self.valid_observation_count,
            "halted": self.halted,
            "reason_codes": list(self.reason_codes),
            "usable_at": iso_z(self.usable_at),
        }


def build_structural_universe_view(
    *,
    identity_rows: Iterable[HistoricalIdentityInterval],
    bars: Iterable[HistoricalRawDailyBar],
    session: date,
    signal_cutoff: datetime,
    minimum_history_sessions: int | None = None,
    include_adrs: bool = False,
) -> tuple[StructuralUniverseRow, ...]:
    """Build structural state and causal inputs without choosing alpha thresholds."""

    cutoff = require_aware_utc(signal_cutoff, "structural universe signal_cutoff")
    if type(session) is not date:
        raise ContractError("structural universe session must be an exact date")
    if minimum_history_sessions is not None and (
        isinstance(minimum_history_sessions, bool)
        or not isinstance(minimum_history_sessions, int)
        or minimum_history_sessions < 1
    ):
        raise ContractError("minimum history must be a positive integer or unresolved")
    all_identity_rows = tuple(identity_rows)
    identities = visible_identity_as_of(
        all_identity_rows,
        session=session,
        signal_cutoff=cutoff,
    )
    visible_ids = {row.stable_security_id for row in identities}
    known_future: dict[str, HistoricalIdentityInterval] = {}
    for row in all_identity_rows:
        row.validate()
        if (
            row.stable_security_id in visible_ids
            or row.effective_start <= session
            or row.availability.usable_time > cutoff
        ):
            continue
        prior = known_future.get(row.stable_security_id)
        if prior is None or (
            row.effective_start,
            -row.revision_number,
            row.row_id,
        ) < (
            prior.effective_start,
            -prior.revision_number,
            prior.row_id,
        ):
            known_future[row.stable_security_id] = row
    materialized_bars = tuple(bars)
    for bar in materialized_bars:
        bar.validate()
    by_security: dict[str, list[HistoricalRawDailyBar]] = defaultdict(list)
    for bar in materialized_bars:
        if bar.session <= session and bar.availability.usable_time <= cutoff:
            by_security[bar.stable_security_id].append(bar)
    result: list[StructuralUniverseRow] = []
    allowed_types = set(V1_INCLUDED_SECURITY_TYPES)
    if include_adrs:
        allowed_types.add("ADR")
    for identity in identities:
        reasons: set[str] = set()
        structural = identity.listing_state == "ACTIVE"
        if identity.security_type not in allowed_types:
            reasons.add("UNSUPPORTED_SECURITY_TYPE")
            structural = False
        if identity.listing_state == "HALTED":
            reasons.add("HALTED")
            structural = False
        elif identity.listing_state in {"DELISTED", "ACQUIRED", "BANKRUPT"}:
            reasons.add("DELISTED")
            structural = False
        elif identity.listing_state == "INACTIVE":
            reasons.add("INACTIVE")
            structural = False
        history = sorted(
            by_security.get(identity.stable_security_id, ()),
            key=lambda row: row.session,
        )
        latest = history[-1] if history else None
        current = next((bar for bar in reversed(history) if bar.session == session), None)
        if current is None:
            reasons.update({"MISSING_CAUSAL_PRICE", "MISSING_CAUSAL_VOLUME"})
        if minimum_history_sessions is not None and len(history) < minimum_history_sessions:
            reasons.add("MISSING_REQUIRED_HISTORY")
        halted = current.halted if current is not None else identity.listing_state == "HALTED"
        if halted:
            reasons.add("HALTED")
            structural = False
        if latest is not None and latest.session < session:
            reasons.add("STALE_OBSERVATION")
        if structural:
            reasons.add("ELIGIBLE_STRUCTURAL")
        usable_at = max(
            [identity.availability.usable_time]
            + ([current.availability.usable_time] if current is not None else [])
        )
        unsigned = {
            "stable_security_id": identity.stable_security_id,
            "ticker": identity.ticker,
            "mic": identity.mic,
            "security_type": identity.security_type,
            "session": session.isoformat(),
            "structural_eligible": structural,
            "completed_session_close": float(current.close) if current is not None else None,
            "completed_session_volume": current.volume if current is not None else None,
            "completed_session_dollar_volume": (
                float(current.close) * current.volume if current is not None else None
            ),
            "valid_observation_count": len(history),
            "halted": halted,
            "reason_codes": sorted(reasons),
            "usable_at": iso_z(usable_at),
        }
        result.append(
            StructuralUniverseRow(
                stable_security_id=identity.stable_security_id,
                ticker=identity.ticker,
                mic=identity.mic,
                security_type=identity.security_type,
                session=session,
                structural_eligible=structural,
                completed_session_close=(
                    float(current.close) if current is not None else None
                ),
                completed_session_volume=current.volume if current is not None else None,
                completed_session_dollar_volume=(
                    float(current.close) * current.volume if current is not None else None
                ),
                valid_observation_count=len(history),
                halted=halted,
                reason_codes=tuple(sorted(reasons)),
                usable_at=usable_at,
                row_id=sha256_bytes(canonical_json_bytes(unsigned)),
            )
        )
    for identity in known_future.values():
        unsigned = {
            "stable_security_id": identity.stable_security_id,
            "ticker": identity.ticker,
            "mic": identity.mic,
            "security_type": identity.security_type,
            "session": session.isoformat(),
            "structural_eligible": False,
            "completed_session_close": None,
            "completed_session_volume": None,
            "completed_session_dollar_volume": None,
            "valid_observation_count": 0,
            "halted": False,
            "reason_codes": ["NOT_YET_LISTED"],
            "usable_at": iso_z(identity.availability.usable_time),
        }
        result.append(
            StructuralUniverseRow(
                stable_security_id=identity.stable_security_id,
                ticker=identity.ticker,
                mic=identity.mic,
                security_type=identity.security_type,
                session=session,
                structural_eligible=False,
                completed_session_close=None,
                completed_session_volume=None,
                completed_session_dollar_volume=None,
                valid_observation_count=0,
                halted=False,
                reason_codes=("NOT_YET_LISTED",),
                usable_at=identity.availability.usable_time,
                row_id=sha256_bytes(canonical_json_bytes(unsigned)),
            )
        )
    return tuple(sorted(result, key=lambda row: row.stable_security_id))


def require_source_admitted_bar(
    bar: CausalDailyBar,
    admission: SourceAdmissionResult,
) -> CausalDailyBar:
    """Bind canonical-panel bar loading to an admitted raw-OHLCV package."""

    bar.validate()
    admission.validate()
    if (
        admission.source_family != "RAW_DAILY_OHLCV"
        or not admission.research_eligible
        or admission.status != "ADMITTED"
        or bar.adjustment_state != "RAW_OBSERVED"
        or bar.availability.source_identifier != admission.source_identifier
    ):
        raise ContractError("canonical panel bar lacks qualified raw-source admission")
    return bar
