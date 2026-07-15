from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import IntegrityError, LockHeldError
from us_stocks_swing_model_v2.locking import ExclusiveFileLock
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


def test_atomic_publication_is_content_addressed_idempotent_and_detects_mutation(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "nested").mkdir(parents=True)
    (stage / "a.json").write_text('{"x":1}\n', encoding="utf-8")
    (stage / "nested" / "b.bin").write_bytes(b"two")
    manifest = _manifest(stage)
    publisher = AtomicReleasePublisher(tmp_path / "releases")

    published = publisher.publish(stage, manifest)
    assert published.name == manifest.release_id
    assert publisher.publish(stage, manifest) == published
    assert verify_release(published) == manifest

    (published / "a.json").write_text('{"x":2}\n', encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        verify_release(published)


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
    first = ExclusiveFileLock(path).acquire()
    try:
        with pytest.raises(LockHeldError):
            ExclusiveFileLock(path).acquire()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["token"] == first.token
    finally:
        first.release()
    assert not path.exists()


def test_orphan_recovery_quarantines_without_deleting(tmp_path: Path) -> None:
    pending = tmp_path / "releases" / "synthetic_bars" / ".pending-crash"
    pending.mkdir(parents=True)
    (pending / "partial.bin").write_bytes(b"partial")
    moved = AtomicReleasePublisher(tmp_path / "releases").quarantine_orphans("synthetic_bars")
    assert len(moved) == 1
    assert (moved[0] / "partial.bin").read_bytes() == b"partial"
    assert not pending.exists()
