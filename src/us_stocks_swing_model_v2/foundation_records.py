"""Validators for durable historical-foundation records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError


GATE_RESULTS = {"PASS", "PASS_WITH_CAVEATS", "BLOCKED", "FAIL"}
REQUIRED_GATES = (
    "CAPTURE_BASELINE_PRESERVED",
    "HISTORICAL_SOURCE_INVENTORY_COMPLETE",
    "POINT_IN_TIME_SECURITY_IDENTITY",
    "SURVIVORSHIP_AND_DELISTING_TREATMENT",
    "CORPORATE_ACTION_RECONCILIATION",
    "SESSION_AND_TIMESTAMP_INTEGRITY",
    "CAUSAL_UNIVERSE_CONSTRUCTION",
    "FEATURE_PREFIX_INVARIANCE",
    "FUTURE_MUTATION_INVARIANCE",
    "AVAILABILITY_ENFORCEMENT",
    "OUTCOME_FIREWALL",
    "RESEARCH_PROTOCOL_FROZEN",
    "SYNTHETIC_END_TO_END_REHEARSAL",
    "CLEAN_WORKTREE_AND_REPRODUCIBLE_TESTS",
)


def load_content_addressed_record(path: Path, *, id_field: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"record is missing or invalid JSON: {path}") from exc
    if type(payload) is not dict or id_field not in payload:
        raise ContractError(f"record lacks {id_field}")
    record_id = payload[id_field]
    require_sha256(record_id, id_field)
    unsigned = dict(payload)
    unsigned.pop(id_field)
    if record_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError(f"{id_field} differs from record content")
    return payload


def validate_historical_source_inventory(path: Path) -> dict[str, Any]:
    payload = load_content_addressed_record(path, id_field="inventory_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != "US_stocks_swing_model_v2"
        or payload.get("record_type") != "HISTORICAL_SOURCE_INVENTORY"
        or payload.get("inventory_scope")
        != "METADATA_AND_BOUNDED_REPRESENTATIVE_RECORDS_ONLY"
    ):
        raise ContractError("historical source inventory identity differs")
    datasets = payload.get("datasets")
    if type(datasets) is not list or not datasets:
        raise ContractError("historical source inventory requires datasets")
    source_ids = [item.get("source_id") for item in datasets if type(item) is dict]
    if len(source_ids) != len(datasets) or len(source_ids) != len(set(source_ids)):
        raise ContractError("historical source inventory IDs must be unique")
    required_source_kinds = {
        "DAILY_RAW_OHLCV",
        "DATED_LISTING_STATUS_CANDIDATE",
        "CORPORATE_ACTION_PROCESS_DATE_CAPTURES",
        "EXCHANGE_SESSION_CALENDAR",
        "SECURITY_IDENTITY_AND_MEMBERSHIP_SNAPSHOT",
    }
    if not required_source_kinds.issubset(
        {item.get("source_kind") for item in datasets}
    ):
        raise ContractError("historical source inventory omits a discovered source class")
    claims = payload.get("claims")
    if (
        type(claims) is not dict
        or claims.get("massive_datasets_copied") is not False
        or claims.get("real_outcomes_accessed") is not False
        or claims.get("real_labels_accessed") is not False
        or claims.get("historical_point_in_time_foundation_complete") is not False
        or claims.get("source_dependent_readiness") != "BLOCKED"
    ):
        raise ContractError("historical source inventory overstates its evidence")
    missing = payload.get("unavailable_or_unqualified_source_classes")
    if type(missing) is not list or len(missing) < 9:
        raise ContractError("historical source blockers are incomplete")
    return payload


def validate_historical_research_protocol(path: Path) -> dict[str, Any]:
    payload = load_content_addressed_record(path, id_field="protocol_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != "US_stocks_swing_model_v2"
        or payload.get("record_type")
        != "FUTURE_REAL_OUTCOME_RESEARCH_PROTOCOL_PREREGISTRATION"
    ):
        raise ContractError("historical research protocol identity differs")
    authority = payload.get("authority")
    denied = (
        "this_protocol_authorizes_outcome_access",
        "this_protocol_authorizes_training",
        "this_protocol_authorizes_evaluation",
        "this_protocol_authorizes_holdout_access",
    )
    if type(authority) is not dict or any(authority.get(name) is not False for name in denied):
        raise ContractError("foundation protocol cannot authorize later phases")
    holdout = payload.get("final_holdout")
    if (
        type(holdout) is not dict
        or holdout.get("populated") is not False
        or holdout.get("ordinary_development_access") is not False
        or holdout.get("success_authorizes_candidate_sealing") is not False
    ):
        raise ContractError("foundation protocol exposes or populates the holdout")
    decisions = payload.get("unresolved_decisions")
    if type(decisions) is not list or not decisions:
        raise ContractError("foundation protocol must retain unresolved decisions")
    claims = payload.get("claims")
    if type(claims) is not dict or any(
        claims.get(name) is not False
        for name in (
            "real_outcomes_created",
            "real_outcomes_accessed",
            "holdout_accessed",
            "model_trained_on_real_outcomes",
            "performance_evaluated",
            "protocol_passing_unlocks_outcomes",
        )
    ):
        raise ContractError("foundation protocol claims prohibited work")
    return payload


def validate_historical_foundation_gate(path: Path) -> dict[str, Any]:
    payload = load_content_addressed_record(path, id_field="gate_record_id")
    if (
        payload.get("schema_version") != 1
        or payload.get("project") != "US_stocks_swing_model_v2"
        or payload.get("phase_status") not in GATE_RESULTS
        or payload.get("source_dependent_readiness") != "BLOCKED"
        or payload.get("future_real_outcome_phase_eligible") is not False
        or payload.get("automatic_outcome_unlock") is not False
    ):
        raise ContractError("historical foundation readiness state differs")
    gates = payload.get("gates")
    if type(gates) is not list:
        raise ContractError("historical foundation gate census is invalid")
    names = tuple(item.get("gate") for item in gates if type(item) is dict)
    if names != REQUIRED_GATES:
        raise ContractError("historical foundation gates differ from the required order")
    if any(item.get("result") not in GATE_RESULTS for item in gates):
        raise ContractError("historical foundation gate result is invalid")
    if not any(item.get("result") == "BLOCKED" for item in gates):
        raise ContractError("source-dependent foundation blockers were lost")
    return payload
