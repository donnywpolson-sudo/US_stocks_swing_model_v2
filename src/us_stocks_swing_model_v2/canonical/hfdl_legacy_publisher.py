"""Deterministic, restart-safe publication of caveated HFDL legacy history.

The publisher reads only a completed content-addressed migration release.  It
never follows the legacy ``source`` paths retained in that release's manifest.
The March 2022 feed break is published as two independent accepted releases;
an immutable set receipt is required to treat the pair as complete.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..common import (
    _fsync_directory,
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from ..environment import validate_environment_lock
from ..errors import ContractError, IntegrityError
from ..locking import ExclusiveFileLock
from ..migration import (
    CONTROLLED_REBUILD_AUTHORIZATION_CLASS,
    CONTROLLED_REBUILD_AUTHORIZATION_ID,
    MIGRATION_MANIFEST_SCHEMA_VERSION,
    PAYLOAD_LAYOUT_VERSION,
    validate_migration_payload_object,
)
from ..releases import (
    AtomicReleasePublisher,
    ReleaseManifest,
    build_manifest,
    verify_accepted_release,
)
from .hfdl import (
    HFDL_HISTORICAL_AVAILABILITY,
    HFDL_POINT_IN_TIME_STATE,
    HFDL_TAGGED_SCHEMA,
    load_validated_hfdl_sidecar,
    validate_and_tag_hfdl,
    write_tagged_hfdl_legacy_epochs,
)


EXPECTED_HFDL_PAIR_COUNT = 1_388
HFDL_MIGRATION_FAMILY = "hfdl_legacy_discovery_payload"
HFDL_PAYLOAD_PREFIX = PurePosixPath(
    "source_releases/hfdl_legacy_discovery/payload/data/raw"
)
HFDL_EPOCHS = (
    "hfdl_pitrading_consolidated",
    "hfdl_iex_only",
)
EPOCH_DATASETS = {
    "hfdl_pitrading_consolidated": "hfdl_pitrading_consolidated_legacy_bars",
    "hfdl_iex_only": "hfdl_iex_only_legacy_bars",
}
SET_DATASET = "hfdl_legacy_epoch_set"
SET_SOURCE_EPOCH = "hfdl_epoch_set_no_pooling"
SYNTHETIC_CONTRACT_SCOPE = "SYNTHETIC_HFDL_LEGACY_PUBLISH_CONTRACT"
SYNTHETIC_PUBLICATION_SCOPE = "SYNTHETIC_HFDL_LEGACY_PUBLICATION"
QUALITY_STATE = "LEGACY_CAVEATED"
EVIDENCE_CLASS = "LEGACY_DISCOVERY"
SOURCE_ADJUSTMENT = "hfdl_clean_source_adjusted"

_EVIDENCE_FIELDS = {
    "plan_id",
    "migration_manifest_schema_version",
    "payload_layout_version",
    "config_sha256",
    "inventory_sha256",
    "migration_implementation_manifest",
    "migration_implementation_sha256",
    "approval_id",
    "authorization_receipt_id",
    "authorization_registry_id",
    "authorization_class",
}
_SUMMARY_FIELDS = {
    "schema_version",
    *_EVIDENCE_FIELDS,
    "state",
    "file_count",
    "total_bytes",
    "role_file_counts",
    "family_file_counts",
    "family_bytes",
}
_CHECKPOINT_FIELDS = {
    "schema_version",
    *_EVIDENCE_FIELDS,
    "state",
    "completed",
    "completed_count",
    "completed_at",
}
_COMPLETION_FIELDS = {
    "schema_version",
    *_EVIDENCE_FIELDS,
    "state",
    "file_count",
    "total_bytes",
    "completed_at",
}
_FAMILY_FIELDS = {
    "schema_version",
    *_EVIDENCE_FIELDS,
    "family_id",
    "role",
    "state",
    "file_count",
    "total_bytes",
    "family_manifest_sha256",
}
_MIGRATION_ENTRY_FIELDS = {
    "schema_version",
    "migration_id",
    "role",
    "source",
    "destination",
    "size",
    "sha256",
    "payload_object",
}


@dataclass(frozen=True, init=False)
class HfdlPublishContract:
    expected_pair_count: int
    synthetic_permit_id: str | None

    @classmethod
    def _construct(cls, expected_pair_count: int, synthetic_permit_id: str | None) -> "HfdlPublishContract":
        value = object.__new__(cls)
        object.__setattr__(value, "expected_pair_count", expected_pair_count)
        object.__setattr__(value, "synthetic_permit_id", synthetic_permit_id)
        value.validate()
        return value

    @classmethod
    def production(cls) -> "HfdlPublishContract":
        return cls._construct(EXPECTED_HFDL_PAIR_COUNT, None)

    @classmethod
    def synthetic_fixture(
        cls,
        expected_pair_count: int,
        *,
        permit: SyntheticOnlyPermit,
    ) -> "HfdlPublishContract":
        verified = require_synthetic_permit(permit, scope=SYNTHETIC_CONTRACT_SCOPE)
        return cls._construct(expected_pair_count, verified.permit_id)

    def validate(self) -> None:
        if (
            isinstance(self.expected_pair_count, bool)
            or not isinstance(self.expected_pair_count, int)
            or self.expected_pair_count < 1
        ):
            raise ContractError("HFDL expected pair count must be a positive exact integer")
        if self.synthetic_permit_id is None:
            if self.expected_pair_count != EXPECTED_HFDL_PAIR_COUNT:
                raise ContractError("production HFDL publication requires exactly 1,388 pairs")
        else:
            require_sha256(self.synthetic_permit_id, "hfdl_contract.synthetic_permit_id")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": 1,
            "expected_pair_count": self.expected_pair_count,
            "synthetic_permit_id": self.synthetic_permit_id,
            "migration_family": HFDL_MIGRATION_FAMILY,
            "physical_epoch_releases": list(HFDL_EPOCHS),
            "evidence_class": EVIDENCE_CLASS,
            "point_in_time_state": HFDL_POINT_IN_TIME_STATE,
            "historical_availability_state": HFDL_HISTORICAL_AVAILABILITY,
            "source_adjustment": SOURCE_ADJUSTMENT,
            "active_pipeline_eligible": False,
            "pooling_permitted": False,
        }

    @property
    def contract_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))


@dataclass(frozen=True)
class MigrationPayloadEntry:
    schema_version: int
    migration_id: str
    role: str
    source: str
    destination: str
    size: int
    sha256: str
    payload_object: str
    payload_relative: str
    payload_path: Path

    def manifest_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "migration_id": self.migration_id,
            "role": self.role,
            "source": self.source,
            "destination": self.destination,
            "size": self.size,
            "sha256": self.sha256,
            "payload_object": self.payload_object,
        }


@dataclass(frozen=True)
class CompletedMigrationRelease:
    root: Path
    plan_id: str
    inventory_sha256: str
    completion_receipt_sha256: str
    family_receipt_sha256: str
    family_manifest_sha256: str
    entries: tuple[MigrationPayloadEntry, ...]


@dataclass(frozen=True)
class HfdlInputPair:
    pair_id: str
    parquet: MigrationPayloadEntry
    sidecar: MigrationPayloadEntry

    def as_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "parquet_relative": self.parquet.payload_relative,
            "parquet_sha256": self.parquet.sha256,
            "parquet_size": self.parquet.size,
            "sidecar_relative": self.sidecar.payload_relative,
            "sidecar_sha256": self.sidecar.sha256,
            "sidecar_size": self.sidecar.size,
        }


@dataclass(frozen=True)
class HfdlPublicationResult:
    build_id: str
    epoch_release_directories: Mapping[str, Path]
    epoch_set_release_directory: Path


def _read_canonical_json(path: Path, expected_fields: set[str], label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise IntegrityError(f"{label} fields differ from the exact completed contract")
    if raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} is not canonically encoded")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise IntegrityError(f"{label} schema_version is invalid")
    return payload


def _validate_evidence(document: Mapping[str, Any]) -> dict[str, Any]:
    evidence = {name: document[name] for name in _EVIDENCE_FIELDS}
    if (
        evidence["migration_manifest_schema_version"]
        != MIGRATION_MANIFEST_SCHEMA_VERSION
        or evidence["payload_layout_version"] != PAYLOAD_LAYOUT_VERSION
    ):
        raise IntegrityError("migration payload layout evidence is unversioned or unsupported")
    for name in (
        "plan_id",
        "config_sha256",
        "inventory_sha256",
        "migration_implementation_sha256",
        "approval_id",
        "authorization_receipt_id",
        "authorization_registry_id",
    ):
        require_sha256(evidence[name], f"migration.{name}")
    implementation = evidence["migration_implementation_manifest"]
    if not isinstance(implementation, dict) or not implementation:
        raise IntegrityError("migration implementation manifest is invalid")
    for path, digest in implementation.items():
        if not isinstance(path, str) or not isinstance(digest, str):
            raise IntegrityError("migration implementation manifest fields are invalid")
        safe_relative_path(path)
        require_sha256(digest, f"migration.implementation.{path}")
    if evidence["migration_implementation_sha256"] != sha256_bytes(
        canonical_json_bytes(dict(sorted(implementation.items())))
    ):
        raise IntegrityError("migration implementation aggregate hash differs")
    authorization_class = evidence["authorization_class"]
    if authorization_class == "EXTERNAL_USER_AUTHORITY":
        pass
    elif authorization_class == CONTROLLED_REBUILD_AUTHORIZATION_CLASS:
        if evidence["authorization_registry_id"] != CONTROLLED_REBUILD_AUTHORIZATION_ID:
            raise IntegrityError(
                "controlled rebuild migration release does not bind the exact user-task authority"
            )
    else:
        raise IntegrityError("migration release was completed under an ineligible authority class")
    return evidence


def verify_completed_migration_release(path: Path) -> CompletedMigrationRelease:
    """Verify the complete immutable migration capsule without touching legacy paths."""

    root = Path(path)
    if not root.is_absolute():
        raise ContractError("migration release path must be absolute")
    reject_link(root)
    if not root.is_dir() or root.parent.name != "migration_releases":
        raise ContractError("migration input must be an exact migration_releases/plan_id directory")
    require_sha256(root.name, "migration.release_directory")
    summary = _read_canonical_json(root / "summary.json", _SUMMARY_FIELDS, "migration summary")
    checkpoint = _read_canonical_json(root / "checkpoint.json", _CHECKPOINT_FIELDS, "migration checkpoint")
    completion = _read_canonical_json(
        root / "completion_receipt.json", _COMPLETION_FIELDS, "migration completion receipt"
    )
    evidence = _validate_evidence(summary)
    if any({name: document[name] for name in _EVIDENCE_FIELDS} != evidence for document in (checkpoint, completion)):
        raise IntegrityError("migration completion documents bind different evidence")
    if evidence["plan_id"] != root.name:
        raise IntegrityError("migration directory does not match its plan_id")
    for document in (summary, checkpoint, completion):
        if document["state"] != "COMPLETE_NON_ACTIVE":
            raise IntegrityError("migration release is not exactly COMPLETE_NON_ACTIVE")
    if checkpoint["completed_at"] != completion["completed_at"]:
        raise IntegrityError("migration completion times differ")
    parse_utc_z(checkpoint["completed_at"], "migration.completed_at")

    manifest_path = root / "migration_files.jsonl"
    reject_link(manifest_path)
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise IntegrityError("migration file manifest is missing") from exc
    if manifest_path.stat().st_nlink != 1 or sha256_bytes(manifest_bytes) != evidence["inventory_sha256"]:
        raise IntegrityError("migration inventory hash differs from the completion receipt")
    raw_entries: list[dict[str, Any]] = []
    for sequence, line in enumerate(manifest_bytes.splitlines(keepends=True)):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError(f"migration manifest JSON is invalid at row {sequence}") from exc
        if not isinstance(entry, dict) or set(entry) != _MIGRATION_ENTRY_FIELDS:
            raise IntegrityError("migration manifest entry fields differ")
        if line != canonical_json_bytes(entry):
            raise IntegrityError("migration manifest entry is not canonically encoded")
        if (
            type(entry["schema_version"]) is not int
            or entry["schema_version"] != MIGRATION_MANIFEST_SCHEMA_VERSION
            or type(entry["size"]) is not int
            or entry["size"] < 0
        ):
            raise IntegrityError("migration manifest size must be an exact nonnegative integer")
        if any(
            type(entry[name]) is not str
            for name in (
                "migration_id",
                "role",
                "source",
                "destination",
                "sha256",
                "payload_object",
            )
        ):
            raise IntegrityError(
                "migration manifest identity/provenance fields must be exact strings"
            )
        require_sha256(entry["sha256"], "migration.entry.sha256")
        try:
            payload_object = validate_migration_payload_object(entry["payload_object"])
        except ContractError as exc:
            raise IntegrityError("migration manifest payload object is invalid") from exc
        if payload_object != entry["payload_object"]:
            raise IntegrityError("migration manifest payload object is not canonical")
        if entry["role"] not in {
            "legacy_discovery_only",
            "qualification_evidence_only",
            "legacy_trial_census_evidence_only",
        }:
            raise IntegrityError("migration manifest contains an active/model role")
        raw_entries.append(entry)
    if not raw_entries:
        raise IntegrityError("migration manifest is empty")
    destinations = [entry["destination"].casefold() for entry in raw_entries]
    if len(destinations) != len(set(destinations)):
        raise IntegrityError("migration manifest contains duplicate destinations")
    payload_objects = [entry["payload_object"] for entry in raw_entries]
    if len(payload_objects) != len(set(payload_objects)):
        raise IntegrityError("migration manifest contains duplicate payload objects")

    completed = checkpoint["completed"]
    if not isinstance(completed, dict) or len(completed) != len(raw_entries):
        raise IntegrityError("migration checkpoint does not cover its full manifest")
    completed_keys: dict[str, str] = {}
    for relative, digest in completed.items():
        normalized = safe_relative_path(relative).as_posix()
        if normalized != relative:
            raise IntegrityError("migration checkpoint paths must be canonical relative paths")
        require_sha256(digest, f"migration.completed.{relative}")
        completed_keys[relative] = digest

    entries: list[MigrationPayloadEntry] = []
    used_keys: set[str] = set()
    for raw in raw_entries:
        destination = raw["destination"].replace("\\", "/")
        matches = [key for key in completed_keys if destination.endswith("/" + key)]
        if len(matches) != 1:
            raise IntegrityError("migration destination cannot be matched to exactly one payload receipt")
        relative = matches[0]
        if relative in used_keys or completed_keys[relative] != raw["sha256"]:
            raise IntegrityError("migration completion map differs from its reviewed manifest")
        used_keys.add(relative)
        object_relative = validate_migration_payload_object(raw["payload_object"])
        payload_path = root.joinpath(
            "payload", *safe_relative_path(object_relative).parts
        )
        reject_link(payload_path)
        if (
            not payload_path.is_file()
            or payload_path.stat().st_nlink != 1
            or payload_path.stat().st_size != raw["size"]
            or sha256_file(payload_path) != raw["sha256"]
        ):
            raise IntegrityError(f"migration payload changed, linked, or is incomplete: {relative}")
        entries.append(
            MigrationPayloadEntry(
                schema_version=raw["schema_version"],
                migration_id=raw["migration_id"],
                role=raw["role"],
                source=raw["source"],
                destination=raw["destination"],
                size=raw["size"],
                sha256=raw["sha256"],
                payload_object=object_relative,
                payload_relative=relative,
                payload_path=payload_path,
            )
        )
    if used_keys != set(completed_keys):
        raise IntegrityError("migration release contains unbound completed payload paths")

    role_counts = dict(sorted(Counter(entry.role for entry in entries).items()))
    family_counts = dict(sorted(Counter(entry.migration_id for entry in entries).items()))
    family_bytes: Counter[str] = Counter()
    for entry in entries:
        family_bytes[entry.migration_id] += entry.size
    if (
        type(summary["file_count"]) is not int
        or type(summary["total_bytes"]) is not int
        or summary["file_count"] != len(entries)
        or summary["total_bytes"] != sum(entry.size for entry in entries)
        or checkpoint["completed_count"] != len(entries)
        or completion["file_count"] != len(entries)
        or completion["total_bytes"] != sum(entry.size for entry in entries)
        or summary["role_file_counts"] != role_counts
        or summary["family_file_counts"] != family_counts
        or summary["family_bytes"] != dict(sorted(family_bytes.items()))
    ):
        raise IntegrityError("migration counts/denominators differ from the completed manifest")

    expected_files = {
        "summary.json",
        "checkpoint.json",
        "completion_receipt.json",
        "migration_files.jsonl",
    }
    family_receipt_hash = ""
    family_manifest_hash = ""
    for family in sorted(family_counts):
        receipt_path = root / "family_receipts" / f"{family}.json"
        receipt = _read_canonical_json(receipt_path, _FAMILY_FIELDS, f"migration family {family}")
        family_entries = [entry for entry in entries if entry.migration_id == family]
        manifest_hash = sha256_bytes(
            b"".join(canonical_json_bytes(entry.manifest_dict()) for entry in family_entries)
        )
        if (
            {name: receipt[name] for name in _EVIDENCE_FIELDS} != evidence
            or receipt["family_id"] != family
            or receipt["role"] != family_entries[0].role
            or receipt["state"] != "COMPLETE_NON_ACTIVE"
            or receipt["file_count"] != len(family_entries)
            or receipt["total_bytes"] != sum(entry.size for entry in family_entries)
            or receipt["family_manifest_sha256"] != manifest_hash
        ):
            raise IntegrityError(f"migration family receipt differs: {family}")
        expected_files.add(f"family_receipts/{family}.json")
        if family == HFDL_MIGRATION_FAMILY:
            family_receipt_hash = sha256_file(receipt_path)
            family_manifest_hash = manifest_hash
    for entry in entries:
        expected_files.add("payload/" + entry.payload_object)
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(root, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError("migration release has missing/extra/linked inputs") from exc
    if not family_receipt_hash:
        raise IntegrityError("completed migration lacks the approved HFDL payload family")
    return CompletedMigrationRelease(
        root=root,
        plan_id=evidence["plan_id"],
        inventory_sha256=evidence["inventory_sha256"],
        completion_receipt_sha256=sha256_file(root / "completion_receipt.json"),
        family_receipt_sha256=family_receipt_hash,
        family_manifest_sha256=family_manifest_hash,
        entries=tuple(entries),
    )


def _hfdl_pairs(
    migration: CompletedMigrationRelease,
    contract: HfdlPublishContract,
) -> tuple[HfdlInputPair, ...]:
    selected = [entry for entry in migration.entries if entry.migration_id == HFDL_MIGRATION_FAMILY]
    if any(entry.role != "legacy_discovery_only" for entry in selected):
        raise IntegrityError("HFDL migration family has a non-legacy-discovery role")
    expected_files = contract.expected_pair_count * 2
    if len(selected) != expected_files:
        raise IntegrityError(
            f"HFDL migration family requires exactly {contract.expected_pair_count} Parquet/sidecar pairs"
        )
    parquet: dict[str, MigrationPayloadEntry] = {}
    sidecars: dict[str, MigrationPayloadEntry] = {}
    prefix = HFDL_PAYLOAD_PREFIX.as_posix() + "/"
    for entry in selected:
        relative = entry.payload_relative
        if not relative.startswith(prefix):
            raise IntegrityError("HFDL input is outside the exact approved payload directory")
        tail = relative[len(prefix) :]
        if "/" in tail or not tail:
            raise IntegrityError("HFDL approved payload contains an unexpected nested/empty input")
        if tail.endswith(".parquet.provenance.json"):
            base = tail[: -len(".provenance.json")]
            target = sidecars
        elif tail.endswith(".parquet"):
            base = tail
            target = parquet
        else:
            raise IntegrityError("HFDL approved payload contains a non-pair input")
        if base in target:
            raise IntegrityError("HFDL migration contains a duplicate pair member")
        target[base] = entry
    if set(parquet) != set(sidecars) or len(parquet) != contract.expected_pair_count:
        raise IntegrityError("HFDL migration has a missing or unmatched Parquet/sidecar pair")
    pairs: list[HfdlInputPair] = []
    for base in sorted(parquet):
        unsigned = {
            "parquet_relative": parquet[base].payload_relative,
            "parquet_sha256": parquet[base].sha256,
            "parquet_size": parquet[base].size,
            "sidecar_relative": sidecars[base].payload_relative,
            "sidecar_sha256": sidecars[base].sha256,
            "sidecar_size": sidecars[base].size,
        }
        pairs.append(
            HfdlInputPair(
                pair_id=sha256_bytes(canonical_json_bytes(unsigned)),
                parquet=parquet[base],
                sidecar=sidecars[base],
            )
        )
    return tuple(pairs)


def _implementation_hash() -> str:
    repo = Path(__file__).resolve().parents[3]
    relatives = (
        "src/us_stocks_swing_model_v2/canonical/hfdl_legacy_publisher.py",
        "src/us_stocks_swing_model_v2/canonical/hfdl.py",
        "src/us_stocks_swing_model_v2/canonical/parquet.py",
        "src/us_stocks_swing_model_v2/releases.py",
        "src/us_stocks_swing_model_v2/common.py",
        "src/us_stocks_swing_model_v2/locking.py",
        "src/us_stocks_swing_model_v2/migration.py",
    )
    manifest: dict[str, str] = {}
    for relative in relatives:
        candidate = repo.joinpath(*safe_relative_path(relative).parts)
        require_contained_path(candidate, repo)
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(f"HFDL publisher implementation dependency is invalid: {relative}")
        manifest[relative] = sha256_file(candidate)
    return sha256_bytes(canonical_json_bytes(manifest))


def _environment_hash() -> str:
    repo = Path(__file__).resolve().parents[3]
    return validate_environment_lock(repo / "config" / "environment.lock.json")


def _verify_pair_inputs(pair: HfdlInputPair) -> None:
    for entry in (pair.parquet, pair.sidecar):
        reject_link(entry.payload_path)
        if (
            not entry.payload_path.is_file()
            or entry.payload_path.stat().st_nlink != 1
            or entry.payload_path.stat().st_size != entry.size
            or sha256_file(entry.payload_path) != entry.sha256
        ):
            raise IntegrityError("verified migration HFDL pair changed during publication")
    load_validated_hfdl_sidecar(pair.sidecar.payload_path)


def _capsule_receipt(path: Path, *, pair: HfdlInputPair, build_id: str) -> dict[str, Any]:
    receipt = _read_canonical_json(
        path / "receipt.json",
        {
            "schema_version",
            "build_id",
            "pair_binding",
            "symbol",
            "source_row_count",
            "outputs",
            "capsule_id",
        },
        "HFDL pair capsule receipt",
    )
    if (
        receipt["build_id"] != build_id
        or receipt["pair_binding"] != pair.as_dict()
        or type(receipt["source_row_count"]) is not int
        or receipt["source_row_count"] < 1
        or not isinstance(receipt["symbol"], str)
        or not receipt["symbol"]
        or not isinstance(receipt["outputs"], dict)
        or not receipt["outputs"]
    ):
        raise IntegrityError("HFDL pair capsule is bound to different input evidence")
    unsigned = dict(receipt)
    capsule_id = unsigned.pop("capsule_id")
    require_sha256(capsule_id, "hfdl_capsule.capsule_id")
    if capsule_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("HFDL pair capsule ID differs from its receipt")
    expected_files = {"receipt.json"}
    row_total = 0
    for epoch, output in receipt["outputs"].items():
        if epoch not in HFDL_EPOCHS or not isinstance(output, dict) or set(output) != {
            "path",
            "sha256",
            "size",
            "row_count",
            "session_start",
            "session_end",
        }:
            raise IntegrityError("HFDL pair capsule output fields/epoch differ")
        if output["path"] != f"{epoch}.parquet":
            raise IntegrityError("HFDL capsule output path differs from its epoch")
        require_sha256(output["sha256"], f"hfdl_capsule.{epoch}.sha256")
        if (
            type(output["size"]) is not int
            or type(output["row_count"]) is not int
            or output["size"] < 1
            or output["row_count"] < 1
        ):
            raise IntegrityError("HFDL capsule output counts are invalid")
        candidate = path / output["path"]
        reject_link(candidate)
        if (
            not candidate.is_file()
            or candidate.stat().st_nlink != 1
            or candidate.stat().st_size != output["size"]
            or sha256_file(candidate) != output["sha256"]
        ):
            raise IntegrityError("HFDL capsule output changed or is partial/linked")
        table = pq.read_table(candidate)
        if table.schema.remove_metadata() != HFDL_TAGGED_SCHEMA or table.num_rows != output["row_count"]:
            raise IntegrityError("HFDL capsule output schema/row count differs")
        rows = table.to_pylist()
        sessions = [row["session"] for row in rows]
        if (
            not rows
            or any(row["symbol"] != receipt["symbol"] for row in rows)
            or any(row["source_epoch"] != epoch for row in rows)
            or any(row["source_adjustment"] != SOURCE_ADJUSTMENT for row in rows)
            or any(row["evidence_class"] != EVIDENCE_CLASS for row in rows)
            or any(row["point_in_time_safe"] is not False for row in rows)
            or any(row["point_in_time_state"] != HFDL_POINT_IN_TIME_STATE for row in rows)
            or any(
                row["historical_availability_state"] != HFDL_HISTORICAL_AVAILABILITY
                for row in rows
            )
            or sessions != sorted(set(sessions))
            or output["session_start"] != sessions[0].isoformat()
            or output["session_end"] != sessions[-1].isoformat()
        ):
            raise IntegrityError("HFDL capsule output mixes epochs, duplicates sessions, or loses caveats")
        expected_files.add(output["path"])
        row_total += output["row_count"]
    if row_total != receipt["source_row_count"]:
        raise IntegrityError("HFDL epoch split does not conserve the source row denominator")
    try:
        assert_exact_tree(path, expected_files, set())
    except ContractError as exc:
        raise IntegrityError("HFDL pair capsule contains missing/extra/linked output") from exc
    return receipt


def _pending_capsule_manifest(path: Path) -> tuple[dict[str, object], ...]:
    reject_link(path)
    if not path.is_dir():
        raise IntegrityError("HFDL pending capsule is not an independent directory")
    entries: list[dict[str, object]] = []
    for candidate in sorted(
        path.rglob("*"),
        key=lambda item: item.relative_to(path).as_posix(),
    ):
        reject_link(candidate)
        relative = candidate.relative_to(path).as_posix()
        if candidate.is_dir():
            entries.append({"kind": "directory", "path": relative})
            continue
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(
                "HFDL pending capsule contains linked or non-regular evidence"
            )
        entries.append(
            {
                "kind": "file",
                "path": relative,
                "size": candidate.stat().st_size,
                "sha256": sha256_file(candidate),
            }
        )
    return tuple(entries)


def _quarantine_pending_capsule(
    pending: Path,
    *,
    pairs_root: Path,
    reason: str,
) -> Path:
    """Atomically retain a failed/interrupted capsule; never reuse or delete it."""

    if reason not in {
        "MATERIALIZATION_FAILED",
        "INTERRUPTED_BEFORE_RETRY",
    }:
        raise ContractError("HFDL pending-capsule quarantine reason is invalid")
    require_contained_path(pending, pairs_root)
    manifest = _pending_capsule_manifest(pending)
    manifest_hash = sha256_bytes(canonical_json_bytes(list(manifest)))
    original_path = str(pending.resolve(strict=True))
    quarantine_root = pairs_root.parent / ".quarantine"
    require_contained_path(
        quarantine_root,
        pairs_root.parent,
        must_exist=False,
    )
    quarantine_root.mkdir(parents=True, exist_ok=True)
    container = quarantine_root / uuid.uuid4().hex[:16]
    container.mkdir()
    payload_target = container / "payload"
    os.replace(pending, payload_target)
    _fsync_directory(pairs_root)
    _fsync_directory(container)
    receipt = {
        "schema_version": 1,
        "evidence_class": "HFDL_PENDING_CAPSULE_FAILURE_EVIDENCE",
        "automation_identity": "HFDL_LEGACY_PUBLISHER_SYNTHETIC_ONLY",
        "operator_action": "AUTOMATED_FAIL_CLOSED_QUARANTINE",
        "reason": reason,
        "original_path": original_path,
        "quarantine_payload_path": str(payload_target.resolve(strict=True)),
        "quarantined_at_utc": iso_z(datetime.now(timezone.utc)),
        "payload_manifest_sha256": manifest_hash,
        "payload_manifest": list(manifest),
        "retention": "INDEFINITE_UNTIL_EXPLICIT_OWNER_DISPOSITION",
        "direct_reuse_or_promotion": "PROHIBITED",
    }
    atomic_write(
        container / "quarantine_receipt.json",
        canonical_json_bytes(receipt),
    )
    return container


def _materialize_pair_capsule(
    pair: HfdlInputPair,
    *,
    pairs_root: Path,
    build_id: str,
) -> dict[str, Any]:
    _verify_pair_inputs(pair)
    final = pairs_root / pair.pair_id
    if final.exists():
        return _capsule_receipt(final, pair=pair, build_id=build_id)
    pending = pairs_root / f".pending-{pair.pair_id[:12]}-{uuid.uuid4().hex[:8]}"
    pending.mkdir()
    try:
        result = validate_and_tag_hfdl(pair.parquet.payload_path, pair.sidecar.payload_path)
        split_root = pending / "split"
        split_outputs = write_tagged_hfdl_legacy_epochs(result, split_root)
        outputs: dict[str, dict[str, object]] = {}
        for epoch, split_path in sorted(split_outputs.items()):
            target = pending / f"{epoch}.parquet"
            os.replace(split_path, target)
            table = pq.read_table(target)
            sessions = table.column("session").to_pylist()
            outputs[epoch] = {
                "path": target.name,
                "sha256": sha256_file(target),
                "size": target.stat().st_size,
                "row_count": table.num_rows,
                "session_start": sessions[0].isoformat(),
                "session_end": sessions[-1].isoformat(),
            }
        for candidate in sorted(
            split_root.rglob("*"),
            key=lambda item: len(item.relative_to(split_root).parts),
            reverse=True,
        ):
            reject_link(candidate)
            if not candidate.is_dir() or any(candidate.iterdir()):
                raise IntegrityError(
                    "HFDL split staging contains unretained evidence"
                )
            candidate.rmdir()
        split_root.rmdir()
        unsigned = {
            "schema_version": 1,
            "build_id": build_id,
            "pair_binding": pair.as_dict(),
            "symbol": result.symbol,
            "source_row_count": result.row_count,
            "outputs": outputs,
        }
        receipt = {
            **unsigned,
            "capsule_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
        atomic_write(pending / "receipt.json", canonical_json_bytes(receipt))
        _capsule_receipt(pending, pair=pair, build_id=build_id)
        os.replace(pending, final)
        return _capsule_receipt(final, pair=pair, build_id=build_id)
    except BaseException:
        if pending.exists():
            _quarantine_pending_capsule(
                pending,
                pairs_root=pairs_root,
                reason="MATERIALIZATION_FAILED",
            )
        raise


def _checkpoint_unsigned(
    *,
    build_id: str,
    contract: HfdlPublishContract,
    migration: CompletedMigrationRelease,
    input_bindings_hash: str,
    created_at: str,
    implementation_hash: str,
    environment_hash: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "build_id": build_id,
        "contract_id": contract.contract_id,
        "migration_plan_id": migration.plan_id,
        "migration_inventory_sha256": migration.inventory_sha256,
        "migration_completion_receipt_sha256": migration.completion_receipt_sha256,
        "migration_family_receipt_sha256": migration.family_receipt_sha256,
        "migration_family_manifest_sha256": migration.family_manifest_sha256,
        "input_bindings_hash": input_bindings_hash,
        "created_at": created_at,
        "implementation_hash": implementation_hash,
        "environment_hash": environment_hash,
    }


def _load_or_create_checkpoint(
    path: Path,
    *,
    evidence: Mapping[str, object],
) -> dict[str, Any]:
    expected_fields = {
        *evidence,
        "state",
        "completed_capsules",
        "release_ids",
        "checkpoint_id",
    }
    if not path.exists():
        unsigned = {
            **evidence,
            "state": "BUILDING_DERIVED_ONLY",
            "completed_capsules": {},
            "release_ids": {},
        }
        checkpoint = {
            **unsigned,
            "checkpoint_id": sha256_bytes(canonical_json_bytes(unsigned)),
        }
        atomic_write(path, canonical_json_bytes(checkpoint))
        return checkpoint
    checkpoint = _read_canonical_json(path, expected_fields, "HFDL publication checkpoint")
    if any(checkpoint[name] != value for name, value in evidence.items()):
        raise IntegrityError("HFDL checkpoint is bound to different immutable inputs")
    if checkpoint["state"] not in {"BUILDING_DERIVED_ONLY", "RELEASES_COMPLETE"}:
        raise IntegrityError("HFDL checkpoint state is invalid")
    if not isinstance(checkpoint["completed_capsules"], dict) or not isinstance(checkpoint["release_ids"], dict):
        raise IntegrityError("HFDL checkpoint maps are invalid")
    unsigned = dict(checkpoint)
    checkpoint_id = unsigned.pop("checkpoint_id")
    require_sha256(checkpoint_id, "hfdl_checkpoint.checkpoint_id")
    if checkpoint_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("HFDL checkpoint ID differs from its content")
    return checkpoint


def _write_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    unsigned = dict(checkpoint)
    unsigned.pop("checkpoint_id", None)
    checkpoint["checkpoint_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    atomic_write(path, canonical_json_bytes(checkpoint))


def _resume_capsules(
    pairs: tuple[HfdlInputPair, ...],
    *,
    pairs_root: Path,
    build_id: str,
    checkpoint_path: Path,
    checkpoint: dict[str, Any],
) -> tuple[dict[str, Any], ...]:
    pairs_root.mkdir(parents=True, exist_ok=True)
    pair_by_id = {pair.pair_id: pair for pair in pairs}
    for child in tuple(pairs_root.iterdir()):
        reject_link(child)
        if child.name.startswith(".pending-"):
            if not child.is_dir():
                raise IntegrityError("HFDL pending capsule is not a directory")
            _quarantine_pending_capsule(
                child,
                pairs_root=pairs_root,
                reason="INTERRUPTED_BEFORE_RETRY",
            )
            continue
        if not child.is_dir() or child.name not in pair_by_id:
            raise IntegrityError("HFDL checkpoint contains an extra or unknown derived capsule")
    completed = checkpoint["completed_capsules"]
    if any(pair_id not in pair_by_id for pair_id in completed):
        raise IntegrityError("HFDL checkpoint references an unknown input pair")
    receipts: list[dict[str, Any]] = []
    for pair in pairs:
        capsule_path = pairs_root / pair.pair_id
        if pair.pair_id in completed:
            if not capsule_path.exists():
                raise IntegrityError("HFDL checkpoint references a missing partial capsule")
            receipt = _capsule_receipt(capsule_path, pair=pair, build_id=build_id)
            if completed[pair.pair_id] != receipt["capsule_id"]:
                raise IntegrityError("HFDL checkpoint capsule ID differs from derived output")
        elif capsule_path.exists():
            receipt = _capsule_receipt(capsule_path, pair=pair, build_id=build_id)
            completed[pair.pair_id] = receipt["capsule_id"]
            _write_checkpoint(checkpoint_path, checkpoint)
        else:
            receipt = _materialize_pair_capsule(pair, pairs_root=pairs_root, build_id=build_id)
            completed[pair.pair_id] = receipt["capsule_id"]
            _write_checkpoint(checkpoint_path, checkpoint)
        receipts.append(receipt)
    if set(completed) != set(pair_by_id):
        raise IntegrityError("HFDL checkpoint does not exactly cover every expected pair")
    symbols = [receipt["symbol"] for receipt in receipts]
    if len(symbols) != len(set(symbols)):
        raise IntegrityError("HFDL inputs contain duplicate canonical symbols")
    return tuple(receipts)


def _copy_derived_exact(source: Path, destination: Path) -> None:
    reject_link(source)
    if not source.is_file() or source.stat().st_nlink != 1:
        raise IntegrityError("derived HFDL capsule source is invalid")
    expected_hash = sha256_file(source)
    if destination.exists():
        reject_link(destination)
        if (
            not destination.is_file()
            or destination.stat().st_nlink != 1
            or destination.stat().st_size != source.stat().st_size
            or sha256_file(destination) != expected_hash
        ):
            raise IntegrityError("partial HFDL release stage differs from its verified capsule")
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the atomic sibling name short enough for the pinned Windows runtime;
    # the full destination hash remains in the immutable index and manifest.
    temporary = destination.with_name(f".copy-{uuid.uuid4().hex[:8]}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if (
            temporary.stat().st_nlink != 1
            or temporary.stat().st_size != source.stat().st_size
            or sha256_file(temporary) != expected_hash
            or os.path.samefile(source, temporary)
        ):
            raise IntegrityError("derived HFDL stage copy failed its byte/link verification")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_exact_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != payload:
            raise IntegrityError(f"partial HFDL stage metadata differs: {path.name}")
        return
    atomic_write(path, payload)


def _assemble_epoch_stage(
    epoch: str,
    *,
    stage: Path,
    pairs: tuple[HfdlInputPair, ...],
    receipts: tuple[dict[str, Any], ...],
    pairs_root: Path,
    input_bindings_bytes: bytes,
    build_id: str,
    contract: HfdlPublishContract,
    migration: CompletedMigrationRelease,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    if epoch not in HFDL_EPOCHS:
        raise ContractError("unknown HFDL source epoch")
    stage.mkdir(parents=True, exist_ok=True)
    for candidate in tuple(stage.rglob(".copy-*.tmp")):
        require_contained_path(candidate, stage)
        reject_link(candidate)
        candidate.unlink()
    receipt_by_pair = {pair.pair_id: receipt for pair, receipt in zip(pairs, receipts, strict=True)}
    index_rows: list[dict[str, object]] = []
    session_counts: Counter[str] = Counter()
    seen_symbol_sessions: set[tuple[str, date]] = set()
    all_source_rows = sum(receipt["source_row_count"] for receipt in receipts)
    symbols_all = {receipt["symbol"] for receipt in receipts}
    data_sources: dict[str, Path] = {}
    for pair in pairs:
        receipt = receipt_by_pair[pair.pair_id]
        output = receipt["outputs"].get(epoch)
        if output is None:
            continue
        source = pairs_root / pair.pair_id / output["path"]
        relative = f"data/{pair.pair_id}.parquet"
        data_sources[relative] = source
        table = pq.read_table(source)
        rows = table.to_pylist()
        for row in rows:
            key = (row["symbol"], row["session"])
            if key in seen_symbol_sessions:
                raise IntegrityError("HFDL epoch contains a duplicate symbol/session")
            seen_symbol_sessions.add(key)
            session_counts[row["session"].isoformat()] += 1
        index_rows.append(
            {
                "symbol": receipt["symbol"],
                "pair_id": pair.pair_id,
                "data_path": relative,
                "sha256": output["sha256"],
                "size": output["size"],
                "row_count": output["row_count"],
                "session_start": output["session_start"],
                "session_end": output["session_end"],
            }
        )
    if not index_rows:
        raise IntegrityError(f"HFDL epoch has no rows and cannot be published: {epoch}")
    index_rows.sort(key=lambda item: str(item["symbol"]))
    if [row["symbol"] for row in index_rows] != sorted({row["symbol"] for row in index_rows}):
        raise IntegrityError("HFDL epoch index has duplicate or unsorted symbols")
    row_count = len(seen_symbol_sessions)
    event_start = min(session_counts)
    event_end = max(session_counts)
    denominator_counts = {
        "approved_input_pairs": contract.expected_pair_count,
        "approved_input_symbols": len(symbols_all),
        "source_rows_all_epochs": all_source_rows,
        "source_rows_this_epoch": row_count,
        "symbols_with_rows_this_epoch": len(index_rows),
        "symbols_without_rows_this_epoch": len(symbols_all) - len(index_rows),
        "unique_sessions_this_epoch": len(session_counts),
        "symbol_session_rows_this_epoch": row_count,
    }
    census = {
        "schema_version": 1,
        "build_id": build_id,
        "source_epoch": epoch,
        "evidence_class": EVIDENCE_CLASS,
        "point_in_time_state": HFDL_POINT_IN_TIME_STATE,
        "historical_availability_state": HFDL_HISTORICAL_AVAILABILITY,
        "source_adjustment": SOURCE_ADJUSTMENT,
        "event_start": event_start,
        "event_end": event_end,
        "denominator_counts": denominator_counts,
        "session_symbol_counts": [
            {"session": session, "symbol_count": count}
            for session, count in sorted(session_counts.items())
        ],
    }
    provenance = {
        "schema_version": 1,
        "build_id": build_id,
        "source_epoch": epoch,
        "quality_state": QUALITY_STATE,
        "evidence_class": EVIDENCE_CLASS,
        "point_in_time_safe": False,
        "point_in_time_state": HFDL_POINT_IN_TIME_STATE,
        "historical_availability_state": HFDL_HISTORICAL_AVAILABILITY,
        "source_adjustment": SOURCE_ADJUSTMENT,
        "active_pipeline_eligible": False,
        "prospective_confirmation_eligible": False,
        "candidate_eligible": False,
        "pooling_permitted": False,
        "synthetic_permit_id": contract.synthetic_permit_id,
        "migration_plan_id": migration.plan_id,
        "migration_inventory_sha256": migration.inventory_sha256,
        "migration_completion_receipt_sha256": migration.completion_receipt_sha256,
        "migration_family_receipt_sha256": migration.family_receipt_sha256,
        "migration_family_manifest_sha256": migration.family_manifest_sha256,
        "input_bindings_hash": sha256_bytes(input_bindings_bytes),
    }
    index_bytes = b"".join(canonical_json_bytes(row) for row in index_rows)
    metadata = {
        "input_bindings.jsonl": input_bindings_bytes,
        "symbol_index.jsonl": index_bytes,
        "census.json": canonical_json_bytes(census),
        "provenance.json": canonical_json_bytes(provenance),
    }
    for relative, source in sorted(data_sources.items()):
        target = stage.joinpath(*safe_relative_path(relative).parts)
        _copy_derived_exact(source, target)
    for relative, payload in metadata.items():
        _write_exact_or_verify(stage / relative, payload)
    expected_files = set(data_sources) | set(metadata)
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(stage, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError("HFDL epoch stage is partial, extra, or linked") from exc
    return census, tuple(sorted(expected_files))


def _manifest_for_epoch(
    epoch: str,
    *,
    stage: Path,
    files: Iterable[str],
    census: Mapping[str, Any],
    created_at: str,
    implementation_hash: str,
    contract_hash: str,
    environment_hash: str,
) -> ReleaseManifest:
    return build_manifest(
        stage,
        files,
        project="US_stocks_swing_model_v2",
        dataset=EPOCH_DATASETS[epoch],
        source_epoch=epoch,
        role="legacy_discovery_only",
        quality_state=QUALITY_STATE,
        created_at=created_at,
        row_count=census["denominator_counts"]["symbol_session_rows_this_epoch"],
        event_start=census["event_start"],
        event_end=census["event_end"],
        schema_fingerprint=sha256_bytes(HFDL_TAGGED_SCHEMA.serialize().to_pybytes()),
        code_hash=implementation_hash,
        config_hash=contract_hash,
        environment_hash=environment_hash,
    )


def _assemble_set_stage(
    stage: Path,
    *,
    build_id: str,
    contract: HfdlPublishContract,
    migration: CompletedMigrationRelease,
    input_bindings_bytes: bytes,
    epoch_manifests: Mapping[str, ReleaseManifest],
    epoch_censuses: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    stage.mkdir(parents=True, exist_ok=True)
    epochs = {
        epoch: {
            "dataset": epoch_manifests[epoch].dataset,
            "source_epoch": epoch,
            "release_id": epoch_manifests[epoch].release_id,
            "row_count": epoch_manifests[epoch].row_count,
            "event_start": epoch_manifests[epoch].event_start,
            "event_end": epoch_manifests[epoch].event_end,
            "symbol_count": epoch_censuses[epoch]["denominator_counts"]["symbols_with_rows_this_epoch"],
        }
        for epoch in HFDL_EPOCHS
    }
    payload = {
        "schema_version": 1,
        "build_id": build_id,
        "publication_state": "COMPLETE_TWO_PHYSICAL_EPOCH_RELEASES",
        "contract": contract.as_dict(),
        "migration_plan_id": migration.plan_id,
        "migration_inventory_sha256": migration.inventory_sha256,
        "migration_completion_receipt_sha256": migration.completion_receipt_sha256,
        "migration_family_receipt_sha256": migration.family_receipt_sha256,
        "migration_family_manifest_sha256": migration.family_manifest_sha256,
        "input_bindings_hash": sha256_bytes(input_bindings_bytes),
        "epoch_release_ids": sorted(manifest.release_id for manifest in epoch_manifests.values()),
        "epochs": epochs,
        "quality_state": QUALITY_STATE,
        "evidence_class": EVIDENCE_CLASS,
        "point_in_time_safe": False,
        "point_in_time_state": HFDL_POINT_IN_TIME_STATE,
        "historical_availability_state": HFDL_HISTORICAL_AVAILABILITY,
        "source_adjustment": SOURCE_ADJUSTMENT,
        "active_pipeline_eligible": False,
        "candidate_eligible": False,
        "pooling_permitted": False,
        "consumer_must_select_one_explicit_epoch": True,
    }
    files = {
        "epoch_set.json": canonical_json_bytes(payload),
        "input_bindings.jsonl": input_bindings_bytes,
    }
    for relative, content in files.items():
        _write_exact_or_verify(stage / relative, content)
    try:
        assert_exact_tree(stage, set(files), set())
    except ContractError as exc:
        raise IntegrityError("HFDL epoch-set stage is partial, extra, or linked") from exc
    return payload, tuple(sorted(files))


def publish_hfdl_legacy_discovery(
    *,
    migration_release_directory: Path,
    accepted_release_root: Path,
    derived_work_root: Path,
    created_at: str,
    contract: HfdlPublishContract | None = None,
    publication_synthetic_permit: SyntheticOnlyPermit | None = None,
    publication_allowed_root: Path | None = None,
) -> HfdlPublicationResult:
    """Publish synthetic fixture epochs without reading or writing the legacy repo."""

    if contract is None or contract.synthetic_permit_id is None:
        raise PermissionError(
            "HFDL publication is synthetic-only; production callers may only plan"
        )
    selected_contract = contract
    selected_contract.validate()
    publication_permit = require_synthetic_permit(
        publication_synthetic_permit,
        scope=SYNTHETIC_PUBLICATION_SCOPE,
    )
    if publication_permit.fixture_id != selected_contract.contract_id:
        raise ContractError(
            "HFDL publication permit is not bound to the exact synthetic contract"
        )
    if publication_allowed_root is None:
        raise ContractError("HFDL publication requires an explicit allowed root")
    allowed_root = Path(publication_allowed_root)
    require_contained_path(allowed_root, allowed_root)
    parse_utc_z(created_at, "hfdl_publish.created_at")
    accepted_root = Path(accepted_release_root)
    work_root = Path(derived_work_root)
    migration_root = Path(migration_release_directory)
    for root, label, must_exist in (
        (migration_root, "migration release", True),
        (accepted_root, "accepted release", accepted_root.exists()),
        (work_root, "derived work", work_root.exists()),
    ):
        if not root.is_absolute():
            raise ContractError(f"{label} root must be absolute")
        require_contained_path(root, allowed_root, must_exist=must_exist)
    for root, label in ((accepted_root, "accepted release"), (work_root, "derived work")):
        root.mkdir(parents=True, exist_ok=True)
        reject_link(root)
    migration = verify_completed_migration_release(migration_root)
    for left, right in (
        (migration.root, accepted_root),
        (migration.root, work_root),
        (accepted_root, work_root),
    ):
        left_resolved = left.resolve(strict=True)
        right_resolved = right.resolve(strict=True)
        if (
            left_resolved == right_resolved
            or left_resolved in right_resolved.parents
            or right_resolved in left_resolved.parents
        ):
            raise ContractError("migration, accepted-release, and derived-work roots must be separate trees")
    pairs = _hfdl_pairs(migration, selected_contract)
    input_bindings_bytes = b"".join(canonical_json_bytes(pair.as_dict()) for pair in pairs)
    input_bindings_hash = sha256_bytes(input_bindings_bytes)
    implementation_hash = _implementation_hash()
    environment_hash = _environment_hash()
    build_unsigned = {
        "schema_version": 1,
        "migration_plan_id": migration.plan_id,
        "migration_inventory_sha256": migration.inventory_sha256,
        "migration_completion_receipt_sha256": migration.completion_receipt_sha256,
        "migration_family_receipt_sha256": migration.family_receipt_sha256,
        "migration_family_manifest_sha256": migration.family_manifest_sha256,
        "input_bindings_hash": input_bindings_hash,
        "contract_id": selected_contract.contract_id,
        "created_at": created_at,
        "implementation_hash": implementation_hash,
        "environment_hash": environment_hash,
    }
    build_id = sha256_bytes(canonical_json_bytes(build_unsigned))
    # A full 64-hex build directory plus a full 64-hex pair filename exceeds
    # classic Windows path limits. The checkpoint inside binds the full build
    # ID, so a 128-bit directory-prefix collision fails closed on evidence.
    build_root = work_root / "hfdl" / build_id[:32]
    build_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = build_root / "checkpoint.json"
    evidence = _checkpoint_unsigned(
        build_id=build_id,
        contract=selected_contract,
        migration=migration,
        input_bindings_hash=input_bindings_hash,
        created_at=created_at,
        implementation_hash=implementation_hash,
        environment_hash=environment_hash,
    )
    lock_path = work_root / ".locks" / f"hfdl-{build_id}.lock"
    with ExclusiveFileLock(lock_path, allowed_root=work_root):
        checkpoint = _load_or_create_checkpoint(checkpoint_path, evidence=evidence)
        if checkpoint["state"] == "RELEASES_COMPLETE":
            release_ids = checkpoint["release_ids"]
            if set(release_ids) != {*HFDL_EPOCHS, "epoch_set"}:
                raise IntegrityError("completed HFDL checkpoint release IDs differ")
            set_directory = accepted_root / SET_DATASET / release_ids["epoch_set"]
            return _verify_hfdl_legacy_publication(
                set_directory,
                accepted_release_root=accepted_root,
                expected_synthetic_permit_id=selected_contract.synthetic_permit_id,
            )

        pairs_root = build_root / "pairs"
        receipts = _resume_capsules(
            pairs,
            pairs_root=pairs_root,
            build_id=build_id,
            checkpoint_path=checkpoint_path,
            checkpoint=checkpoint,
        )
        epoch_censuses: dict[str, Mapping[str, Any]] = {}
        epoch_manifests: dict[str, ReleaseManifest] = {}
        epoch_stages: dict[str, Path] = {}
        for epoch in HFDL_EPOCHS:
            stage = build_root / "release_stages" / EPOCH_DATASETS[epoch]
            census, files = _assemble_epoch_stage(
                epoch,
                stage=stage,
                pairs=pairs,
                receipts=receipts,
                pairs_root=pairs_root,
                input_bindings_bytes=input_bindings_bytes,
                build_id=build_id,
                contract=selected_contract,
                migration=migration,
            )
            manifest = _manifest_for_epoch(
                epoch,
                stage=stage,
                files=files,
                census=census,
                created_at=created_at,
                implementation_hash=implementation_hash,
                contract_hash=selected_contract.contract_id,
                environment_hash=environment_hash,
            )
            epoch_censuses[epoch] = census
            epoch_manifests[epoch] = manifest
            epoch_stages[epoch] = stage

        publisher = AtomicReleasePublisher(accepted_root)
        epoch_directories = {
            epoch: publisher.publish(epoch_stages[epoch], epoch_manifests[epoch])
            for epoch in HFDL_EPOCHS
        }
        set_stage = build_root / "release_stages" / SET_DATASET
        set_payload, set_files = _assemble_set_stage(
            set_stage,
            build_id=build_id,
            contract=selected_contract,
            migration=migration,
            input_bindings_bytes=input_bindings_bytes,
            epoch_manifests=epoch_manifests,
            epoch_censuses=epoch_censuses,
        )
        set_manifest = build_manifest(
            set_stage,
            set_files,
            project="US_stocks_swing_model_v2",
            dataset=SET_DATASET,
            source_epoch=SET_SOURCE_EPOCH,
            role="legacy_discovery_only",
            quality_state=QUALITY_STATE,
            created_at=created_at,
            row_count=2,
            event_start=min(manifest.event_start for manifest in epoch_manifests.values()),
            event_end=max(manifest.event_end for manifest in epoch_manifests.values()),
            upstream_release_ids=sorted(manifest.release_id for manifest in epoch_manifests.values()),
            schema_fingerprint=sha256_bytes(canonical_json_bytes(set_payload)),
            code_hash=implementation_hash,
            config_hash=selected_contract.contract_id,
            environment_hash=environment_hash,
        )
        set_directory = publisher.publish(set_stage, set_manifest)
        checkpoint["state"] = "RELEASES_COMPLETE"
        checkpoint["release_ids"] = {
            **{epoch: epoch_manifests[epoch].release_id for epoch in HFDL_EPOCHS},
            "epoch_set": set_manifest.release_id,
        }
        _write_checkpoint(checkpoint_path, checkpoint)
        result = _verify_hfdl_legacy_publication(
            set_directory,
            accepted_release_root=accepted_root,
            expected_synthetic_permit_id=selected_contract.synthetic_permit_id,
        )
        if result.build_id != build_id or dict(result.epoch_release_directories) != epoch_directories:
            raise IntegrityError("HFDL publication verification returned different release bindings")
        return result


def _load_canonical_payload(path: Path, expected_fields: set[str], label: str) -> dict[str, Any]:
    return _read_canonical_json(path, expected_fields, label)


def _verify_input_bindings(
    path: Path,
    *,
    expected_hash: str,
    expected_pair_count: int,
) -> dict[str, dict[str, Any]]:
    reject_link(path)
    if (
        not path.is_file()
        or path.stat().st_nlink != 1
        or sha256_file(path) != expected_hash
    ):
        raise IntegrityError("HFDL input bindings changed or are linked")
    bindings: dict[str, dict[str, Any]] = {}
    seen_paths: set[str] = set()
    for line in path.read_bytes().splitlines(keepends=True):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError("HFDL input bindings contain invalid JSON") from exc
        if line != canonical_json_bytes(row) or not isinstance(row, dict) or set(row) != {
            "pair_id",
            "parquet_relative",
            "parquet_sha256",
            "parquet_size",
            "sidecar_relative",
            "sidecar_sha256",
            "sidecar_size",
        }:
            raise IntegrityError("HFDL input binding fields/encoding differ")
        pair_id = row["pair_id"]
        require_sha256(pair_id, "hfdl_binding.pair_id")
        require_sha256(row["parquet_sha256"], "hfdl_binding.parquet_sha256")
        require_sha256(row["sidecar_sha256"], "hfdl_binding.sidecar_sha256")
        if (
            type(row["parquet_size"]) is not int
            or row["parquet_size"] < 1
            or type(row["sidecar_size"]) is not int
            or row["sidecar_size"] < 1
        ):
            raise IntegrityError("HFDL input binding sizes are invalid")
        parquet_relative = safe_relative_path(row["parquet_relative"]).as_posix()
        sidecar_relative = safe_relative_path(row["sidecar_relative"]).as_posix()
        prefix = HFDL_PAYLOAD_PREFIX.as_posix() + "/"
        if (
            parquet_relative != row["parquet_relative"]
            or sidecar_relative != row["sidecar_relative"]
            or not parquet_relative.startswith(prefix)
            or "/" in parquet_relative[len(prefix) :]
            or not parquet_relative.endswith(".parquet")
            or sidecar_relative != parquet_relative + ".provenance.json"
        ):
            raise IntegrityError("HFDL input binding pair paths differ from the approved layout")
        unsigned = {key: value for key, value in row.items() if key != "pair_id"}
        if pair_id != sha256_bytes(canonical_json_bytes(unsigned)):
            raise IntegrityError("HFDL input pair ID differs from its exact hash binding")
        if pair_id in bindings or {parquet_relative, sidecar_relative} & seen_paths:
            raise IntegrityError("HFDL input bindings contain duplicate pairs or paths")
        bindings[pair_id] = row
        seen_paths.update((parquet_relative, sidecar_relative))
    if len(bindings) != expected_pair_count:
        raise IntegrityError("HFDL input binding denominator differs from the publication contract")
    return bindings


def _verify_epoch_release_payload(
    directory: Path,
    *,
    accepted_root: Path,
    expected_epoch: str,
    expected_release_id: str,
    expected_input_bindings_hash: str,
    expected_build_id: str,
    input_pair_ids: set[str],
) -> tuple[ReleaseManifest, dict[str, Any]]:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        manifest.release_id != expected_release_id
        or manifest.dataset != EPOCH_DATASETS[expected_epoch]
        or manifest.source_epoch != expected_epoch
        or manifest.role != "legacy_discovery_only"
        or manifest.quality_state != QUALITY_STATE
    ):
        raise IntegrityError("HFDL epoch release manifest differs from its immutable set receipt")
    provenance = _load_canonical_payload(
        directory / "provenance.json",
        {
            "schema_version",
            "build_id",
            "source_epoch",
            "quality_state",
            "evidence_class",
            "point_in_time_safe",
            "point_in_time_state",
            "historical_availability_state",
            "source_adjustment",
            "active_pipeline_eligible",
            "prospective_confirmation_eligible",
            "candidate_eligible",
            "pooling_permitted",
            "synthetic_permit_id",
            "migration_plan_id",
            "migration_inventory_sha256",
            "migration_completion_receipt_sha256",
            "migration_family_receipt_sha256",
            "migration_family_manifest_sha256",
            "input_bindings_hash",
        },
        "HFDL epoch provenance",
    )
    census = _load_canonical_payload(
        directory / "census.json",
        {
            "schema_version",
            "build_id",
            "source_epoch",
            "evidence_class",
            "point_in_time_state",
            "historical_availability_state",
            "source_adjustment",
            "event_start",
            "event_end",
            "denominator_counts",
            "session_symbol_counts",
        },
        "HFDL epoch census",
    )
    if (
        provenance["build_id"] != expected_build_id
        or census["build_id"] != expected_build_id
        or provenance["source_epoch"] != expected_epoch
        or census["source_epoch"] != expected_epoch
        or provenance["input_bindings_hash"] != expected_input_bindings_hash
        or sha256_file(directory / "input_bindings.jsonl") != expected_input_bindings_hash
        or provenance["quality_state"] != QUALITY_STATE
        or provenance["evidence_class"] != EVIDENCE_CLASS
        or provenance["point_in_time_safe"] is not False
        or provenance["point_in_time_state"] != HFDL_POINT_IN_TIME_STATE
        or provenance["historical_availability_state"] != HFDL_HISTORICAL_AVAILABILITY
        or provenance["source_adjustment"] != SOURCE_ADJUSTMENT
        or census["evidence_class"] != EVIDENCE_CLASS
        or census["point_in_time_state"] != HFDL_POINT_IN_TIME_STATE
        or census["historical_availability_state"] != HFDL_HISTORICAL_AVAILABILITY
        or census["source_adjustment"] != SOURCE_ADJUSTMENT
        or any(
            provenance[name] is not False
            for name in (
                "active_pipeline_eligible",
                "prospective_confirmation_eligible",
                "candidate_eligible",
                "pooling_permitted",
            )
        )
    ):
        raise IntegrityError("HFDL epoch release loses its discovery/PIT/source caveats")
    index_path = directory / "symbol_index.jsonl"
    index_rows: list[dict[str, Any]] = []
    for line in index_path.read_bytes().splitlines(keepends=True):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError("HFDL symbol index is invalid JSON") from exc
        if line != canonical_json_bytes(row):
            raise IntegrityError("HFDL symbol index is noncanonical")
        index_rows.append(row)
    if not index_rows or [row["symbol"] for row in index_rows] != sorted({row["symbol"] for row in index_rows}):
        raise IntegrityError("HFDL symbol index is empty, duplicated, or unsorted")
    row_count = 0
    seen: set[tuple[str, date]] = set()
    seen_pair_ids: set[str] = set()
    session_counts: Counter[str] = Counter()
    pair_symbols: dict[str, str] = {}
    for index in index_rows:
        if set(index) != {
            "symbol",
            "pair_id",
            "data_path",
            "sha256",
            "size",
            "row_count",
            "session_start",
            "session_end",
        }:
            raise IntegrityError("HFDL symbol index fields differ")
        require_sha256(index["pair_id"], "hfdl_index.pair_id")
        require_sha256(index["sha256"], "hfdl_index.sha256")
        if (
            index["pair_id"] not in input_pair_ids
            or index["pair_id"] in seen_pair_ids
            or not isinstance(index["symbol"], str)
            or not index["symbol"]
        ):
            raise IntegrityError("HFDL symbol index has an unknown/duplicate pair or symbol")
        seen_pair_ids.add(index["pair_id"])
        pair_symbols[index["pair_id"]] = index["symbol"]
        if (
            type(index["size"]) is not int
            or index["size"] < 1
            or type(index["row_count"]) is not int
            or index["row_count"] < 1
            or not isinstance(index["session_start"], str)
            or not isinstance(index["session_end"], str)
        ):
            raise IntegrityError("HFDL symbol index size/count/session fields are invalid")
        data_path = safe_relative_path(index["data_path"])
        if data_path.parts != ("data", f"{index['pair_id']}.parquet"):
            raise IntegrityError("HFDL symbol index data path differs from its pair binding")
        candidate = directory.joinpath(*data_path.parts)
        if (
            sha256_file(candidate) != index["sha256"]
            or candidate.stat().st_nlink != 1
            or candidate.stat().st_size != index["size"]
        ):
            raise IntegrityError("HFDL indexed epoch data changed or is linked")
        table = pq.read_table(candidate)
        if table.schema.remove_metadata() != HFDL_TAGGED_SCHEMA or table.num_rows != index["row_count"]:
            raise IntegrityError("HFDL indexed epoch data schema/count differs")
        for row in table.to_pylist():
            key = (row["symbol"], row["session"])
            if (
                key in seen
                or row["source_epoch"] != expected_epoch
                or row["symbol"] != index["symbol"]
                or row["source_adjustment"] != SOURCE_ADJUSTMENT
                or row["evidence_class"] != EVIDENCE_CLASS
                or row["point_in_time_safe"] is not False
                or row["point_in_time_state"] != HFDL_POINT_IN_TIME_STATE
                or row["historical_availability_state"] != HFDL_HISTORICAL_AVAILABILITY
            ):
                raise IntegrityError("HFDL release mixes epochs or duplicates symbol/session")
            seen.add(key)
            session_counts[row["session"].isoformat()] += 1
        sessions = table.column("session").to_pylist()
        if (
            not sessions
            or sessions[0].isoformat() != index["session_start"]
            or sessions[-1].isoformat() != index["session_end"]
        ):
            raise IntegrityError("HFDL symbol index session bounds differ")
        row_count += table.num_rows
    denominators = census["denominator_counts"]
    expected_denominator_fields = {
        "approved_input_pairs",
        "approved_input_symbols",
        "source_rows_all_epochs",
        "source_rows_this_epoch",
        "symbols_with_rows_this_epoch",
        "symbols_without_rows_this_epoch",
        "unique_sessions_this_epoch",
        "symbol_session_rows_this_epoch",
    }
    expected_sessions = [
        {"session": session, "symbol_count": count}
        for session, count in sorted(session_counts.items())
    ]
    if (
        not isinstance(denominators, dict)
        or set(denominators) != expected_denominator_fields
        or any(type(value) is not int or value < 0 for value in denominators.values())
        or denominators.get("approved_input_pairs") != len(input_pair_ids)
        or denominators.get("approved_input_symbols") != len(input_pair_ids)
        or denominators.get("symbol_session_rows_this_epoch") != row_count
        or denominators.get("source_rows_this_epoch") != row_count
        or denominators.get("symbols_with_rows_this_epoch") != len(index_rows)
        or denominators.get("symbols_without_rows_this_epoch")
        != len(input_pair_ids) - len(index_rows)
        or denominators.get("unique_sessions_this_epoch") != len(session_counts)
        or census["session_symbol_counts"] != expected_sessions
        or census["event_start"] != min(session_counts)
        or census["event_end"] != max(session_counts)
        or manifest.row_count != row_count
        or manifest.event_start != census["event_start"]
        or manifest.event_end != census["event_end"]
    ):
        raise IntegrityError("HFDL release row/symbol/session denominators differ")
    return manifest, {
        "denominators": denominators,
        "pair_symbols": pair_symbols,
    }


def _verify_hfdl_legacy_publication(
    epoch_set_release_directory: Path,
    *,
    accepted_release_root: Path,
    synthetic_permit: SyntheticOnlyPermit | None = None,
    expected_synthetic_permit_id: str | None = None,
) -> HfdlPublicationResult:
    accepted_root = Path(accepted_release_root)
    set_directory = Path(epoch_set_release_directory)
    set_manifest = verify_accepted_release(set_directory, accepted_root=accepted_root)
    if (
        set_manifest.dataset != SET_DATASET
        or set_manifest.source_epoch != SET_SOURCE_EPOCH
        or set_manifest.role != "legacy_discovery_only"
        or set_manifest.quality_state != QUALITY_STATE
        or set_manifest.row_count != 2
    ):
        raise IntegrityError("HFDL epoch-set release has the wrong immutable role/quality contract")
    payload = _load_canonical_payload(
        set_directory / "epoch_set.json",
        {
            "schema_version",
            "build_id",
            "publication_state",
            "contract",
            "migration_plan_id",
            "migration_inventory_sha256",
            "migration_completion_receipt_sha256",
            "migration_family_receipt_sha256",
            "migration_family_manifest_sha256",
            "input_bindings_hash",
            "epoch_release_ids",
            "epochs",
            "quality_state",
            "evidence_class",
            "point_in_time_safe",
            "point_in_time_state",
            "historical_availability_state",
            "source_adjustment",
            "active_pipeline_eligible",
            "candidate_eligible",
            "pooling_permitted",
            "consumer_must_select_one_explicit_epoch",
        },
        "HFDL epoch-set receipt",
    )
    contract_payload = payload["contract"]
    if not isinstance(contract_payload, dict):
        raise IntegrityError("HFDL epoch-set contract is invalid")
    synthetic_id = contract_payload.get("synthetic_permit_id")
    expected_pair_count = contract_payload.get("expected_pair_count")
    if (
        type(expected_pair_count) is not int
        or expected_pair_count < 1
        or not isinstance(payload["epochs"], dict)
        or not isinstance(payload["epoch_release_ids"], list)
    ):
        raise IntegrityError("HFDL epoch-set contract denominators/types are invalid")
    if synthetic_id is None:
        if expected_synthetic_permit_id is not None or contract_payload.get("expected_pair_count") != EXPECTED_HFDL_PAIR_COUNT:
            raise IntegrityError("production HFDL epoch set does not enforce the 1,388-pair denominator")
    else:
        require_sha256(synthetic_id, "hfdl_epoch_set.synthetic_permit_id")
        if expected_synthetic_permit_id != synthetic_id:
            if synthetic_permit is None:
                raise ContractError("synthetic HFDL epoch set requires its explicit fixture permit")
            require_synthetic_permit(synthetic_permit, scope=SYNTHETIC_CONTRACT_SCOPE)
            if synthetic_permit.permit_id != synthetic_id:
                raise ContractError("synthetic HFDL epoch set permit differs")
    expected_contract = HfdlPublishContract._construct(expected_pair_count, synthetic_id)
    if contract_payload != expected_contract.as_dict():
        raise IntegrityError("HFDL epoch-set contract loses its exact legacy-only restrictions")
    for hash_field in (
        "build_id",
        "migration_plan_id",
        "migration_inventory_sha256",
        "migration_completion_receipt_sha256",
        "migration_family_receipt_sha256",
        "migration_family_manifest_sha256",
        "input_bindings_hash",
    ):
        require_sha256(payload[hash_field], f"hfdl_epoch_set.{hash_field}")
    if (
        payload["publication_state"] != "COMPLETE_TWO_PHYSICAL_EPOCH_RELEASES"
        or set(payload["epochs"]) != set(HFDL_EPOCHS)
        or payload["quality_state"] != QUALITY_STATE
        or payload["evidence_class"] != EVIDENCE_CLASS
        or payload["point_in_time_safe"] is not False
        or payload["point_in_time_state"] != HFDL_POINT_IN_TIME_STATE
        or payload["historical_availability_state"] != HFDL_HISTORICAL_AVAILABILITY
        or payload["source_adjustment"] != SOURCE_ADJUSTMENT
        or payload["active_pipeline_eligible"] is not False
        or payload["candidate_eligible"] is not False
        or payload["pooling_permitted"] is not False
        or payload["consumer_must_select_one_explicit_epoch"] is not True
        or sha256_file(set_directory / "input_bindings.jsonl") != payload["input_bindings_hash"]
    ):
        raise IntegrityError("HFDL epoch-set receipt loses its no-pooling legacy caveats")
    input_bindings = _verify_input_bindings(
        set_directory / "input_bindings.jsonl",
        expected_hash=payload["input_bindings_hash"],
        expected_pair_count=expected_pair_count,
    )
    epoch_directories: dict[str, Path] = {}
    epoch_ids: list[str] = []
    epoch_details: dict[str, dict[str, Any]] = {}
    for epoch in HFDL_EPOCHS:
        binding = payload["epochs"][epoch]
        if not isinstance(binding, dict) or set(binding) != {
            "dataset",
            "source_epoch",
            "release_id",
            "row_count",
            "event_start",
            "event_end",
            "symbol_count",
        }:
            raise IntegrityError("HFDL epoch-set binding fields differ")
        if binding["dataset"] != EPOCH_DATASETS[epoch] or binding["source_epoch"] != epoch:
            raise IntegrityError("HFDL epoch set silently relabels a source epoch")
        require_sha256(binding["release_id"], f"hfdl_epoch_set.{epoch}.release_id")
        directory = accepted_root / binding["dataset"] / binding["release_id"]
        manifest, details = _verify_epoch_release_payload(
            directory,
            accepted_root=accepted_root,
            expected_epoch=epoch,
            expected_release_id=binding["release_id"],
            expected_input_bindings_hash=payload["input_bindings_hash"],
            expected_build_id=payload["build_id"],
            input_pair_ids=set(input_bindings),
        )
        if (
            manifest.row_count != binding["row_count"]
            or manifest.event_start != binding["event_start"]
            or manifest.event_end != binding["event_end"]
            or details["denominators"]["symbols_with_rows_this_epoch"]
            != binding["symbol_count"]
        ):
            raise IntegrityError("HFDL epoch release differs from its set denominators")
        epoch_directories[epoch] = directory
        epoch_ids.append(manifest.release_id)
        epoch_details[epoch] = details
    source_totals = {
        details["denominators"]["source_rows_all_epochs"]
        for details in epoch_details.values()
    }
    source_rows = sum(
        details["denominators"]["source_rows_this_epoch"]
        for details in epoch_details.values()
    )
    pair_symbols: dict[str, str] = {}
    for details in epoch_details.values():
        for pair_id, symbol in details["pair_symbols"].items():
            if pair_id in pair_symbols and pair_symbols[pair_id] != symbol:
                raise IntegrityError("HFDL physical epochs disagree on pair-to-symbol identity")
            pair_symbols[pair_id] = symbol
    if (
        len(source_totals) != 1
        or source_rows != next(iter(source_totals))
        or set(pair_symbols) != set(input_bindings)
        or len(set(pair_symbols.values())) != expected_pair_count
    ):
        raise IntegrityError("HFDL two-epoch set does not conserve its complete input denominator")
    if (
        sorted(epoch_ids) != payload["epoch_release_ids"]
        or tuple(sorted(epoch_ids)) != set_manifest.upstream_release_ids
    ):
        raise IntegrityError("HFDL epoch set does not bind exactly both physical releases")
    return HfdlPublicationResult(
        build_id=payload["build_id"],
        epoch_release_directories=epoch_directories,
        epoch_set_release_directory=set_directory,
    )


def verify_hfdl_legacy_publication(
    epoch_set_release_directory: Path,
    *,
    accepted_release_root: Path,
    synthetic_permit: SyntheticOnlyPermit | None = None,
) -> HfdlPublicationResult:
    """Verify the complete two-release set; synthetic fixtures need their permit."""

    return _verify_hfdl_legacy_publication(
        epoch_set_release_directory,
        accepted_release_root=accepted_release_root,
        synthetic_permit=synthetic_permit,
    )
