"""Local-only census intake and S3 provisioning preparation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError
from .prospective_governance import HISTORICAL_CENSUS_SOURCES
from .s3_object_lock_trial_registry import S3ObjectLockRegistryPolicy, S3ObjectLockTrialRegistryTarget


@dataclass(frozen=True)
class HistoricalTrialCensusIntake:
    source_kind: str
    locator_sha256: str
    inspected: bool
    outcome_informed_attempt_count: int | None

    def validate(self) -> None:
        if self.source_kind not in HISTORICAL_CENSUS_SOURCES:
            raise ContractError("historical trial census source kind is invalid")
        require_sha256(self.locator_sha256, "historical_trial_census.locator_sha256")
        if type(self.inspected) is not bool:
            raise ContractError("historical trial census inspection state must be boolean")
        if self.outcome_informed_attempt_count is not None and (
            isinstance(self.outcome_informed_attempt_count, bool)
            or not isinstance(self.outcome_informed_attempt_count, int)
            or self.outcome_informed_attempt_count < 0
        ):
            raise ContractError("historical trial census count is invalid")


def build_historical_trial_census_intake_plan(
    sources: Iterable[HistoricalTrialCensusIntake],
) -> dict[str, object]:
    """Inventory declared evidence sources without scanning, writing, or concluding."""
    items = tuple(sources)
    if len(items) != len(HISTORICAL_CENSUS_SOURCES):
        raise ContractError("historical trial census intake requires every source kind")
    for item in items:
        if type(item) is not HistoricalTrialCensusIntake:
            raise ContractError("historical trial census intake row is invalid")
        item.validate()
    if tuple(item.source_kind for item in items) != HISTORICAL_CENSUS_SOURCES:
        raise ContractError("historical trial census intake source order differs")
    unsigned = {
        "schema_version": 1,
        "mode": "HISTORICAL_TRIAL_CENSUS_INTAKE_PLAN_ONLY",
        "sources": [{
            "source_kind": item.source_kind,
            "locator_sha256": item.locator_sha256,
            "inspected": item.inspected,
            "outcome_informed_attempt_count": item.outcome_informed_attempt_count,
        } for item in items],
        "completion": {
            "exact_census_complete": False,
            "status": "INDETERMINATE_BLOCKS_TRUSTED_GATE",
            "local_ledger_substitution_allowed": False,
        },
        "authorities": {"filesystem_discovery": False, "external_repository_access": False, "registration": False, "training": False, "evaluation": False},
    }
    return {**unsigned, "intake_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_s3_object_lock_provisioning_checklist(
    *,
    repository_root: Path,
    proposed_target: S3ObjectLockTrialRegistryTarget | None,
    aws_account_id: str | None,
    bucket_policy_sha256: str | None,
) -> dict[str, object]:
    """Validate local inputs for a future AWS gate without importing credentials or SDK clients."""
    root = Path(repository_root).resolve(strict=True)
    policy = S3ObjectLockRegistryPolicy.load(root / "config/trial_registry_s3_object_lock_policy.json", repository_root=root)
    if policy.status != "BACKEND_SELECTED_NOT_CONFIGURED":
        raise ContractError("S3 provisioning checklist is only valid before configuration")
    configured = proposed_target is not None and aws_account_id is not None and bucket_policy_sha256 is not None
    if configured:
        proposed_target.validate()
        if type(aws_account_id) is not str or not aws_account_id.isdigit() or len(aws_account_id) != 12:
            raise ContractError("AWS account ID must be a 12-digit text value")
        require_sha256(bucket_policy_sha256, "bucket_policy_sha256")
    unsigned = {
        "schema_version": 1,
        "mode": "S3_OBJECT_LOCK_PROVISIONING_CHECKLIST_ONLY",
        "policy_id": policy.policy_id,
        "backend": "AWS_S3_OBJECT_LOCK_COMPLIANCE",
        "minimum_retention_days": policy.minimum_retention_days,
        "current_status": policy.status,
        "required_before_external_gate": ["dedicated_bucket", "ObjectLockEnabled", "COMPLIANCE_retention_at_least_3650_days", "version_id", "bucket_policy_hash", "project_only_prefix", "write_read_retention_verification"],
        "proposed_target": None if proposed_target is None else {"bucket": proposed_target.bucket, "region": proposed_target.region, "prefix": proposed_target.prefix, "version_id": proposed_target.version_id},
        "aws_account_id": aws_account_id,
        "bucket_policy_sha256": bucket_policy_sha256,
        "authorities": {"credentials_read": False, "aws_calls": 0, "bucket_creation": False, "object_write": False, "trial_registration": False},
    }
    return {**unsigned, "provisioning_checklist_id": sha256_bytes(canonical_json_bytes(unsigned))}
