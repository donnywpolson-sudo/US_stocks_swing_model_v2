from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError, IntegrityError, LockHeldError
from us_stocks_swing_model_v2.locking import ExclusiveFileLock
from us_stocks_swing_model_v2 import locking as locking_module
from us_stocks_swing_model_v2 import releases as releases_module
from us_stocks_swing_model_v2.releases import (
    AtomicReleasePublisher,
    build_manifest,
    verify_release,
)


def _manifest(stage: Path):
    return build_manifest(
        stage,
        ["a.json", "nested/b.bin"],
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


def test_orphan_recovery_quarantines_without_deleting(tmp_path: Path) -> None:
    pending = tmp_path / "releases" / "synthetic_bars" / ".pending-crash"
    pending.mkdir(parents=True)
    (pending / "partial.bin").write_bytes(b"partial")
    moved = AtomicReleasePublisher(tmp_path / "releases").quarantine_orphans("synthetic_bars")
    assert len(moved) == 1
    assert (moved[0] / "partial.bin").read_bytes() == b"partial"
    assert not pending.exists()
