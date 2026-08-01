"""Fail-closed inventory builder for the later, separately authorized purge."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, require_contained_path, sha256_bytes, sha256_file
from .errors import ContractError


POLICY_PATH = "config/legacy_cleanup_policy.json"


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
