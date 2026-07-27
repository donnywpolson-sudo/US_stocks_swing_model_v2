from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .common import (
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
from .errors import ContractError, IntegrityError
from .clock import TrustedClock, require_trusted_clock
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .governance import AuthorizationAuthority, SignedAuthorizationReceipt
from .locking import ExclusiveFileLock


COPY_AUTH_ENV = "HASH_COPY_APPROVED"
APPROVAL_SCOPE = "COPY_TO_NON_ACTIVE_CONTENT_ADDRESSED_MIGRATION_RELEASE"
COPY_AUTHORIZATION_SCOPE = "AUTHORIZE_CONTROLLED_HASH_COPY"
SYNTHETIC_COPY_SCOPE = "SYNTHETIC_HASH_COPY_EXECUTION"
MIGRATION_MANIFEST_SCHEMA_VERSION = 2
PAYLOAD_LAYOUT_VERSION = "flat_object_160bit_v1"
MIGRATION_APPROVAL_RETIREMENT_PATH = "config/migration_approval_retirement.json"
RETIRED_MIGRATION_APPROVAL_ID = (
    "06b0acdbc332e88e1d21ccd4144740d522c03d4e054295703b1a80de651cf336"
)
RETIRED_MIGRATION_PLAN_ID = (
    "479e3943b0eeae69d08aa078eece05ff73b20def0500b13f746646bb1534ef82"
)
MIGRATION_APPROVAL_RETIRED_STATUS = (
    "RETIRED_NON_AUTHORIZING_HISTORICAL_EVIDENCE_ONLY"
)
_PAYLOAD_OBJECT_PATTERN = re.compile(r"^o/[0-9a-f]{40}$")
_COPY_TEMP_PATTERN = re.compile(r"^\.cp\.([0-9a-f]{40})\.[0-9a-f]{8}\.tmp$")
_ATOMIC_TEMP_PATTERN = re.compile(r"^\.aw\.[^.]+\.tmp$")
CONTROLLED_REBUILD_AUTHORIZATION_ID = (
    "dd131238845c26cd9dca58aa6b0986e5ec46238881cecc411af2ddbb58b5bbf7"
)
CONTROLLED_REBUILD_AUTHORIZATION_CLASS = (
    "EXPLICIT_CODEX_TASK_USER_AUTHORITY_NON_ALPHA_COPY"
)
MIGRATION_IMPLEMENTATION_PATHS = (
    "src/us_stocks_swing_model_v2/migration.py",
    "src/us_stocks_swing_model_v2/common.py",
    "src/us_stocks_swing_model_v2/locking.py",
    "src/us_stocks_swing_model_v2/clock.py",
    "src/us_stocks_swing_model_v2/capabilities.py",
    "src/us_stocks_swing_model_v2/governance.py",
    "src/us_stocks_swing_model_v2/cli/hash_copy.py",
    "config/controlled_rebuild_authorization.json",
    "config/environment.lock.json",
    "requirements.lock",
    "requirements.sha256.lock",
    "pyproject.toml",
)


@dataclass(frozen=True, init=False)
class ControlledRebuildAuthorization:
    """Exact user-task authority for this one non-alpha rebuild copy.

    This is deliberately not a ``SignedAuthorizationReceipt`` and therefore
    cannot satisfy trial, evaluation, sealing, or production gates.  Its only
    consumer is the controlled hash-copy path below, where it is combined with
    the reviewed plan, inventory, implementation closure, and approval.
    """

    path: str
    authorization_id: str
    task_thread_id: str
    active_root: str
    legacy_root: str

    @classmethod
    def load(cls, path: Path) -> "ControlledRebuildAuthorization":
        candidate = Path(path).resolve(strict=True)
        repository = Path(__file__).resolve().parents[2]
        expected_path = (repository / "config" / "controlled_rebuild_authorization.json").resolve(
            strict=True
        )
        if candidate != expected_path:
            raise PermissionError(
                "controlled rebuild authority must be the repository-pinned receipt"
            )
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise PermissionError("controlled rebuild authority must be an independent plain file")
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionError("controlled rebuild authority is unreadable") from exc
        expected_fields = {
            "authorization_version",
            "authorization_source",
            "task_thread_id",
            "authorization_text",
            "data_reuse_policy",
            "hard_pauses",
            "project",
            "legacy_root",
            "active_root",
            "allowed_actions",
            "authorization_id",
        }
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise PermissionError("controlled rebuild authority fields differ")
        core = {key: value for key, value in payload.items() if key != "authorization_id"}
        authorization_id = sha256_bytes(canonical_json_bytes(core).removesuffix(b"\n"))
        data_policy = payload["data_reuse_policy"]
        required_pauses = {
            "candidate_sealing",
            "destructive_cutover",
            "external_push",
            "legacy_repository_write",
            "paid_databento_download",
            "real_history_hypothesis_or_wfa_execution",
            "trading",
        }
        allowed_actions = set(payload["allowed_actions"]) if isinstance(payload["allowed_actions"], list) else set()
        hard_pauses = set(payload["hard_pauses"]) if isinstance(payload["hard_pauses"], list) else set()
        if (
            payload["authorization_id"] != authorization_id
            or authorization_id != CONTROLLED_REBUILD_AUTHORIZATION_ID
            or payload["authorization_version"] != "1.0.0"
            or payload["authorization_source"] != "CODEX_TASK_USER_MESSAGE"
            or payload["task_thread_id"] != "019f6329-fb5d-7b53-8db6-93ab519f4da8"
            or payload["project"] != "US_stocks_swing_model_v2"
            or not isinstance(payload["authorization_text"], str)
            or not payload["authorization_text"].startswith("START THE CONTROLLED REBUILD.")
            or data_policy
            != {
                "blanket_redownload_allowed": False,
                "copy_mode": "HASH_VERIFIED_COPY_NOT_MOVE",
                "links_allowed": False,
                "legacy_bytes_remain_unchanged": True,
            }
            or "hash_copy_approved_legacy_data" not in allowed_actions
            or not required_pauses <= hard_pauses
        ):
            raise PermissionError("controlled rebuild authority does not match the user task")
        instance = object.__new__(cls)
        for name, value in {
            "path": str(candidate),
            "authorization_id": authorization_id,
            "task_thread_id": str(payload["task_thread_id"]),
            "active_root": str(payload["active_root"]),
            "legacy_root": str(payload["legacy_root"]),
        }.items():
            object.__setattr__(instance, name, value)
        instance.validate_file()
        return instance

    def validate_file(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        expected_path = (repository / "config" / "controlled_rebuild_authorization.json").resolve(
            strict=True
        )
        if Path(self.path).resolve(strict=True) != expected_path:
            raise PermissionError("controlled rebuild authority path changed after loading")
        reject_link(expected_path)
        # Re-read without recursive construction so later file mutation fails.
        try:
            payload = json.loads(expected_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionError("controlled rebuild authority changed after loading") from exc
        core = {key: value for key, value in payload.items() if key != "authorization_id"}
        if (
            payload.get("authorization_id") != self.authorization_id
            or sha256_bytes(canonical_json_bytes(core).removesuffix(b"\n"))
            != self.authorization_id
            or self.authorization_id != CONTROLLED_REBUILD_AUTHORIZATION_ID
            or payload.get("task_thread_id") != self.task_thread_id
            or payload.get("active_root") != self.active_root
            or payload.get("legacy_root") != self.legacy_root
        ):
            raise PermissionError("controlled rebuild authority changed after loading")

    def validate_plan(self, plan: "MigrationPlan") -> None:
        self.validate_file()
        active = Path(self.active_root).resolve(strict=True)
        legacy = Path(self.legacy_root).resolve(strict=True)
        if active != Path(__file__).resolve().parents[2]:
            raise PermissionError("controlled rebuild authority active root differs")
        if Path(plan.allowed_vault_root).resolve(strict=True) != active:
            raise PermissionError("migration vault root differs from controlled rebuild authority")
        destination = Path(plan.destination_vault).resolve(strict=False)
        try:
            destination.relative_to(active)
        except ValueError as exc:
            raise PermissionError("migration destination escapes the controlled rebuild") from exc
        source_roots = tuple(Path(value).resolve(strict=True) for value in plan.allowed_source_roots)
        if source_roots != (legacy,):
            raise PermissionError("migration sources differ from controlled rebuild authority")


@dataclass(frozen=True)
class CopyAuthorizationEvidence:
    receipt_id: str
    registry_id: str
    authorization_class: str

    def as_dict(self) -> dict[str, str]:
        return {
            "authorization_receipt_id": self.receipt_id,
            "authorization_registry_id": self.registry_id,
            "authorization_class": self.authorization_class,
        }


def migration_implementation_manifest() -> dict[str, str]:
    repo_root = Path(__file__).resolve().parents[2]
    manifest: dict[str, str] = {}
    for relative in MIGRATION_IMPLEMENTATION_PATHS:
        candidate = repo_root.joinpath(*safe_relative_path(relative).parts)
        require_contained_path(candidate, repo_root)
        reject_link(candidate)
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(f"migration implementation dependency is not a plain file: {relative}")
        manifest[relative] = sha256_file(candidate)
    return dict(sorted(manifest.items()))


def migration_implementation_sha256() -> str:
    return sha256_bytes(canonical_json_bytes(migration_implementation_manifest()))


@dataclass(frozen=True)
class CopyPlanEntry:
    schema_version: int
    migration_id: str
    role: str
    source: str
    destination: str
    size: int
    sha256: str
    payload_object: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def migration_payload_object_relative(
    destination_relative: str | Path,
    digest: str,
) -> str:
    """Return a short, collision-bound object path for one reviewed entry.

    The reviewed destination remains in the manifest/checkpoint.  Payload
    bytes use a flat content-object namespace so valid Windows repositories do
    not fail merely because legacy evidence contained deeply nested names.
    """

    relative = safe_relative_path(str(destination_relative)).as_posix()
    require_sha256(digest, "migration.payload_sha256")
    binding = sha256_bytes(
        canonical_json_bytes(
            {"destination_relative": relative, "sha256": digest}
        )
    )
    # The full binding remains in the manifest/checkpoint and every object is
    # re-hashed before acceptance. This 160-bit namespace keeps final Windows
    # paths well below MAX_PATH; an in-plan prefix collision is rejected.
    return validate_migration_payload_object(f"o/{binding[:40]}")


def validate_migration_payload_object(value: str) -> str:
    """Validate a sealed schema-v2 payload object without deriving it again."""

    if not isinstance(value, str):
        raise ContractError("migration payload object must be a string")
    relative = safe_relative_path(value).as_posix()
    if relative != value or _PAYLOAD_OBJECT_PATTERN.fullmatch(relative) is None:
        raise ContractError("migration payload object does not match the sealed flat layout")
    return relative


@dataclass(frozen=True)
class MigrationPlan:
    entries: tuple[CopyPlanEntry, ...]
    config_sha256: str
    inventory_sha256: str
    plan_id: str
    destination_vault: str
    allowed_vault_root: str
    allowed_source_roots: tuple[str, ...]
    migration_manifest_schema_version: int
    payload_layout_version: str

    def __iter__(self) -> Iterator[CopyPlanEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, index: int) -> CopyPlanEntry:
        return self.entries[index]

    def concise_summary(self) -> dict[str, Any]:
        roles = Counter(entry.role for entry in self.entries)
        families = Counter(entry.migration_id for entry in self.entries)
        return {
            "migration_manifest_schema_version": self.migration_manifest_schema_version,
            "payload_layout_version": self.payload_layout_version,
            "config_sha256": self.config_sha256,
            "inventory_sha256": self.inventory_sha256,
            "plan_id": self.plan_id,
            "destination_vault": self.destination_vault,
            "file_count": len(self.entries),
            "total_bytes": sum(entry.size for entry in self.entries),
            "role_file_counts": dict(sorted(roles.items())),
            "family_file_counts": dict(sorted(families.items())),
        }


@dataclass(frozen=True)
class MigrationApproval:
    schema_version: int
    approval_scope: str
    migration_manifest_schema_version: int
    payload_layout_version: str
    config_sha256: str
    inventory_sha256: str
    plan_id: str
    migration_implementation_manifest: Mapping[str, str]
    migration_implementation_sha256: str
    file_count: int
    total_bytes: int
    approved_at: str
    approval_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("approval_id")
        return value

    def validate_sealed(self) -> None:
        """Authenticate the historical approval without rescanning legacy inputs."""

        parse_utc_z(self.approved_at, "approved_at")
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.approval_scope != APPROVAL_SCOPE
            or type(self.migration_manifest_schema_version) is not int
            or self.migration_manifest_schema_version != MIGRATION_MANIFEST_SCHEMA_VERSION
            or self.payload_layout_version != PAYLOAD_LAYOUT_VERSION
            or type(self.file_count) is not int
            or self.file_count <= 0
            or type(self.total_bytes) is not int
            or self.total_bytes < 0
        ):
            raise PermissionError("sealed migration approval scope/schema/counts are invalid")
        for field in ("config_sha256", "inventory_sha256", "plan_id"):
            require_sha256(getattr(self, field), f"migration_approval.{field}")
        manifest = dict(self.migration_implementation_manifest)
        if set(manifest) != set(MIGRATION_IMPLEMENTATION_PATHS):
            raise PermissionError("sealed migration implementation manifest fields differ")
        for relative, digest in manifest.items():
            if safe_relative_path(relative).as_posix() != relative:
                raise PermissionError("sealed migration implementation path is not canonical")
            require_sha256(digest, f"migration_approval.implementation.{relative}")
        implementation_sha256 = sha256_bytes(canonical_json_bytes(dict(sorted(manifest.items()))))
        if self.migration_implementation_sha256 != implementation_sha256:
            raise PermissionError("sealed migration_implementation_sha256 is invalid")
        if self.approval_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise PermissionError("migration approval_id does not match its content")

    def validate(self, plan: MigrationPlan) -> None:
        self.validate_sealed()
        retirement = load_migration_approval_retirement()
        if self.approval_id == retirement.approval_id:
            if self.plan_id != retirement.plan_id:
                raise PermissionError(
                    "retired migration approval differs from its bound historical plan"
                )
            raise PermissionError(
                "migration approval is retired and remains historical evidence only"
            )
        expected = {
            "migration_manifest_schema_version": plan.migration_manifest_schema_version,
            "payload_layout_version": plan.payload_layout_version,
            "config_sha256": plan.config_sha256,
            "inventory_sha256": plan.inventory_sha256,
            "plan_id": plan.plan_id,
            "migration_implementation_manifest": migration_implementation_manifest(),
            "migration_implementation_sha256": migration_implementation_sha256(),
            "file_count": len(plan),
            "total_bytes": sum(entry.size for entry in plan),
        }
        for field, value in expected.items():
            if getattr(self, field) != value:
                raise PermissionError(f"migration approval does not bind current {field}")
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MigrationApproval":
        if set(payload) != set(cls.__dataclass_fields__):
            raise PermissionError("migration approval fields differ from the exact contract")
        fields = dict(payload)
        fields["migration_implementation_manifest"] = {
            str(key): str(value)
            for key, value in dict(fields["migration_implementation_manifest"]).items()
        }
        return cls(**fields)


@dataclass(frozen=True)
class MigrationApprovalRetirement:
    schema_version: int
    status: str
    approval_id: str
    plan_id: str
    retired_at: str
    reason: str
    replacement_approval_id: None
    retirement_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("retirement_id")
        return value

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.status != MIGRATION_APPROVAL_RETIRED_STATUS
            or self.approval_id != RETIRED_MIGRATION_APPROVAL_ID
            or self.plan_id != RETIRED_MIGRATION_PLAN_ID
            or not isinstance(self.reason, str)
            or not self.reason.strip()
            or self.replacement_approval_id is not None
        ):
            raise PermissionError("migration approval retirement contract is invalid")
        parse_utc_z(self.retired_at, "retired_at")
        require_sha256(self.approval_id, "retirement.approval_id")
        require_sha256(self.plan_id, "retirement.plan_id")
        if self.retirement_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise PermissionError("migration approval retirement_id does not match its content")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MigrationApprovalRetirement":
        if set(payload) != set(cls.__dataclass_fields__):
            raise PermissionError(
                "migration approval retirement fields differ from the exact contract"
            )
        return cls(**payload)


def load_migration_approval_retirement() -> MigrationApprovalRetirement:
    """Load the repository-pinned record that makes the old approval non-authorizing."""

    repository = Path(__file__).resolve().parents[2]
    retirement_path = repository.joinpath(
        *safe_relative_path(MIGRATION_APPROVAL_RETIREMENT_PATH).parts
    )
    require_contained_path(retirement_path, repository)
    reject_link(retirement_path)
    if not retirement_path.is_file() or retirement_path.stat().st_nlink != 1:
        raise PermissionError(
            "migration approval retirement must be a repository-pinned independent plain file"
        )
    try:
        payload = json.loads(retirement_path.read_text(encoding="utf-8"))
        retirement = MigrationApprovalRetirement.from_dict(payload)
    except (OSError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        raise PermissionError("migration approval retirement is invalid") from exc
    retirement.validate()
    return retirement


def approval_payload_for_review(plan: MigrationPlan, approved_at: str) -> dict[str, Any]:
    """Return exact fields to review; this function does not authorize execution."""
    unsigned = {
        "schema_version": 1,
        "approval_scope": APPROVAL_SCOPE,
        "migration_manifest_schema_version": plan.migration_manifest_schema_version,
        "payload_layout_version": plan.payload_layout_version,
        "config_sha256": plan.config_sha256,
        "inventory_sha256": plan.inventory_sha256,
        "plan_id": plan.plan_id,
        "migration_implementation_manifest": migration_implementation_manifest(),
        "migration_implementation_sha256": migration_implementation_sha256(),
        "file_count": len(plan),
        "total_bytes": sum(entry.size for entry in plan),
        "approved_at": approved_at,
    }
    parse_utc_z(approved_at, "approved_at")
    return {**unsigned, "approval_id": sha256_bytes(canonical_json_bytes(unsigned))}


def load_sealed_migration_approval(path: Path) -> MigrationApproval:
    """Load an authenticated approval for an already completed migration."""

    approval_path = Path(path)
    reject_link(approval_path)
    if not approval_path.is_file() or approval_path.stat().st_nlink != 1:
        raise PermissionError("migration approval must be an independent plain file")
    try:
        payload = json.loads(approval_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        raise PermissionError("migration approval is invalid") from exc
    try:
        approval = MigrationApproval.from_dict(payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise PermissionError("migration approval schema is invalid") from exc
    approval.validate_sealed()
    return approval


def load_migration_approval(path: Path, plan: MigrationPlan) -> MigrationApproval:
    approval = load_sealed_migration_approval(path)
    approval.validate(plan)
    return approval


def migration_authorization_bindings(
    plan: MigrationPlan,
    approval: MigrationApproval,
) -> dict[str, str]:
    approval.validate(plan)
    return {
        "approval_id": approval.approval_id,
        "config_sha256": plan.config_sha256,
        "inventory_sha256": plan.inventory_sha256,
        "migration_manifest_schema_version": str(plan.migration_manifest_schema_version),
        "payload_layout_version": plan.payload_layout_version,
        "migration_implementation_sha256": approval.migration_implementation_sha256,
        "file_count": str(len(plan)),
        "total_bytes": str(sum(entry.size for entry in plan)),
    }


def load_migration_config(path: Path) -> dict[str, Any]:
    config_path = Path(path)
    reject_link(config_path)
    if not config_path.is_file() or config_path.stat().st_nlink != 1:
        raise ContractError("migration config must be an independent plain file")
    raw = config_path.read_bytes()
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError("migration config is invalid JSON") from exc
    if config.get("schema_version") != 1 or config.get("mode") != "deny_unlisted":
        raise ContractError("migration config must be schema v1 and deny_unlisted")
    vault = Path(config.get("destination_vault", ""))
    vault_root = Path(config.get("allowed_vault_root", ""))
    source_roots = tuple(Path(value) for value in config.get("allowed_source_roots", []))
    if not vault.is_absolute() or not vault_root.is_absolute() or not source_roots or not all(root.is_absolute() for root in source_roots):
        raise ContractError("migration vault/source roots must be explicit absolute paths")
    require_contained_path(vault, vault_root, must_exist=False)
    for root in source_roots:
        require_contained_path(root, root, must_exist=True)
    ids = [entry.get("id") for entry in config.get("entries", [])]
    if not ids or ids != list(dict.fromkeys(ids)):
        raise ContractError("migration IDs must be nonempty and unique")
    config["_config_sha256"] = sha256_bytes(raw)
    config["_config_path"] = str(config_path.resolve(strict=True))
    return config


def _approved_root(path: Path, roots: tuple[Path, ...]) -> Path:
    candidates: list[Path] = []
    for root in roots:
        try:
            path.relative_to(root)
            candidates.append(root)
        except ValueError:
            continue
    if len(candidates) != 1:
        raise ContractError(f"source path is not inside exactly one approved root: {path}")
    return candidates[0]


def plan_migration(config: dict[str, Any]) -> MigrationPlan:
    if "_config_sha256" not in config:
        raise ContractError("migration config must be loaded from an exact hashed file")
    vault = Path(config["destination_vault"])
    vault_root = Path(config["allowed_vault_root"])
    allowed_sources = tuple(Path(value) for value in config["allowed_source_roots"])
    require_contained_path(vault, vault_root, must_exist=False)
    plans: list[CopyPlanEntry] = []
    destinations: set[str] = set()
    payload_objects: set[str] = set()
    for entry in config["entries"]:
        if entry.get("status") != "approved_for_dry_run_only":
            raise ContractError(f"migration entry lacks reviewed status: {entry.get('id')}")
        if entry.get("role") not in {
            "legacy_discovery_only",
            "qualification_evidence_only",
            "legacy_trial_census_evidence_only",
        }:
            raise ContractError(f"active/model artifacts cannot migrate: {entry.get('id')}")
        source_root = Path(entry["source_root"])
        approved_root = _approved_root(source_root, allowed_sources)
        require_contained_path(source_root, approved_root, must_exist=True)
        reject_link(source_root)
        if not source_root.is_dir():
            raise ContractError(f"migration source root is not a directory: {source_root}")
        destination_subdir = safe_relative_path(entry["destination_subdir"])
        include_specs = entry.get("include_specs", [])
        if not include_specs:
            raise ContractError("migration entry must have per-glob count/byte specifications")
        matched: set[Path] = set()
        excludes = entry.get("exclude_globs", [])
        for spec in include_specs:
            pattern = spec.get("glob", "")
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ContractError(f"unsafe migration glob: {pattern}")
            pattern_matches: set[Path] = set()
            for path in source_root.glob(pattern):
                if not path.is_file():
                    continue
                require_contained_path(path, source_root, must_exist=True)
                reject_link(path)
                if path.stat().st_nlink != 1:
                    raise ContractError(f"hardlinked source is prohibited: {path}")
                relative = path.relative_to(source_root).as_posix()
                if not any(fnmatch.fnmatch(relative, excluded) for excluded in excludes):
                    pattern_matches.add(path)
            actual_files = len(pattern_matches)
            actual_bytes = sum(path.stat().st_size for path in pattern_matches)
            if actual_files != spec.get("files") or actual_bytes != spec.get("bytes"):
                raise IntegrityError(
                    f"migration glob drift for {entry['id']}:{pattern}: "
                    f"expected {spec.get('files')}/{spec.get('bytes')}, found {actual_files}/{actual_bytes}"
                )
            if matched & pattern_matches:
                raise IntegrityError(f"migration globs overlap for {entry['id']}: {pattern}")
            matched.update(pattern_matches)
        expected_total = entry.get("expected_total", {})
        if len(matched) != expected_total.get("files") or sum(path.stat().st_size for path in matched) != expected_total.get("bytes"):
            raise IntegrityError(f"migration family total drift: {entry['id']}")
        for source in sorted(matched):
            relative = source.relative_to(source_root).as_posix()
            destination = vault.joinpath(*destination_subdir.parts, *safe_relative_path(relative).parts)
            require_contained_path(destination, vault_root, must_exist=False)
            destination_key = str(destination).casefold()
            if destination_key in destinations:
                raise IntegrityError(f"migration destination collision: {destination}")
            destinations.add(destination_key)
            digest = sha256_file(source)
            destination_relative = destination.relative_to(vault).as_posix()
            payload_object = migration_payload_object_relative(
                destination_relative, digest
            )
            if payload_object in payload_objects:
                raise IntegrityError(
                    f"migration payload object namespace collision: {payload_object}"
                )
            payload_objects.add(payload_object)
            plans.append(
                CopyPlanEntry(
                    schema_version=MIGRATION_MANIFEST_SCHEMA_VERSION,
                    migration_id=entry["id"],
                    role=entry["role"],
                    source=str(source),
                    destination=str(destination),
                    size=source.stat().st_size,
                    sha256=digest,
                    payload_object=payload_object,
                )
            )
    expected_grand = config.get("expected_grand_total", {})
    if len(plans) != expected_grand.get("files") or sum(item.size for item in plans) != expected_grand.get("bytes"):
        raise IntegrityError("migration grand total drift")
    entries = tuple(plans)
    inventory_hash = sha256_bytes(b"".join(canonical_json_bytes(entry.as_dict()) for entry in entries))
    unsigned = {
        "migration_manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "payload_layout_version": PAYLOAD_LAYOUT_VERSION,
        "config_sha256": config["_config_sha256"],
        "inventory_sha256": inventory_hash,
        "destination_vault": str(vault),
        "allowed_vault_root": str(vault_root),
        "allowed_source_roots": [str(root) for root in allowed_sources],
        "file_count": len(entries),
        "total_bytes": sum(entry.size for entry in entries),
    }
    return MigrationPlan(
        entries=entries,
        config_sha256=config["_config_sha256"],
        inventory_sha256=inventory_hash,
        plan_id=sha256_bytes(canonical_json_bytes(unsigned)),
        destination_vault=str(vault),
        allowed_vault_root=str(vault_root),
        allowed_source_roots=tuple(str(root) for root in allowed_sources),
        migration_manifest_schema_version=MIGRATION_MANIFEST_SCHEMA_VERSION,
        payload_layout_version=PAYLOAD_LAYOUT_VERSION,
    )


def execute_copy_plan(
    plan: MigrationPlan,
    *,
    approval: MigrationApproval,
    authorization: SignedAuthorizationReceipt | None = None,
    authorization_authority: AuthorizationAuthority | None = None,
    controlled_rebuild_authorization: ControlledRebuildAuthorization | None = None,
    synthetic_permit: SyntheticOnlyPermit | None = None,
    synthetic_allowed_root: Path | None = None,
    clock: TrustedClock,
    execute: bool = False,
) -> Path:
    if not execute or os.environ.get(COPY_AUTH_ENV) != "YES":
        raise PermissionError(f"copy disabled; require explicit flag and {COPY_AUTH_ENV}=YES")
    approval.validate(plan)
    trusted_clock = require_trusted_clock(clock)
    if trusted_clock.mode != "PRODUCTION_SYSTEM_UTC":
        raise PermissionError("controlled hash copy requires the production system UTC clock")
    external_mode = authorization is not None or authorization_authority is not None
    controlled_mode = controlled_rebuild_authorization is not None
    synthetic_mode = synthetic_permit is not None or synthetic_allowed_root is not None
    if sum((external_mode, controlled_mode, synthetic_mode)) != 1:
        raise PermissionError(
            "copy requires exactly one asymmetric external, controlled-rebuild, "
            "or synthetic-only authority mode"
        )
    if external_mode:
        if authorization is None or authorization_authority is None:
            raise PermissionError("external copy authority is incomplete")
        if authorization_authority.authorization_class != "EXTERNAL_USER_AUTHORITY":
            raise PermissionError("controlled hash copy requires external user authority")
        authorization.validate(
            authority=authorization_authority,
            expected_scope=COPY_AUTHORIZATION_SCOPE,
            expected_subject_id=plan.plan_id,
            required_bindings=migration_authorization_bindings(plan, approval),
            clock=trusted_clock,
        )
        authorization_evidence = CopyAuthorizationEvidence(
            receipt_id=authorization.receipt_id,
            registry_id=authorization.authority_registry_id,
            authorization_class=authorization.authorization_class,
        )
    elif controlled_mode:
        assert controlled_rebuild_authorization is not None
        controlled_rebuild_authorization.validate_plan(plan)
        authorization_core = {
            "authorization_id": controlled_rebuild_authorization.authorization_id,
            "approval_id": approval.approval_id,
            "bindings": migration_authorization_bindings(plan, approval),
            "plan_id": plan.plan_id,
            "scope": COPY_AUTHORIZATION_SCOPE,
            "task_thread_id": controlled_rebuild_authorization.task_thread_id,
        }
        authorization_evidence = CopyAuthorizationEvidence(
            receipt_id=sha256_bytes(canonical_json_bytes(authorization_core)),
            registry_id=controlled_rebuild_authorization.authorization_id,
            authorization_class=CONTROLLED_REBUILD_AUTHORIZATION_CLASS,
        )
    else:
        permit = require_synthetic_permit(
            synthetic_permit,
            scope=SYNTHETIC_COPY_SCOPE,
        )
        if synthetic_allowed_root is None:
            raise ContractError("synthetic copy requires an explicit allowed root")
        synthetic_root = Path(synthetic_allowed_root)
        if not synthetic_root.is_absolute():
            raise ContractError("synthetic copy allowed root must be absolute")
        synthetic_root = require_contained_path(synthetic_root, synthetic_root)
        reject_link(synthetic_root)
        for label, candidate, must_exist in (
            ("vault root", Path(plan.allowed_vault_root), True),
            ("destination", Path(plan.destination_vault), False),
            *(
                ("source", Path(source), True)
                for source in plan.allowed_source_roots
            ),
        ):
            try:
                require_contained_path(
                    candidate,
                    synthetic_root,
                    must_exist=must_exist,
                )
            except ContractError as exc:
                raise ContractError(
                    f"synthetic copy {label} escapes the allowed root"
                ) from exc
        permit.validate(SYNTHETIC_COPY_SCOPE)
        authorization_evidence = CopyAuthorizationEvidence(
            receipt_id=permit.permit_id,
            registry_id=permit.permit_id,
            authorization_class="SYNTHETIC_ONLY_NOT_AUTHORITY",
        )
    vault = Path(plan.destination_vault)
    vault_root = Path(plan.allowed_vault_root)
    require_contained_path(vault, vault_root, must_exist=False)
    vault.mkdir(parents=True, exist_ok=True)
    require_contained_path(vault, vault_root, must_exist=True)
    final = vault / "migration_releases" / plan.plan_id
    # A checkpoint is evidence-bound to the exact reviewed implementation and
    # approval. A later hardening approval therefore gets a distinct, short
    # Windows-safe staging generation. Prior partial trees remain preserved;
    # the full binding inside checkpoint.json makes any prefix collision stop.
    staging = vault / ".s" / f"{plan.plan_id[:16]}.{approval.approval_id[:16]}"
    evidence = _evidence_binding(plan, approval, authorization_evidence)
    with ExclusiveFileLock(
        vault / ".locks" / "migration.writer.lock",
        allowed_root=vault,
    ):
        if final.exists():
            _verify_migration_release(final, plan, approval, authorization_evidence)
            return final
        staging.mkdir(parents=True, exist_ok=True)
        require_contained_path(staging, vault, must_exist=True)
        _recover_owned_orphan_temps(staging, plan)
        checkpoint_path = staging / "checkpoint.json"
        if checkpoint_path.exists():
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if any(checkpoint.get(key) != value for key, value in evidence.items()):
                raise IntegrityError("migration checkpoint evidence binding differs")
            _verify_partial_stage(staging, plan, checkpoint)
        else:
            checkpoint = {"schema_version": 1, **evidence, "state": "COPYING", "completed": {}}
            atomic_write(checkpoint_path, canonical_json_bytes(checkpoint))
        for entry in plan:
            _verify_source(entry, plan)
            relative = Path(entry.destination).relative_to(vault).as_posix()
            object_relative = validate_migration_payload_object(entry.payload_object)
            staged_destination = staging.joinpath(
                "payload", *safe_relative_path(object_relative).parts
            )
            completed = checkpoint["completed"]
            if relative in completed or staged_destination.exists():
                if (
                    completed.get(relative) != entry.sha256
                    or not staged_destination.is_file()
                    or staged_destination.stat().st_nlink != 1
                    or staged_destination.stat().st_size != entry.size
                    or sha256_file(staged_destination) != entry.sha256
                ):
                    raise IntegrityError(f"staged migration checkpoint mismatch: {relative}")
                continue
            staged_destination.parent.mkdir(parents=True, exist_ok=True)
            require_contained_path(staged_destination.parent, staging, must_exist=True)
            temporary = staged_destination.with_name(
                f".cp.{staged_destination.name}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                shutil.copyfile(entry.source, temporary)
                with temporary.open("r+b") as copied:
                    os.fsync(copied.fileno())
                if (
                    temporary.stat().st_nlink != 1
                    or temporary.stat().st_size != entry.size
                    or sha256_file(temporary) != entry.sha256
                    or os.path.samefile(entry.source, temporary)
                ):
                    raise IntegrityError(f"copied bytes/link check failed: {entry.source}")
                _verify_source(entry, plan)
                os.replace(temporary, staged_destination)
                completed[relative] = entry.sha256
                atomic_write(checkpoint_path, canonical_json_bytes(checkpoint))
            finally:
                if temporary.exists():
                    temporary.unlink()
        for entry in plan:
            _verify_source(entry, plan)
        _finalize_stage(
            staging,
            plan,
            approval,
            authorization_evidence,
            checkpoint,
            clock=trusted_clock,
        )
        _verify_migration_release(staging, plan, approval, authorization_evidence)
        final.parent.mkdir(parents=True, exist_ok=True)
        require_contained_path(final.parent, vault, must_exist=True)
        os.replace(staging, final)
        _verify_migration_release(final, plan, approval, authorization_evidence)
        return final


def _evidence_binding(
    plan: MigrationPlan,
    approval: MigrationApproval,
    authorization: CopyAuthorizationEvidence,
) -> dict[str, Any]:
    return {
        "plan_id": plan.plan_id,
        "migration_manifest_schema_version": plan.migration_manifest_schema_version,
        "payload_layout_version": plan.payload_layout_version,
        "config_sha256": plan.config_sha256,
        "inventory_sha256": plan.inventory_sha256,
        "migration_implementation_manifest": dict(approval.migration_implementation_manifest),
        "migration_implementation_sha256": approval.migration_implementation_sha256,
        "approval_id": approval.approval_id,
        **authorization.as_dict(),
    }


def _verify_source(entry: CopyPlanEntry, plan: MigrationPlan) -> None:
    source = Path(entry.source)
    approved = _approved_root(source, tuple(Path(value) for value in plan.allowed_source_roots))
    require_contained_path(source, approved, must_exist=True)
    reject_link(source)
    if (
        not source.is_file()
        or source.stat().st_nlink != 1
        or source.stat().st_size != entry.size
        or sha256_file(source) != entry.sha256
    ):
        raise IntegrityError(f"migration source changed or is linked: {source}")


def _recover_owned_orphan_temps(staging: Path, plan: MigrationPlan) -> None:
    """Recover only migration-owned temps after an ungraceful process stop."""

    reject_link(staging)
    plan_by_object = {
        validate_migration_payload_object(entry.payload_object): entry for entry in plan
    }
    if len(plan_by_object) != len(plan):
        raise IntegrityError("migration payload object namespace collision")
    payload_objects = staging / "payload" / "o"
    copy_temps: list[tuple[Path, re.Match[str]]] = []
    for candidate in sorted(staging.rglob("*")):
        reject_link(candidate)
        if candidate.is_dir():
            continue
        if not candidate.is_file() or candidate.stat().st_nlink != 1:
            raise IntegrityError(f"migration staging contains a non-plain entry: {candidate}")
        if _ATOMIC_TEMP_PATTERN.fullmatch(candidate.name):
            # tempfile-generated .aw names do not encode their intended target
            # or journal identity. They are therefore preserved as failure
            # evidence and require explicit operator disposition; recovery
            # must not infer ownership from a generic basename.
            raise IntegrityError(
                f"migration staging contains an unowned atomic-write temp: {candidate}"
            )
        match = _COPY_TEMP_PATTERN.fullmatch(candidate.name)
        if match is None:
            continue
        if candidate.parent != payload_objects:
            raise IntegrityError(
                f"migration copy temp is outside the exact payload-object directory: {candidate}"
            )
        copy_temps.append((candidate, match))

    # The complete read-only census above succeeds before any cleanup or
    # adoption. This prevents an earlier sorted candidate from being mutated
    # before a later unexpected matching file is rejected.
    for candidate, match in copy_temps:
        object_relative = validate_migration_payload_object(f"o/{match.group(1)}")
        entry = plan_by_object.get(object_relative)
        if entry is None:
            raise IntegrityError(
                f"orphan copy temp does not bind a reviewed payload object: {candidate}"
            )
        target = staging.joinpath(
            "payload", *safe_relative_path(object_relative).parts
        )
        exact = (
            candidate.stat().st_size == entry.size
            and sha256_file(candidate) == entry.sha256
        )
        if exact and not target.exists():
            os.replace(candidate, target)
            if (
                not target.is_file()
                or target.stat().st_nlink != 1
                or target.stat().st_size != entry.size
                or sha256_file(target) != entry.sha256
            ):
                raise IntegrityError(
                    f"recovered migration payload temp differs: {object_relative}"
                )
            continue
        if target.exists():
            reject_link(target)
            if (
                not target.is_file()
                or target.stat().st_nlink != 1
                or target.stat().st_size != entry.size
                or sha256_file(target) != entry.sha256
            ):
                raise IntegrityError(
                    f"existing migration object differs during temp recovery: {object_relative}"
                )
        try:
            candidate.unlink()
        except OSError as exc:
            raise IntegrityError(
                f"cannot remove owned incomplete copy temp: {candidate}"
            ) from exc


def _verify_partial_stage(staging: Path, plan: MigrationPlan, checkpoint: dict[str, Any]) -> None:
    completed = checkpoint.get("completed")
    if not isinstance(completed, dict):
        raise IntegrityError("migration checkpoint completed map is invalid")
    state = checkpoint.get("state")
    if state not in {"COPYING", "COMPLETE_NON_ACTIVE"}:
        raise IntegrityError("migration checkpoint state is invalid")
    plan_by_relative = {
        Path(entry.destination).relative_to(plan.destination_vault).as_posix(): entry
        for entry in plan
    }
    plan_by_object = {
        validate_migration_payload_object(entry.payload_object): (relative, entry)
        for relative, entry in plan_by_relative.items()
    }
    if len(plan_by_object) != len(plan_by_relative):
        raise IntegrityError("migration payload object namespace collision")
    recovered = False
    payload_root = staging / "payload"
    if payload_root.exists():
        reject_link(payload_root)
        for staged in sorted(payload_root.rglob("*")):
            reject_link(staged)
            if staged.is_dir():
                continue
            object_relative = staged.relative_to(payload_root).as_posix()
            matched = plan_by_object.get(object_relative)
            relative, entry = matched if matched is not None else ("", None)
            if (
                entry is None
                or not staged.is_file()
                or staged.stat().st_nlink != 1
                or staged.stat().st_size != entry.size
                or sha256_file(staged) != entry.sha256
            ):
                raise IntegrityError(
                    "uncheckpointed migration file is not an exact reviewed copy: "
                    f"{object_relative}"
                )
            recorded = completed.get(relative)
            if recorded is None:
                completed[relative] = entry.sha256
                recovered = True
            elif recorded != entry.sha256:
                raise IntegrityError(f"checkpoint hash differs from staged file: {relative}")
    if recovered:
        atomic_write(staging / "checkpoint.json", canonical_json_bytes(checkpoint))
    if state == "COMPLETE_NON_ACTIVE":
        if (
            checkpoint.get("completed_count") != len(plan)
            or set(completed) != set(plan_by_relative)
            or not isinstance(checkpoint.get("completed_at"), str)
        ):
            raise IntegrityError("completed migration checkpoint is incomplete")
        parse_utc_z(checkpoint["completed_at"], "migration.completed_at")
    expected_files = {"checkpoint.json"}
    for relative, digest in completed.items():
        safe_relative_path(relative)
        entry = plan_by_relative.get(relative)
        if entry is None or entry.sha256 != digest:
            raise IntegrityError(f"checkpoint references an unreviewed file: {relative}")
        expected_files.add(f"payload/{entry.payload_object}")
    generated_metadata = {
        "migration_files.jsonl",
        "summary.json",
        "completion_receipt.json",
        *{
            f"family_receipts/{family_id}.json"
            for family_id in {entry.migration_id for entry in plan}
        },
    }
    for relative in generated_metadata:
        if staging.joinpath(*safe_relative_path(relative).parts).is_file():
            expected_files.add(relative)
    _assert_tree(
        staging,
        expected_files,
        allowed_existing_directories={"payload", "payload/o", "family_receipts"},
    )


def _finalize_stage(
    staging: Path,
    plan: MigrationPlan,
    approval: MigrationApproval,
    authorization: CopyAuthorizationEvidence,
    checkpoint: dict[str, Any],
    *,
    clock: TrustedClock,
) -> None:
    if (
        plan.migration_manifest_schema_version != MIGRATION_MANIFEST_SCHEMA_VERSION
        or plan.payload_layout_version != PAYLOAD_LAYOUT_VERSION
    ):
        raise IntegrityError("migration plan layout contract differs")
    manifest_bytes = b"".join(canonical_json_bytes(entry.as_dict()) for entry in plan)
    if sha256_bytes(manifest_bytes) != plan.inventory_sha256:
        raise IntegrityError("migration inventory bytes differ from reviewed hash")
    atomic_write(staging / "migration_files.jsonl", manifest_bytes)
    role_counts = Counter(entry.role for entry in plan)
    family_counts = Counter(entry.migration_id for entry in plan)
    family_bytes = Counter()
    for entry in plan:
        family_bytes[entry.migration_id] += entry.size
    evidence = _evidence_binding(plan, approval, authorization)
    for family_id in sorted(family_counts):
        family_entries = [entry for entry in plan if entry.migration_id == family_id]
        receipt = {
            "schema_version": 1,
            **evidence,
            "family_id": family_id,
            "role": family_entries[0].role,
            "state": "COMPLETE_NON_ACTIVE",
            "file_count": family_counts[family_id],
            "total_bytes": family_bytes[family_id],
            "family_manifest_sha256": sha256_bytes(
                b"".join(canonical_json_bytes(entry.as_dict()) for entry in family_entries)
            ),
        }
        atomic_write(staging / "family_receipts" / f"{family_id}.json", canonical_json_bytes(receipt))
    summary = {
        "schema_version": 1,
        **evidence,
        "state": "COMPLETE_NON_ACTIVE",
        "file_count": len(plan),
        "total_bytes": sum(entry.size for entry in plan),
        "role_file_counts": dict(sorted(role_counts.items())),
        "family_file_counts": dict(sorted(family_counts.items())),
        "family_bytes": dict(sorted(family_bytes.items())),
    }
    atomic_write(staging / "summary.json", canonical_json_bytes(summary))
    if checkpoint.get("state") == "COMPLETE_NON_ACTIVE":
        completed_at = checkpoint.get("completed_at")
        if not isinstance(completed_at, str):
            raise IntegrityError("completed migration checkpoint lacks its timestamp")
        parse_utc_z(completed_at, "migration.completed_at")
    elif checkpoint.get("state") == "COPYING":
        completed_at = iso_z(require_trusted_clock(clock).now())
    else:
        raise IntegrityError("migration checkpoint state cannot be finalized")
    checkpoint.update(
        {
            "state": "COMPLETE_NON_ACTIVE",
            "completed_count": len(checkpoint["completed"]),
            "completed_at": completed_at,
        }
    )
    atomic_write(staging / "checkpoint.json", canonical_json_bytes(checkpoint))
    completion = {
        "schema_version": 1,
        **evidence,
        "state": "COMPLETE_NON_ACTIVE",
        "file_count": len(plan),
        "total_bytes": sum(entry.size for entry in plan),
        "completed_at": checkpoint["completed_at"],
    }
    atomic_write(staging / "completion_receipt.json", canonical_json_bytes(completion))


def _assert_tree(
    root: Path,
    expected_files: set[str],
    *,
    allowed_existing_directories: set[str] | None = None,
) -> None:
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    for relative in allowed_existing_directories or set():
        candidate = root.joinpath(*safe_relative_path(relative).parts)
        if candidate.is_dir():
            expected_directories.add(relative)
    try:
        assert_exact_tree(root, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError(str(exc)) from exc


def _verify_migration_release(
    root: Path,
    plan: MigrationPlan,
    approval: MigrationApproval,
    authorization: CopyAuthorizationEvidence,
) -> None:
    reject_link(root)
    if (
        plan.migration_manifest_schema_version != MIGRATION_MANIFEST_SCHEMA_VERSION
        or plan.payload_layout_version != PAYLOAD_LAYOUT_VERSION
        or any(
            entry.schema_version != MIGRATION_MANIFEST_SCHEMA_VERSION
            or validate_migration_payload_object(entry.payload_object)
            != entry.payload_object
            for entry in plan
        )
        or len({entry.payload_object for entry in plan}) != len(plan)
    ):
        raise IntegrityError("migration release plan uses an invalid payload layout")
    evidence = _evidence_binding(plan, approval, authorization)
    try:
        summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
        checkpoint = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
        completion = json.loads((root / "completion_receipt.json").read_text(encoding="utf-8"))
        manifest_bytes = (root / "migration_files.jsonl").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("migration release metadata is missing or invalid") from exc
    if manifest_bytes != b"".join(canonical_json_bytes(entry.as_dict()) for entry in plan):
        raise IntegrityError("migration release manifest differs from reviewed inventory")
    for document in (summary, checkpoint, completion):
        if any(document.get(key) != value for key, value in evidence.items()):
            raise IntegrityError("migration release evidence binding mismatch")
    if (
        summary.get("state") != "COMPLETE_NON_ACTIVE"
        or summary.get("file_count") != len(plan)
        or summary.get("total_bytes") != sum(entry.size for entry in plan)
        or checkpoint.get("state") != "COMPLETE_NON_ACTIVE"
        or checkpoint.get("completed_count") != len(plan)
        or completion.get("state") != "COMPLETE_NON_ACTIVE"
        or completion.get("file_count") != len(plan)
        or completion.get("total_bytes") != sum(entry.size for entry in plan)
    ):
        raise IntegrityError("migration release completion metadata mismatch")
    vault = Path(plan.destination_vault)
    expected_completed = {
        Path(entry.destination).relative_to(vault).as_posix(): entry.sha256
        for entry in plan
    }
    expected_roles = dict(sorted(Counter(entry.role for entry in plan).items()))
    expected_families = dict(sorted(Counter(entry.migration_id for entry in plan).items()))
    expected_family_bytes: Counter[str] = Counter()
    for entry in plan:
        expected_family_bytes[entry.migration_id] += entry.size
    if (
        checkpoint.get("completed") != expected_completed
        or summary.get("role_file_counts") != expected_roles
        or summary.get("family_file_counts") != expected_families
        or summary.get("family_bytes") != dict(sorted(expected_family_bytes.items()))
    ):
        raise IntegrityError("migration release checkpoint/summary differs from reviewed inventory")
    expected_files = {
        "summary.json",
        "checkpoint.json",
        "completion_receipt.json",
        "migration_files.jsonl",
    }
    for family_id in sorted({entry.migration_id for entry in plan}):
        path = root / "family_receipts" / f"{family_id}.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntegrityError(f"migration family receipt is invalid: {family_id}") from exc
        family_entries = [entry for entry in plan if entry.migration_id == family_id]
        if (
            any(receipt.get(key) != value for key, value in evidence.items())
            or receipt.get("state") != "COMPLETE_NON_ACTIVE"
            or receipt.get("role") != family_entries[0].role
            or receipt.get("file_count") != len(family_entries)
            or receipt.get("total_bytes") != sum(entry.size for entry in family_entries)
            or receipt.get("family_manifest_sha256")
            != sha256_bytes(b"".join(canonical_json_bytes(entry.as_dict()) for entry in family_entries))
        ):
            raise IntegrityError(f"migration family receipt mismatch: {family_id}")
        expected_files.add(f"family_receipts/{family_id}.json")
    for entry in plan:
        relative = Path(entry.destination).relative_to(vault).as_posix()
        object_relative = validate_migration_payload_object(entry.payload_object)
        candidate = root.joinpath(
            "payload", *safe_relative_path(object_relative).parts
        )
        if (
            not candidate.is_file()
            or candidate.stat().st_nlink != 1
            or candidate.stat().st_size != entry.size
            or sha256_file(candidate) != entry.sha256
        ):
            raise IntegrityError(f"migration release payload mismatch: {relative}")
        expected_files.add(f"payload/{object_relative}")
    _assert_tree(root, expected_files)
