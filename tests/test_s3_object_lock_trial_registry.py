from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import EvaluationAuthorizationError
from us_stocks_swing_model_v2.s3_object_lock_trial_registry import (
    S3ObjectLockRegistryPolicy,
    S3ObjectLockTrialRegistryTarget,
    load_s3_object_lock_trial_registration,
)
from us_stocks_swing_model_v2.trials import TrialSpec


class _Reader:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs: str) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


def _policy(repo_root: Path) -> S3ObjectLockRegistryPolicy:
    policy = S3ObjectLockRegistryPolicy.load(
        repo_root / "config" / "trial_registry_s3_object_lock_policy.json",
        repository_root=repo_root,
    )
    return S3ObjectLockRegistryPolicy(
        policy_id=policy.policy_id,
        minimum_retention_days=policy.minimum_retention_days,
        status="CONFIGURED",
    )


def _payload(*, binding_id: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "hypothesis_id": "fixed_discovery_hypothesis",
        "evidence_class": "REGISTERED_HISTORICAL_DISCOVERY",
        "data_release_ids": ["a" * 64],
        "release_bindings": [{"release_id": "a" * 64, "project": "US_stocks_swing_model_v2", "dataset": "alpaca_discovery_proxy_features", "source_epoch": "legacy_alpaca", "role": "legacy_discovery_only", "quality_state": "LEGACY_CAVEATED", "created_at": "2026-07-30T00:00:00Z", "event_start": "2016-01-04T00:00:00Z", "event_end": "2026-07-10T00:00:00Z"}],
        "feature_schema_id": "c" * 64,
        "outcome_schema_id": "d" * 64,
        "split_plan_id": "e" * 64,
        "model_family": "fixed_linear",
        "primary_metric": "multiclass_log_loss",
        "primary_gate_id": "f" * 64,
        "robustness_policy_id": "1" * 64,
        "cost_policy_id": "cost_policy_v1",
        "trial_family_id": "discovery_family_v1",
        "census_anchor_id": "2" * 64,
        "trial_family_anchor_id": "3" * 64,
        "evaluator_closure_hash": "4" * 64,
        "governance_contract_hash": "5" * 64,
        "code_hash": "6" * 64,
        "config_hash": "7" * 64,
        "environment_hash": "8" * 64,
        "trial_id": "0" * 64,
        "registered_at": "2026-07-31T00:00:00Z",
        "trial_registry_binding_id": binding_id,
    }
    payload["trial_id"] = TrialSpec.from_registered_payload(payload).trial_id
    return payload


def _target() -> S3ObjectLockTrialRegistryTarget:
    return S3ObjectLockTrialRegistryTarget(
        bucket="swing-model-trial-registry",
        region="us-west-2",
        prefix="trial-registry/v1",
        version_id="3HL4kqtJlcpXrof3fjVBH40Nr8X8gXbo",
    )


def test_loads_only_a_versioned_compliance_retained_trial_record() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    reader = _Reader({"VersionId": target.version_id, "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": datetime.now(timezone.utc) + timedelta(days=3651), "Body": BytesIO(canonical_json_bytes(payload))})

    record = load_s3_object_lock_trial_registration(reader=reader, policy=policy, target=target, trial_id=trial_id)

    assert record.trial_id == trial_id
    assert len(record.external_anchor_receipt_id) == 64
    assert reader.calls == [{"Bucket": target.bucket, "Key": target.key_for(trial_id), "VersionId": target.version_id}]


@pytest.mark.parametrize("field, value, message", [("ObjectLockMode", "GOVERNANCE", "Compliance"), ("VersionId", "other", "version")])
def test_rejects_nonimmutable_or_wrong_version_evidence(field: str, value: object, message: str) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    response: dict[str, object] = {"VersionId": target.version_id, "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": datetime.now(timezone.utc) + timedelta(days=3651), "Body": BytesIO(canonical_json_bytes(payload))}
    response[field] = value
    with pytest.raises(EvaluationAuthorizationError, match=message):
        load_s3_object_lock_trial_registration(reader=_Reader(response), policy=policy, target=target, trial_id=trial_id)


def test_rejects_short_retention_and_local_backend_status() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    selected = S3ObjectLockRegistryPolicy.load(repo_root / "config" / "trial_registry_s3_object_lock_policy.json", repository_root=repo_root)
    target = _target()
    trial_id = "9" * 64
    with pytest.raises(EvaluationAuthorizationError, match="not configured"):
        load_s3_object_lock_trial_registration(reader=_Reader({}), policy=selected, target=target, trial_id=trial_id)

    policy = _policy(repo_root)
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    response = {"VersionId": target.version_id, "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": datetime.now(timezone.utc) + timedelta(days=1), "Body": BytesIO(canonical_json_bytes(payload))}
    with pytest.raises(EvaluationAuthorizationError, match="shorter"):
        load_s3_object_lock_trial_registration(reader=_Reader(response), policy=policy, target=target, trial_id=trial_id)
