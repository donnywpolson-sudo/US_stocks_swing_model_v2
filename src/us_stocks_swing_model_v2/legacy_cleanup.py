"""Fail-closed inventory builder for the later, separately authorized purge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from .common import atomic_write, canonical_json_bytes, require_contained_path, sha256_bytes, sha256_file
from .errors import ContractError


POLICY_PATH = "config/legacy_cleanup_policy.json"
WORK_ROOT = "data/w/legacy_cleanup"


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
