"""Fail-closed access to externally retained S3 Object Lock trial records.

This module never treats a local copy as immutable. Reads and the single-write
registration path require a separately authorized external invocation.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from .clock import TrustedClock, require_trusted_clock
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_aware_utc,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError
from .trials import TrialSpec, validate_trial_evidence_roles
from .governance import (
    LocalIntegrityRecord,
    ReleaseBinding,
    release_bindings_hash,
    verify_release_bindings,
)


_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_MAX_RECORD_BYTES = 512 * 1024
_MAX_REGISTRATION_TIME_SKEW = timedelta(minutes=5)
_REGISTRATION_ACTION_SCOPE = "AUTHORIZE_EXTERNAL_TRIAL_REGISTRATION"
_REGISTRATION_ACTION_METADATA_KEY = "registration-authorization-record-id"
_CONSUMED_REGISTRATION_ACTION_RECORD_IDS: set[str] = set()
_REGISTRATION_ACTION_LOCK = threading.Lock()
_POLICY_FIXED_FIELDS = {
    "backend": "AWS_S3_OBJECT_LOCK_COMPLIANCE",
    "mode": "EXTERNAL_IMMUTABLE_TRIAL_REGISTRY",
    "project": "US_stocks_swing_model_v2",
    "schema_version": 1,
}


class S3ObjectReader(Protocol):
    def get_object(self, *, Bucket: str, Key: str, VersionId: str) -> Mapping[str, Any]: ...


class S3ObjectLockTrialRegistryClient(S3ObjectReader, Protocol):
    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class S3ObjectLockRegistryPolicy:
    policy_id: str
    minimum_retention_days: int
    status: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            **_POLICY_FIXED_FIELDS,
            "minimum_retention_days": self.minimum_retention_days,
            "status": self.status,
        }

    def validate(self) -> None:
        if (
            type(self.minimum_retention_days) is not int
            or not 3650 <= self.minimum_retention_days <= 36500
            or self.status not in {"BACKEND_SELECTED_NOT_CONFIGURED", "CONFIGURED"}
        ):
            raise ContractError("S3 Object Lock registry policy is invalid")
        require_sha256(self.policy_id, "S3 Object Lock registry policy ID")
        if self.policy_id != sha256_bytes(
            canonical_json_bytes(self.unsigned_dict())
        ):
            raise ContractError(
                "S3 Object Lock registry policy ID differs from its canonical policy"
            )

    @classmethod
    def load(cls, path: Path, *, repository_root: Path) -> "S3ObjectLockRegistryPolicy":
        candidate = require_contained_path(Path(path), Path(repository_root))
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("S3 Object Lock registry policy is unreadable") from exc
        expected = {
            "schema_version",
            "project",
            "mode",
            "backend",
            "status",
            "minimum_retention_days",
        }
        if type(value) is not dict or set(value) != expected:
            raise ContractError("S3 Object Lock registry policy fields differ")
        if (
            value["schema_version"] != 1
            or value["project"] != "US_stocks_swing_model_v2"
            or value["mode"] != "EXTERNAL_IMMUTABLE_TRIAL_REGISTRY"
            or value["backend"] != "AWS_S3_OBJECT_LOCK_COMPLIANCE"
            or value["status"] not in {"BACKEND_SELECTED_NOT_CONFIGURED", "CONFIGURED"}
            or type(value["minimum_retention_days"]) is not int
            or not 3650 <= value["minimum_retention_days"] <= 36500
        ):
            raise ContractError("S3 Object Lock registry policy is invalid")
        unsigned = dict(value)
        result = cls(
            policy_id=sha256_bytes(canonical_json_bytes(unsigned)),
            minimum_retention_days=value["minimum_retention_days"],
            status=value["status"],
        )
        result.validate()
        return result

    def require_configured(self) -> None:
        self.validate()
        if self.status != "CONFIGURED":
            raise EvaluationAuthorizationError(
                "S3 Object Lock trial registry backend is not configured"
            )


@dataclass(frozen=True)
class S3ObjectLockTrialRegistryTarget:
    bucket: str
    region: str
    prefix: str
    version_id: str

    def validate(self) -> None:
        if (
            type(self.bucket) is not str
            or _BUCKET.fullmatch(self.bucket) is None
            or ".." in self.bucket
            or type(self.region) is not str
            or _REGION.fullmatch(self.region) is None
            or type(self.version_id) is not str
            or not self.version_id
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in self.version_id)
        ):
            raise ContractError("S3 Object Lock registry target is invalid")
        safe_relative_path(self.prefix)

    def registry_binding_id(self, policy: S3ObjectLockRegistryPolicy) -> str:
        self.validate()
        policy.validate()
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "policy_id": policy.policy_id,
                    "bucket": self.bucket,
                    "region": self.region,
                    "prefix": self.prefix,
                }
            )
        )

    def key_for(self, trial_id: str) -> str:
        self.validate()
        require_sha256(trial_id, "trial_id")
        return f"{self.prefix}/{trial_id}.json"


@dataclass(frozen=True)
class S3ObjectLockTrialRegistryLocation:
    bucket: str
    region: str
    prefix: str

    def target(self, version_id: str) -> S3ObjectLockTrialRegistryTarget:
        return S3ObjectLockTrialRegistryTarget(
            bucket=self.bucket,
            region=self.region,
            prefix=self.prefix,
            version_id=version_id,
        )

    def registry_binding_id(self, policy: S3ObjectLockRegistryPolicy) -> str:
        return self.target("placeholder").registry_binding_id(policy)

    def key_for(self, trial_id: str) -> str:
        return self.target("placeholder").key_for(trial_id)


@dataclass(frozen=True)
class ExternalTrialRegistration:
    trial_id: str
    trial_registry_binding_id: str
    registration_hash: str
    external_anchor_receipt_id: str
    registration_authorization_record_id: str
    s3_version_id: str
    object_created_at: str
    retained_until: str
    registered_payload: Mapping[str, Any]


def _registration_action_bindings(
    *,
    policy: S3ObjectLockRegistryPolicy,
    trial_registry_binding_id: str,
    verified_release_bindings: Iterable[ReleaseBinding],
) -> dict[str, str]:
    return {
        "policy_id": policy.policy_id,
        "release_bindings_hash": release_bindings_hash(verified_release_bindings),
        "trial_registry_binding_id": trial_registry_binding_id,
    }


def register_s3_object_lock_trial(
    *,
    client: S3ObjectLockTrialRegistryClient,
    policy: S3ObjectLockRegistryPolicy,
    location: S3ObjectLockTrialRegistryLocation,
    spec: TrialSpec,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    action_record: LocalIntegrityRecord,
    clock: TrustedClock | None = None,
) -> ExternalTrialRegistration:
    """Create one Compliance-retained trial record, then reload that exact version.

    This is an external side effect and intentionally has no retry behavior.
    A failed post-write verification leaves the S3 object untouched for explicit
    later recovery handling.
    """

    if not hasattr(client, "put_object") or not hasattr(client, "get_object"):
        raise ContractError("S3 Object Lock trial registry requires an S3 client")
    policy.require_configured()
    target_probe = location.target("placeholder")
    target_probe.validate()
    spec.validate()
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise EvaluationAuthorizationError("external trial registration requires production UTC")
    release_directories = tuple(verified_release_directories)
    verified = verify_release_bindings(
        release_directories,
        accepted_release_root=Path(accepted_release_root),
        expected_project="US_stocks_swing_model_v2",
    )
    if verified != spec.release_bindings:
        raise ContractError("trial release bindings differ from verified release manifests")
    validate_trial_evidence_roles(spec)
    binding_id = location.registry_binding_id(policy)
    if type(action_record) is not LocalIntegrityRecord:
        raise EvaluationAuthorizationError(
            "external trial registration requires its exact schema-v2 action record"
        )
    action_record.validate(
        expected_scope=_REGISTRATION_ACTION_SCOPE,
        expected_subject_id=spec.trial_id,
        required_bindings=_registration_action_bindings(
            policy=policy,
            trial_registry_binding_id=binding_id,
            verified_release_bindings=verified,
        ),
        clock=trusted_clock,
    )
    registered_at = trusted_clock.now()
    payload = {
        **spec.unsigned_dict(),
        "trial_id": spec.trial_id,
        "registered_at": iso_z(registered_at),
        "trial_registry_binding_id": binding_id,
    }
    raw = canonical_json_bytes(payload)
    retained_until = registered_at + timedelta(days=policy.minimum_retention_days)
    with _REGISTRATION_ACTION_LOCK:
        if action_record.record_id in _CONSUMED_REGISTRATION_ACTION_RECORD_IDS:
            raise EvaluationAuthorizationError(
                "external trial registration action record was already consumed"
            )
        _CONSUMED_REGISTRATION_ACTION_RECORD_IDS.add(action_record.record_id)
    try:
        response = client.put_object(
            Bucket=location.bucket,
            Key=location.key_for(spec.trial_id),
            Body=raw,
            ContentType="application/json",
            ChecksumAlgorithm="SHA256",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retained_until,
            Metadata={
                _REGISTRATION_ACTION_METADATA_KEY: action_record.record_id,
            },
        )
    except Exception as exc:
        raise EvaluationAuthorizationError("S3 Object Lock trial registration write failed") from exc
    if type(response) is not dict or type(response.get("VersionId")) is not str:
        raise EvaluationAuthorizationError("S3 Object Lock trial registration lacks a version ID")
    target = location.target(response["VersionId"])
    return load_s3_object_lock_trial_registration(
        reader=client,
        policy=policy,
        target=target,
        trial_id=spec.trial_id,
        verified_release_directories=release_directories,
        accepted_release_root=accepted_release_root,
        action_record=action_record,
        clock=trusted_clock,
    )


def create_aws_s3_object_lock_client(*, region: str) -> S3ObjectLockTrialRegistryClient:
    """Create the selected SDK client without reading credentials here.

    The later S3 operation is the only point at which the SDK resolves AWS
    credentials.  This factory makes no request and exposes no configuration.
    """

    if type(region) is not str or _REGION.fullmatch(region) is None:
        raise ContractError("AWS S3 region is invalid")
    try:
        import boto3
    except ImportError as exc:
        raise EvaluationAuthorizationError("pinned boto3 SDK is unavailable") from exc
    return boto3.client("s3", region_name=region)


def load_s3_object_lock_trial_registration(
    *,
    reader: S3ObjectReader,
    policy: S3ObjectLockRegistryPolicy,
    target: S3ObjectLockTrialRegistryTarget,
    trial_id: str,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    action_record: LocalIntegrityRecord,
    clock: TrustedClock | None = None,
) -> ExternalTrialRegistration:
    """Load one immutable externally retained registration and verify its evidence."""

    if not hasattr(reader, "get_object"):
        raise ContractError("S3 Object Lock trial registry requires an S3 object reader")
    policy.require_configured()
    target.validate()
    require_sha256(trial_id, "trial_id")
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise EvaluationAuthorizationError("external trial registry loading requires production UTC")
    release_directories = tuple(verified_release_directories)
    try:
        verified = verify_release_bindings(
            release_directories,
            accepted_release_root=Path(accepted_release_root),
            expected_project="US_stocks_swing_model_v2",
        )
    except ContractError as exc:
        raise EvaluationAuthorizationError(
            "S3 trial registration accepted-release verification failed"
        ) from exc
    binding_id = target.registry_binding_id(policy)
    if type(action_record) is not LocalIntegrityRecord:
        raise EvaluationAuthorizationError(
            "external trial registration requires its exact schema-v2 action record"
        )
    action_record.validate(
        expected_scope=_REGISTRATION_ACTION_SCOPE,
        expected_subject_id=trial_id,
        required_bindings=_registration_action_bindings(
            policy=policy,
            trial_registry_binding_id=binding_id,
            verified_release_bindings=verified,
        ),
        clock=trusted_clock,
    )
    key = target.key_for(trial_id)
    try:
        response = reader.get_object(
            Bucket=target.bucket,
            Key=key,
            VersionId=target.version_id,
        )
    except Exception as exc:  # external client details must not alter the contract
        raise EvaluationAuthorizationError("S3 Object Lock trial registry read failed") from exc
    if type(response) is not dict:
        raise EvaluationAuthorizationError("S3 Object Lock response is invalid")
    if response.get("VersionId") != target.version_id:
        raise EvaluationAuthorizationError("S3 Object Lock response version differs")
    if response.get("ObjectLockMode") != "COMPLIANCE":
        raise EvaluationAuthorizationError("S3 trial record is not in Compliance retention mode")
    observed_at = require_aware_utc(
        trusted_clock.now(),
        "S3 trial record observed_at",
    )
    last_modified = response.get("LastModified")
    if type(last_modified) is not datetime:
        raise EvaluationAuthorizationError(
            "S3 trial record lacks authoritative object creation time"
        )
    try:
        object_created_at = require_aware_utc(
            last_modified,
            "S3 trial record LastModified",
        )
    except ContractError as exc:
        raise EvaluationAuthorizationError(
            "S3 trial record object creation time is invalid"
        ) from exc
    metadata = response.get("Metadata")
    if (
        type(metadata) is not dict
        or set(metadata) != {_REGISTRATION_ACTION_METADATA_KEY}
        or metadata.get(_REGISTRATION_ACTION_METADATA_KEY) != action_record.record_id
    ):
        raise EvaluationAuthorizationError(
            "S3 trial record differs from its registration action record"
        )
    retained = response.get("ObjectLockRetainUntilDate")
    if type(retained) is datetime:
        try:
            retained_at = require_aware_utc(retained, "S3 Object Lock retention")
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "S3 trial record retention is invalid"
            ) from exc
    elif type(retained) is str:
        retained_at = parse_utc_z(retained, "S3 Object Lock retention")
    else:
        raise EvaluationAuthorizationError("S3 trial record retention is invalid")
    body = response.get("Body")
    if body is None or not hasattr(body, "read"):
        raise EvaluationAuthorizationError("S3 trial record body is unavailable")
    raw = body.read(_MAX_RECORD_BYTES + 1)
    if type(raw) is not bytes or len(raw) > _MAX_RECORD_BYTES:
        raise EvaluationAuthorizationError("S3 trial record exceeds the allowed size")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationAuthorizationError("S3 trial record is not valid UTF-8 JSON") from exc
    if type(payload) is not dict or canonical_json_bytes(payload) != raw:
        raise EvaluationAuthorizationError("S3 trial record is not canonical JSON")
    try:
        spec = TrialSpec.from_registered_payload(payload)
        spec.validate()
        validate_trial_evidence_roles(spec)
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise EvaluationAuthorizationError("S3 trial registration payload is invalid") from exc
    registered_at = parse_utc_z(str(payload["registered_at"]), "registered_at")
    action_recorded_at = parse_utc_z(
        action_record.recorded_at,
        "registration action recorded_at",
    )
    if registered_at > observed_at + _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError("S3 trial registration time is in the future")
    if object_created_at > observed_at + _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError("S3 trial object creation time is in the future")
    if abs(object_created_at - registered_at) > _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError(
            "S3 trial registration time differs from authoritative object creation"
        )
    if action_recorded_at > object_created_at + _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError(
            "S3 trial registration action record postdates object creation"
        )
    if retained_at < registered_at + timedelta(days=policy.minimum_retention_days):
        raise EvaluationAuthorizationError("S3 trial record retention is shorter than policy")
    if verified != spec.release_bindings:
        raise EvaluationAuthorizationError(
            "S3 trial registration release bindings differ from verified accepted releases"
        )
    if (
        spec.trial_id != trial_id
        or payload.get("trial_id") != trial_id
        or payload.get("trial_registry_binding_id") != binding_id
    ):
        raise EvaluationAuthorizationError("S3 trial registration differs from its binding")
    registration_hash = sha256_bytes(canonical_json_bytes(payload))
    unsigned = {
        "schema_version": 2,
        "backend": "AWS_S3_OBJECT_LOCK_COMPLIANCE",
        "policy_id": policy.policy_id,
        "trial_registry_binding_id": binding_id,
        "trial_id": trial_id,
        "registration_hash": registration_hash,
        "registration_authorization_record_id": action_record.record_id,
        "bucket": target.bucket,
        "region": target.region,
        "key": key,
        "version_id": target.version_id,
        "object_sha256": sha256_bytes(raw),
        "object_created_at": iso_z(object_created_at),
        "object_lock_mode": "COMPLIANCE",
        "retained_until": iso_z(retained_at),
    }
    return ExternalTrialRegistration(
        trial_id=trial_id,
        trial_registry_binding_id=binding_id,
        registration_hash=registration_hash,
        external_anchor_receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
        registration_authorization_record_id=action_record.record_id,
        s3_version_id=target.version_id,
        object_created_at=iso_z(object_created_at),
        retained_until=iso_z(retained_at),
        registered_payload=payload,
    )
