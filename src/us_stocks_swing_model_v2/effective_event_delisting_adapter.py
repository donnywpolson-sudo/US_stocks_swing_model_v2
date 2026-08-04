"""Provider-neutral, unconfigured effective-event and delisting boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, require_sha256, sha256_bytes, sha256_file
from .errors import ContractError


POLICY_PATH = "config/effective_event_delisting_adapter_policy.json"
PROJECT = "US_stocks_swing_model_v2"
UNSELECTED = "BACKEND_UNSELECTED"
REQUIRED_CAPABILITIES = (
    "effective_event_census",
    "delisting_census",
    "asset_session_scope",
    "raw_as_received_lineage",
    "receipt_time_availability",
    "revision_and_late_arrival_history",
)


@dataclass(frozen=True)
class EffectiveEventDelistingEvidenceDescriptor:
    """Future provider evidence shape; it is not accepted while unconfigured."""

    provider_id: str
    source_epoch: str
    provider_contract_id: str
    raw_snapshot_ids: tuple[str, ...]
    effective_event_census_complete: bool
    delisting_census_complete: bool
    revision_history_complete: bool

    def validate_shape(self) -> None:
        if type(self.provider_id) is not str or not self.provider_id:
            raise ContractError("effective-event provider ID is required")
        if self.source_epoch == "alpaca_corporate_actions_v1":
            raise ContractError("Alpaca process-date evidence cannot be effective-event/delisting-complete")
        if type(self.source_epoch) is not str or not self.source_epoch:
            raise ContractError("effective-event source epoch is required")
        require_sha256(self.provider_contract_id, "effective_event.provider_contract_id")
        if not self.raw_snapshot_ids or self.raw_snapshot_ids != tuple(sorted(set(self.raw_snapshot_ids))):
            raise ContractError("effective-event raw snapshot census is invalid")
        for index, snapshot_id in enumerate(self.raw_snapshot_ids):
            require_sha256(snapshot_id, f"effective_event.raw_snapshot_ids[{index}]")
        if any(type(value) is not bool for value in (
            self.effective_event_census_complete,
            self.delisting_census_complete,
            self.revision_history_complete,
        )):
            raise ContractError("effective-event evidence flags must be boolean")


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("effective-event/delisting adapter policy is unreadable") from exc
    expected = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": "EFFECTIVE_EVENT_DELISTING_ADAPTER_QUALIFICATION_ONLY",
        "status": UNSELECTED,
        "required_capabilities": list(REQUIRED_CAPABILITIES),
        "coverage_contract": {
            "absence_requires_complete_census": True,
            "unresolved_rows_remain_in_denominator": True,
            "imputation_or_drop_allowed": False,
            "late_arrival_policy": "revise_outcome_or_abstain_never_backdate",
        },
        "prohibitions": [
            "alpaca_process_date_evidence_as_complete_effective_event_coverage",
            "source_activation",
            "release_publication",
            "outcome_access",
            "training",
            "evaluation",
        ],
    }
    if value != expected:
        raise ContractError("effective-event/delisting adapter policy differs")
    return value


def build_effective_event_delisting_qualification_plan(*, repository_root: Path) -> dict[str, object]:
    """Describe future provider qualification without selecting or calling one."""
    root = Path(repository_root).resolve(strict=True)
    policy = _load_policy(root)
    unsigned = {
        "schema_version": 1,
        "mode": policy["mode"],
        "policy_sha256": sha256_file(root / POLICY_PATH),
        "status": policy["status"],
        "required_capabilities": policy["required_capabilities"],
        "coverage_contract": policy["coverage_contract"],
        "provider_selection_required": True,
        "qualification_complete": False,
        "effective_event_coverage_usable": False,
        "delisting_coverage_usable": False,
        "authorities": {
            "network_calls": 0,
            "credentials_read": False,
            "raw_snapshot_write": False,
            "release_publication": False,
            "source_activation": False,
            "outcome_access": False,
            "training": False,
            "evaluation": False,
        },
    }
    return {**unsigned, "qualification_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def require_configured_effective_event_delisting_adapter(
    descriptor: EffectiveEventDelistingEvidenceDescriptor,
    *,
    repository_root: Path,
) -> None:
    """Reject all evidence until a separately reviewed provider is configured."""
    root = Path(repository_root).resolve(strict=True)
    _load_policy(root)
    descriptor.validate_shape()
    raise ContractError("effective-event/delisting provider is unselected and cannot clear coverage")
