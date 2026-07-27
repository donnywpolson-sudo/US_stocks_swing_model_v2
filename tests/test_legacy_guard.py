from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_file
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.legacy_guard import (
    _porcelain_v1_z_record_count,
    capture_legacy_baseline,
    load_legacy_baseline,
    verify_legacy_baseline,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(root, "add", "tracked.txt")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "baseline")
    (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    return root


def test_verify_is_read_only_and_detects_content_mutation(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    expected = capture_legacy_baseline(root)
    config = tmp_path / "baseline.json"
    config.write_bytes(canonical_json_bytes(expected) + b"\n")
    index = root / ".git" / "index"
    before = (sha256_file(index), capture_legacy_baseline(root))
    assert verify_legacy_baseline(config) == expected
    assert (sha256_file(index), capture_legacy_baseline(root)) == before
    (root / "untracked.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="legacy worktree changed"):
        verify_legacy_baseline(config)


def test_checked_in_legacy_baseline_is_an_authenticated_historical_capture() -> None:
    config = Path(__file__).parents[1] / "config" / "legacy_baseline.json"
    observed = load_legacy_baseline(config)
    assert observed["head"] == "beab97d89a527a04c3640ba3cc70c2f9493044cc"
    assert observed["status_count"] == 31


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (b"", 0),
        (b" M tracked.txt\0?? untracked.txt\0", 2),
        (b"R  renamed.txt\0original.txt\0?? untracked.txt\0", 2),
        (b" C copied.txt\0original.txt\0", 1),
    ],
)
def test_porcelain_status_count_uses_logical_records(
    status: bytes,
    expected: int,
) -> None:
    assert _porcelain_v1_z_record_count(status) == expected


@pytest.mark.parametrize(
    "status",
    [
        b" M missing-terminal-nul",
        b"bad\0",
        b"R  renamed.txt\0",
    ],
)
def test_porcelain_status_count_rejects_malformed_records(status: bytes) -> None:
    with pytest.raises(IntegrityError, match="status"):
        _porcelain_v1_z_record_count(status)


def test_capture_counts_git_rename_as_one_logical_record(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _git(root, "mv", "tracked.txt", "renamed.txt")
    observed = capture_legacy_baseline(root)
    assert observed["status_count"] == 2
