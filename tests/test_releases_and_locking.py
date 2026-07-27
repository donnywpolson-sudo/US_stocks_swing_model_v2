from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError, IntegrityError, LockHeldError
from us_stocks_swing_model_v2 import common as common_module
from us_stocks_swing_model_v2.common import (
    atomic_write,
    atomic_write_new,
    canonical_json_bytes,
    sha256_bytes,
)
from us_stocks_swing_model_v2.locking import ExclusiveFileLock
from us_stocks_swing_model_v2 import locking as locking_module
from us_stocks_swing_model_v2 import releases as releases_module
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    ReleaseFile,
    ReleaseManifest,
    build_manifest,
    verify_release,
)


def _manifest(
    stage: Path,
    relative_paths: tuple[str, ...] = ("a.json", "nested/b.bin"),
):
    return build_manifest(
        stage,
        relative_paths,
        project="US_stocks_swing_model_v2",
        dataset="synthetic_bars",
        source_epoch="fixture_v1",
        role="active_historical",
        quality_state="PASS",
        created_at="2026-07-15T00:00:00Z",
        row_count=2,
        event_start="2026-07-01",
        event_end="2026-07-02",
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
    )


def _direct_manifest(relative_path: str, raw: bytes) -> ReleaseManifest:
    entry = ReleaseFile(
        path=relative_path,
        size=len(raw),
        sha256=sha256_bytes(raw),
    )
    unsigned = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "dataset": "synthetic_bars",
        "source_epoch": "fixture_v1",
        "role": "active_historical",
        "quality_state": "PASS",
        "created_at": "2026-07-15T00:00:00Z",
        "row_count": 1,
        "event_start": "2026-07-01",
        "event_end": "2026-07-01",
        "upstream_release_ids": [],
        "schema_fingerprint": "1" * 64,
        "code_hash": "2" * 64,
        "config_hash": "3" * 64,
        "environment_hash": "4" * 64,
        "files": [entry.as_dict()],
    }
    return ReleaseManifest(
        schema_version=1,
        project="US_stocks_swing_model_v2",
        dataset="synthetic_bars",
        source_epoch="fixture_v1",
        role="active_historical",
        quality_state="PASS",
        created_at="2026-07-15T00:00:00Z",
        row_count=1,
        event_start="2026-07-01",
        event_end="2026-07-01",
        upstream_release_ids=(),
        schema_fingerprint="1" * 64,
        code_hash="2" * 64,
        config_hash="3" * 64,
        environment_hash="4" * 64,
        files=(entry,),
        release_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "stage"
    (stage / "nested").mkdir(parents=True)
    (stage / "a.json").write_text('{"x":1}\n', encoding="utf-8")
    (stage / "nested" / "b.bin").write_bytes(b"two")
    return stage


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"directory links unavailable: {result.stderr.strip()}")


def test_atomic_file_publication_syncs_parent_directory_and_propagates_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[Path] = []
    monkeypatch.setattr(
        common_module,
        "_fsync_directory",
        lambda path: observed.append(Path(path)),
    )
    replaced = tmp_path / "replace.json"
    atomic_write(replaced, b"replace")
    assert replaced.read_bytes() == b"replace"
    assert observed == [tmp_path]

    observed.clear()
    created = tmp_path / "new.json"
    atomic_write_new(created, b"new")
    assert created.read_bytes() == b"new"
    assert observed == [tmp_path, tmp_path]

    def fail_sync(path: Path) -> None:
        raise OSError("synthetic directory durability failure")

    monkeypatch.setattr(common_module, "_fsync_directory", fail_sync)
    with pytest.raises(OSError, match="directory durability"):
        atomic_write(
            tmp_path / "durability-failure.json",
            b"visible-but-uncommitted",
        )


def test_atomic_publication_is_content_addressed_idempotent_and_detects_mutation(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    manifest = _manifest(stage)
    publisher = AtomicReleasePublisher(tmp_path / "releases")

    published = publisher.publish(stage, manifest)
    assert published.name == manifest.release_id
    assert publisher.publish(stage, manifest) == published
    assert verify_release(published) == manifest

    (published / "a.json").write_text('{"x":2}\n', encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        verify_release(published)


def test_publication_rejects_linked_dataset_root(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    release_root = tmp_path / "releases"
    outside = tmp_path / "outside"
    release_root.mkdir()
    outside.mkdir()
    _symlink_or_skip(release_root / "synthetic_bars", outside)

    with pytest.raises(ContractError, match="(links|junction/reparse points) are prohibited"):
        AtomicReleasePublisher(release_root).publish(stage, _manifest(stage))


def test_publication_rejects_linked_destination(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    manifest = _manifest(stage)
    dataset_root = tmp_path / "releases" / manifest.dataset
    outside = tmp_path / "outside"
    dataset_root.mkdir(parents=True)
    outside.mkdir()
    _symlink_or_skip(dataset_root / manifest.release_id, outside)

    with pytest.raises(ContractError, match="(links|junction/reparse points) are prohibited"):
        AtomicReleasePublisher(tmp_path / "releases").publish(stage, manifest)


def test_publication_rejects_linked_pending_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = _stage(tmp_path)
    manifest = _manifest(stage)
    dataset_root = tmp_path / "releases" / manifest.dataset
    outside = tmp_path / "outside"
    dataset_root.mkdir(parents=True)
    outside.mkdir()
    fixed_hex = "12345678" + ("0" * 24)
    pending = dataset_root / f".pending-{manifest.release_id[:12]}-12345678"
    _symlink_or_skip(pending, outside)
    monkeypatch.setattr(
        releases_module.uuid,
        "uuid4",
        lambda: type("FixedUuid", (), {"hex": fixed_hex})(),
    )

    with pytest.raises(ContractError, match="(links|junction/reparse points) are prohibited"):
        AtomicReleasePublisher(tmp_path / "releases").publish(stage, manifest)


def test_publication_rejects_escaping_linked_staging_ancestor_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    outside = tmp_path / "outside"
    stage.mkdir()
    outside.mkdir()
    raw = b"outside-authenticated-bytes"
    (outside / "payload.bin").write_bytes(raw)
    _symlink_or_skip(stage / "linked", outside)
    manifest = _direct_manifest("linked/payload.bin", raw)
    monkeypatch.setattr(
        releases_module,
        "sha256_file",
        lambda _path: pytest.fail(
            "escaping linked source reached file hashing"
        ),
    )
    release_root = tmp_path / "releases"

    with pytest.raises(
        ContractError,
        match="(links|junction/reparse points) are prohibited",
    ):
        AtomicReleasePublisher(release_root).publish(stage, manifest)

    assert (outside / "payload.bin").read_bytes() == raw
    dataset_root = release_root / manifest.dataset
    assert not (dataset_root / manifest.release_id).exists()
    assert not list(dataset_root.glob(".pending-*"))


def test_manifest_rejects_undeclared_extra_payload(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "nested").mkdir(parents=True)
    (stage / "a.json").write_text("{}", encoding="utf-8")
    (stage / "nested" / "b.bin").write_bytes(b"b")
    manifest = _manifest(stage)
    published = AtomicReleasePublisher(tmp_path / "releases").publish(stage, manifest)
    (published / "stray.parquet").write_bytes(b"poison")
    with pytest.raises(IntegrityError, match="extra"):
        verify_release(published)


def test_manifest_rejects_undeclared_empty_directory(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "nested").mkdir(parents=True)
    (stage / "a.json").write_text("{}", encoding="utf-8")
    (stage / "nested" / "b.bin").write_bytes(b"b")
    published = AtomicReleasePublisher(tmp_path / "releases").publish(stage, _manifest(stage))
    (published / "undeclared-empty").mkdir()
    with pytest.raises(IntegrityError, match="extra_dirs"):
        verify_release(published)


@pytest.mark.parametrize(
    "relative_paths",
    (
        ("a.json", "a.json"),
        ("nested/b.bin", r"nested\b.bin"),
    ),
)
def test_manifest_rejects_duplicate_normalized_caller_paths_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_paths: tuple[str, str],
) -> None:
    stage = _stage(tmp_path)
    monkeypatch.setattr(
        releases_module,
        "sha256_file",
        lambda _path: pytest.fail(
            "duplicate census reached file hashing"
        ),
    )
    with pytest.raises(
        ContractError,
        match="unique after normalization",
    ):
        _manifest(stage, relative_paths)


def test_unique_manifest_input_preserves_deterministic_release_identity(
    tmp_path: Path,
) -> None:
    stage = _stage(tmp_path)
    manifest = _manifest(
        stage,
        ("nested/b.bin", "a.json"),
    )
    assert manifest.release_id == (
        "584eeead94178b894c704d32d4944d335e394a64dabde1c68a303ac96a6d64cd"
    )
    assert tuple(entry.path for entry in manifest.files) == (
        "a.json",
        "nested/b.bin",
    )


@pytest.mark.parametrize(
    "role",
    (
        "active_historical",
        "prospective_as_received",
        "derived_causal",
        "feature_only",
        "outcome_only",
    ),
)
def test_pass_release_rejects_empty_payload_census(
    tmp_path: Path,
    role: str,
) -> None:
    stage = tmp_path / f"stage-{role}"
    stage.mkdir()
    with pytest.raises(ContractError, match="nonempty"):
        build_manifest(
            stage,
            [],
            project="US_stocks_swing_model_v2",
            dataset=f"empty_{role}",
            source_epoch="fixture_v1",
            role=role,
            quality_state="PASS",
            created_at="2026-07-15T00:00:00Z",
            row_count=0,
            event_start=None,
            event_end=None,
            schema_fingerprint="1" * 64,
            code_hash="2" * 64,
            config_hash="3" * 64,
            environment_hash="4" * 64,
        )

    unsigned = {
        "schema_version": 1,
        "project": "US_stocks_swing_model_v2",
        "dataset": f"empty_{role}",
        "source_epoch": "fixture_v1",
        "role": role,
        "quality_state": "PASS",
        "created_at": "2026-07-15T00:00:00Z",
        "row_count": 0,
        "event_start": None,
        "event_end": None,
        "upstream_release_ids": [],
        "schema_fingerprint": "1" * 64,
        "code_hash": "2" * 64,
        "config_hash": "3" * 64,
        "environment_hash": "4" * 64,
        "files": [],
    }
    release = tmp_path / f"release-{role}"
    release.mkdir()
    (release / "release_manifest.json").write_bytes(
        canonical_json_bytes(
            {
                **unsigned,
                "release_id": sha256_bytes(canonical_json_bytes(unsigned)),
            }
        )
    )
    with pytest.raises(IntegrityError, match="schema"):
        verify_release(release)


def test_release_rejects_hardlinked_authoritative_manifest(tmp_path: Path) -> None:
    stage = _stage(tmp_path)
    published = AtomicReleasePublisher(tmp_path / "releases").publish(
        stage,
        _manifest(stage),
    )
    manifest_path = published / "release_manifest.json"
    try:
        os.link(manifest_path, tmp_path / "manifest-hardlink.json")
    except OSError as exc:
        pytest.skip(f"hardlinks are unavailable on this test filesystem: {exc}")
    with pytest.raises(IntegrityError, match="manifest"):
        verify_release(published)


def test_lock_is_non_stealing_and_owned(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    first = ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
    try:
        with pytest.raises(LockHeldError):
            ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
        assert first._descriptor is not None
        os.lseek(first._descriptor, 0, os.SEEK_SET)
        payload = json.loads(os.read(first._descriptor, 65536).decode("utf-8"))
        assert payload["token"] == first.token
    finally:
        first.release()
    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows delete-sharing contract")
def test_windows_lock_path_cannot_be_deleted_or_replaced_while_held(
    tmp_path: Path,
) -> None:
    path = tmp_path / "writer.lock"
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("replacement", encoding="utf-8")
    lock = ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
    try:
        with pytest.raises(PermissionError):
            path.unlink()
        with pytest.raises(PermissionError):
            replacement.replace(path)
        with pytest.raises(LockHeldError):
            ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
    finally:
        lock.release()
    assert not path.exists()
    assert replacement.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle deletion contract")
def test_windows_failed_acquisition_retires_exact_handle_before_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "writer.lock"
    replacement = tmp_path / "replacement.lock"
    replacement.write_text("replacement", encoding="utf-8")
    original_mark = locking_module._mark_open_file_for_deletion
    observed: list[tuple[int, int]] = []

    def fail_fsync(descriptor: int) -> None:
        raise OSError("synthetic acquisition durability failure")

    def mark_exact_handle(descriptor: int) -> None:
        observed.append(
            (
                os.fstat(descriptor).st_ino,
                os.stat(path, follow_symlinks=False).st_ino,
            )
        )
        with pytest.raises(PermissionError):
            replacement.replace(path)
        original_mark(descriptor)

    def prohibit_path_unlink(
        self: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        raise AssertionError(
            "failed acquisition must not unlink a pathname after handle close"
        )

    monkeypatch.setattr(locking_module.os, "fsync", fail_fsync)
    monkeypatch.setattr(
        locking_module,
        "_mark_open_file_for_deletion",
        mark_exact_handle,
    )
    monkeypatch.setattr(Path, "unlink", prohibit_path_unlink)

    with pytest.raises(OSError, match="durability failure"):
        ExclusiveFileLock(path, allowed_root=tmp_path).acquire()

    assert observed and observed[0][0] == observed[0][1]
    assert not path.exists()
    assert replacement.read_text(encoding="utf-8") == "replacement"


def test_lock_requires_path_within_caller_approved_root(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    approved.mkdir()
    with pytest.raises(ContractError, match="escapes its approved root"):
        ExclusiveFileLock(
            tmp_path / "outside.lock",
            allowed_root=approved,
        ).acquire()


def test_lock_rejects_linked_path_component(tmp_path: Path) -> None:
    approved = tmp_path / "approved"
    outside = tmp_path / "outside"
    approved.mkdir()
    outside.mkdir()
    linked_parent = approved / "linked"
    _symlink_or_skip(linked_parent, outside)

    with pytest.raises(
        ContractError,
        match="(links|junction/reparse points) are prohibited",
    ):
        ExclusiveFileLock(
            linked_parent / "writer.lock",
            allowed_root=approved,
        ).acquire()


def test_lock_release_refuses_changed_pathname_identity(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    lock = ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
    displaced = tmp_path / "displaced.lock"
    try:
        path.replace(displaced)
    except PermissionError as exc:
        lock.release()
        pytest.skip(f"platform prevents replacement of an open lock: {exc}")
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "created_at": "2026-07-15T00:00:00Z",
                "token": lock.token,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LockHeldError, match="identity changed"):
        lock.release()
    assert path.exists()
    assert displaced.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows exact-handle deletion contract")
def test_lock_release_deletes_exact_open_handle_without_path_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "writer.lock"
    lock = ExclusiveFileLock(path, allowed_root=tmp_path).acquire()
    original = locking_module._mark_open_file_for_deletion
    observed: list[tuple[int, int]] = []

    def mark_exact_handle(descriptor: int) -> None:
        observed.append(
            (
                os.fstat(descriptor).st_ino,
                os.stat(path, follow_symlinks=False).st_ino,
            )
        )
        original(descriptor)

    def prohibit_path_unlink(self: Path, *args: object, **kwargs: object) -> None:
        raise AssertionError("release must not unlink a pathname after closing its handle")

    monkeypatch.setattr(locking_module, "_mark_open_file_for_deletion", mark_exact_handle)
    monkeypatch.setattr(Path, "unlink", prohibit_path_unlink)
    lock.release()

    assert observed and observed[0][0] == observed[0][1]
    assert not path.exists()


def test_posix_retirement_unlinks_only_after_identity_authentication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "writer.lock"
    retired = tmp_path / ".released-writer.lock-token-fixed"
    events: list[str] = []
    linked = type(
        "Metadata",
        (),
        {"st_dev": 7, "st_ino": 11, "st_nlink": 1},
    )()
    detached = type(
        "Metadata",
        (),
        {"st_dev": 7, "st_ino": 11, "st_nlink": 0},
    )()
    monkeypatch.setattr(
        locking_module.uuid,
        "uuid4",
        lambda: type("FixedUuid", (), {"hex": "fixed"})(),
    )
    monkeypatch.setattr(
        locking_module,
        "require_contained_path",
        lambda *args, **kwargs: events.append("contained"),
    )
    monkeypatch.setattr(
        locking_module.os,
        "replace",
        lambda source, target: (
            events.append("replace"),
            target == retired,
        ),
    )
    monkeypatch.setattr(
        locking_module.os,
        "stat",
        lambda *args, **kwargs: (events.append("stat"), linked)[1],
    )
    monkeypatch.setattr(
        locking_module.os,
        "unlink",
        lambda target: events.append("unlink"),
    )
    monkeypatch.setattr(
        locking_module.os,
        "fstat",
        lambda descriptor: (events.append("fstat"), detached)[1],
    )

    locking_module._retire_posix_lock(
        path,
        descriptor=17,
        identity=(7, 11),
        allowed_root=tmp_path,
        token="token",
    )
    assert events == ["contained", "replace", "stat", "unlink", "fstat"]


@pytest.mark.skipif(os.name == "nt", reason="requires real POSIX unlink semantics")
def test_repeated_posix_lock_cycles_leave_no_released_files(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    for _ in range(20):
        with ExclusiveFileLock(path, allowed_root=tmp_path):
            assert path.exists()
    assert not path.exists()
    assert list(tmp_path.glob(".released-writer.lock-*")) == []


def test_orphan_recovery_quarantines_without_deleting(tmp_path: Path) -> None:
    pending = tmp_path / "releases" / "synthetic_bars" / ".pending-crash"
    pending.mkdir(parents=True)
    (pending / "partial.bin").write_bytes(b"partial")
    moved = AtomicReleasePublisher(tmp_path / "releases").quarantine_orphans("synthetic_bars")
    assert len(moved) == 1
    assert (moved[0] / "partial.bin").read_bytes() == b"partial"
    assert not pending.exists()
