from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_sha256,
    require_contained_path,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .locking import ExclusiveFileLock


MANIFEST_NAME = "release_manifest.json"
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _parse_event_bound(value: str, field: str) -> tuple[str, object]:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a nonempty ISO date or canonical UTC timestamp")
    try:
        return ("date", date.fromisoformat(value))
    except ValueError:
        return ("timestamp", parse_utc_z(value, field))


@dataclass(frozen=True)
class ReleaseFile:
    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class ReleaseManifest:
    schema_version: int
    project: str
    dataset: str
    source_epoch: str
    role: str
    quality_state: str
    created_at: str
    row_count: int
    event_start: str | None
    event_end: str | None
    upstream_release_ids: tuple[str, ...]
    schema_fingerprint: str
    code_hash: str
    config_hash: str
    environment_hash: str
    files: tuple[ReleaseFile, ...]
    release_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "dataset": self.dataset,
            "source_epoch": self.source_epoch,
            "role": self.role,
            "quality_state": self.quality_state,
            "created_at": self.created_at,
            "row_count": self.row_count,
            "event_start": self.event_start,
            "event_end": self.event_end,
            "upstream_release_ids": list(self.upstream_release_ids),
            "schema_fingerprint": self.schema_fingerprint,
            "code_hash": self.code_hash,
            "config_hash": self.config_hash,
            "environment_hash": self.environment_hash,
            "files": [entry.as_dict() for entry in self.files],
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "release_id": self.release_id}

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported release manifest schema")
        if not all(IDENTIFIER.fullmatch(value) for value in (self.project, self.dataset, self.source_epoch, self.role)):
            raise ContractError("project, dataset, and source_epoch must be safe identifiers")
        if self.role not in {
            "legacy_discovery_only",
            "qualification_evidence_only",
            "active_historical",
            "prospective_as_received",
            "derived_causal",
            "feature_only",
            "outcome_only",
        }:
            raise ContractError("release role is not recognized")
        role_quality = {
            "legacy_discovery_only": {"LEGACY_CAVEATED"},
            "qualification_evidence_only": {"FAIL", "QUALIFICATION_EVIDENCE"},
            "active_historical": {"PASS"},
            "prospective_as_received": {"PASS"},
            "derived_causal": {"PASS"},
            "feature_only": {"PASS"},
            "outcome_only": {"PASS"},
        }
        if self.quality_state not in role_quality[self.role]:
            raise ContractError("release role and quality_state are incompatible")
        parse_utc_z(self.created_at, "created_at")
        if type(self.row_count) is not int or self.row_count < 0:
            raise ContractError("row_count cannot be negative")
        parsed_start = _parse_event_bound(self.event_start, "event_start") if self.event_start is not None else None
        parsed_end = _parse_event_bound(self.event_end, "event_end") if self.event_end is not None else None
        if parsed_start and parsed_end:
            if parsed_start[0] != parsed_end[0]:
                raise ContractError("event bounds must use the same date/timestamp representation")
            if parsed_start[1] > parsed_end[1]:
                raise ContractError("event_start must not follow event_end")
        if list(self.upstream_release_ids) != sorted(set(self.upstream_release_ids)):
            raise ContractError("upstream release IDs must be sorted and unique")
        paths = [entry.path for entry in self.files]
        if paths != sorted(set(paths)):
            raise ContractError("release file paths must be sorted and unique")
        for entry in self.files:
            safe_relative_path(entry.path)
            if entry.path == MANIFEST_NAME:
                raise ContractError("payload cannot shadow the release manifest")
            if type(entry.size) is not int or entry.size < 0:
                raise ContractError(f"invalid file metadata: {entry.path}")
            require_sha256(entry.sha256, f"release_file.{entry.path}.sha256")
        for field_name in ("schema_fingerprint", "code_hash", "config_hash", "environment_hash"):
            require_sha256(getattr(self, field_name), field_name)
        for index, release_id in enumerate(self.upstream_release_ids):
            require_sha256(release_id, f"upstream_release_ids[{index}]")
        require_sha256(self.release_id, "release_id")
        expected = sha256_bytes(canonical_json_bytes(self.unsigned_dict()))
        if self.release_id != expected:
            raise IntegrityError("release_id does not match canonical manifest content")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReleaseManifest":
        if set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("release manifest fields differ from the exact contract")
        if type(payload["schema_version"]) is not int or type(payload["row_count"]) is not int:
            raise ContractError("release manifest integer fields require exact JSON integers")
        if not isinstance(payload["files"], list) or not isinstance(payload["upstream_release_ids"], list):
            raise ContractError("release manifest collections require exact JSON arrays")
        manifest = cls(
            schema_version=int(payload["schema_version"]),
            project=str(payload["project"]),
            dataset=str(payload["dataset"]),
            source_epoch=str(payload["source_epoch"]),
            role=str(payload["role"]),
            quality_state=str(payload["quality_state"]),
            created_at=str(payload["created_at"]),
            row_count=int(payload["row_count"]),
            event_start=payload.get("event_start"),
            event_end=payload.get("event_end"),
            upstream_release_ids=tuple(payload.get("upstream_release_ids", [])),
            schema_fingerprint=str(payload["schema_fingerprint"]),
            code_hash=str(payload["code_hash"]),
            config_hash=str(payload["config_hash"]),
            environment_hash=str(payload["environment_hash"]),
            files=tuple(ReleaseFile(**entry) for entry in payload["files"]),
            release_id=str(payload["release_id"]),
        )
        manifest.validate()
        return manifest


def build_manifest(
    staging_root: Path,
    relative_paths: Iterable[str],
    *,
    project: str,
    dataset: str,
    source_epoch: str,
    role: str,
    quality_state: str,
    created_at: str,
    row_count: int,
    event_start: str | None,
    event_end: str | None,
    upstream_release_ids: Iterable[str] = (),
    schema_fingerprint: str,
    code_hash: str,
    config_hash: str,
    environment_hash: str,
) -> ReleaseManifest:
    root = Path(staging_root).resolve(strict=True)
    reject_link(root)
    entries: list[ReleaseFile] = []
    for raw_path in sorted(set(relative_paths)):
        relative = safe_relative_path(raw_path)
        candidate = root.joinpath(*relative.parts)
        reject_link(candidate)
        if not candidate.is_file():
            raise ContractError(f"manifest path is not a regular file: {candidate}")
        if candidate.stat().st_nlink != 1:
            raise ContractError(f"hardlinked staging files are prohibited: {candidate}")
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            raise ContractError(f"manifest path escapes staging root: {candidate}")
        entries.append(
            ReleaseFile(path=relative.as_posix(), size=candidate.stat().st_size, sha256=sha256_file(candidate))
        )
    unsigned = {
        "schema_version": 1,
        "project": project,
        "dataset": dataset,
        "source_epoch": source_epoch,
        "role": role,
        "quality_state": quality_state,
        "created_at": created_at,
        "row_count": row_count,
        "event_start": event_start,
        "event_end": event_end,
        "upstream_release_ids": sorted(set(upstream_release_ids)),
        "schema_fingerprint": schema_fingerprint,
        "code_hash": code_hash,
        "config_hash": config_hash,
        "environment_hash": environment_hash,
        "files": [entry.as_dict() for entry in entries],
    }
    manifest = ReleaseManifest(
        schema_version=1,
        project=project,
        dataset=dataset,
        source_epoch=source_epoch,
        role=role,
        quality_state=quality_state,
        created_at=created_at,
        row_count=row_count,
        event_start=event_start,
        event_end=event_end,
        upstream_release_ids=tuple(unsigned["upstream_release_ids"]),
        schema_fingerprint=schema_fingerprint,
        code_hash=code_hash,
        config_hash=config_hash,
        environment_hash=environment_hash,
        files=tuple(entries),
        release_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    manifest.validate()
    return manifest


def _payload_files(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*"):
        reject_link(path)
        if path.is_file() and path.name != MANIFEST_NAME:
            found.add(path.relative_to(root).as_posix())
    return found


def verify_release(release_dir: Path, expected: ReleaseManifest | None = None) -> ReleaseManifest:
    root = Path(release_dir)
    reject_link(root)
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise IntegrityError(f"release manifest missing: {manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"release manifest unreadable: {manifest_path}") from exc
    try:
        manifest = ReleaseManifest.from_dict(payload)
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise IntegrityError("release manifest violates the exact schema") from exc
    if expected and manifest != expected:
        raise IntegrityError("published manifest differs from expected manifest")
    declared = {entry.path for entry in manifest.files}
    actual = _payload_files(root)
    if actual != declared:
        raise IntegrityError(f"release payload differs: missing={declared-actual}, extra={actual-declared}")
    for entry in manifest.files:
        candidate = root.joinpath(*safe_relative_path(entry.path).parts)
        if (
            not candidate.is_file()
            or candidate.stat().st_nlink != 1
            or candidate.stat().st_size != entry.size
            or sha256_file(candidate) != entry.sha256
        ):
            raise IntegrityError(f"release payload hash mismatch: {entry.path}")
    expected_files = declared | {MANIFEST_NAME}
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(root, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError(str(exc)) from exc
    return manifest


def verify_accepted_release(
    release_dir: Path,
    *,
    accepted_root: Path,
    expected: ReleaseManifest | None = None,
) -> ReleaseManifest:
    """Verify a published content-addressed release under one accepted root."""

    root = Path(accepted_root)
    directory = Path(release_dir)
    if not root.is_absolute() or not directory.is_absolute():
        raise ContractError("accepted release root and directory must be absolute")
    require_contained_path(root, root)
    require_contained_path(directory, root)
    manifest = verify_release(directory, expected)
    expected_directory = root / manifest.dataset / manifest.release_id
    if directory.resolve(strict=True) != expected_directory.resolve(strict=True):
        raise IntegrityError(
            "release is self-consistent but is not published at accepted_root/dataset/release_id"
        )
    return manifest


class AtomicReleasePublisher:
    def __init__(self, release_root: Path):
        self.release_root = Path(release_root)
        if not self.release_root.is_absolute():
            raise ContractError("accepted release root must be absolute")

    def _authenticate(self, path: Path, *, must_exist: bool) -> Path:
        authenticated = require_contained_path(
            path,
            self.release_root,
            must_exist=must_exist,
        )
        if authenticated.exists():
            reject_link(authenticated)
        return authenticated

    def publish(self, staging_root: Path, manifest: ReleaseManifest) -> Path:
        manifest.validate()
        root = self.release_root
        root.mkdir(parents=True, exist_ok=True)
        require_contained_path(root, root)
        dataset_root = root / manifest.dataset
        destination = dataset_root / manifest.release_id
        lock_path = root / ".locks" / f"{manifest.dataset}.lock"
        with ExclusiveFileLock(lock_path, allowed_root=root):
            self._authenticate(dataset_root, must_exist=False)
            self._authenticate(destination, must_exist=False)
            if destination.exists():
                verify_accepted_release(
                    destination,
                    accepted_root=root,
                    expected=manifest,
                )
                return destination
            dataset_root.mkdir(parents=True, exist_ok=True)
            self._authenticate(dataset_root, must_exist=True)
            pending = dataset_root / f".pending-{manifest.release_id[:12]}-{uuid.uuid4().hex[:8]}"
            self._authenticate(pending, must_exist=False)
            pending.mkdir()
            self._authenticate(pending, must_exist=True)
            try:
                stage = Path(staging_root).resolve(strict=True)
                for entry in manifest.files:
                    relative = safe_relative_path(entry.path)
                    source = stage.joinpath(*relative.parts)
                    reject_link(source)
                    if source.stat().st_size != entry.size or sha256_file(source) != entry.sha256:
                        raise IntegrityError(f"staging payload changed: {entry.path}")
                    if not source.is_file() or source.stat().st_nlink != 1:
                        raise IntegrityError(f"staging payload is not an independent plain file: {entry.path}")
                    target = pending.joinpath(*relative.parts)
                    self._authenticate(target.parent, must_exist=False)
                    self._authenticate(target, must_exist=False)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self._authenticate(target.parent, must_exist=True)
                    shutil.copyfile(source, target)
                    self._authenticate(target, must_exist=True)
                    with target.open("r+b") as copied:
                        os.fsync(copied.fileno())
                    if target.stat().st_nlink != 1 or os.path.samefile(source, target):
                        raise IntegrityError(f"copy unexpectedly shares file identity: {entry.path}")
                manifest_path = pending / MANIFEST_NAME
                self._authenticate(manifest_path, must_exist=False)
                atomic_write(manifest_path, canonical_json_bytes(manifest.as_dict()))
                self._authenticate(manifest_path, must_exist=True)
                verify_release(pending, manifest)
                self._authenticate(pending, must_exist=True)
                self._authenticate(destination, must_exist=False)
                os.replace(pending, destination)
                verify_accepted_release(
                    destination,
                    accepted_root=root,
                    expected=manifest,
                )
                return destination
            except Exception:
                # Preserve partial work for explicit quarantine/recovery; never
                # expose it under an accepted release ID.
                raise

    def quarantine_orphans(self, dataset: str) -> list[Path]:
        if not IDENTIFIER.fullmatch(dataset):
            raise ContractError("orphan dataset must be a safe identifier")
        if not self.release_root.is_absolute():
            raise ContractError("release root must be absolute for orphan quarantine")
        require_contained_path(self.release_root, self.release_root)
        dataset_root = self.release_root / dataset
        require_contained_path(dataset_root, self.release_root, must_exist=False)
        if not dataset_root.exists():
            return []
        moved: list[Path] = []
        lock_path = self.release_root / ".locks" / f"{dataset}.lock"
        with ExclusiveFileLock(lock_path, allowed_root=self.release_root):
            quarantine = self.release_root / ".quarantine" / dataset
            require_contained_path(quarantine, self.release_root, must_exist=False)
            for pending in sorted(dataset_root.glob(".pending-*")):
                require_contained_path(pending, self.release_root)
                reject_link(pending)
                quarantine.mkdir(parents=True, exist_ok=True)
                target = quarantine / pending.name
                if target.exists():
                    target = quarantine / f"{pending.name}-{uuid.uuid4().hex}"
                os.replace(pending, target)
                moved.append(target)
        return moved
