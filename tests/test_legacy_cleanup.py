from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_file
from us_stocks_swing_model_v2.legacy_cleanup import (
    APPROVED_CLEANUP_PLAN_ID,
    build_cleanup_plan,
    execute_purge,
    prepare_purge_execution,
    write_cleanup_plan,
)


def _policy(root: Path, targets: list[str], protected: list[str] | None = None) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "legacy_cleanup_policy.json").write_text(json.dumps({"mode": "PLAN_ONLY_NO_DELETION", "target_roots": targets, "protected_prefixes": protected or ["retain"]}), encoding="utf-8")


def test_cleanup_inventory_is_hash_bound_and_plan_only(tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"])
    plan = build_cleanup_plan(tmp_path)
    assert plan["file_count"] == 1
    assert plan["total_bytes"] == 3
    assert plan["execution_authorized"] is False
    assert (tmp_path / "old" / "item.bin").read_bytes() == b"old"


def test_cleanup_inventory_rejects_links_and_protected_paths(tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"], ["old/retain"])
    (tmp_path / "old" / "item.bin").rename(tmp_path / "old" / "retain")
    with pytest.raises(ContractError, match="protected"):
        build_cleanup_plan(tmp_path)


def test_cleanup_writer_reloads_and_rejects_census_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "item.bin").write_bytes(b"old")
    _policy(tmp_path, ["old"])
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.legacy_cleanup.repository_binding",
        lambda root, expected_commit: {"commit": expected_commit, "tree": "a" * 40},
    )
    result = write_cleanup_plan(tmp_path, expected_commit="b" * 40, created_at="2026-08-01T00:00:00Z")
    assert json.loads((Path(result["directory"]) / "cleanup_plan.json").read_text()) == result["plan"]
    assert result["receipt"]["execution_authorized"] is False
    with pytest.raises(ContractError, match="already exists"):
        write_cleanup_plan(tmp_path, expected_commit="b" * 40, created_at="2026-08-01T00:00:00Z")


def _purge_fixture(root: Path) -> Path:
    target = root / "legacy"
    target.mkdir()
    (target / "one.bin").write_bytes(b"one")
    _policy(root, ["legacy"], ["retain"])
    plan_path = root / "data" / "w" / "legacy_cleanup" / APPROVED_CLEANUP_PLAN_ID / "cleanup_plan.json"
    plan_path.parent.mkdir(parents=True)
    plan = {
        "cleanup_plan_id": APPROVED_CLEANUP_PLAN_ID,
        "policy_hash": sha256_file(root / "config" / "legacy_cleanup_policy.json"),
        "execution_authorized": False,
        "files": [{"path": "legacy/one.bin", "root": "legacy", "bytes": 3, "sha256": sha256_file(target / "one.bin")}],
    }
    plan_path.write_bytes(canonical_json_bytes(plan))
    (plan_path.parent / "receipt.json").write_bytes(canonical_json_bytes({"cleanup_plan_id": APPROVED_CLEANUP_PLAN_ID, "cleanup_plan_sha256": sha256_file(plan_path), "execution_authorized": False}))
    return target


def test_purge_executor_deletes_only_exact_synthetic_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _purge_fixture(tmp_path)
    (tmp_path / "retain").write_bytes(b"keep")
    monkeypatch.setattr("us_stocks_swing_model_v2.legacy_cleanup.repository_binding", lambda root, expected_commit: {"commit": expected_commit, "tree": "a" * 40})
    result = execute_purge(tmp_path, plan_id=APPROVED_CLEANUP_PLAN_ID, expected_executor_commit="b" * 40, owner_confirmation=f"IRREVERSIBLE_PURGE:{APPROVED_CLEANUP_PLAN_ID}", created_at="2026-08-01T00:00:00Z")
    assert result["deleted_file_count"] == 1
    assert not target.exists()
    assert (tmp_path / "retain").read_bytes() == b"keep"


def test_purge_preflight_rejects_unexpected_or_altered_files(tmp_path: Path) -> None:
    target = _purge_fixture(tmp_path)
    (target / "unexpected.bin").write_bytes(b"no")
    with pytest.raises(ContractError, match="census"):
        prepare_purge_execution(tmp_path, plan_id=APPROVED_CLEANUP_PLAN_ID)
    (target / "unexpected.bin").unlink()
    (target / "one.bin").write_bytes(b"changed")
    with pytest.raises(ContractError, match="hash"):
        prepare_purge_execution(tmp_path, plan_id=APPROVED_CLEANUP_PLAN_ID)


def test_purge_failure_writes_no_retry_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _purge_fixture(tmp_path)
    monkeypatch.setattr("us_stocks_swing_model_v2.legacy_cleanup.repository_binding", lambda root, expected_commit: {"commit": expected_commit, "tree": "a" * 40})
    original_rmdir = Path.rmdir
    def fail_target_rmdir(path: Path) -> None:
        if path == target:
            raise OSError("synthetic rmdir failure")
        original_rmdir(path)
    monkeypatch.setattr(Path, "rmdir", fail_target_rmdir)
    with pytest.raises(OSError, match="synthetic"):
        execute_purge(tmp_path, plan_id=APPROVED_CLEANUP_PLAN_ID, expected_executor_commit="b" * 40, owner_confirmation=f"IRREVERSIBLE_PURGE:{APPROVED_CLEANUP_PLAN_ID}", created_at="2026-08-01T00:00:00Z")
    outcome = json.loads((tmp_path / "data" / "w" / "legacy_cleanup" / APPROVED_CLEANUP_PLAN_ID / "purge_outcome.json").read_text())
    assert outcome["mode"] == "IRREVERSIBLE_PURGE_FAILED_NO_RETRY"
    assert outcome["completed_paths"] == ["legacy/one.bin"]
