from __future__ import annotations

from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    sha256_bytes,
)
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError
from us_stocks_swing_model_v2.governance import (
    LocalIntegrityRecord,
    create_local_integrity_record,
    release_bindings_hash,
)
from us_stocks_swing_model_v2.s3_object_lock_trial_registry import (
    S3ObjectLockRegistryPolicy,
    S3ObjectLockTrialRegistryLocation,
    S3ObjectLockTrialRegistryTarget,
    create_aws_s3_object_lock_client,
    load_s3_object_lock_trial_registration,
    register_s3_object_lock_trial,
)
from us_stocks_swing_model_v2.trials import TrialSpec


class _Reader:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs: str) -> dict[str, object]:
        self.calls.append(kwargs)
        return self.response


class _Client(_Reader):
    def __init__(self, response: dict[str, object]) -> None:
        super().__init__(response)
        self.puts: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.puts.append(kwargs)
        payload = json.loads(bytes(kwargs["Body"]).decode("utf-8"))
        self.response["VersionId"] = "3HL4kqtJlcpXrof3fjVBH40Nr8X8gXbo"
        self.response["ObjectLockMode"] = kwargs["ObjectLockMode"]
        self.response["ObjectLockRetainUntilDate"] = kwargs["ObjectLockRetainUntilDate"]
        self.response["LastModified"] = parse_utc_z(
            payload["registered_at"],
            "registered_at",
        )
        self.response["Metadata"] = dict(kwargs["Metadata"])
        self.response["Body"] = BytesIO(kwargs["Body"])
        return {"VersionId": self.response["VersionId"]}


def _policy(repo_root: Path) -> S3ObjectLockRegistryPolicy:
    policy = S3ObjectLockRegistryPolicy.load(
        repo_root / "config" / "trial_registry_s3_object_lock_policy.json",
        repository_root=repo_root,
    )
    unsigned = policy.unsigned_dict() | {"status": "CONFIGURED"}
    return S3ObjectLockRegistryPolicy(
        policy_id=sha256_bytes(canonical_json_bytes(unsigned)),
        minimum_retention_days=policy.minimum_retention_days,
        status="CONFIGURED",
    )


def _payload(
    *,
    binding_id: str,
    evidence_class: str = "REGISTERED_HISTORICAL_DISCOVERY",
    role: str = "active_historical",
    quality_state: str = "PASS",
    registered_at: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "hypothesis_id": "fixed_discovery_hypothesis",
        "evidence_class": evidence_class,
        "data_release_ids": ["a" * 64],
        "release_bindings": [{"release_id": "a" * 64, "project": "US_stocks_swing_model_v2", "dataset": "eligible_features", "source_epoch": "accepted_causal_v1", "role": role, "quality_state": quality_state, "created_at": "2026-07-30T00:00:00Z", "event_start": "2016-01-04T00:00:00Z", "event_end": "2026-07-10T00:00:00Z"}],
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
        "registered_at": registered_at or iso_z(datetime.now(timezone.utc)),
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


def _spec() -> TrialSpec:
    payload = _payload(binding_id="0" * 64)
    return TrialSpec.from_registered_payload(payload)


def _action_record(
    *,
    policy: S3ObjectLockRegistryPolicy,
    binding_id: str,
    spec: TrialSpec,
) -> LocalIntegrityRecord:
    return create_local_integrity_record(
        scope="AUTHORIZE_EXTERNAL_TRIAL_REGISTRATION",
        subject_id=spec.trial_id,
        bindings={
            "policy_id": policy.policy_id,
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "trial_registry_binding_id": binding_id,
        },
        clock=TrustedClock.production(),
    )


def _response(
    *,
    target: S3ObjectLockTrialRegistryTarget,
    payload: dict[str, object],
    action_record: LocalIntegrityRecord,
    retention_days: int = 3651,
    last_modified: datetime | None = None,
) -> dict[str, object]:
    registered_at = parse_utc_z(str(payload["registered_at"]), "registered_at")
    action_recorded_at = parse_utc_z(
        action_record.recorded_at,
        "action_record.recorded_at",
    )
    return {
        "VersionId": target.version_id,
        "ObjectLockMode": "COMPLIANCE",
        "ObjectLockRetainUntilDate": registered_at + timedelta(days=retention_days),
        "LastModified": last_modified or max(registered_at, action_recorded_at),
        "Metadata": {
            "registration-authorization-record-id": action_record.record_id,
        },
        "Body": BytesIO(canonical_json_bytes(payload)),
    }


def test_loads_only_a_versioned_compliance_retained_trial_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    spec = TrialSpec.from_registered_payload(payload)
    action_record = _action_record(
        policy=policy,
        binding_id=target.registry_binding_id(policy),
        spec=spec,
    )
    reader = _Reader(
        _response(
            target=target,
            payload=payload,
            action_record=action_record,
        )
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )

    record = load_s3_object_lock_trial_registration(
        reader=reader, policy=policy, target=target, trial_id=trial_id,
        verified_release_directories=(), accepted_release_root=repo_root,
        action_record=action_record,
    )

    assert record.trial_id == trial_id
    assert len(record.external_anchor_receipt_id) == 64
    assert record.registration_authorization_record_id == action_record.record_id
    assert record.object_created_at == iso_z(
        reader.response["LastModified"]  # type: ignore[arg-type]
    )
    assert reader.calls == [{"Bucket": target.bucket, "Key": target.key_for(trial_id), "VersionId": target.version_id}]


@pytest.mark.parametrize("field, value, message", [("ObjectLockMode", "GOVERNANCE", "Compliance"), ("VersionId", "other", "version")])
def test_rejects_nonimmutable_or_wrong_version_evidence(
    field: str,
    value: object,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    spec = TrialSpec.from_registered_payload(payload)
    action_record = _action_record(
        policy=policy,
        binding_id=target.registry_binding_id(policy),
        spec=spec,
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )
    response = _response(
        target=target,
        payload=payload,
        action_record=action_record,
    )
    response[field] = value
    with pytest.raises(EvaluationAuthorizationError, match=message):
        load_s3_object_lock_trial_registration(
            reader=_Reader(response), policy=policy, target=target, trial_id=trial_id,
            verified_release_directories=(), accepted_release_root=repo_root,
            action_record=action_record,
        )


def test_rejects_short_retention_and_local_backend_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    selected = S3ObjectLockRegistryPolicy.load(repo_root / "config" / "trial_registry_s3_object_lock_policy.json", repository_root=repo_root)
    target = _target()
    selected_spec = _spec()
    trial_id = selected_spec.trial_id
    selected_action = _action_record(
        policy=selected,
        binding_id=target.registry_binding_id(selected),
        spec=selected_spec,
    )
    with pytest.raises(EvaluationAuthorizationError, match="not configured"):
        load_s3_object_lock_trial_registration(
            reader=_Reader({}), policy=selected, target=target, trial_id=trial_id,
            verified_release_directories=(), accepted_release_root=repo_root,
            action_record=selected_action,
        )

    policy = _policy(repo_root)
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    spec = TrialSpec.from_registered_payload(payload)
    action_record = _action_record(
        policy=policy,
        binding_id=target.registry_binding_id(policy),
        spec=spec,
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )
    response = _response(
        target=target,
        payload=payload,
        action_record=action_record,
        retention_days=1,
    )
    with pytest.raises(EvaluationAuthorizationError, match="shorter"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(response), policy=policy, target=target, trial_id=trial_id,
            verified_release_directories=(), accepted_release_root=repo_root,
            action_record=action_record,
        )


def test_registration_requests_compliance_retention_and_reloads_its_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    location = S3ObjectLockTrialRegistryLocation(bucket="swing-model-trial-registry", region="us-west-2", prefix="trial-registry/v1")
    spec = _spec()
    action_record = _action_record(
        policy=policy,
        binding_id=location.registry_binding_id(policy),
        spec=spec,
    )
    client = _Client({})
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )

    record = register_s3_object_lock_trial(
        client=client,
        policy=policy,
        location=location,
        spec=spec,
        verified_release_directories=(),
        accepted_release_root=tmp_path,
        action_record=action_record,
    )

    assert record.trial_id == spec.trial_id
    assert len(client.puts) == 1
    assert client.puts[0]["ObjectLockMode"] == "COMPLIANCE"
    assert client.puts[0]["Metadata"] == {
        "registration-authorization-record-id": action_record.record_id,
    }
    assert record.registration_authorization_record_id == action_record.record_id
    assert client.calls == [{"Bucket": location.bucket, "Key": location.key_for(spec.trial_id), "VersionId": "3HL4kqtJlcpXrof3fjVBH40Nr8X8gXbo"}]
    with pytest.raises(EvaluationAuthorizationError, match="already consumed"):
        register_s3_object_lock_trial(
            client=client,
            policy=policy,
            location=location,
            spec=spec,
            verified_release_directories=(),
            accepted_release_root=tmp_path,
            action_record=action_record,
        )
    assert len(client.puts) == 1


def test_registration_requires_exact_action_record_before_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    location = S3ObjectLockTrialRegistryLocation(
        bucket="swing-model-trial-registry",
        region="us-west-2",
        prefix="trial-registry/v1",
    )
    spec = _spec()
    wrong_action = create_local_integrity_record(
        scope="AUTHORIZE_EXTERNAL_TRIAL_REGISTRATION",
        subject_id=spec.trial_id,
        bindings={
            "policy_id": policy.policy_id,
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "trial_registry_binding_id": "0" * 64,
        },
        clock=TrustedClock.production(),
    )
    client = _Client({})
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )

    with pytest.raises(EvaluationAuthorizationError, match="bindings differ"):
        register_s3_object_lock_trial(
            client=client,
            policy=policy,
            location=location,
            spec=spec,
            verified_release_directories=(),
            accepted_release_root=tmp_path,
            action_record=wrong_action,
        )
    assert client.puts == []


def test_load_binds_action_metadata_and_authoritative_creation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    binding_id = target.registry_binding_id(policy)
    payload = _payload(binding_id=binding_id)
    spec = TrialSpec.from_registered_payload(payload)
    action_record = _action_record(
        policy=policy,
        binding_id=binding_id,
        spec=spec,
    )
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )

    mismatched_metadata = _response(
        target=target,
        payload=payload,
        action_record=action_record,
    )
    mismatched_metadata["Metadata"] = {
        "registration-authorization-record-id": "f" * 64,
    }
    with pytest.raises(EvaluationAuthorizationError, match="action record"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(mismatched_metadata),
            policy=policy,
            target=target,
            trial_id=spec.trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )

    missing_creation = _response(
        target=target,
        payload=payload,
        action_record=action_record,
    )
    missing_creation.pop("LastModified")
    with pytest.raises(EvaluationAuthorizationError, match="creation time"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(missing_creation),
            policy=policy,
            target=target,
            trial_id=spec.trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )

    future_creation = _response(
        target=target,
        payload=payload,
        action_record=action_record,
        last_modified=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    with pytest.raises(EvaluationAuthorizationError, match="creation time is in the future"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(future_creation),
            policy=policy,
            target=target,
            trial_id=spec.trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )

    backdated_payload = _payload(
        binding_id=binding_id,
        registered_at=iso_z(datetime.now(timezone.utc) - timedelta(days=1)),
    )
    backdated_response = _response(
        target=target,
        payload=backdated_payload,
        action_record=action_record,
        last_modified=datetime.now(timezone.utc),
    )
    with pytest.raises(EvaluationAuthorizationError, match="authoritative object creation"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(backdated_response),
            policy=policy,
            target=target,
            trial_id=spec.trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )


@pytest.mark.parametrize(
    ("evidence_class", "role", "quality_state", "message"),
    [
        (
            "REGISTERED_HISTORICAL_DISCOVERY",
            "qualification_evidence_only",
            "QUALIFICATION_EVIDENCE",
            "qualification evidence",
        ),
        (
            "REGISTERED_HISTORICAL_DISCOVERY",
            "legacy_discovery_only",
            "LEGACY_CAVEATED",
            "legacy releases",
        ),
        (
            "PROSPECTIVE_FINAL",
            "legacy_discovery_only",
            "LEGACY_CAVEATED",
            "legacy releases",
        ),
    ],
)
def test_s3_registry_enforces_release_roles_before_write_and_after_load(
    evidence_class: str,
    role: str,
    quality_state: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    location = S3ObjectLockTrialRegistryLocation(
        bucket="swing-model-trial-registry",
        region="us-west-2",
        prefix="trial-registry/v1",
    )
    spec_payload = _payload(
        binding_id="0" * 64,
        evidence_class=evidence_class,
        role=role,
        quality_state=quality_state,
    )
    spec = TrialSpec.from_registered_payload(spec_payload)
    action_record = _action_record(
        policy=policy,
        binding_id=location.registry_binding_id(policy),
        spec=spec,
    )
    client = _Client({})
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.s3_object_lock_trial_registry.verify_release_bindings",
        lambda *_args, **_kwargs: spec.release_bindings,
    )
    with pytest.raises(ContractError, match=message):
        register_s3_object_lock_trial(
            client=client,
            policy=policy,
            location=location,
            spec=spec,
            verified_release_directories=(),
            accepted_release_root=tmp_path,
            action_record=action_record,
        )
    assert client.puts == []

    target = _target()
    retained_payload = _payload(
        binding_id=target.registry_binding_id(policy),
        evidence_class=evidence_class,
        role=role,
        quality_state=quality_state,
    )
    trial_id = str(retained_payload["trial_id"])
    response = _response(
        target=target,
        payload=retained_payload,
        action_record=action_record,
    )
    with pytest.raises(EvaluationAuthorizationError, match="payload is invalid"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(response),
            policy=policy,
            target=target,
            trial_id=trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )


def test_loaded_registration_rejects_unverified_release_bindings() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    policy = _policy(repo_root)
    target = _target()
    payload = _payload(binding_id=target.registry_binding_id(policy))
    trial_id = str(payload["trial_id"])
    spec = TrialSpec.from_registered_payload(payload)
    action_record = _action_record(
        policy=policy,
        binding_id=target.registry_binding_id(policy),
        spec=spec,
    )
    response = _response(
        target=target,
        payload=payload,
        action_record=action_record,
    )
    with pytest.raises(EvaluationAuthorizationError, match="accepted-release verification"):
        load_s3_object_lock_trial_registration(
            reader=_Reader(response),
            policy=policy,
            target=target,
            trial_id=trial_id,
            verified_release_directories=(),
            accepted_release_root=repo_root,
            action_record=action_record,
        )


def test_policy_status_cannot_be_forged_with_a_stale_policy_id() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    selected = S3ObjectLockRegistryPolicy.load(
        repo_root / "config" / "trial_registry_s3_object_lock_policy.json",
        repository_root=repo_root,
    )
    forged = S3ObjectLockRegistryPolicy(
        policy_id=selected.policy_id,
        minimum_retention_days=selected.minimum_retention_days,
        status="CONFIGURED",
    )
    with pytest.raises(ContractError, match="differs from its canonical policy"):
        forged.require_configured()


def test_sdk_factory_rejects_an_invalid_region_before_loading_boto3() -> None:
    with pytest.raises(Exception, match="region"):
        create_aws_s3_object_lock_client(region="invalid")
