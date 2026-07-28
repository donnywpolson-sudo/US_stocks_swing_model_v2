from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator, Mapping

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.canonical import hfdl_legacy_publisher as publisher_module
from us_stocks_swing_model_v2.canonical.hfdl import (
    HFDL_HISTORICAL_AVAILABILITY,
    HFDL_NATIVE_SCHEMA,
    HFDL_POINT_IN_TIME_STATE,
)
from us_stocks_swing_model_v2.canonical.hfdl_legacy_publisher import (
    EPOCH_DATASETS,
    HFDL_EPOCHS,
    HFDL_MIGRATION_FAMILY,
    HfdlPublishContract,
    SET_DATASET,
    SYNTHETIC_CONTRACT_SCOPE,
    SYNTHETIC_PUBLICATION_SCOPE,
    publish_hfdl_legacy_discovery,
    verify_completed_migration_release,
    verify_hfdl_legacy_publication,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes, sha256_file
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.migration import (
    CONTROLLED_REBUILD_AUTHORIZATION_CLASS,
    CONTROLLED_REBUILD_AUTHORIZATION_ID,
    MIGRATION_MANIFEST_SCHEMA_VERSION,
    PAYLOAD_LAYOUT_VERSION,
    migration_payload_object_relative,
)


CREATED_AT = "2026-07-15T00:00:00Z"
PAYLOAD_PREFIX = "source_releases/hfdl_legacy_discovery/payload/data/raw"


@pytest.fixture
def hfdl_tmp() -> Iterator[Path]:
    # The production paths fit under the short repository root. Pytest's nested
    # Windows temp names do not once the content-addressed release ID is added.
    root = Path(tempfile.mkdtemp(prefix="hfp-"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _permit(fixture_id: str) -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(fixture_id=fixture_id, scope=SYNTHETIC_CONTRACT_SCOPE)


def _publication_kwargs(
    root: Path,
    contract: HfdlPublishContract,
) -> dict[str, object]:
    return {
        "publication_synthetic_permit": SyntheticOnlyPermit.create(
            fixture_id=contract.contract_id,
            scope=SYNTHETIC_PUBLICATION_SCOPE,
        ),
        "publication_allowed_root": root,
    }


def _write_native_pair(
    root: Path,
    *,
    filename_symbol: str,
    canonical_symbol: str,
    sidecar_updates: Mapping[str, object] | None = None,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    parquet = root / f"{filename_symbol}.parquet"
    table = pa.Table.from_pydict(
        {
            "ticker": [canonical_symbol, canonical_symbol],
            "per": ["D", "D"],
            "date": ["20220303", "20220304"],
            "time": ["000000", "000000"],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "vol": [100, 200],
            "openint": [0, 0],
        },
        schema=HFDL_NATIVE_SCHEMA,
    )
    pq.write_table(table, parquet)
    sidecar = root / f"{filename_symbol}.parquet.provenance.json"
    payload: dict[str, object] = {
        "canonical_symbol": canonical_symbol,
        "created_at_utc": CREATED_AT,
        "row_count": 2,
        "sha256": sha256_file(parquet),
        "timeframe": "daily",
        "validation_passed": True,
        "version": "clean",
        "source_limitations": [
            "Universe is a fixed snapshot.",
            "Pre-March 2022 and post-March 2022 feeds differ.",
            "Clean files are source-adjusted.",
        ],
    }
    if sidecar_updates is not None:
        payload.update(sidecar_updates)
    sidecar.write_bytes(canonical_json_bytes(payload))
    return parquet, sidecar


def _completed_migration_release(
    root: Path,
    pairs: tuple[tuple[str, str], ...] = (("AAA", "AAA"), ("BBB", "BBB")),
    *,
    omit_sidecar_for: str | None = None,
    authorization_class: str = CONTROLLED_REBUILD_AUTHORIZATION_CLASS,
    authorization_registry_id: str = CONTROLLED_REBUILD_AUTHORIZATION_ID,
    sealed_non_derived_objects: bool = False,
    sidecar_updates: Mapping[str, object] | None = None,
) -> Path:
    source = root / "fixture_source"
    entries: list[dict[str, object]] = []
    payloads: dict[str, Path] = {}
    for filename_symbol, canonical_symbol in pairs:
        parquet, sidecar = _write_native_pair(
            source,
            filename_symbol=filename_symbol,
            canonical_symbol=canonical_symbol,
            sidecar_updates=sidecar_updates,
        )
        members = (parquet,) if filename_symbol == omit_sidecar_for else (parquet, sidecar)
        for member in members:
            relative = f"{PAYLOAD_PREFIX}/{member.name}"
            payloads[relative] = member
            digest = sha256_file(member)
            payload_object = (
                f"o/{sha256_bytes(('sealed:' + relative).encode())[:40]}"
                if sealed_non_derived_objects
                else migration_payload_object_relative(relative, digest)
            )
            entries.append(
                {
                    "schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
                    "migration_id": HFDL_MIGRATION_FAMILY,
                    "role": "legacy_discovery_only",
                    # Deliberately impossible: publication must never touch legacy source paths.
                    "source": f"Z:/legacy-path-must-never-be-read/{member.name}",
                    "destination": f"C:/synthetic-vault/{relative}",
                    "size": member.stat().st_size,
                    "sha256": digest,
                    "payload_object": payload_object,
                }
            )
    entries.sort(key=lambda item: str(item["destination"]))
    manifest_bytes = b"".join(canonical_json_bytes(entry) for entry in entries)
    inventory_sha256 = sha256_bytes(manifest_bytes)
    plan_id = sha256_bytes(b"synthetic-hfdl-publication-plan")
    release = root / "migration_releases" / plan_id
    payload_objects = {
        f"{PAYLOAD_PREFIX}/{Path(str(entry['destination'])).name}": str(
            entry["payload_object"]
        )
        for entry in entries
    }
    for relative, source_file in payloads.items():
        destination = release / "payload" / payload_objects[relative]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source_file.read_bytes())

    implementation_manifest = {"src/synthetic_migration.py": "2" * 64}
    evidence = {
        "plan_id": plan_id,
        "migration_manifest_schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "payload_layout_version": PAYLOAD_LAYOUT_VERSION,
        "config_sha256": "1" * 64,
        "inventory_sha256": inventory_sha256,
        "migration_implementation_manifest": implementation_manifest,
        "migration_implementation_sha256": sha256_bytes(
            canonical_json_bytes(implementation_manifest)
        ),
        "approval_id": "4" * 64,
        "authorization_receipt_id": "5" * 64,
        "authorization_registry_id": authorization_registry_id,
        "authorization_class": authorization_class,
    }
    completed = {
        f"{PAYLOAD_PREFIX}/{Path(str(entry['destination'])).name}": entry["sha256"]
        for entry in entries
    }
    total_bytes = sum(int(entry["size"]) for entry in entries)
    family_manifest_sha256 = sha256_bytes(manifest_bytes)
    documents = {
        "summary.json": {
            "schema_version": 1,
            **evidence,
            "state": "COMPLETE_NON_ACTIVE",
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "role_file_counts": {"legacy_discovery_only": len(entries)},
            "family_file_counts": {HFDL_MIGRATION_FAMILY: len(entries)},
            "family_bytes": {HFDL_MIGRATION_FAMILY: total_bytes},
        },
        "checkpoint.json": {
            "schema_version": 1,
            **evidence,
            "state": "COMPLETE_NON_ACTIVE",
            "completed": completed,
            "completed_count": len(entries),
            "completed_at": CREATED_AT,
        },
        "completion_receipt.json": {
            "schema_version": 1,
            **evidence,
            "state": "COMPLETE_NON_ACTIVE",
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "completed_at": CREATED_AT,
        },
    }
    release.mkdir(parents=True, exist_ok=True)
    (release / "migration_files.jsonl").write_bytes(manifest_bytes)
    for name, payload in documents.items():
        (release / name).write_bytes(canonical_json_bytes(payload))
    family_receipt = {
        "schema_version": 1,
        **evidence,
        "family_id": HFDL_MIGRATION_FAMILY,
        "role": "legacy_discovery_only",
        "state": "COMPLETE_NON_ACTIVE",
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "family_manifest_sha256": family_manifest_sha256,
    }
    family_path = release / "family_receipts" / f"{HFDL_MIGRATION_FAMILY}.json"
    family_path.parent.mkdir(exist_ok=True)
    family_path.write_bytes(canonical_json_bytes(family_receipt))
    return release


def _poison_completed_migration_entry(
    release: Path,
    *,
    field: str,
    value: object,
) -> None:
    manifest_path = release / "migration_files.jsonl"
    entries = [
        json.loads(line)
        for line in manifest_path.read_bytes().splitlines()
    ]
    entries[0][field] = value
    manifest_bytes = b"".join(canonical_json_bytes(entry) for entry in entries)
    manifest_path.write_bytes(manifest_bytes)
    inventory_sha256 = sha256_bytes(manifest_bytes)
    for path in (
        release / "summary.json",
        release / "checkpoint.json",
        release / "completion_receipt.json",
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["inventory_sha256"] = inventory_sha256
        path.write_bytes(canonical_json_bytes(payload))
    family_path = (
        release
        / "family_receipts"
        / f"{HFDL_MIGRATION_FAMILY}.json"
    )
    family = json.loads(family_path.read_text(encoding="utf-8"))
    family["inventory_sha256"] = inventory_sha256
    family["family_manifest_sha256"] = inventory_sha256
    family_path.write_bytes(canonical_json_bytes(family))


def _publish_fixture(
    tmp_path: Path,
    *,
    pairs: tuple[tuple[str, str], ...] = (("AAA", "AAA"), ("BBB", "BBB")),
) -> tuple[Path, Path, SyntheticOnlyPermit, object]:
    release = _completed_migration_release(tmp_path / "migration", pairs)
    accepted = tmp_path / "accepted"
    work = tmp_path / "derived"
    permit = _permit("hfdl-legacy-publisher")
    contract = HfdlPublishContract.synthetic_fixture(len(pairs), permit=permit)
    result = publish_hfdl_legacy_discovery(
        migration_release_directory=release,
        accepted_release_root=accepted,
        derived_work_root=work,
        created_at=CREATED_AT,
        contract=contract,
        **_publication_kwargs(tmp_path, contract),
    )
    return accepted, work, permit, result


def test_hfdl_publication_requires_explicit_authorization_before_mutation(
    hfdl_tmp: Path,
) -> None:
    release = _completed_migration_release(hfdl_tmp / "migration")
    accepted = hfdl_tmp / "accepted"
    work = hfdl_tmp / "work"
    with pytest.raises(PermissionError, match="one-shot refresh authorization"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=accepted,
            derived_work_root=work,
            created_at=CREATED_AT,
            contract=HfdlPublishContract.production(),
        )
    assert not accepted.exists()
    assert not work.exists()

    permit = _permit("hfdl-publication-guard")
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    with pytest.raises(ContractError, match="synthetic-only permit"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=accepted,
            derived_work_root=work,
            created_at=CREATED_AT,
            contract=contract,
        )
    assert not accepted.exists()
    assert not work.exists()


def test_publisher_splits_physical_epochs_binds_denominators_and_reruns_deterministically(
    hfdl_tmp: Path,
) -> None:
    tmp_path = hfdl_tmp
    accepted, _work, permit, result = _publish_fixture(tmp_path)
    with pytest.raises(ContractError, match="fixture permit"):
        verify_hfdl_legacy_publication(
            result.epoch_set_release_directory,
            accepted_release_root=accepted,
        )
    verified = verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    assert verified == result

    all_epochs: dict[str, set[str]] = {}
    for epoch in HFDL_EPOCHS:
        directory = result.epoch_release_directories[epoch]
        assert directory.parent.name == EPOCH_DATASETS[epoch]
        census = json.loads((directory / "census.json").read_text(encoding="utf-8"))
        assert census["denominator_counts"] == {
            "approved_input_pairs": 2,
            "approved_input_symbols": 2,
            "source_rows_all_epochs": 4,
            "source_rows_this_epoch": 2,
            "symbols_with_rows_this_epoch": 2,
            "symbols_without_rows_this_epoch": 0,
            "unique_sessions_this_epoch": 1,
            "symbol_session_rows_this_epoch": 2,
        }
        rows = []
        for parquet in sorted((directory / "data").glob("*.parquet")):
            rows.extend(pq.read_table(parquet).to_pylist())
        all_epochs[epoch] = {row["source_epoch"] for row in rows}
        assert {row["evidence_class"] for row in rows} == {"LEGACY_DISCOVERY"}
        assert {row["point_in_time_safe"] for row in rows} == {False}
        assert {row["point_in_time_state"] for row in rows} == {HFDL_POINT_IN_TIME_STATE}
        assert {row["historical_availability_state"] for row in rows} == {
            HFDL_HISTORICAL_AVAILABILITY
        }
    assert all_epochs == {epoch: {epoch} for epoch in HFDL_EPOCHS}

    rerun_contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    rerun = publish_hfdl_legacy_discovery(
        migration_release_directory=_completed_migration_release(tmp_path / "migration"),
        accepted_release_root=accepted,
        derived_work_root=tmp_path / "derived",
        created_at=CREATED_AT,
        contract=rerun_contract,
        **_publication_kwargs(tmp_path, rerun_contract),
    )
    assert rerun == result


def test_publisher_uses_canonical_sidecar_validation_and_ignores_extensions(
    hfdl_tmp: Path,
) -> None:
    tmp_path = hfdl_tmp
    release = _completed_migration_release(
        tmp_path / "migration",
        pairs=(("AAA", "AAA"),),
        sidecar_updates={
            "vendor_extension": {
                "row_count": "untrusted",
                "validation_passed": False,
            }
        },
    )
    permit = _permit("hfdl-sidecar-extension")
    contract = HfdlPublishContract.synthetic_fixture(1, permit=permit)
    result = publish_hfdl_legacy_discovery(
        migration_release_directory=release,
        accepted_release_root=tmp_path / "accepted",
        derived_work_root=tmp_path / "derived",
        created_at=CREATED_AT,
        contract=contract,
        **_publication_kwargs(tmp_path, contract),
    )
    verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=tmp_path / "accepted",
        synthetic_permit=permit,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeframe", True),
        ("version", 7),
        ("validation_passed", 1),
        (
            "source_limitations",
            ["fixed snapshot", "March 2022 split", False],
        ),
    ],
)
def test_publisher_rejects_sidecars_rejected_by_the_canonical_validator(
    hfdl_tmp: Path,
    field: str,
    value: object,
) -> None:
    tmp_path = hfdl_tmp
    release = _completed_migration_release(
        tmp_path / f"migration-{field}",
        pairs=(("AAA", "AAA"),),
        sidecar_updates={field: value},
    )
    permit = _permit(f"hfdl-sidecar-invalid-{field}")
    contract = HfdlPublishContract.synthetic_fixture(1, permit=permit)

    with pytest.raises(ContractError):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=tmp_path / "accepted",
            derived_work_root=tmp_path / "derived",
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(tmp_path, contract),
        )

    assert not any(
        path.name == "release_manifest.json"
        for path in (tmp_path / "accepted").rglob("*")
    )


def test_failed_capsule_materialization_is_quarantined_and_never_reused(
    hfdl_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = hfdl_tmp
    release = _completed_migration_release(
        tmp_path / "migration",
        pairs=(("AAA", "AAA"),),
    )
    accepted = tmp_path / "accepted"
    work = tmp_path / "derived"
    permit = _permit("hfdl-materialization-quarantine")
    contract = HfdlPublishContract.synthetic_fixture(1, permit=permit)
    original_writer = publisher_module.write_tagged_hfdl_legacy_epochs

    def fail_after_partial_evidence(result, output_root):
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "diagnostic.partial").write_bytes(b"retain-exact-bytes")
        raise RuntimeError("synthetic partial capsule failure")

    monkeypatch.setattr(
        publisher_module,
        "write_tagged_hfdl_legacy_epochs",
        fail_after_partial_evidence,
    )
    with pytest.raises(RuntimeError, match="partial capsule failure"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=accepted,
            derived_work_root=work,
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(tmp_path, contract),
        )

    receipts = list(
        (work / "hfdl").glob(
            "*/.quarantine/*/quarantine_receipt.json"
        )
    )
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipts[0].read_bytes() == canonical_json_bytes(receipt)
    assert receipt["reason"] == "MATERIALIZATION_FAILED"
    assert receipt["retention"] == (
        "INDEFINITE_UNTIL_EXPLICIT_OWNER_DISPOSITION"
    )
    assert receipt["direct_reuse_or_promotion"] == "PROHIBITED"
    assert receipt["payload_manifest_sha256"] == sha256_bytes(
        canonical_json_bytes(receipt["payload_manifest"])
    )
    payload = receipts[0].parent / "payload"
    assert (payload / "split" / "diagnostic.partial").read_bytes() == (
        b"retain-exact-bytes"
    )
    assert not list(receipts[0].parents[2].glob("pairs/.pending-*"))
    receipt_bytes = receipts[0].read_bytes()

    monkeypatch.setattr(
        publisher_module,
        "write_tagged_hfdl_legacy_epochs",
        original_writer,
    )
    result = publish_hfdl_legacy_discovery(
        migration_release_directory=release,
        accepted_release_root=accepted,
        derived_work_root=work,
        created_at=CREATED_AT,
        contract=contract,
        **_publication_kwargs(tmp_path, contract),
    )
    verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    assert receipts[0].read_bytes() == receipt_bytes
    assert (payload / "split" / "diagnostic.partial").read_bytes() == (
        b"retain-exact-bytes"
    )


def test_publisher_resumes_after_capsule_crash_without_partial_accepted_release(
    hfdl_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path = hfdl_tmp
    release = _completed_migration_release(tmp_path / "migration")
    accepted = tmp_path / "accepted"
    work = tmp_path / "derived"
    permit = _permit("hfdl-restart")
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    original = publisher_module._materialize_pair_capsule
    calls = 0

    def crash_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic capsule crash")
        return original(*args, **kwargs)

    monkeypatch.setattr(publisher_module, "_materialize_pair_capsule", crash_second)
    with pytest.raises(RuntimeError, match="synthetic capsule crash"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=accepted,
            derived_work_root=work,
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(tmp_path, contract),
        )
    assert not any(path.name == "release_manifest.json" for path in accepted.rglob("*"))
    checkpoints = list((work / "hfdl").glob("*/checkpoint.json"))
    assert len(checkpoints) == 1
    checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
    assert len(checkpoint["completed_capsules"]) == 1
    interrupted = checkpoints[0].parent / "pairs" / ".pending-interrupted"
    (interrupted / "split").mkdir(parents=True)
    (interrupted / "split" / "partial.bin").write_bytes(b"interrupted-evidence")

    monkeypatch.setattr(publisher_module, "_materialize_pair_capsule", original)
    result = publish_hfdl_legacy_discovery(
        migration_release_directory=release,
        accepted_release_root=accepted,
        derived_work_root=work,
        created_at=CREATED_AT,
        contract=contract,
        **_publication_kwargs(tmp_path, contract),
    )
    verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )
    checkpoint = json.loads(checkpoints[0].read_text(encoding="utf-8"))
    assert checkpoint["state"] == "RELEASES_COMPLETE"
    assert len(checkpoint["completed_capsules"]) == 2
    quarantine_receipts = list(
        checkpoints[0].parent.glob(
            ".quarantine/*/quarantine_receipt.json"
        )
    )
    assert len(quarantine_receipts) == 1
    quarantine = json.loads(
        quarantine_receipts[0].read_text(encoding="utf-8")
    )
    assert quarantine["reason"] == "INTERRUPTED_BEFORE_RETRY"
    assert (
        quarantine_receipts[0].parent
        / "payload"
        / "split"
        / "partial.bin"
    ).read_bytes() == b"interrupted-evidence"
    assert not interrupted.exists()


def test_completed_migration_verification_fails_closed_on_mutation_extra_and_hardlink(
    hfdl_tmp: Path,
) -> None:
    tmp_path = hfdl_tmp
    mutation = _completed_migration_release(tmp_path / "mutation")
    target = next(path for path in (mutation / "payload").rglob("*") if path.is_file())
    target.write_bytes(target.read_bytes() + b"mutated")
    with pytest.raises(IntegrityError, match="changed, linked, or is incomplete"):
        verify_completed_migration_release(mutation)

    extra = _completed_migration_release(tmp_path / "extra")
    (extra / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(IntegrityError, match="missing/extra/linked"):
        verify_completed_migration_release(extra)

    linked = _completed_migration_release(tmp_path / "linked")
    target = next(path for path in (linked / "payload").rglob("*") if path.is_file())
    hardlink = target.with_name("unexpected-hardlink.bin")
    try:
        os.link(target, hardlink)
    except OSError:
        pytest.skip("hardlinks are unavailable on this test filesystem")
    with pytest.raises((ContractError, IntegrityError), match="linked|independent|missing/extra"):
        verify_completed_migration_release(linked)


@pytest.mark.parametrize(
    "field",
    ["migration_id", "source", "destination"],
)
@pytest.mark.parametrize(
    "value",
    [True, 7, None, ["coercible"], {"value": "coercible"}],
)
def test_completed_migration_rejects_non_string_identity_and_provenance(
    hfdl_tmp: Path,
    field: str,
    value: object,
) -> None:
    release = _completed_migration_release(
        hfdl_tmp / f"strict-{field}-{type(value).__name__}"
    )
    _poison_completed_migration_entry(
        release,
        field=field,
        value=value,
    )

    with pytest.raises(IntegrityError, match="must be exact strings"):
        verify_completed_migration_release(release)


def test_completed_migration_consumes_sealed_objects_and_rejects_old_layouts(
    hfdl_tmp: Path,
) -> None:
    sealed = _completed_migration_release(
        hfdl_tmp / "sealed", sealed_non_derived_objects=True
    )
    first = json.loads(
        (sealed / "migration_files.jsonl").read_bytes().splitlines()[0]
    )
    relative = f"{PAYLOAD_PREFIX}/{Path(str(first['destination'])).name}"
    assert first["payload_object"] != migration_payload_object_relative(
        relative, str(first["sha256"])
    )
    verify_completed_migration_release(sealed)

    nested = _completed_migration_release(hfdl_tmp / "old-nested")
    old_entry = json.loads(
        (nested / "migration_files.jsonl").read_bytes().splitlines()[0]
    )
    flat_path = nested / "payload" / str(old_entry["payload_object"])
    old_path = (
        nested
        / "payload"
        / PAYLOAD_PREFIX
        / Path(str(old_entry["destination"])).name
    )
    old_path.parent.mkdir(parents=True, exist_ok=True)
    flat_path.replace(old_path)
    with pytest.raises(IntegrityError, match="changed, linked, or is incomplete"):
        verify_completed_migration_release(nested)

    unversioned = _completed_migration_release(hfdl_tmp / "unversioned")
    summary_path = unversioned / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary.pop("payload_layout_version")
    summary_path.write_bytes(canonical_json_bytes(summary))
    with pytest.raises(IntegrityError, match="fields differ"):
        verify_completed_migration_release(unversioned)


def test_completed_migration_recomputes_implementation_aggregate_hash(
    hfdl_tmp: Path,
) -> None:
    release = _completed_migration_release(hfdl_tmp / "implementation-binding")
    for path in (
        release / "summary.json",
        release / "checkpoint.json",
        release / "completion_receipt.json",
        release / "family_receipts" / f"{HFDL_MIGRATION_FAMILY}.json",
    ):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["migration_implementation_sha256"] = "0" * 64
        path.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(IntegrityError, match="aggregate hash differs"):
        verify_completed_migration_release(release)


def test_partial_two_epoch_publication_has_no_complete_set_and_resumes(
    hfdl_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _completed_migration_release(hfdl_tmp / "migration")
    accepted = hfdl_tmp / "accepted"
    work = hfdl_tmp / "derived"
    permit = _permit("hfdl-release-resume")
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    original = publisher_module.AtomicReleasePublisher.publish
    calls = 0

    def fail_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic second epoch publish crash")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(publisher_module.AtomicReleasePublisher, "publish", fail_second)
    with pytest.raises(RuntimeError, match="second epoch publish crash"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=release,
            accepted_release_root=accepted,
            derived_work_root=work,
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(hfdl_tmp, contract),
        )
    assert not (accepted / SET_DATASET).exists()

    monkeypatch.setattr(publisher_module.AtomicReleasePublisher, "publish", original)
    result = publish_hfdl_legacy_discovery(
        migration_release_directory=release,
        accepted_release_root=accepted,
        derived_work_root=work,
        created_at=CREATED_AT,
        contract=contract,
        **_publication_kwargs(hfdl_tmp, contract),
    )
    verify_hfdl_legacy_publication(
        result.epoch_set_release_directory,
        accepted_release_root=accepted,
        synthetic_permit=permit,
    )


def test_pair_completeness_duplicate_symbols_and_authority_binding_fail_closed(
    hfdl_tmp: Path,
) -> None:
    tmp_path = hfdl_tmp
    permit = _permit("hfdl-adversarial")
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    missing = _completed_migration_release(tmp_path / "missing", omit_sidecar_for="BBB")
    with pytest.raises(IntegrityError, match="exactly 2 Parquet/sidecar pairs"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=missing,
            accepted_release_root=tmp_path / "accepted-missing",
            derived_work_root=tmp_path / "work-missing",
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(tmp_path, contract),
        )

    duplicate = _completed_migration_release(
        tmp_path / "duplicate",
        (("AAA", "AAA"), ("DUP", "AAA")),
    )
    duplicate_accepted = tmp_path / "accepted-duplicate"
    with pytest.raises(IntegrityError, match="duplicate canonical symbols"):
        publish_hfdl_legacy_discovery(
            migration_release_directory=duplicate,
            accepted_release_root=duplicate_accepted,
            derived_work_root=tmp_path / "work-duplicate",
            created_at=CREATED_AT,
            contract=contract,
            **_publication_kwargs(tmp_path, contract),
        )
    assert not any(path.name == "release_manifest.json" for path in duplicate_accepted.rglob("*"))

    wrong_authority = _completed_migration_release(
        tmp_path / "wrong-authority",
        authorization_registry_id="f" * 64,
    )
    with pytest.raises(IntegrityError, match="exact user-task authority"):
        verify_completed_migration_release(wrong_authority)
