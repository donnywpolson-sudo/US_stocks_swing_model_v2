from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import (
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_sha256,
    sha256_bytes,
)
from .clock import TrustedClock, require_trusted_clock
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .errors import ContractError, EvaluationAuthorizationError
from .releases import ReleaseManifest, verify_accepted_release


@dataclass(frozen=True, init=False)
class AuthorizationAuthority:
    registry_id: str
    key_id: str
    key_sha256: str
    authorization_class: str
    verification_key: bytes
    synthetic_permit_id: str | None = None
    registry_path: str | None = None

    @classmethod
    def _construct(cls, **fields: object) -> "AuthorizationAuthority":
        authority = object.__new__(cls)
        for name in cls.__dataclass_fields__:
            object.__setattr__(authority, name, fields.get(name))
        authority.validate()
        return authority

    @classmethod
    def synthetic(
        cls,
        *,
        key_id: str,
        verification_key: bytes,
        permit: SyntheticOnlyPermit,
    ) -> "AuthorizationAuthority":
        verified = require_synthetic_permit(
            permit,
            scope="SYNTHETIC_AUTHORIZATION_AUTHORITY",
        )
        authority = cls._construct(
            registry_id=verified.permit_id,
            key_id=key_id,
            key_sha256=sha256_bytes(verification_key),
            authorization_class="SYNTHETIC_ONLY_NOT_AUTHORITY",
            verification_key=verification_key,
            synthetic_permit_id=verified.permit_id,
            registry_path=None,
        )
        return authority

    def validate(self) -> None:
        try:
            require_sha256(self.registry_id, "authority.registry_id")
            require_sha256(self.key_sha256, "authority.key_sha256")
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        if not self.key_id or not self.verification_key or sha256_bytes(self.verification_key) != self.key_sha256:
            raise EvaluationAuthorizationError("authorization authority registry/key binding is invalid")
        if self.authorization_class == "SYNTHETIC_ONLY_NOT_AUTHORITY":
            if self.synthetic_permit_id != self.registry_id or self.registry_path is not None:
                raise EvaluationAuthorizationError("synthetic authority lacks its mechanics permit")
        elif self.authorization_class == "EXTERNAL_USER_AUTHORITY":
            raise EvaluationAuthorizationError(
                "external authorization is disabled until asymmetric signature "
                "verification is implemented"
            )
        else:
            raise EvaluationAuthorizationError("authorization authority class is invalid")


def _read_authority_registry(registry_path: Path) -> tuple[dict[str, Any], str]:
    path = Path(registry_path)
    if not path.is_absolute():
        raise EvaluationAuthorizationError("external authority registry path must be absolute")
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise EvaluationAuthorizationError("external authority registry must be an independent plain file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationAuthorizationError("external authority registry is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "project",
        "status",
        "authorities",
    }:
        raise EvaluationAuthorizationError("external authority registry fields differ")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["project"] != "US_stocks_swing_model_v2"
        or not isinstance(payload["authorities"], list)
    ):
        raise EvaluationAuthorizationError("external authority registry project/schema differs")
    return payload, sha256_bytes(canonical_json_bytes(payload))


def load_external_authority(
    registry_path: Path,
    *,
    key_id: str,
    verification_key: bytes,
) -> AuthorizationAuthority:
    """Load a pinned authority registry that is outside candidate/trial inputs."""
    path = Path(registry_path).resolve(strict=True)
    payload, registry_id = _read_authority_registry(path)
    matches = [row for row in payload["authorities"] if isinstance(row, dict) and row.get("key_id") == key_id]
    if payload["status"] != "ACTIVE" or len(matches) != 1:
        raise EvaluationAuthorizationError("external authority is not pinned and active")
    row = matches[0]
    if set(row) != {"key_id", "key_sha256", "authorization_class"}:
        raise EvaluationAuthorizationError("external authority entry fields differ")
    authority = AuthorizationAuthority._construct(
        registry_id=registry_id,
        key_id=key_id,
        key_sha256=str(row["key_sha256"]),
        authorization_class=str(row["authorization_class"]),
        verification_key=verification_key,
        synthetic_permit_id=None,
        registry_path=str(path),
    )
    return authority


@dataclass(frozen=True)
class ReleaseBinding:
    release_id: str
    project: str
    dataset: str
    source_epoch: str
    role: str
    quality_state: str
    created_at: str
    event_start: str | None
    event_end: str | None

    @classmethod
    def from_manifest(cls, manifest: ReleaseManifest) -> "ReleaseBinding":
        manifest.validate()
        return cls(
            release_id=manifest.release_id,
            project=manifest.project,
            dataset=manifest.dataset,
            source_epoch=manifest.source_epoch,
            role=manifest.role,
            quality_state=manifest.quality_state,
            created_at=manifest.created_at,
            event_start=manifest.event_start,
            event_end=manifest.event_end,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "release_id": self.release_id,
            "project": self.project,
            "dataset": self.dataset,
            "source_epoch": self.source_epoch,
            "role": self.role,
            "quality_state": self.quality_state,
            "created_at": self.created_at,
            "event_start": self.event_start,
            "event_end": self.event_end,
        }

    def validate(self) -> None:
        require_sha256(self.release_id, "release_binding.release_id")
        if not all((self.project, self.dataset, self.source_epoch, self.role, self.quality_state)):
            raise ContractError("release binding fields cannot be empty")
        parse_utc_z(self.created_at, "release_binding.created_at")


def verify_release_bindings(
    release_directories: Iterable[Path],
    *,
    accepted_release_root: Path,
    expected_project: str,
) -> tuple[ReleaseBinding, ...]:
    bindings: list[ReleaseBinding] = []
    for directory in release_directories:
        manifest = verify_accepted_release(
            Path(directory),
            accepted_root=Path(accepted_release_root),
        )
        if manifest.project != expected_project:
            raise ContractError("verified release belongs to a different project")
        bindings.append(ReleaseBinding.from_manifest(manifest))
    ordered = tuple(sorted(bindings, key=lambda item: item.release_id))
    ids = [item.release_id for item in ordered]
    if not ordered or ids != sorted(set(ids)):
        raise ContractError("verified releases must be nonempty and unique")
    return ordered


def release_bindings_hash(bindings: Iterable[ReleaseBinding]) -> str:
    ordered = tuple(sorted(bindings, key=lambda item: item.release_id))
    for binding in ordered:
        binding.validate()
    return sha256_bytes(canonical_json_bytes([item.as_dict() for item in ordered]))


@dataclass(frozen=True)
class SignedAuthorizationReceipt:
    schema_version: int
    scope: str
    subject_id: str
    bindings: Mapping[str, str]
    issued_at: str
    expires_at: str
    key_id: str
    authority_registry_id: str
    authorization_class: str
    signature: str
    receipt_id: str

    def signing_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "subject_id": self.subject_id,
            "bindings": dict(sorted(self.bindings.items())),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
            "authority_registry_id": self.authority_registry_id,
            "authorization_class": self.authorization_class,
        }

    def unsigned_dict(self) -> dict[str, object]:
        return {**self.signing_dict(), "signature": self.signature}

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "receipt_id": self.receipt_id}

    def validate_content(self) -> None:
        try:
            require_sha256(self.authority_registry_id, "authorization.authority_registry_id")
            require_sha256(self.signature, "authorization.signature")
            require_sha256(self.receipt_id, "authorization.receipt_id")
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not self.scope
            or not self.subject_id
            or not self.key_id
            or self.authorization_class not in {
                "EXTERNAL_USER_AUTHORITY",
                "SYNTHETIC_ONLY_NOT_AUTHORITY",
            }
        ):
            raise EvaluationAuthorizationError("authorization content is incomplete")
        issued = parse_utc_z(self.issued_at, "authorization.issued_at")
        expires = parse_utc_z(self.expires_at, "authorization.expires_at")
        if issued >= expires:
            raise EvaluationAuthorizationError("authorization chronology/signature shape is invalid")
        if not self.bindings or any(not str(key) or not str(value) for key, value in self.bindings.items()):
            raise EvaluationAuthorizationError("authorization bindings cannot be empty")
        expected_id = sha256_bytes(canonical_json_bytes(self.unsigned_dict()))
        if self.receipt_id != expected_id:
            raise EvaluationAuthorizationError("authorization receipt ID is invalid")

    def validate(
        self,
        *,
        authority: AuthorizationAuthority,
        expected_scope: str,
        expected_subject_id: str,
        required_bindings: Mapping[str, str],
        clock: TrustedClock,
    ) -> None:
        self.validate_at(
            authority=authority,
            expected_scope=expected_scope,
            expected_subject_id=expected_subject_id,
            required_bindings=required_bindings,
            observed_at=require_trusted_clock(clock).now(),
        )

    def validate_at(
        self,
        *,
        authority: AuthorizationAuthority,
        expected_scope: str,
        expected_subject_id: str,
        required_bindings: Mapping[str, str],
        observed_at: object,
    ) -> None:
        self.validate_content()
        if type(self.schema_version) is not int or self.schema_version != 1 or self.scope != expected_scope:
            raise EvaluationAuthorizationError("authorization scope/schema is invalid")
        if self.subject_id != expected_subject_id:
            raise EvaluationAuthorizationError("authorization subject differs")
        authority.validate()
        if (
            self.key_id != authority.key_id
            or self.authority_registry_id != authority.registry_id
            or self.authorization_class != authority.authorization_class
        ):
            raise EvaluationAuthorizationError("authorization differs from the pinned external authority")
        issued = parse_utc_z(self.issued_at, "authorization.issued_at")
        expires = parse_utc_z(self.expires_at, "authorization.expires_at")
        from datetime import datetime

        if not isinstance(observed_at, datetime):
            raise EvaluationAuthorizationError("authorization observation time must be datetime")
        from .common import require_aware_utc

        current = require_aware_utc(observed_at, "authorization.observed_at")
        if issued >= expires or current < issued or current >= expires:
            raise EvaluationAuthorizationError("authorization is not current")
        if dict(self.bindings) != dict(required_bindings):
            raise EvaluationAuthorizationError("authorization bindings differ from governed evidence")
        expected_signature = hmac.new(
            authority.verification_key,
            canonical_json_bytes(self.signing_dict()),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(self.signature, expected_signature):
            raise EvaluationAuthorizationError("authorization signature is invalid")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SignedAuthorizationReceipt":
        if set(payload) != set(cls.__dataclass_fields__):
            raise EvaluationAuthorizationError("authorization receipt fields differ")
        if type(payload["schema_version"]) is not int:
            raise EvaluationAuthorizationError("authorization schema_version must be an integer")
        if not isinstance(payload["bindings"], dict):
            raise EvaluationAuthorizationError("authorization bindings must be an object")
        return cls(
            schema_version=int(payload["schema_version"]),
            scope=str(payload["scope"]),
            subject_id=str(payload["subject_id"]),
            bindings={str(key): str(value) for key, value in dict(payload["bindings"]).items()},
            issued_at=str(payload["issued_at"]),
            expires_at=str(payload["expires_at"]),
            key_id=str(payload["key_id"]),
            authority_registry_id=str(payload["authority_registry_id"]),
            authorization_class=str(payload["authorization_class"]),
            signature=str(payload["signature"]),
            receipt_id=str(payload["receipt_id"]),
        )


def load_signed_authorization_receipt(path: Path) -> SignedAuthorizationReceipt:
    receipt_path = Path(path)
    reject_link(receipt_path)
    if not receipt_path.is_file() or receipt_path.stat().st_nlink != 1:
        raise EvaluationAuthorizationError("authorization receipt must be an independent plain file")
    try:
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt = SignedAuthorizationReceipt.from_dict(payload)
        receipt.validate_content()
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise EvaluationAuthorizationError("authorization receipt is invalid") from exc
    return receipt


def sign_authorization_receipt(
    *,
    scope: str,
    subject_id: str,
    bindings: Mapping[str, str],
    issued_at: str,
    expires_at: str,
    authority: AuthorizationAuthority,
) -> SignedAuthorizationReceipt:
    authority.validate()
    if authority.authorization_class != "SYNTHETIC_ONLY_NOT_AUTHORITY":
        raise EvaluationAuthorizationError("repository signing helper is synthetic-only")
    signing = {
        "schema_version": 1,
        "scope": scope,
        "subject_id": subject_id,
        "bindings": dict(sorted(bindings.items())),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    parse_utc_z(issued_at, "authorization.issued_at")
    parse_utc_z(expires_at, "authorization.expires_at")
    signature = hmac.new(
        authority.verification_key,
        canonical_json_bytes(signing),
        hashlib.sha256,
    ).hexdigest()
    unsigned = {**signing, "signature": signature}
    receipt = SignedAuthorizationReceipt(
        **signing,
        signature=signature,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    receipt.validate_content()
    return receipt
