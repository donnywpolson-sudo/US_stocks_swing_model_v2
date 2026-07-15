"""Restart-safe, no-model stock historical-foundation orchestration.

This module consumes only a completed schema-v2 migration capsule and accepted
content-addressed releases.  It never reads legacy paths, calls providers,
fits/evaluates a model, emits labels, or changes candidate state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .canonical.hfdl_legacy_publisher import (
    EPOCH_DATASETS,
    HFDL_EPOCHS,
    SET_DATASET as HFDL_SET_DATASET,
    SYNTHETIC_CONTRACT_SCOPE,
    HfdlPublicationResult,
    HfdlPublishContract,
    publish_hfdl_legacy_discovery,
    verify_completed_migration_release,
    verify_hfdl_legacy_publication,
)
from .common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .exchange_calendar import LoadedExchangeCalendar, load_xnys_calendar_release
from .historical_foundation import (
    BRIDGE_DATASETS,
    BRIDGE_SET_DATASET,
    OUTPUT_KINDS,
    HistoricalFoundationResult,
    load_hfdl_historical_foundation,
    publish_hfdl_historical_foundation,
)
from .locking import ExclusiveFileLock
from .migration import MIGRATION_MANIFEST_SCHEMA_VERSION, PAYLOAD_LAYOUT_VERSION
from .releases import (
    AtomicReleasePublisher,
    ReleaseManifest,
    build_manifest,
    verify_accepted_release,
)


PROJECT = "US_stocks_swing_model_v2"
ORCHESTRATOR_VERSION = "1.0.0"
AGGREGATE_DATASET = "stock_historical_foundation_set"
AGGREGATE_SOURCE_EPOCH = "hfdl_two_epoch_legacy_discovery_no_pooling"
AGGREGATE_ROLE = "legacy_discovery_only"
AGGREGATE_QUALITY = "LEGACY_CAVEATED"
AGGREGATE_COMPONENT_COUNT = 11
PHASES = ("migration", "calendar", "hfdl", "bridge", "aggregate")
_ATOMIC_TEMP = re.compile(r"^\.aw\.[^.]+\.tmp$")
_INDEX_FIELDS = {
    "sequence",
    "phase",
    "epoch",
    "kind",
    "dataset",
    "release_id",
    "relative_directory",
    "source_epoch",
    "role",
    "quality_state",
    "row_count",
    "event_start",
    "event_end",
    "manifest_sha256",
}
_CALENDAR_RECEIPT_FIELDS = {
    "schema_version",
    "project",
    "dataset",
    "role",
    "release_id",
    "release_manifest_sha256",
    "sessions_sha256",
    "provenance_sha256",
    "verification_receipt_id",
    "policy_sha256",
    "code_sha256",
    "environment_sha256",
    "calendar_version",
    "session_count",
    "first_session",
    "last_session",
    "execution_authority",
    "receipt_id",
}
_IMPLEMENTATION_PATHS = (
    "src/us_stocks_swing_model_v2/foundation_orchestrator.py",
    "src/us_stocks_swing_model_v2/cli/build_historical_foundation.py",
    "src/us_stocks_swing_model_v2/canonical/hfdl_legacy_publisher.py",
    "src/us_stocks_swing_model_v2/canonical/hfdl.py",
    "src/us_stocks_swing_model_v2/historical_foundation.py",
    "src/us_stocks_swing_model_v2/exchange_calendar.py",
    "src/us_stocks_swing_model_v2/migration.py",
    "src/us_stocks_swing_model_v2/releases.py",
    "src/us_stocks_swing_model_v2/common.py",
    "src/us_stocks_swing_model_v2/locking.py",
    "src/us_stocks_swing_model_v2/capabilities.py",
    "config/hfdl_historical_foundation_contract.json",
    "config/xnys_calendar_release_receipt.json",
    "config/xnys_calendar_policy.json",
    "config/environment.lock.json",
    "requirements.sha256.lock",
)
_AGGREGATE_CONTRACT = {
    "schema_version": 1,
    "contract_version": ORCHESTRATOR_VERSION,
    "project": PROJECT,
    "source": "VERIFIED_SCHEMA_V2_MIGRATION_RELEASE_ONLY",
    "calendar": "REPO_PINNED_ACCEPTED_XNYS_RELEASE_ONLY",
    "physical_hfdl_epochs": list(HFDL_EPOCHS),
    "historical_release_kinds": list(OUTPUT_KINDS),
    "component_count": AGGREGATE_COMPONENT_COUNT,
    "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
    "epochs_may_be_pooled": False,
    "providers_allowed": False,
    "models_allowed": False,
    "wfa_allowed": False,
    "labels_allowed": False,
    "candidate_actions_allowed": False,
}
_RECEIPT_FIELDS = {
    "schema_version",
    "publication_state",
    "build_id",
    "created_at",
    "contract",
    "contract_id",
    "implementation_hash",
    "environment_hash",
    "synthetic_permit_id",
    "migration",
    "calendar",
    "hfdl",
    "historical_foundation",
    "component_count",
    "component_release_ids",
    "index_sha256",
    "historical_evidence_scope",
    "point_in_time_safe",
    "epochs_may_be_pooled",
    "provider_calls_made",
    "legacy_paths_read",
    "model_or_evaluation_inputs_read",
    "real_history_hypothesis_executed",
    "wfa_executed",
    "labels_emitted",
    "matured_outcomes_emitted",
    "alpha_evidence",
    "candidate_eligible",
}


@dataclass(frozen=True)
class StockHistoricalFoundationResult:
    build_id: str
    migration_plan_id: str
    calendar_release_directory: Path
    hfdl_publication: HfdlPublicationResult
    historical_foundation: HistoricalFoundationResult
    aggregate_set_release_directory: Path


@dataclass(frozen=True)
class _PreparedInputs:
    migration: Any
    calendar_directory: Path
    calendar: LoadedExchangeCalendar
    calendar_binding: Mapping[str, Any]
    hfdl_contract: HfdlPublishContract
    synthetic_permit: SyntheticOnlyPermit | None
    created_at: str
    implementation_hash: str
    environment_hash: str
    contract_id: str
    build_id: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _implementation_hash() -> str:
    root = _repo_root()
    manifest: dict[str, str] = {}
    for relative in _IMPLEMENTATION_PATHS:
        path = root.joinpath(*safe_relative_path(relative).parts)
        require_contained_path(path, root, must_exist=True)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(f"foundation implementation dependency differs: {relative}")
        manifest[relative] = sha256_file(path)
    return sha256_bytes(canonical_json_bytes(dict(sorted(manifest.items()))))


def _environment_hash() -> str:
    return sha256_file(_repo_root() / "config" / "environment.lock.json")


def _contract_id() -> str:
    return sha256_bytes(canonical_json_bytes(_AGGREGATE_CONTRACT))


def _read_json(path: Path, *, canonical: bool, label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if not isinstance(payload, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    if canonical and raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} is not canonically encoded")
    return payload


def _accepted_binding(
    directory: Path,
    *,
    accepted_root: Path,
    phase: str,
    epoch: str,
    kind: str,
) -> dict[str, Any]:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    return {
        "phase": phase,
        "epoch": epoch,
        "kind": kind,
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "relative_directory": f"{manifest.dataset}/{manifest.release_id}",
        "source_epoch": manifest.source_epoch,
        "role": manifest.role,
        "quality_state": manifest.quality_state,
        "row_count": manifest.row_count,
        "event_start": manifest.event_start,
        "event_end": manifest.event_end,
        "manifest_sha256": sha256_file(directory / "release_manifest.json"),
    }


def _production_calendar(
    *, accepted_root: Path, receipt_path: Path
) -> tuple[Path, LoadedExchangeCalendar, dict[str, Any]]:
    expected_receipt = (
        _repo_root() / "config" / "xnys_calendar_release_receipt.json"
    ).resolve(strict=True)
    if receipt_path.resolve(strict=True) != expected_receipt:
        raise ContractError("production foundation requires the repo-pinned XNYS receipt")
    receipt = _read_json(receipt_path, canonical=False, label="XNYS release receipt")
    if set(receipt) != _CALENDAR_RECEIPT_FIELDS:
        raise IntegrityError("XNYS release receipt fields differ")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    if (
        receipt["receipt_id"] != sha256_bytes(canonical_json_bytes(unsigned))
        or receipt["schema_version"] != 1
        or receipt["project"] != PROJECT
        or receipt["dataset"] != "xnys_sessions"
        or receipt["execution_authority"] is not False
        or receipt["policy_sha256"]
        != sha256_file(_repo_root() / "config" / "xnys_calendar_policy.json")
        or receipt["code_sha256"]
        != sha256_file(_repo_root() / "src" / "us_stocks_swing_model_v2" / "exchange_calendar.py")
        or receipt["environment_sha256"] != _environment_hash()
    ):
        raise IntegrityError("XNYS release receipt binding differs")
    directory = accepted_root / "xnys_sessions" / str(receipt["release_id"])
    loaded = load_xnys_calendar_release(directory, accepted_release_root=accepted_root)
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        sha256_file(directory / "release_manifest.json")
        != receipt["release_manifest_sha256"]
        or sha256_file(directory / "sessions.parquet") != receipt["sessions_sha256"]
        or sha256_file(directory / "provenance.json") != receipt["provenance_sha256"]
        or loaded.calendar.verification_receipt_id != receipt["verification_receipt_id"]
        or manifest.release_id != receipt["release_id"]
        or manifest.row_count != receipt["session_count"]
        or manifest.event_start != receipt["first_session"]
        or manifest.event_end != receipt["last_session"]
    ):
        raise IntegrityError("accepted XNYS release differs from its pinned receipt")
    binding = {
        "binding_class": "REPO_PINNED_ACCEPTED_XNYS",
        "receipt_id": receipt["receipt_id"],
        "verification_receipt_id": loaded.calendar.verification_receipt_id,
        "release": _accepted_binding(
            directory,
            accepted_root=accepted_root,
            phase="calendar",
            epoch="xnys_exchange_calendars_4_13_2",
            kind="pinned_sessions",
        ),
    }
    return directory, loaded, binding


def _synthetic_calendar(
    *, accepted_root: Path, directory: Path
) -> tuple[Path, LoadedExchangeCalendar, dict[str, Any]]:
    loaded = load_xnys_calendar_release(directory, accepted_release_root=accepted_root)
    release = _accepted_binding(
        directory,
        accepted_root=accepted_root,
        phase="calendar",
        epoch="xnys_exchange_calendars_4_13_2",
        kind="pinned_sessions",
    )
    receipt_id = sha256_bytes(
        canonical_json_bytes(
            {
                "binding_class": "SYNTHETIC_TEST_ONLY",
                "release": release,
                "verification_receipt_id": loaded.calendar.verification_receipt_id,
            }
        )
    )
    return directory, loaded, {
        "binding_class": "SYNTHETIC_TEST_ONLY",
        "receipt_id": receipt_id,
        "verification_receipt_id": loaded.calendar.verification_receipt_id,
        "release": release,
    }


def _prepare_inputs(
    *,
    migration_release_directory: Path,
    accepted_release_root: Path,
    created_at: str,
    calendar_receipt_path: Path | None,
    calendar_release_directory: Path | None,
    hfdl_contract: HfdlPublishContract | None,
    hfdl_synthetic_permit: SyntheticOnlyPermit | None,
) -> _PreparedInputs:
    parse_utc_z(created_at, "foundation.created_at")
    migration_path = Path(migration_release_directory)
    accepted = Path(accepted_release_root)
    if not migration_path.is_absolute() or not accepted.is_absolute():
        raise ContractError("foundation migration and accepted roots must be absolute")
    migration = verify_completed_migration_release(migration_path)
    selected = hfdl_contract or HfdlPublishContract.production()
    selected.validate()
    synthetic_id = selected.synthetic_permit_id
    if synthetic_id is None:
        if hfdl_synthetic_permit is not None or calendar_release_directory is not None:
            raise ContractError("production foundation cannot accept synthetic overrides")
        expected_migration_root = (
            _repo_root() / "data" / "vault" / "migration_releases"
        ).resolve(strict=True)
        expected_accepted = (
            _repo_root() / "data" / "vault" / "accepted"
        ).resolve(strict=True)
        if (
            migration.root.parent.resolve(strict=True) != expected_migration_root
            or accepted.resolve(strict=True) != expected_accepted
        ):
            raise ContractError("production foundation roots differ from the controlled rebuild")
        receipt = calendar_receipt_path or (
            _repo_root() / "config" / "xnys_calendar_release_receipt.json"
        )
        calendar_directory, calendar, calendar_binding = _production_calendar(
            accepted_root=accepted, receipt_path=Path(receipt)
        )
        permit = None
    else:
        if hfdl_synthetic_permit is None or calendar_release_directory is None:
            raise ContractError("synthetic foundation requires permit and calendar release")
        permit = require_synthetic_permit(
            hfdl_synthetic_permit, scope=SYNTHETIC_CONTRACT_SCOPE
        )
        if permit.permit_id != synthetic_id or calendar_receipt_path is not None:
            raise ContractError("synthetic foundation inputs differ from their permit")
        calendar_directory, calendar, calendar_binding = _synthetic_calendar(
            accepted_root=accepted, directory=Path(calendar_release_directory)
        )
    implementation_hash = _implementation_hash()
    environment_hash = _environment_hash()
    contract_id = _contract_id()
    build_unsigned = {
        "schema_version": 1,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "migration_plan_id": migration.plan_id,
        "migration_inventory_sha256": migration.inventory_sha256,
        "migration_completion_receipt_sha256": migration.completion_receipt_sha256,
        "migration_manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "payload_layout_version": PAYLOAD_LAYOUT_VERSION,
        "calendar_binding": calendar_binding,
        "hfdl_contract_id": selected.contract_id,
        "synthetic_permit_id": synthetic_id,
        "created_at": created_at,
        "implementation_hash": implementation_hash,
        "environment_hash": environment_hash,
        "contract_id": contract_id,
    }
    return _PreparedInputs(
        migration=migration,
        calendar_directory=calendar_directory,
        calendar=calendar,
        calendar_binding=calendar_binding,
        hfdl_contract=selected,
        synthetic_permit=permit,
        created_at=created_at,
        implementation_hash=implementation_hash,
        environment_hash=environment_hash,
        contract_id=contract_id,
        build_id=sha256_bytes(canonical_json_bytes(build_unsigned)),
    )


def _migration_binding(prepared: _PreparedInputs) -> dict[str, Any]:
    migration = prepared.migration
    return {
        "manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "payload_layout_version": PAYLOAD_LAYOUT_VERSION,
        "plan_id": migration.plan_id,
        "inventory_sha256": migration.inventory_sha256,
        "completion_receipt_sha256": migration.completion_receipt_sha256,
        "file_count": len(migration.entries),
        "total_bytes": sum(entry.size for entry in migration.entries),
    }


def _checkpoint_evidence(prepared: _PreparedInputs) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project": PROJECT,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "build_id": prepared.build_id,
        "created_at": prepared.created_at,
        "implementation_hash": prepared.implementation_hash,
        "environment_hash": prepared.environment_hash,
        "contract_id": prepared.contract_id,
        "synthetic_permit_id": (
            prepared.synthetic_permit.permit_id if prepared.synthetic_permit else None
        ),
    }


def _cleanup_owned_atomic_temps(root: Path) -> None:
    if not root.exists():
        return
    reject_link(root)
    for path in sorted(root.rglob("*")):
        reject_link(path)
        if path.is_file() and _ATOMIC_TEMP.fullmatch(path.name):
            if path.stat().st_nlink != 1:
                raise IntegrityError("foundation atomic temp is hardlinked")
            path.unlink()


def _write_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    unsigned = dict(checkpoint)
    unsigned.pop("checkpoint_id", None)
    checkpoint["checkpoint_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    atomic_write(path, canonical_json_bytes(checkpoint))


def _load_or_create_checkpoint(
    path: Path, *, prepared: _PreparedInputs
) -> dict[str, Any]:
    evidence = _checkpoint_evidence(prepared)
    if not path.exists():
        checkpoint = {
            **evidence,
            "state": "BUILDING_NON_ACTIVE_FOUNDATION",
            "phases": {
                "migration": _migration_binding(prepared),
                "calendar": dict(prepared.calendar_binding),
                "hfdl": None,
                "bridge": None,
                "aggregate": None,
            },
        }
        _write_checkpoint(path, checkpoint)
        return checkpoint
    checkpoint = _read_json(path, canonical=True, label="foundation checkpoint")
    expected_fields = {*evidence, "state", "phases", "checkpoint_id"}
    if set(checkpoint) != expected_fields:
        raise IntegrityError("foundation checkpoint fields differ")
    unsigned = dict(checkpoint)
    checkpoint_id = unsigned.pop("checkpoint_id")
    require_sha256(checkpoint_id, "foundation.checkpoint_id")
    if (
        checkpoint_id != sha256_bytes(canonical_json_bytes(unsigned))
        or any(checkpoint[name] != value for name, value in evidence.items())
        or checkpoint["state"]
        not in {"BUILDING_NON_ACTIVE_FOUNDATION", "COMPLETE_NON_ACTIVE_FOUNDATION"}
        or not isinstance(checkpoint["phases"], dict)
        or set(checkpoint["phases"]) != set(PHASES)
        or checkpoint["phases"]["migration"] != _migration_binding(prepared)
        or checkpoint["phases"]["calendar"] != dict(prepared.calendar_binding)
    ):
        raise IntegrityError("foundation checkpoint evidence differs")
    if checkpoint["state"] == "COMPLETE_NON_ACTIVE_FOUNDATION" and any(
        checkpoint["phases"][phase] is None for phase in PHASES
    ):
        raise IntegrityError("completed foundation checkpoint lacks a phase")
    return checkpoint


def _record_phase(
    checkpoint_path: Path,
    checkpoint: dict[str, Any],
    phase: str,
    binding: Mapping[str, Any],
) -> None:
    if phase not in PHASES:
        raise ContractError("unknown foundation phase")
    existing = checkpoint["phases"].get(phase)
    if existing is not None and existing != binding:
        raise IntegrityError(f"foundation checkpoint {phase} binding differs")
    checkpoint["phases"][phase] = dict(binding)
    _write_checkpoint(checkpoint_path, checkpoint)


def _hfdl_binding_with_permit(
    result: HfdlPublicationResult,
    *,
    accepted_root: Path,
    permit: SyntheticOnlyPermit | None,
) -> dict[str, Any]:
    verified = verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=accepted_root,
        synthetic_permit=permit,
    )
    if verified != result:
        raise IntegrityError("HFDL publication verification binding differs")
    return {
        "build_id": result.build_id,
        "epochs": {
            epoch: _accepted_binding(
                result.epoch_release_directories[epoch],
                accepted_root=accepted_root,
                phase="hfdl",
                epoch=epoch,
                kind="physical_epoch",
            )
            for epoch in HFDL_EPOCHS
        },
        "epoch_set": _accepted_binding(
            result.epoch_set_release_directory,
            accepted_root=accepted_root,
            phase="hfdl",
            epoch="hfdl_epoch_set_no_pooling",
            kind="epoch_set",
        ),
    }


def _hfdl_from_binding(
    binding: Mapping[str, Any],
    *,
    accepted_root: Path,
    permit: SyntheticOnlyPermit | None,
) -> HfdlPublicationResult:
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"build_id", "epochs", "epoch_set"}
        or not isinstance(binding["epoch_set"], Mapping)
    ):
        raise IntegrityError("foundation HFDL phase binding fields differ")
    set_binding = binding["epoch_set"]
    directory = accepted_root / str(set_binding["dataset"]) / str(
        set_binding["release_id"]
    )
    result = verify_hfdl_legacy_publication(
        directory,
        accepted_release_root=accepted_root,
        synthetic_permit=permit,
    )
    observed = _hfdl_binding_with_permit(
        result, accepted_root=accepted_root, permit=permit
    )
    if observed != dict(binding):
        raise IntegrityError("foundation HFDL checkpoint differs from accepted releases")
    return result


def _bridge_binding(
    result: HistoricalFoundationResult, *, accepted_root: Path
) -> dict[str, Any]:
    return {
        "build_id": result.build_id,
        "epochs": {
            epoch: {
                kind: _accepted_binding(
                    result.epoch_release_directories[epoch][kind],
                    accepted_root=accepted_root,
                    phase="bridge",
                    epoch=epoch,
                    kind=kind,
                )
                for kind in OUTPUT_KINDS
            }
            for epoch in HFDL_EPOCHS
        },
        "bridge_set": _accepted_binding(
            result.bridge_set_release_directory,
            accepted_root=accepted_root,
            phase="bridge",
            epoch="hfdl_historical_foundation_no_pooling",
            kind="bridge_set",
        ),
    }


def _bridge_from_binding(
    binding: Mapping[str, Any],
    *,
    accepted_root: Path,
    permit: SyntheticOnlyPermit | None,
) -> HistoricalFoundationResult:
    if (
        not isinstance(binding, Mapping)
        or set(binding) != {"build_id", "epochs", "bridge_set"}
        or not isinstance(binding["bridge_set"], Mapping)
    ):
        raise IntegrityError("foundation bridge phase binding fields differ")
    set_binding = binding["bridge_set"]
    directory = accepted_root / str(set_binding["dataset"]) / str(
        set_binding["release_id"]
    )
    result = load_hfdl_historical_foundation(
        directory,
        accepted_release_root=accepted_root,
        hfdl_synthetic_permit=permit,
    )
    observed = _bridge_binding(result, accepted_root=accepted_root)
    if observed != dict(binding):
        raise IntegrityError("foundation bridge checkpoint differs from accepted releases")
    return result


def _component_index(
    prepared: _PreparedInputs,
    hfdl: HfdlPublicationResult,
    bridge: HistoricalFoundationResult,
    *,
    accepted_root: Path,
) -> tuple[dict[str, Any], ...]:
    bindings: list[dict[str, Any]] = [dict(prepared.calendar_binding["release"])]
    bindings.extend(
        _accepted_binding(
            hfdl.epoch_release_directories[epoch],
            accepted_root=accepted_root,
            phase="hfdl",
            epoch=epoch,
            kind="physical_epoch",
        )
        for epoch in HFDL_EPOCHS
    )
    bindings.append(
        _accepted_binding(
            hfdl.epoch_set_release_directory,
            accepted_root=accepted_root,
            phase="hfdl",
            epoch="hfdl_epoch_set_no_pooling",
            kind="epoch_set",
        )
    )
    for epoch in HFDL_EPOCHS:
        bindings.extend(
            _accepted_binding(
                bridge.epoch_release_directories[epoch][kind],
                accepted_root=accepted_root,
                phase="bridge",
                epoch=epoch,
                kind=kind,
            )
            for kind in OUTPUT_KINDS
        )
    bindings.append(
        _accepted_binding(
            bridge.bridge_set_release_directory,
            accepted_root=accepted_root,
            phase="bridge",
            epoch="hfdl_historical_foundation_no_pooling",
            kind="bridge_set",
        )
    )
    rows = tuple({"sequence": sequence, **binding} for sequence, binding in enumerate(bindings))
    if (
        len(rows) != AGGREGATE_COMPONENT_COUNT
        or len({row["release_id"] for row in rows}) != AGGREGATE_COMPONENT_COUNT
        or any(set(row) != _INDEX_FIELDS for row in rows)
    ):
        raise IntegrityError("foundation aggregate component denominator differs")
    return rows


def _index_bytes(rows: tuple[dict[str, Any], ...]) -> bytes:
    return b"".join(canonical_json_bytes(row) for row in rows)


def _aggregate_receipt(
    prepared: _PreparedInputs,
    *,
    hfdl_binding: Mapping[str, Any],
    bridge_binding: Mapping[str, Any],
    rows: tuple[dict[str, Any], ...],
    index_bytes: bytes,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "publication_state": "COMPLETE_NON_ACTIVE_HISTORICAL_FOUNDATION",
        "build_id": prepared.build_id,
        "created_at": prepared.created_at,
        "contract": _AGGREGATE_CONTRACT,
        "contract_id": prepared.contract_id,
        "implementation_hash": prepared.implementation_hash,
        "environment_hash": prepared.environment_hash,
        "synthetic_permit_id": (
            prepared.synthetic_permit.permit_id if prepared.synthetic_permit else None
        ),
        "migration": _migration_binding(prepared),
        "calendar": dict(prepared.calendar_binding),
        "hfdl": dict(hfdl_binding),
        "historical_foundation": dict(bridge_binding),
        "component_count": len(rows),
        "component_release_ids": sorted(row["release_id"] for row in rows),
        "index_sha256": sha256_bytes(index_bytes),
        "historical_evidence_scope": "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED",
        "point_in_time_safe": False,
        "epochs_may_be_pooled": False,
        "provider_calls_made": False,
        "legacy_paths_read": False,
        "model_or_evaluation_inputs_read": False,
        "real_history_hypothesis_executed": False,
        "wfa_executed": False,
        "labels_emitted": False,
        "matured_outcomes_emitted": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }


def _write_or_verify(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != payload:
            raise IntegrityError(f"foundation aggregate stage differs: {path.name}")
        return
    atomic_write(path, payload)


def _stage_exact(stage: Path, expected_files: set[str]) -> None:
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    try:
        assert_exact_tree(stage, expected_files, expected_directories)
    except ContractError as exc:
        raise IntegrityError("foundation aggregate stage is partial or extra") from exc


def _verify_aggregate_expected(
    directory: Path,
    *,
    prepared: _PreparedInputs,
    hfdl: HfdlPublicationResult,
    bridge: HistoricalFoundationResult,
    accepted_root: Path,
) -> ReleaseManifest:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    rows = _component_index(
        prepared, hfdl, bridge, accepted_root=accepted_root
    )
    index_bytes = _index_bytes(rows)
    hfdl_binding = _hfdl_binding_with_permit(
        hfdl, accepted_root=accepted_root, permit=prepared.synthetic_permit
    )
    bridge_binding = _bridge_binding(bridge, accepted_root=accepted_root)
    expected_receipt = _aggregate_receipt(
        prepared,
        hfdl_binding=hfdl_binding,
        bridge_binding=bridge_binding,
        rows=rows,
        index_bytes=index_bytes,
    )
    index_path = directory / "foundation_index.jsonl"
    receipt_path = directory / "foundation_set.json"
    if index_path.read_bytes() != index_bytes:
        raise IntegrityError("foundation aggregate index differs from verified components")
    receipt = _read_json(receipt_path, canonical=True, label="foundation aggregate receipt")
    if set(receipt) != _RECEIPT_FIELDS or receipt != expected_receipt:
        raise IntegrityError("foundation aggregate receipt differs from verified components")
    event_starts = [row["event_start"] for row in rows if row["event_start"] is not None]
    event_ends = [row["event_end"] for row in rows if row["event_end"] is not None]
    expected_upstream = tuple(sorted(row["release_id"] for row in rows))
    schema_fingerprint = sha256_bytes(
        canonical_json_bytes(
            {"receipt_fields": sorted(_RECEIPT_FIELDS), "index_fields": sorted(_INDEX_FIELDS)}
        )
    )
    if (
        manifest.project != PROJECT
        or manifest.dataset != AGGREGATE_DATASET
        or manifest.source_epoch != AGGREGATE_SOURCE_EPOCH
        or manifest.role != AGGREGATE_ROLE
        or manifest.quality_state != AGGREGATE_QUALITY
        or manifest.row_count != AGGREGATE_COMPONENT_COUNT
        or manifest.event_start != min(event_starts)
        or manifest.event_end != max(event_ends)
        or manifest.upstream_release_ids != expected_upstream
        or manifest.schema_fingerprint != schema_fingerprint
        or manifest.code_hash != prepared.implementation_hash
        or manifest.config_hash != prepared.contract_id
        or manifest.environment_hash != prepared.environment_hash
        or {entry.path for entry in manifest.files}
        != {"foundation_index.jsonl", "foundation_set.json"}
    ):
        raise IntegrityError("foundation aggregate release manifest differs")
    return manifest


def _publish_aggregate(
    *,
    prepared: _PreparedInputs,
    hfdl: HfdlPublicationResult,
    bridge: HistoricalFoundationResult,
    accepted_root: Path,
    build_root: Path,
) -> Path:
    rows = _component_index(prepared, hfdl, bridge, accepted_root=accepted_root)
    index_bytes = _index_bytes(rows)
    hfdl_binding = _hfdl_binding_with_permit(
        hfdl, accepted_root=accepted_root, permit=prepared.synthetic_permit
    )
    bridge_binding = _bridge_binding(bridge, accepted_root=accepted_root)
    receipt = _aggregate_receipt(
        prepared,
        hfdl_binding=hfdl_binding,
        bridge_binding=bridge_binding,
        rows=rows,
        index_bytes=index_bytes,
    )
    stage = build_root / "aggregate_stage"
    stage.mkdir(parents=True, exist_ok=True)
    _cleanup_owned_atomic_temps(stage)
    _write_or_verify(stage / "foundation_index.jsonl", index_bytes)
    _write_or_verify(stage / "foundation_set.json", canonical_json_bytes(receipt))
    _stage_exact(stage, {"foundation_index.jsonl", "foundation_set.json"})
    event_starts = [row["event_start"] for row in rows if row["event_start"] is not None]
    event_ends = [row["event_end"] for row in rows if row["event_end"] is not None]
    schema_fingerprint = sha256_bytes(
        canonical_json_bytes(
            {"receipt_fields": sorted(_RECEIPT_FIELDS), "index_fields": sorted(_INDEX_FIELDS)}
        )
    )
    manifest = build_manifest(
        stage,
        ("foundation_index.jsonl", "foundation_set.json"),
        project=PROJECT,
        dataset=AGGREGATE_DATASET,
        source_epoch=AGGREGATE_SOURCE_EPOCH,
        role=AGGREGATE_ROLE,
        quality_state=AGGREGATE_QUALITY,
        created_at=prepared.created_at,
        row_count=len(rows),
        event_start=min(event_starts),
        event_end=max(event_ends),
        upstream_release_ids=(row["release_id"] for row in rows),
        schema_fingerprint=schema_fingerprint,
        code_hash=prepared.implementation_hash,
        config_hash=prepared.contract_id,
        environment_hash=prepared.environment_hash,
    )
    directory = AtomicReleasePublisher(accepted_root).publish(stage, manifest)
    _verify_aggregate_expected(
        directory,
        prepared=prepared,
        hfdl=hfdl,
        bridge=bridge,
        accepted_root=accepted_root,
    )
    return directory


def _aggregate_binding(directory: Path, *, accepted_root: Path) -> dict[str, Any]:
    return _accepted_binding(
        directory,
        accepted_root=accepted_root,
        phase="aggregate",
        epoch=AGGREGATE_SOURCE_EPOCH,
        kind="foundation_set",
    )


def plan_stock_historical_foundation(
    *,
    migration_release_directory: Path,
    accepted_release_root: Path,
    created_at: str,
    calendar_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Verify production inputs and return a write-free orchestration plan."""

    prepared = _prepare_inputs(
        migration_release_directory=migration_release_directory,
        accepted_release_root=accepted_release_root,
        created_at=created_at,
        calendar_receipt_path=calendar_receipt_path,
        calendar_release_directory=None,
        hfdl_contract=None,
        hfdl_synthetic_permit=None,
    )
    return {
        "schema_version": 1,
        "mode": "DRY_RUN_VERIFIED_INPUTS_ONLY",
        "project": PROJECT,
        "build_id": prepared.build_id,
        "migration": _migration_binding(prepared),
        "calendar": dict(prepared.calendar_binding),
        "hfdl_contract_id": prepared.hfdl_contract.contract_id,
        "aggregate_contract_id": prepared.contract_id,
        "component_count": AGGREGATE_COMPONENT_COUNT,
        "provider_calls_authorized": False,
        "model_or_wfa_execution_authorized": False,
    }


def run_stock_historical_foundation(
    *,
    migration_release_directory: Path,
    accepted_release_root: Path,
    derived_work_root: Path,
    created_at: str,
    calendar_receipt_path: Path | None = None,
    calendar_release_directory: Path | None = None,
    hfdl_contract: HfdlPublishContract | None = None,
    hfdl_synthetic_permit: SyntheticOnlyPermit | None = None,
) -> StockHistoricalFoundationResult:
    """Build and verify the complete non-active historical foundation."""

    accepted = Path(accepted_release_root)
    work = Path(derived_work_root)
    prepared = _prepare_inputs(
        migration_release_directory=migration_release_directory,
        accepted_release_root=accepted,
        created_at=created_at,
        calendar_receipt_path=calendar_receipt_path,
        calendar_release_directory=calendar_release_directory,
        hfdl_contract=hfdl_contract,
        hfdl_synthetic_permit=hfdl_synthetic_permit,
    )
    if not work.is_absolute():
        raise ContractError("foundation derived-work root must be absolute")
    if prepared.synthetic_permit is None:
        require_contained_path(work, _repo_root(), must_exist=False)
    work.mkdir(parents=True, exist_ok=True)
    reject_link(work)
    for left, right in (
        (prepared.migration.root, accepted),
        (prepared.migration.root, work),
        (accepted, work),
    ):
        left_resolved = left.resolve(strict=True)
        right_resolved = right.resolve(strict=True)
        if (
            left_resolved == right_resolved
            or left_resolved in right_resolved.parents
            or right_resolved in left_resolved.parents
        ):
            raise ContractError("foundation migration, accepted, and work roots must be separate")
    # The bridge writes 64-hex Parquet basenames through an atomic sibling.
    # Keep internal names short enough for the pinned Windows runtime.
    worst_case = (
        work
        / "b"
        / "hfdl_foundation"
        / ("f" * 32)
        / "stages"
        / "hfdl_pitrading_consolidated_feature_inputs"
        / "data"
        / ("." + "f" * 64 + ".parquet." + "f" * 8 + ".tmp")
    )
    if len(str(worst_case)) >= 250:
        raise ContractError("foundation work root exceeds the conservative Windows path budget")
    build_root = work / "o" / prepared.build_id[:16]
    build_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = build_root / "checkpoint.json"
    lock_path = work / ".locks" / f"foundation-{prepared.build_id}.lock"
    with ExclusiveFileLock(lock_path):
        _cleanup_owned_atomic_temps(build_root)
        checkpoint = _load_or_create_checkpoint(checkpoint_path, prepared=prepared)

        stored_hfdl = checkpoint["phases"]["hfdl"]
        if stored_hfdl is None:
            hfdl = publish_hfdl_legacy_discovery(
                migration_release_directory=prepared.migration.root,
                accepted_release_root=accepted,
                derived_work_root=work / "h",
                created_at=prepared.created_at,
                contract=prepared.hfdl_contract,
            )
            hfdl_binding = _hfdl_binding_with_permit(
                hfdl, accepted_root=accepted, permit=prepared.synthetic_permit
            )
            _record_phase(checkpoint_path, checkpoint, "hfdl", hfdl_binding)
        else:
            hfdl = _hfdl_from_binding(
                stored_hfdl,
                accepted_root=accepted,
                permit=prepared.synthetic_permit,
            )

        stored_bridge = checkpoint["phases"]["bridge"]
        if stored_bridge is None:
            bridge = publish_hfdl_historical_foundation(
                hfdl_epoch_set_release_directory=hfdl.epoch_set_release_directory,
                calendar_release_directory=prepared.calendar_directory,
                accepted_release_root=accepted,
                derived_work_root=work / "b",
                created_at=prepared.created_at,
                hfdl_synthetic_permit=prepared.synthetic_permit,
            )
            bridge_binding = _bridge_binding(bridge, accepted_root=accepted)
            _record_phase(checkpoint_path, checkpoint, "bridge", bridge_binding)
        else:
            bridge = _bridge_from_binding(
                stored_bridge,
                accepted_root=accepted,
                permit=prepared.synthetic_permit,
            )

        stored_aggregate = checkpoint["phases"]["aggregate"]
        if stored_aggregate is None:
            aggregate = _publish_aggregate(
                prepared=prepared,
                hfdl=hfdl,
                bridge=bridge,
                accepted_root=accepted,
                build_root=build_root,
            )
            aggregate_binding = _aggregate_binding(
                aggregate, accepted_root=accepted
            )
            _record_phase(
                checkpoint_path, checkpoint, "aggregate", aggregate_binding
            )
        else:
            if not isinstance(stored_aggregate, Mapping):
                raise IntegrityError("foundation aggregate checkpoint binding is invalid")
            aggregate = accepted / str(stored_aggregate["dataset"]) / str(
                stored_aggregate["release_id"]
            )
            _verify_aggregate_expected(
                aggregate,
                prepared=prepared,
                hfdl=hfdl,
                bridge=bridge,
                accepted_root=accepted,
            )
            if _aggregate_binding(aggregate, accepted_root=accepted) != dict(
                stored_aggregate
            ):
                raise IntegrityError("foundation aggregate checkpoint differs")

        checkpoint["state"] = "COMPLETE_NON_ACTIVE_FOUNDATION"
        _write_checkpoint(checkpoint_path, checkpoint)
        _verify_aggregate_expected(
            aggregate,
            prepared=prepared,
            hfdl=hfdl,
            bridge=bridge,
            accepted_root=accepted,
        )
        return StockHistoricalFoundationResult(
            build_id=prepared.build_id,
            migration_plan_id=prepared.migration.plan_id,
            calendar_release_directory=prepared.calendar_directory,
            hfdl_publication=hfdl,
            historical_foundation=bridge,
            aggregate_set_release_directory=aggregate,
        )
