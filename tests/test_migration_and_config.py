from __future__ import annotations

import json
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, iso_z, sha256_bytes
from us_stocks_swing_model_v2.cli.hash_copy import main as hash_copy_main
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.migration import (
    COPY_AUTHORIZATION_SCOPE,
    CONTROLLED_REBUILD_AUTHORIZATION_CLASS,
    MIGRATION_MANIFEST_SCHEMA_VERSION,
    PAYLOAD_LAYOUT_VERSION,
    ControlledRebuildAuthorization,
    MigrationApproval,
    approval_payload_for_review,
    execute_copy_plan,
    load_migration_approval,
    load_sealed_migration_approval,
    load_migration_config,
    migration_payload_object_relative,
    migration_authorization_bindings,
    plan_migration,
)
from us_stocks_swing_model_v2.canonical.hfdl_legacy_publisher import (
    CompletedMigrationRelease,
    verify_completed_migration_release,
)
from us_stocks_swing_model_v2.governance import (
    SignedAuthorizationReceipt,
    load_external_authority,
)
import us_stocks_swing_model_v2.migration as migration_module


REPO = Path(__file__).parents[1]


@pytest.fixture(scope="module")
def completed_real_migration() -> tuple[MigrationApproval, CompletedMigrationRelease]:
    approval = load_sealed_migration_approval(REPO / "config" / "migration_approval.json")
    release = verify_completed_migration_release(
        REPO / "data" / "vault" / "migration_releases" / approval.plan_id
    )
    assert release.plan_id == approval.plan_id
    assert release.inventory_sha256 == approval.inventory_sha256
    return approval, release


def _external_execution_kwargs(
    tmp_path: Path,
    plan,
    approval: MigrationApproval,
) -> dict[str, object]:
    key = b"externally-controlled-test-key"
    registry = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "status": "ACTIVE",
        "authorities": [
            {
                "key_id": "migration-user-key",
                "key_sha256": sha256_bytes(key),
                "authorization_class": "EXTERNAL_USER_AUTHORITY",
            }
        ],
    }
    registry_path = tmp_path / "external-authority-registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    authority = load_external_authority(
        registry_path,
        key_id="migration-user-key",
        verification_key=key,
    )
    now = datetime.now(timezone.utc)
    signing = {
        "schema_version": 1,
        "scope": COPY_AUTHORIZATION_SCOPE,
        "subject_id": plan.plan_id,
        "bindings": dict(sorted(migration_authorization_bindings(plan, approval).items())),
        "issued_at": iso_z(now - timedelta(minutes=1)),
        "expires_at": iso_z(now + timedelta(hours=1)),
        "key_id": authority.key_id,
        "authority_registry_id": authority.registry_id,
        "authorization_class": authority.authorization_class,
    }
    signature = hmac.new(key, canonical_json_bytes(signing), hashlib.sha256).hexdigest()
    unsigned = {**signing, "signature": signature}
    authorization = SignedAuthorizationReceipt(
        **signing,
        signature=signature,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    return {
        "authorization": authorization,
        "authorization_authority": authority,
        "clock": TrustedClock.production(),
    }


def test_checked_in_source_roles_and_exclusions_are_fail_closed() -> None:
    sources = json.loads((REPO / "config" / "sources.json").read_text(encoding="utf-8"))["sources"]
    assert sources["hfdl_legacy_discovery"]["role"] == "legacy_discovery_only"
    assert sources["hfdl_legacy_discovery"]["epochs"][1]["start_inclusive"] == "2022-03-04"
    capsule = sources["alpaca_sip_qualification_legacy"]
    assert capsule["quality_status"].startswith("fail_")
    assert "780_stock_symbols" in capsule["scope"]
    policy = sources["alpaca_basic_delayed_sip"]["request_contract"]
    assert policy == {
        "qualified_feed": None,
        "qualification_candidates": ["sip", "iex"],
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "minimum_end_lag_minutes": 20,
        "sort": "asc",
    }
    nasdaq = sources["nasdaq_symbol_directory"]
    assert nasdaq["status"] == (
        "qualified_public_snapshot_pending_alpaca_asset_join_and_identity_release"
    )
    assert nasdaq["qualification_receipt"] == "config/nasdaq_qualification_receipt.json"
    assert len(nasdaq["latest_snapshot_id"]) == 64
    assert sources["alpha_vantage"]["role"] == "excluded"
    assert sources["options_data"]["role"] == "excluded"


def test_synthetic_hash_copy_is_dry_by_default_and_verifies_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "A.parquet").write_bytes(b"fixture")
    vault = tmp_path / "vault"
    config_path = tmp_path / "migration.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "deny_unlisted",
                "allowed_vault_root": str(tmp_path),
                "allowed_source_roots": [str(legacy)],
                "destination_vault": str(vault),
                "expected_grand_total": {"files": 1, "bytes": 7},
                "entries": [
                    {
                        "id": "fixture",
                        "source_root": str(legacy),
                        "destination_subdir": "legacy_discovery",
                        "include_specs": [{"glob": "*.parquet", "files": 1, "bytes": 7}],
                        "exclude_globs": [],
                        "expected_total": {"files": 1, "bytes": 7},
                        "role": "legacy_discovery_only",
                        "status": "approved_for_dry_run_only",
                    }
                ],
                "global_prohibitions": [],
            }
        ),
        encoding="utf-8",
    )
    config = load_migration_config(config_path)
    plan = plan_migration(config)
    approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    execution = _external_execution_kwargs(tmp_path, plan, approval)
    assert len(plan) == 1
    assert plan.migration_manifest_schema_version == MIGRATION_MANIFEST_SCHEMA_VERSION
    assert plan.payload_layout_version == PAYLOAD_LAYOUT_VERSION
    assert plan[0].schema_version == MIGRATION_MANIFEST_SCHEMA_VERSION
    assert not vault.exists()
    monkeypatch.delenv("HASH_COPY_APPROVED", raising=False)
    with pytest.raises(PermissionError):
        execute_copy_plan(plan, approval=approval, execute=False, **execution)
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    release = execute_copy_plan(plan, approval=approval, execute=True, **execution)
    relative = Path(plan[0].destination).relative_to(vault)
    copied = release / "payload" / migration_payload_object_relative(
        relative, plan[0].sha256
    )
    assert copied.read_bytes() == b"fixture"
    summary = json.loads((release / "summary.json").read_text(encoding="utf-8"))
    assert summary["migration_manifest_schema_version"] == MIGRATION_MANIFEST_SCHEMA_VERSION
    assert summary["payload_layout_version"] == PAYLOAD_LAYOUT_VERSION
    assert summary["config_sha256"] == plan.config_sha256
    assert summary["inventory_sha256"] == plan.inventory_sha256
    assert summary["plan_id"] == plan.plan_id
    assert summary["migration_implementation_sha256"] == approval.migration_implementation_sha256
    assert summary["migration_implementation_manifest"] == approval.migration_implementation_manifest
    assert summary["approval_id"] == approval.approval_id
    with pytest.raises(IntegrityError, match="payload mismatch"):
        copied.write_bytes(b"tampered")
        execute_copy_plan(plan, approval=approval, execute=True, **execution)
    copied.write_bytes(b"fixture")
    (release / "undeclared-empty").mkdir()
    with pytest.raises(IntegrityError, match="extra_dirs"):
        execute_copy_plan(plan, approval=approval, execute=True, **execution)


def test_real_migration_config_excludes_derived_and_option_branches() -> None:
    config = load_migration_config(REPO / "config" / "migration_allowlist.json")
    prohibitions = set(config["global_prohibitions"])
    assert {
        "adopt_legacy_alpaca_parquet",
        "copy_current_optionable_domain",
        "copy_asof_disabled_remediation",
        "copy_options_data",
        "copy_legacy_models",
    } <= prohibitions
    roles = {entry["role"] for entry in config["entries"]}
    assert roles <= {
        "legacy_discovery_only",
        "qualification_evidence_only",
        "legacy_trial_census_evidence_only",
    }
    census = next(
        entry for entry in config["entries"] if entry["id"] == "legacy_trial_census_evidence"
    )
    assert census["expected_total"] == {"files": 8, "bytes": 75_143}
    assert "pending" not in json.dumps(config["entries"]).lower()


def test_explicit_user_task_authority_binds_completed_non_alpha_copy(
    completed_real_migration: tuple[MigrationApproval, CompletedMigrationRelease],
) -> None:
    approval, release = completed_real_migration
    authority = ControlledRebuildAuthorization.load(
        REPO / "config" / "controlled_rebuild_authorization.json"
    )
    summary = json.loads((release.root / "summary.json").read_text(encoding="utf-8"))
    assert summary["authorization_registry_id"] == authority.authorization_id
    assert summary["authorization_class"] == CONTROLLED_REBUILD_AUTHORIZATION_CLASS
    assert summary["approval_id"] == approval.approval_id
    assert summary["plan_id"] == approval.plan_id
    assert summary["state"] == "COMPLETE_NON_ACTIVE"
    receipt_id = sha256_bytes(
        canonical_json_bytes(
            {
                "authorization_id": authority.authorization_id,
                "approval_id": approval.approval_id,
                "bindings": {
                    "config_sha256": approval.config_sha256,
                    "inventory_sha256": approval.inventory_sha256,
                },
                "plan_id": approval.plan_id,
                "scope": COPY_AUTHORIZATION_SCOPE,
                "task_thread_id": authority.task_thread_id,
            }
        )
    )
    assert len(receipt_id) == 64
    assert CONTROLLED_REBUILD_AUTHORIZATION_CLASS.endswith("NON_ALPHA_COPY")
    assert "real_history" not in CONTROLLED_REBUILD_AUTHORIZATION_CLASS.lower()


def test_checked_in_migration_approval_binds_completed_reviewed_capsule(
    completed_real_migration: tuple[MigrationApproval, CompletedMigrationRelease],
) -> None:
    approval, release = completed_real_migration
    assert approval.plan_id == release.plan_id
    assert approval.inventory_sha256 == release.inventory_sha256
    assert approval.file_count == 4_911
    assert approval.total_bytes == 345_845_816
    assert len(release.entries) == approval.file_count
    assert sum(entry.size for entry in release.entries) == approval.total_bytes


def test_migration_checkpoint_resumes_without_partial_promotion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "legacy"
    source.mkdir()
    (source / "a.bin").write_bytes(b"a")
    (source / "b.bin").write_bytes(b"bb")
    vault = tmp_path / "vault"
    config_path = tmp_path / "resume.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "deny_unlisted",
                "allowed_vault_root": str(tmp_path),
                "allowed_source_roots": [str(source)],
                "destination_vault": str(vault),
                "expected_grand_total": {"files": 2, "bytes": 3},
                "entries": [
                    {
                        "id": "family",
                        "source_root": str(source),
                        "destination_subdir": "evidence",
                        "include_specs": [
                            {"glob": "a.bin", "files": 1, "bytes": 1},
                            {"glob": "b.bin", "files": 1, "bytes": 2},
                        ],
                        "exclude_globs": [],
                        "expected_total": {"files": 2, "bytes": 3},
                        "role": "qualification_evidence_only",
                        "status": "approved_for_dry_run_only",
                    }
                ],
                "global_prohibitions": [],
            }
        ),
        encoding="utf-8",
    )
    plan = plan_migration(load_migration_config(config_path))
    approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    execution = _external_execution_kwargs(tmp_path, plan, approval)
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    original = migration_module.shutil.copyfile
    calls = 0

    def crash_second(source_path, destination_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic crash")
        return original(source_path, destination_path)

    monkeypatch.setattr(migration_module.shutil, "copyfile", crash_second)
    with pytest.raises(OSError, match="synthetic crash"):
        execute_copy_plan(plan, approval=approval, execute=True, **execution)
    assert not (vault / "migration_releases").exists()
    monkeypatch.setattr(migration_module.shutil, "copyfile", original)
    release = execute_copy_plan(plan, approval=approval, execute=True, **execution)
    assert json.loads((release / "completion_receipt.json").read_text(encoding="utf-8"))["state"] == "COMPLETE_NON_ACTIVE"
    assert json.loads((release / "family_receipts" / "family.json").read_text(encoding="utf-8"))["file_count"] == 2


@pytest.mark.parametrize("orphan_bytes", [b"fixture", b"partial"])
def test_resume_recovers_owned_copy_and_atomic_write_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_bytes: bytes,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    plan = plan_migration(load_migration_config(_one_file_config(tmp_path, source)))
    approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    execution = _external_execution_kwargs(tmp_path, plan, approval)
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    original_copy = migration_module.shutil.copyfile

    def crash_before_copy(source_path, destination_path):
        raise OSError("synthetic hard-stop window")

    monkeypatch.setattr(migration_module.shutil, "copyfile", crash_before_copy)
    with pytest.raises(OSError, match="hard-stop window"):
        execute_copy_plan(plan, approval=approval, execute=True, **execution)
    stage = (
        Path(plan.destination_vault)
        / ".s"
        / f"{plan.plan_id[:16]}.{approval.approval_id[:16]}"
    )
    object_name = Path(plan[0].payload_object).name
    copy_temp = stage / "payload" / "o" / f".cp.{object_name}.deadbeef.tmp"
    copy_temp.parent.mkdir(parents=True, exist_ok=True)
    copy_temp.write_bytes(orphan_bytes)
    (stage / ".aw.deadbeef.tmp").write_bytes(b"orphan-checkpoint-temp")
    family_dir = stage / "family_receipts"
    family_dir.mkdir()
    (family_dir / ".aw.deadbeef.tmp").write_bytes(b"orphan-family-temp")

    monkeypatch.setattr(migration_module.shutil, "copyfile", original_copy)
    release = execute_copy_plan(plan, approval=approval, execute=True, **execution)
    assert (release / "payload" / plan[0].payload_object).read_bytes() == b"fixture"
    assert not stage.exists()


@pytest.mark.parametrize(
    ("crash_target", "write_before_crash", "checkpoint_state"),
    [
        ("summary.json", True, "COPYING"),
        ("completion_receipt.json", False, "COMPLETE_NON_ACTIVE"),
    ],
)
def test_resume_is_idempotent_across_finalization_kill_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_target: str,
    write_before_crash: bool,
    checkpoint_state: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    plan = plan_migration(load_migration_config(_one_file_config(tmp_path, source)))
    approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    execution = _external_execution_kwargs(tmp_path, plan, approval)
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    original_atomic = migration_module.atomic_write
    crashed = False

    def crash_in_finalize(path: Path, payload: bytes) -> None:
        nonlocal crashed
        if path.name == crash_target and not crashed:
            crashed = True
            if write_before_crash:
                original_atomic(path, payload)
            raise OSError("synthetic finalization hard-stop")
        original_atomic(path, payload)

    monkeypatch.setattr(migration_module, "atomic_write", crash_in_finalize)
    with pytest.raises(OSError, match="finalization hard-stop"):
        execute_copy_plan(plan, approval=approval, execute=True, **execution)
    stage = (
        Path(plan.destination_vault)
        / ".s"
        / f"{plan.plan_id[:16]}.{approval.approval_id[:16]}"
    )
    checkpoint = json.loads((stage / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["state"] == checkpoint_state

    monkeypatch.setattr(migration_module, "atomic_write", original_atomic)
    release = execute_copy_plan(plan, approval=approval, execute=True, **execution)
    completion = json.loads(
        (release / "completion_receipt.json").read_text(encoding="utf-8")
    )
    assert completion["state"] == "COMPLETE_NON_ACTIVE"


def test_resume_adopts_only_an_exact_reviewed_file_written_before_checkpoint(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    plan = plan_migration(load_migration_config(_one_file_config(tmp_path, source)))
    stage = tmp_path / "vault" / ".staging" / "migrations" / plan.plan_id
    stage.mkdir(parents=True)
    checkpoint = {"schema_version": 1, "state": "COPYING", "completed": {}}
    (stage / "checkpoint.json").write_bytes(canonical_json_bytes(checkpoint))
    entry = plan[0]
    relative = Path(entry.destination).relative_to(plan.destination_vault)
    copied = stage / "payload" / migration_payload_object_relative(
        relative, entry.sha256
    )
    copied.parent.mkdir(parents=True)
    copied.write_bytes(b"fixture")

    migration_module._verify_partial_stage(stage, plan, checkpoint)
    persisted = json.loads((stage / "checkpoint.json").read_text(encoding="utf-8"))
    assert persisted["completed"] == {relative.as_posix(): entry.sha256}

    extra = stage / "payload" / "unreviewed.bin"
    extra.write_bytes(b"not in plan")
    with pytest.raises(IntegrityError, match="not an exact reviewed copy"):
        migration_module._verify_partial_stage(stage, plan, persisted)


def test_new_approval_uses_a_new_stage_and_preserves_old_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    plan = plan_migration(load_migration_config(_one_file_config(tmp_path, source)))
    old_approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    new_approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:01Z")
    )
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    original = migration_module.shutil.copyfile

    def crash_before_copy(source_path, destination_path):
        raise OSError("synthetic pre-copy crash")

    monkeypatch.setattr(migration_module.shutil, "copyfile", crash_before_copy)
    with pytest.raises(OSError, match="synthetic pre-copy crash"):
        execute_copy_plan(
            plan,
            approval=old_approval,
            execute=True,
            **_external_execution_kwargs(tmp_path, plan, old_approval),
        )
    old_stage = (
        tmp_path
        / "vault"
        / ".s"
        / f"{plan.plan_id[:16]}.{old_approval.approval_id[:16]}"
    )
    assert (old_stage / "checkpoint.json").is_file()

    monkeypatch.setattr(migration_module.shutil, "copyfile", original)
    release = execute_copy_plan(
        plan,
        approval=new_approval,
        execute=True,
        **_external_execution_kwargs(tmp_path, plan, new_approval),
    )
    assert release.is_dir()
    assert (old_stage / "checkpoint.json").is_file()
    receipt = json.loads((release / "completion_receipt.json").read_text(encoding="utf-8"))
    assert receipt["approval_id"] == new_approval.approval_id


def _one_file_config(tmp_path: Path, source: Path, *, marker: str = "one") -> Path:
    vault = tmp_path / "vault"
    path = tmp_path / f"migration-{marker}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "deny_unlisted",
                "allowed_vault_root": str(tmp_path),
                "allowed_source_roots": [str(source)],
                "destination_vault": str(vault),
                "expected_grand_total": {"files": 1, "bytes": 7},
                "entries": [
                    {
                        "id": "fixture",
                        "source_root": str(source),
                        "destination_subdir": "legacy",
                        "include_specs": [{"glob": "a.bin", "files": 1, "bytes": 7}],
                        "exclude_globs": [],
                        "expected_total": {"files": 1, "bytes": 7},
                        "role": "legacy_discovery_only",
                        "status": "approved_for_dry_run_only",
                    }
                ],
                "global_prohibitions": [marker],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_long_logical_destination_uses_only_the_sealed_flat_payload_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    config_path = _one_file_config(tmp_path, source, marker="long-logical")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["entries"][0]["destination_subdir"] = "/".join(
        f"logical-segment-{index:02d}" for index in range(20)
    )
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    plan = plan_migration(load_migration_config(config_path))
    relative = Path(plan[0].destination).relative_to(plan.destination_vault)
    assert len(relative.as_posix()) > 300
    assert plan[0].payload_object.startswith("o/")
    assert len(plan[0].payload_object) == 42

    approval = MigrationApproval.from_dict(
        approval_payload_for_review(plan, "2026-07-15T00:00:00Z")
    )
    monkeypatch.setenv("HASH_COPY_APPROVED", "YES")
    release = execute_copy_plan(
        plan,
        approval=approval,
        execute=True,
        **_external_execution_kwargs(tmp_path, plan, approval),
    )
    flat = release / "payload" / plan[0].payload_object
    assert flat.read_bytes() == b"fixture"
    assert not (release / "payload" / relative).exists()


def test_completed_real_migration_flat_namespace_is_unique_and_windows_safe(
    completed_real_migration: tuple[MigrationApproval, CompletedMigrationRelease],
) -> None:
    _, release = completed_real_migration
    vault = REPO / "data" / "vault"
    final_payload = release.root / "payload"
    objects = [entry.payload_object for entry in release.entries]
    logical = [Path(entry.destination).relative_to(vault) for entry in release.entries]
    assert len(objects) == len(set(objects)) == 4_911
    assert all(
        entry.schema_version == MIGRATION_MANIFEST_SCHEMA_VERSION
        for entry in release.entries
    )
    assert max(len(str(final_payload / item)) for item in objects) < 240
    assert max(len(str(final_payload / item)) for item in logical) > 260


def test_plan_rejects_a_forced_flat_object_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    (source / "b.bin").write_bytes(b"fixture")
    config_path = _one_file_config(tmp_path, source, marker="collision")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["entries"][0]["include_specs"] = [
        {"glob": "*.bin", "files": 2, "bytes": 14}
    ]
    payload["entries"][0]["expected_total"] = {"files": 2, "bytes": 14}
    payload["expected_grand_total"] = {"files": 2, "bytes": 14}
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(
        migration_module,
        "migration_payload_object_relative",
        lambda destination_relative, digest: "o/" + "0" * 40,
    )
    with pytest.raises(IntegrityError, match="payload object namespace collision"):
        plan_migration(load_migration_config(config_path))


def test_approval_binds_exact_config_inventory_and_full_migration_implementation_closure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    config_path = _one_file_config(tmp_path, source)
    first_plan = plan_migration(load_migration_config(config_path))
    approval_payload = approval_payload_for_review(first_plan, "2026-07-15T00:00:00Z")
    approval_path = tmp_path / "approval.json"
    approval_path.write_bytes(canonical_json_bytes(approval_payload))

    changed_plan = plan_migration(
        load_migration_config(_one_file_config(tmp_path, source, marker="semantically-changed"))
    )
    with pytest.raises(PermissionError, match="config_sha256"):
        load_migration_approval(approval_path, changed_plan)

    (source / "a.bin").write_bytes(b"changed")
    inventory_changed = plan_migration(load_migration_config(config_path))
    with pytest.raises(PermissionError, match="inventory_sha256"):
        load_migration_approval(approval_path, inventory_changed)
    # Compare against an approval for the exact inventory but a forged code hash,
    # recomputing approval_id so the code-binding check—not merely ID corruption—fires.
    forged = approval_payload_for_review(inventory_changed, "2026-07-15T00:00:00Z")
    forged["migration_implementation_sha256"] = "0" * 64
    unsigned = {key: value for key, value in forged.items() if key != "approval_id"}
    forged["approval_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    forged_path = tmp_path / "forged-code-approval.json"
    forged_path.write_bytes(canonical_json_bytes(forged))
    with pytest.raises(PermissionError, match="migration_implementation_sha256"):
        load_migration_approval(forged_path, inventory_changed)


def test_migration_rejects_outside_roots_and_hardlinked_sources(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    (outside / "a.bin").write_bytes(b"fixture")
    path = _one_file_config(tmp_path, outside)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["allowed_source_roots"] = [str(approved)]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ContractError, match="approved root"):
        plan_migration(load_migration_config(path))

    (approved / "a.bin").write_bytes(b"fixture")
    os.link(approved / "a.bin", approved / "linked.bin")
    hardlink_config = _one_file_config(tmp_path, approved, marker="hardlink")
    with pytest.raises(ContractError, match="hardlinked source"):
        plan_migration(load_migration_config(hardlink_config))


def test_hash_copy_dry_run_is_concise_unless_detailed_is_requested(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"fixture")
    config = _one_file_config(tmp_path, source)
    assert hash_copy_main(["--config", str(config)]) == 0
    concise = json.loads(capsys.readouterr().out)
    assert concise["file_count"] == 1
    assert "entries" not in concise
    assert hash_copy_main(["--config", str(config), "--detailed-plan"]) == 0
    detailed = json.loads(capsys.readouterr().out)
    assert detailed["entries"][0]["sha256"] == plan_migration(
        load_migration_config(config)
    )[0].sha256
