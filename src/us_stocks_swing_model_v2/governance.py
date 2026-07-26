from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
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


SYNTHETIC_SIGNATURE_ALGORITHM = "HMAC_SHA256_SYNTHETIC_ONLY"
EXTERNAL_SIGNATURE_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA256"
_LOWER_HEX = re.compile(r"^[0-9a-f]+$")
_BASE64URL_UINT = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_DIGEST_INFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420"
)


def _reviewed_external_authority_registry_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "config"
        / "authorization_authorities.json"
    )


def _require_reviewed_external_authority_registry(registry_path: Path) -> Path:
    path = Path(registry_path)
    reviewed = _reviewed_external_authority_registry_path()
    if not path.is_absolute() or path != reviewed:
        raise EvaluationAuthorizationError(
            "external authority must use the reviewed project registry"
        )
    reject_link(path)
    return path


def _decode_base64url_uint(value: object, *, label: str) -> int:
    if not isinstance(value, str) or _BASE64URL_UINT.fullmatch(value) is None:
        raise EvaluationAuthorizationError(f"{label} is not canonical base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except (ValueError, TypeError) as exc:
        raise EvaluationAuthorizationError(f"{label} is not valid base64url") from exc
    if not decoded or decoded[0] == 0:
        raise EvaluationAuthorizationError(f"{label} is not a minimal unsigned integer")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise EvaluationAuthorizationError(f"{label} is not canonical base64url")
    return int.from_bytes(decoded, "big")


def _parse_external_public_jwk(value: bytes) -> tuple[int, int]:
    try:
        payload = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationAuthorizationError("external public key is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"alg", "e", "kty", "n", "use"}:
        raise EvaluationAuthorizationError("external public key fields differ")
    if payload["kty"] != "RSA" or payload["alg"] != "RS256" or payload["use"] != "sig":
        raise EvaluationAuthorizationError("external public key algorithm/use is invalid")
    modulus = _decode_base64url_uint(payload["n"], label="external public key modulus")
    exponent = _decode_base64url_uint(payload["e"], label="external public key exponent")
    if not 2048 <= modulus.bit_length() <= 8192 or modulus % 2 == 0:
        raise EvaluationAuthorizationError("external RSA modulus is outside policy")
    if exponent != 65537:
        raise EvaluationAuthorizationError("external RSA exponent is outside policy")
    return modulus, exponent


def _verify_external_signature(
    *,
    public_jwk: bytes,
    message: bytes,
    signature_hex: str,
) -> bool:
    modulus, exponent = _parse_external_public_jwk(public_jwk)
    modulus_bytes = (modulus.bit_length() + 7) // 8
    if (
        len(signature_hex) != modulus_bytes * 2
        or _LOWER_HEX.fullmatch(signature_hex) is None
    ):
        return False
    signature = int(signature_hex, 16)
    if signature >= modulus:
        return False
    encoded = pow(signature, exponent, modulus).to_bytes(modulus_bytes, "big")
    digest_info = _SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    padding_length = modulus_bytes - len(digest_info) - 3
    if padding_length < 8:
        return False
    expected = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    return hmac.compare_digest(encoded, expected)


@dataclass(frozen=True, init=False)
class AuthorizationAuthority:
    registry_id: str
    key_id: str
    key_sha256: str
    authorization_class: str
    signature_algorithm: str
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
            signature_algorithm=SYNTHETIC_SIGNATURE_ALGORITHM,
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
            if (
                self.synthetic_permit_id != self.registry_id
                or self.registry_path is not None
                or self.signature_algorithm != SYNTHETIC_SIGNATURE_ALGORITHM
            ):
                raise EvaluationAuthorizationError("synthetic authority lacks its mechanics permit")
        elif self.authorization_class == "EXTERNAL_USER_AUTHORITY":
            if (
                self.synthetic_permit_id is not None
                or self.registry_path is None
                or self.signature_algorithm != EXTERNAL_SIGNATURE_ALGORITHM
            ):
                raise EvaluationAuthorizationError("external authority key class/algorithm is invalid")
            _parse_external_public_jwk(self.verification_key)
            path = _require_reviewed_external_authority_registry(
                Path(self.registry_path)
            )
            payload, registry_id = _read_authority_registry(path)
            if registry_id != self.registry_id:
                raise EvaluationAuthorizationError("external authority registry changed after loading")
            matches = [
                row
                for row in payload["authorities"]
                if isinstance(row, dict) and row.get("key_id") == self.key_id
            ]
            expected = {
                "key_id": self.key_id,
                "key_sha256": self.key_sha256,
                "authorization_class": self.authorization_class,
                "signature_algorithm": self.signature_algorithm,
            }
            if payload["status"] != "ACTIVE" or matches != [expected]:
                raise EvaluationAuthorizationError("external authority is no longer pinned and active")
        else:
            raise EvaluationAuthorizationError("authorization authority class is invalid")

    def verify_external_signature(self, *, message: bytes, signature_hex: str) -> None:
        self.validate()
        if self.authorization_class != "EXTERNAL_USER_AUTHORITY":
            raise EvaluationAuthorizationError(
                "external signature verification requires external user authority"
            )
        if not _verify_external_signature(
            public_jwk=self.verification_key,
            message=message,
            signature_hex=signature_hex,
        ):
            raise EvaluationAuthorizationError("external signature is invalid")


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
    """Load only the separately reviewed, checked-in production trust anchor."""
    path = _require_reviewed_external_authority_registry(Path(registry_path))
    payload, registry_id = _read_authority_registry(path)
    matches = [row for row in payload["authorities"] if isinstance(row, dict) and row.get("key_id") == key_id]
    if payload["status"] != "ACTIVE" or len(matches) != 1:
        raise EvaluationAuthorizationError("external authority is not pinned and active")
    row = matches[0]
    if set(row) != {
        "key_id",
        "key_sha256",
        "authorization_class",
        "signature_algorithm",
    }:
        raise EvaluationAuthorizationError("external authority entry fields differ")
    authority = AuthorizationAuthority._construct(
        registry_id=registry_id,
        key_id=key_id,
        key_sha256=str(row["key_sha256"]),
        authorization_class=str(row["authorization_class"]),
        signature_algorithm=str(row["signature_algorithm"]),
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
        if (
            not isinstance(self.signature, str)
            or len(self.signature) % 2 != 0
            or _LOWER_HEX.fullmatch(self.signature) is None
        ):
            raise EvaluationAuthorizationError("authorization signature encoding is invalid")
        if self.authorization_class == "SYNTHETIC_ONLY_NOT_AUTHORITY":
            try:
                require_sha256(self.signature, "authorization.signature")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        elif not 512 <= len(self.signature) <= 2048:
            raise EvaluationAuthorizationError("external authorization signature size is invalid")
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
        message = canonical_json_bytes(self.signing_dict())
        if authority.authorization_class == "SYNTHETIC_ONLY_NOT_AUTHORITY":
            valid_signature = hmac.compare_digest(
                self.signature,
                hmac.new(
                    authority.verification_key,
                    message,
                    hashlib.sha256,
                ).hexdigest(),
            )
        else:
            try:
                authority.verify_external_signature(
                    message=message,
                    signature_hex=self.signature,
                )
                valid_signature = True
            except EvaluationAuthorizationError:
                valid_signature = False
        if not valid_signature:
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
