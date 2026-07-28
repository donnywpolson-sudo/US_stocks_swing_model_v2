from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import TrustedClock, require_trusted_clock
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError
from .releases import ReleaseManifest, verify_accepted_release


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
        if not all(
            (
                self.project,
                self.dataset,
                self.source_epoch,
                self.role,
                self.quality_state,
            )
        ):
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
class LocalIntegrityRecord:
    """Content-addressed evidence of an explicit owner-operated local action."""

    schema_version: int
    record_type: str
    scope: str
    subject_id: str
    bindings: Mapping[str, str]
    recorded_at: str
    clock_mode: str
    synthetic_permit_id: str | None
    record_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "record_type": self.record_type,
            "scope": self.scope,
            "subject_id": self.subject_id,
            "bindings": dict(sorted(self.bindings.items())),
            "recorded_at": self.recorded_at,
            "clock_mode": self.clock_mode,
            "synthetic_permit_id": self.synthetic_permit_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "record_id": self.record_id}

    def validate_content(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 2
            or self.record_type != "OWNER_OPERATED_LOCAL_INTEGRITY"
            or type(self.scope) is not str
            or not self.scope
            or type(self.subject_id) is not str
            or not self.subject_id
            or self.clock_mode
            not in {
                "PRODUCTION_SYSTEM_UTC",
                "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE",
            }
        ):
            raise EvaluationAuthorizationError("local integrity record content is invalid")
        if (
            type(self.bindings) is not dict
            or not self.bindings
            or any(
                type(key) is not str
                or not key
                or type(value) is not str
                or not value
                for key, value in self.bindings.items()
            )
        ):
            raise EvaluationAuthorizationError(
                "local integrity bindings must be exact nonempty text"
            )
        if self.clock_mode == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_permit_id is not None:
                raise EvaluationAuthorizationError(
                    "production local integrity record cannot carry a synthetic permit"
                )
        else:
            try:
                require_sha256(
                    self.synthetic_permit_id or "",
                    "local_integrity.synthetic_permit_id",
                )
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        parse_utc_z(self.recorded_at, "local_integrity.recorded_at")
        try:
            require_sha256(self.record_id, "local_integrity.record_id")
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        expected = sha256_bytes(canonical_json_bytes(self.unsigned_dict()))
        if self.record_id != expected:
            raise EvaluationAuthorizationError("local integrity record ID is invalid")

    def validate(
        self,
        *,
        expected_scope: str,
        expected_subject_id: str,
        required_bindings: Mapping[str, str],
        clock: TrustedClock,
    ) -> None:
        self.validate_at(
            expected_scope=expected_scope,
            expected_subject_id=expected_subject_id,
            required_bindings=required_bindings,
            observed_at=require_trusted_clock(clock).now(),
        )

    def validate_at(
        self,
        *,
        expected_scope: str,
        expected_subject_id: str,
        required_bindings: Mapping[str, str],
        observed_at: object,
    ) -> None:
        self.validate_content()
        if self.scope != expected_scope:
            raise EvaluationAuthorizationError("local integrity scope differs")
        if self.subject_id != expected_subject_id:
            raise EvaluationAuthorizationError("local integrity subject differs")
        if dict(self.bindings) != dict(required_bindings):
            raise EvaluationAuthorizationError(
                "local integrity bindings differ from governed evidence"
            )
        if not isinstance(observed_at, datetime):
            raise EvaluationAuthorizationError(
                "local integrity observation time must be datetime"
            )
        current = require_aware_utc(observed_at, "local_integrity.observed_at")
        if parse_utc_z(self.recorded_at, "local_integrity.recorded_at") > current:
            raise EvaluationAuthorizationError(
                "local integrity record cannot be observed before it was created"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "LocalIntegrityRecord":
        if set(payload) != set(cls.__dataclass_fields__):
            raise EvaluationAuthorizationError("local integrity record fields differ")
        if type(payload["schema_version"]) is not int:
            raise EvaluationAuthorizationError(
                "local integrity schema_version must be an integer"
            )
        text_fields = (
            "record_type",
            "scope",
            "subject_id",
            "recorded_at",
            "clock_mode",
            "record_id",
        )
        if any(type(payload[name]) is not str for name in text_fields):
            raise EvaluationAuthorizationError(
                "local integrity record text fields must be exact strings"
            )
        if (
            type(payload["bindings"]) is not dict
            or any(
                type(key) is not str or type(value) is not str
                for key, value in payload["bindings"].items()
            )
        ):
            raise EvaluationAuthorizationError(
                "local integrity bindings must contain exact string pairs"
            )
        if payload["synthetic_permit_id"] is not None and type(
            payload["synthetic_permit_id"]
        ) is not str:
            raise EvaluationAuthorizationError(
                "local integrity synthetic permit ID must be text or null"
            )
        record = cls(
            schema_version=payload["schema_version"],
            record_type=payload["record_type"],
            scope=payload["scope"],
            subject_id=payload["subject_id"],
            bindings=dict(payload["bindings"]),
            recorded_at=payload["recorded_at"],
            clock_mode=payload["clock_mode"],
            synthetic_permit_id=payload["synthetic_permit_id"],
            record_id=payload["record_id"],
        )
        record.validate_content()
        return record


def create_local_integrity_record(
    *,
    scope: str,
    subject_id: str,
    bindings: Mapping[str, str],
    clock: TrustedClock,
) -> LocalIntegrityRecord:
    trusted_clock = require_trusted_clock(clock)
    unsigned = {
        "schema_version": 2,
        "record_type": "OWNER_OPERATED_LOCAL_INTEGRITY",
        "scope": scope,
        "subject_id": subject_id,
        "bindings": dict(sorted(bindings.items())),
        "recorded_at": iso_z(trusted_clock.now()),
        "clock_mode": trusted_clock.mode,
        "synthetic_permit_id": trusted_clock.synthetic_permit_id,
    }
    record = LocalIntegrityRecord(
        **unsigned,
        record_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    record.validate_content()
    return record
