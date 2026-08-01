from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_file
from us_stocks_swing_model_v2.directory_remediation import (
    APPROVED_CLEANUP_PLAN_ID,
    build_remediation_plan,
    execute_directory_remediation,
)
from us_stocks_swing_model_v2.errors import ContractError


def _fixture(root: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    (root / "config").mkdir()
    (root / "config" / "legacy_cleanup_policy.json").write_text(json.dumps({"target_roots": ["old"], "protected_prefixes": ["retain"]}), encoding="utf-8")
    target = root / "old" / "nested"
    target.mkdir(parents=True)
    package = root / "data" / "w" / "legacy_cleanup" / APPROVED_CLEANUP_PLAN_ID
    package.mkdir(parents=True)
    (package / "cleanup_plan.json").write_text("{}", encoding="utf-8")
    outcome = {"mode": "IRREVERSIBLE_PURGE_FAILED_NO_RETRY", "plan_id": APPROVED_CLEANUP_PLAN_ID, "remaining_count": 0, "completed_paths": [str(index) for index in range(70_728)]}
    (package / "purge_outcome.json").write_bytes(canonical_json_bytes(outcome))
    monkeypatch.setattr("us_stocks_swing_model_v2.directory_remediation.EXPECTED_DIRECTORY_COUNT", 2)
    return target, sha256_file(package / "purge_outcome.json")


def test_directory_remediation_plan_is_exact_and_rejects_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, outcome_hash = _fixture(tmp_path, monkeypatch)
    plan = build_remediation_plan(tmp_path)
    assert plan["directory_count"] == 2
    assert plan["failed_outcome_sha256"] == outcome_hash
    (target / "unexpected.bin").write_bytes(b"no")
    with pytest.raises(ContractError, match="empty"):
        build_remediation_plan(tmp_path)


def test_directory_remediation_removes_only_manifest_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, _ = _fixture(tmp_path, monkeypatch)
    plan = build_remediation_plan(tmp_path)
    package = tmp_path / "data" / "w" / "legacy_directory_remediation" / plan["remediation_plan_id"]
    package.mkdir(parents=True)
    plan_path = package / "remediation_plan.json"
    plan_path.write_bytes(canonical_json_bytes(plan))
    (package / "receipt.json").write_bytes(canonical_json_bytes({"remediation_plan_id": plan["remediation_plan_id"], "plan_sha256": sha256_file(plan_path)}))
    monkeypatch.setattr("us_stocks_swing_model_v2.directory_remediation.repository_binding", lambda root, expected_commit: {"commit": expected_commit, "tree": "a" * 40})
    result = execute_directory_remediation(tmp_path, plan_id=plan["remediation_plan_id"], expected_commit="b" * 40, confirmation=f"REMOVE_EMPTY_DIRECTORIES:{plan['remediation_plan_id']}", created_at="2026-08-01T00:00:00Z")
    assert result["removed_directory_count"] == 2
    assert not target.exists()


def test_directory_remediation_rejects_changed_failed_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, _ = _fixture(tmp_path, monkeypatch)
    outcome = tmp_path / "data" / "w" / "legacy_cleanup" / APPROVED_CLEANUP_PLAN_ID / "purge_outcome.json"
    outcome.write_bytes(canonical_json_bytes({"mode": "IRREVERSIBLE_PURGE_FAILED_NO_RETRY"}))
    with pytest.raises(ContractError, match="spent purge"):
        build_remediation_plan(tmp_path)


def test_directory_remediation_cli_module_has_an_entrypoint() -> None:
    source = (Path(__file__).parents[1] / "src" / "us_stocks_swing_model_v2" / "cli" / "directory_remediation.py").read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in source
