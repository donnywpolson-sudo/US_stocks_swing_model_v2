"""Fail-closed inventory builder for the later, separately authorized purge."""

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
PURGE_TIMEOUT_SECONDS = 30 * 60


def _normal(relative: str) -> str:
    value = Path(relative).as_posix()
    if value.startswith("/") or ".." in Path(value).parts:
        raise ContractError("cleanup path is not a contained relative path")
    return value


def build_cleanup_plan(root: Path) -> dict[str, Any]:
    """Return an inventory only. This function never writes or deletes."""
    root = root.resolve(strict=True)
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    if policy.get("mode") != "PLAN_ONLY_NO_DELETION":
        raise ContractError("cleanup policy is not plan-only")
    targets = tuple(_normal(str(item)) for item in policy["target_roots"])
    protected = tuple(_normal(str(item)) for item in policy["protected_prefixes"])
    records: list[dict[str, Any]] = []
    for relative in targets:
        target = require_contained_path(root / relative, root, must_exist=True)
        if target.is_symlink() or not target.is_dir():
            raise ContractError("cleanup target must be a real directory")
        for current, dirs, files in os.walk(target, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink():
                raise ContractError("cleanup inventory rejects links")
            for name in dirs:
                if (current_path / name).is_symlink():
                    raise ContractError("cleanup inventory rejects links")
            for name in files:
                item = current_path / name
                if item.is_symlink() or not item.is_file():
                    raise ContractError("cleanup inventory rejects non-regular files")
                item_relative = item.relative_to(root).as_posix()
                if any(item_relative == prefix or item_relative.startswith(prefix + "/") for prefix in protected):
                    raise ContractError("cleanup inventory intersects protected evidence")
                records.append({"path": item_relative, "root": relative, "bytes": item.stat().st_size, "sha256": sha256_file(item)})
    records.sort(key=lambda item: item["path"])
    if len({item["path"] for item in records}) != len(records):
        raise ContractError("cleanup inventory has duplicate files")
    payload = {"schema_version": 1, "mode": "PLAN_ONLY_NO_DELETION", "policy_hash": sha256_file(root / POLICY_PATH), "files": records, "file_count": len(records), "total_bytes": sum(item["bytes"] for item in records), "execution_authorized": False}
    payload["cleanup_plan_id"] = sha256_bytes(canonical_json_bytes(payload))
    return payload


def repository_binding(root: Path, *, expected_commit: str) -> dict[str, str]:
    """Require the exact clean Git closure before generated-plan publication."""
    def git(*args: str) -> str:
        return subprocess.run(
            ("git", *args), cwd=root, check=True, text=True,
            capture_output=True,
        ).stdout.strip()

    if git("rev-parse", "--show-toplevel").replace("/", "\\").casefold() != str(root).replace("/", "\\").casefold():
        raise ContractError("cleanup repository root differs")
    if git("status", "--porcelain=v1"):
        raise ContractError("cleanup plan requires a clean repository")
    commit = git("rev-parse", "HEAD")
    if commit != expected_commit:
        raise ContractError("cleanup plan commit differs")
    return {"commit": commit, "tree": git("rev-parse", "HEAD^{tree}")}


def write_cleanup_plan(
    root: Path,
    *,
    expected_commit: str,
    created_at: str,
) -> dict[str, Any]:
    """Atomically create one untracked no-deletion plan package."""
    root = root.resolve(strict=True)
    repository = repository_binding(root, expected_commit=expected_commit)
    plan = build_cleanup_plan(root)
    if build_cleanup_plan(root) != plan:
        raise ContractError("cleanup census changed during planning")
    output_root = require_contained_path(root / WORK_ROOT, root, must_exist=False)
    for item in plan["files"]:
        if str(item["path"]).startswith(WORK_ROOT + "/"):
            raise ContractError("cleanup plan output intersects a deletion target")
    final = output_root / str(plan["cleanup_plan_id"])
    if final.exists():
        raise ContractError("cleanup plan ID already exists")
    output_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".cleanup-plan-", dir=output_root))
    try:
        plan_path = temporary / "cleanup_plan.json"
        atomic_write(plan_path, canonical_json_bytes(plan))
        receipt = {
            "schema_version": 1,
            "mode": "LOCAL_CLEANUP_PLAN_RECEIPT_NO_DELETION",
            "cleanup_plan_id": plan["cleanup_plan_id"],
            "policy_hash": plan["policy_hash"],
            "repository": repository,
            "created_at": created_at,
            "cleanup_plan_sha256": sha256_file(plan_path),
            "execution_authorized": False,
        }
        atomic_write(temporary / "receipt.json", canonical_json_bytes(receipt))
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            raise ContractError("cleanup plan reload differs")
        os.replace(temporary, final)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return {"directory": str(final), "plan": plan, "receipt": receipt}


def _plan_package(root: Path, plan_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    if plan_id != APPROVED_CLEANUP_PLAN_ID:
        raise ContractError("cleanup purge requires the exact approved plan ID")
    package = require_contained_path(root / WORK_ROOT / plan_id, root, must_exist=True)
    plan_path = package / "cleanup_plan.json"
    receipt_path = package / "receipt.json"
    if not plan_path.is_file() or not receipt_path.is_file():
        raise ContractError("cleanup plan package is incomplete")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        plan.get("cleanup_plan_id") != plan_id
        or receipt.get("cleanup_plan_id") != plan_id
        or receipt.get("cleanup_plan_sha256") != sha256_file(plan_path)
        or receipt.get("execution_authorized") is not False
        or plan.get("execution_authorized") is not False
        or plan.get("policy_hash") != sha256_file(root / POLICY_PATH)
    ):
        raise ContractError("cleanup plan package binding differs")
    return package, plan, receipt


def _preflight_purge(root: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    expected_roots = tuple(_normal(str(item)) for item in policy["target_roots"])
    protected = tuple(_normal(str(item)) for item in policy["protected_prefixes"])
    files = tuple(plan.get("files", ()))
    if not files or tuple(sorted({str(item["root"]) for item in files})) != tuple(sorted(expected_roots)):
        raise ContractError("cleanup plan target-root census differs")
    expected_paths = {str(item["path"]): item for item in files}
    if len(expected_paths) != len(files):
        raise ContractError("cleanup plan has duplicate paths")
    actual_paths: set[str] = set()
    for relative in expected_roots:
        target = require_contained_path(root / relative, root, must_exist=True)
        if target.is_symlink() or not target.is_dir():
            raise ContractError("cleanup target must be a real directory")
        for current, dirs, names in os.walk(target, followlinks=False):
            current_path = Path(current)
            if current_path.is_symlink():
                raise ContractError("cleanup purge rejects links")
            for name in dirs:
                if (current_path / name).is_symlink():
                    raise ContractError("cleanup purge rejects links")
            for name in names:
                item = current_path / name
                if item.is_symlink() or not item.is_file():
                    raise ContractError("cleanup purge rejects non-regular files")
                relative_path = item.relative_to(root).as_posix()
                if any(relative_path == prefix or relative_path.startswith(prefix + "/") for prefix in protected):
                    raise ContractError("cleanup purge intersects protected evidence")
                actual_paths.add(relative_path)
    if actual_paths != set(expected_paths):
        raise ContractError("cleanup target file census differs")
    for relative_path, item in expected_paths.items():
        path = require_contained_path(root / relative_path, root, must_exist=True)
        if path.stat().st_size != item["bytes"] or sha256_file(path) != item["sha256"]:
            raise ContractError("cleanup target hash or size differs")
    return tuple(expected_paths[path] for path in sorted(expected_paths))


def prepare_purge_execution(root: Path, *, plan_id: str) -> dict[str, Any]:
    """Read-only exact-plan preflight for a later one-shot purge."""
    root = root.resolve(strict=True)
    package, plan, receipt = _plan_package(root, plan_id)
    files = _preflight_purge(root, plan)
    return {"package": str(package), "plan": plan, "receipt": receipt, "files": files}


def execute_purge(
    root: Path,
    *,
    plan_id: str,
    expected_executor_commit: str,
    owner_confirmation: str,
    created_at: str,
    timeout_seconds: int = PURGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One irreversible attempt; callers must obtain separate exact authority."""
    if timeout_seconds != PURGE_TIMEOUT_SECONDS:
        raise ContractError("cleanup purge timeout differs from the fixed 30-minute limit")
    root = root.resolve(strict=True)
    repository = repository_binding(root, expected_commit=expected_executor_commit)
    expected_confirmation = f"IRREVERSIBLE_PURGE:{plan_id}"
    if owner_confirmation != expected_confirmation:
        raise ContractError("cleanup purge owner confirmation differs")
    prepared = prepare_purge_execution(root, plan_id=plan_id)
    package = Path(prepared["package"])
    attempt_path = package / "purge_attempt.json"
    outcome_path = package / "purge_outcome.json"
    if attempt_path.exists() or outcome_path.exists():
        raise ContractError("cleanup plan already has a purge attempt record")
    attempt = {"schema_version": 1, "mode": "IRREVERSIBLE_PURGE_ATTEMPT", "plan_id": plan_id, "repository": repository, "created_at": created_at, "timeout_seconds": timeout_seconds, "execution_authorized": True}
    atomic_write_new(attempt_path, canonical_json_bytes(attempt))
    deadline = time.monotonic() + timeout_seconds
    completed: list[str] = []
    files = prepared["files"]
    try:
        for item in files:
            if time.monotonic() > deadline:
                raise TimeoutError("cleanup purge exceeded its fixed timeout")
            path = require_contained_path(root / str(item["path"]), root, must_exist=True)
            if path.is_symlink() or not path.is_file():
                raise ContractError("cleanup target changed after preflight")
            path.unlink()
            completed.append(str(item["path"]))
        directories = {str(item["root"]) for item in files}
        directories.update(str(Path(str(item["path"])).parent.as_posix()) for item in files)
        for relative in sorted(directories, key=lambda value: (len(Path(value).parts), value), reverse=True):
            if time.monotonic() > deadline:
                raise TimeoutError("cleanup purge exceeded its fixed timeout")
            path = require_contained_path(root / relative, root, must_exist=True)
            path.rmdir()
    except Exception as error:
        outcome = {"schema_version": 1, "mode": "IRREVERSIBLE_PURGE_FAILED_NO_RETRY", "plan_id": plan_id, "completed_paths": completed, "remaining_count": len(files) - len(completed), "error": f"{type(error).__name__}: {error}", "execution_authorized": False}
        atomic_write_new(outcome_path, canonical_json_bytes(outcome))
        raise
    outcome = {"schema_version": 1, "mode": "IRREVERSIBLE_PURGE_SUCCESS", "plan_id": plan_id, "deleted_file_count": len(completed), "deleted_bytes": sum(int(item["bytes"]) for item in files), "target_roots_absent": sorted({str(item["root"]) for item in files}), "execution_authorized": False}
    atomic_write_new(outcome_path, canonical_json_bytes(outcome))
    return outcome
