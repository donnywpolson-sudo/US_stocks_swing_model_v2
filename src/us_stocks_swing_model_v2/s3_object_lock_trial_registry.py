"""Fail-closed loader for externally retained S3 Object Lock trial records.

This module never writes to S3 and never treats a local copy as immutable.
Callers supply an AWS S3-compatible ``get_object`` client only during a
separately authorized external invocation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Protocol

from .clock import TrustedClock, require_trusted_clock
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError
from .trials import TrialSpec


_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-\d$")
_MAX_RECORD_BYTES = 512 * 1024


class S3ObjectReader(Protocol):
    def get_object(self, *, Bucket: str, Key: str, VersionId: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class S3ObjectLockRegistryPolicy:
    policy_id: str
    minimum_retention_days: int
    status: str

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
        return cls(
            policy_id=sha256_bytes(canonical_json_bytes(unsigned)),
            minimum_retention_days=value["minimum_retention_days"],
            status=value["status"],
        )

    def require_configured(self) -> None:
        require_sha256(self.policy_id, "S3 Object Lock registry policy ID")
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
        require_sha256(policy.policy_id, "S3 Object Lock registry policy ID")
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
class ExternalTrialRegistration:
    trial_id: str
    trial_registry_binding_id: str
    registration_hash: str
    external_anchor_receipt_id: str
    s3_version_id: str
    retained_until: str
    registered_payload: Mapping[str, Any]


def load_s3_object_lock_trial_registration(
    *,
    reader: S3ObjectReader,
    policy: S3ObjectLockRegistryPolicy,
    target: S3ObjectLockTrialRegistryTarget,
    trial_id: str,
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
    retained = response.get("ObjectLockRetainUntilDate")
    if type(retained) is datetime:
        retained_at = retained
    elif type(retained) is str:
        retained_at = parse_utc_z(retained, "S3 Object Lock retention")
    else:
        raise EvaluationAuthorizationError("S3 trial record retention is invalid")
    retained_at = retained_at.astimezone(trusted_clock.now().tzinfo)
    now = trusted_clock.now()
    if retained_at < now + timedelta(days=policy.minimum_retention_days):
        raise EvaluationAuthorizationError("S3 trial record retention is shorter than policy")
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
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise EvaluationAuthorizationError("S3 trial registration payload is invalid") from exc
    binding_id = target.registry_binding_id(policy)
    if (
        spec.trial_id != trial_id
        or payload.get("trial_id") != trial_id
        or payload.get("trial_registry_binding_id") != binding_id
    ):
        raise EvaluationAuthorizationError("S3 trial registration differs from its binding")
    registration_hash = sha256_bytes(canonical_json_bytes(payload))
    unsigned = {
        "schema_version": 1,
        "backend": "AWS_S3_OBJECT_LOCK_COMPLIANCE",
        "policy_id": policy.policy_id,
        "trial_registry_binding_id": binding_id,
        "trial_id": trial_id,
        "registration_hash": registration_hash,
        "bucket": target.bucket,
        "region": target.region,
        "key": key,
        "version_id": target.version_id,
        "object_sha256": sha256_bytes(raw),
        "object_lock_mode": "COMPLIANCE",
        "retained_until": iso_z(retained_at),
    }
    return ExternalTrialRegistration(
        trial_id=trial_id,
        trial_registry_binding_id=binding_id,
        registration_hash=registration_hash,
        external_anchor_receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
        s3_version_id=target.version_id,
        retained_until=iso_z(retained_at),
        registered_payload=payload,
    )
