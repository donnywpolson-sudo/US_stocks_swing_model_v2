"""One-shot, directory-only remediation for the spent legacy purge plan."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .common import atomic_write, atomic_write_new, canonical_json_bytes, require_contained_path, sha256_bytes, sha256_file
from .errors import ContractError


POLICY_PATH = "config/legacy_cleanup_policy.json"
WORK_ROOT = "data/w/legacy_cleanup"
APPROVED_CLEANUP_PLAN_ID = "30c1dc0d506cdda1fc8d5349f27ae2b97bdba489f98a2e7c051650b471817f5a"
FAILED_EXECUTOR_COMMIT = "2d4c040ba769985baacbe5c3de5cd67bccf0f6e0"
EXPECTED_DIRECTORY_COUNT = 72
REMEDIATION_ROOT = "data/w/legacy_directory_remediation"
TIMEOUT_SECONDS = 300


def repository_binding(root: Path, *, expected_commit: str) -> dict[str, str]:
    def git(*args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip()

    if Path(git("rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise ContractError("cleanup repository root differs")
    if git("status", "--porcelain=v1"):
        raise ContractError("cleanup plan requires a clean repository")
    commit = git("rev-parse", "HEAD")
    if commit != expected_commit:
        raise ContractError("cleanup plan commit differs")
    return {"commit": commit, "tree": git("rev-parse", "HEAD^{tree}")}


def _normal(value: str) -> str:
    path = Path(value).as_posix()
    if path.startswith("/") or ".." in Path(path).parts:
        raise ContractError("directory remediation path escapes the repository")
    return path


def _failed_evidence(root: Path) -> tuple[dict[str, Any], str]:
    package = root / WORK_ROOT / APPROVED_CLEANUP_PLAN_ID
    outcome_path = package / "purge_outcome.json"
    plan_path = package / "cleanup_plan.json"
    if not outcome_path.is_file() or not plan_path.is_file():
        raise ContractError("spent purge evidence is incomplete")
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    if outcome.get("mode") != "IRREVERSIBLE_PURGE_FAILED_NO_RETRY" or outcome.get("plan_id") != APPROVED_CLEANUP_PLAN_ID or outcome.get("remaining_count") != 0 or len(outcome.get("completed_paths", ())) != 70_728:
        raise ContractError("spent purge outcome differs")
    return outcome, sha256_file(outcome_path)


def _directory_census(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    roots = tuple(_normal(str(value)) for value in policy["target_roots"])
    directories: set[str] = set()
    for relative in roots:
        target = require_contained_path(root / relative, root, must_exist=True)
        if target.is_symlink() or not target.is_dir():
            raise ContractError("remediation target must be a real directory")
        for current, dirs, files in os.walk(target, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink() or files:
                raise ContractError("directory remediation requires empty non-link directories")
            for name in dirs:
                child = current_path / name
                if child.is_symlink():
                    raise ContractError("directory remediation rejects links")
            directories.add(current_path.relative_to(root).as_posix())
    ordered = tuple(sorted(directories, key=lambda value: (len(Path(value).parts), value), reverse=True))
    return roots, ordered


def build_remediation_plan(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=True)
    outcome, outcome_hash = _failed_evidence(root)
    roots, directories = _directory_census(root)
    if len(directories) != EXPECTED_DIRECTORY_COUNT:
        raise ContractError("directory remediation census count differs")
    plan = {"schema_version": 1, "mode": "DIRECTORY_REMEDIATION_PLAN_ONLY", "spent_cleanup_plan_id": APPROVED_CLEANUP_PLAN_ID, "failed_executor_commit": FAILED_EXECUTOR_COMMIT, "failed_outcome_sha256": outcome_hash, "failed_outcome_mode": outcome["mode"], "policy_hash": sha256_file(root / POLICY_PATH), "target_roots": list(roots), "directories": list(directories), "directory_count": len(directories), "file_count": 0, "execution_authorized": False}
    plan["remediation_plan_id"] = sha256_bytes(canonical_json_bytes(plan))
    return plan


def write_remediation_plan(root: Path, *, expected_commit: str, created_at: str) -> dict[str, Any]:
    root = root.resolve(strict=True)
    repository = repository_binding(root, expected_commit=expected_commit)
    plan = build_remediation_plan(root)
    if build_remediation_plan(root) != plan:
        raise ContractError("directory remediation census changed during planning")
    parent = require_contained_path(root / REMEDIATION_ROOT, root, must_exist=False)
    final = parent / plan["remediation_plan_id"]
    if final.exists():
        raise ContractError("directory remediation plan already exists")
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".directory-remediation-", dir=parent))
    try:
        plan_path = temporary / "remediation_plan.json"
        atomic_write(plan_path, canonical_json_bytes(plan))
        receipt = {"schema_version": 1, "mode": "DIRECTORY_REMEDIATION_RECEIPT_NO_DELETION", "remediation_plan_id": plan["remediation_plan_id"], "repository": repository, "created_at": created_at, "plan_sha256": sha256_file(plan_path), "execution_authorized": False}
        atomic_write(temporary / "receipt.json", canonical_json_bytes(receipt))
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ContractError("directory remediation plan reload differs")
        os.replace(temporary, final)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {"directory": str(final), "plan": plan, "receipt": receipt}


def execute_directory_remediation(root: Path, *, plan_id: str, expected_commit: str, confirmation: str, created_at: str, timeout_seconds: int = TIMEOUT_SECONDS) -> dict[str, Any]:
    if timeout_seconds != TIMEOUT_SECONDS:
        raise ContractError("directory remediation timeout differs")
    root = root.resolve(strict=True)
    repository = repository_binding(root, expected_commit=expected_commit)
    package = require_contained_path(root / REMEDIATION_ROOT / plan_id, root, must_exist=True)
    plan_path, receipt_path = package / "remediation_plan.json", package / "receipt.json"
    plan, receipt = json.loads(plan_path.read_text(encoding="utf-8")), json.loads(receipt_path.read_text(encoding="utf-8"))
    if plan.get("remediation_plan_id") != plan_id or receipt.get("remediation_plan_id") != plan_id or receipt.get("plan_sha256") != sha256_file(plan_path) or confirmation != f"REMOVE_EMPTY_DIRECTORIES:{plan_id}":
        raise ContractError("directory remediation binding differs")
    current = build_remediation_plan(root)
    if current != plan:
        raise ContractError("directory remediation census differs")
    attempt_path, outcome_path = package / "attempt.json", package / "outcome.json"
    if attempt_path.exists() or outcome_path.exists():
        raise ContractError("directory remediation already attempted")
    atomic_write_new(attempt_path, canonical_json_bytes({"schema_version": 1, "mode": "DIRECTORY_REMEDIATION_ATTEMPT", "remediation_plan_id": plan_id, "repository": repository, "created_at": created_at, "timeout_seconds": timeout_seconds, "execution_authorized": True}))
    completed: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    try:
        for relative in plan["directories"]:
            if time.monotonic() > deadline:
                raise TimeoutError("directory remediation exceeded timeout")
            path = require_contained_path(root / relative, root, must_exist=True)
            if path.is_symlink() or any(path.iterdir()):
                raise ContractError("directory changed after preflight")
            path.rmdir()
            completed.append(relative)
    except Exception as error:
        atomic_write_new(outcome_path, canonical_json_bytes({"schema_version": 1, "mode": "DIRECTORY_REMEDIATION_FAILED_NO_RETRY", "remediation_plan_id": plan_id, "completed_directories": completed, "remaining_count": len(plan["directories"]) - len(completed), "error": f"{type(error).__name__}: {error}", "execution_authorized": False}))
        raise
    outcome = {"schema_version": 1, "mode": "DIRECTORY_REMEDIATION_SUCCESS", "remediation_plan_id": plan_id, "removed_directory_count": len(completed), "target_roots_absent": sorted(plan["target_roots"]), "execution_authorized": False}
    atomic_write_new(outcome_path, canonical_json_bytes(outcome))
    return outcome
