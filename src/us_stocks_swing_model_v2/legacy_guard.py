"""Capture and verify a legacy stock worktree before migration detaches it."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Iterable

from .common import canonical_json_bytes, sha256_file, sha256_bytes
from .errors import ContractError, IntegrityError


_HASH_POLICY = "git-visible-v1-status-index-diffs-and-content"


def _sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _git(root: Path, *args: str) -> bytes:
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["LC_ALL"] = "C"
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise ContractError(f"read-only Git query failed: git {' '.join(args)}: {detail}")
    return completed.stdout


def _paths(raw: bytes) -> tuple[str, ...]:
    values = tuple(item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item)
    if values != tuple(sorted(set(values))):
        raise IntegrityError("Git path enumeration is not unique and sorted")
    return values


def _content_entry(root: Path, relative: str) -> dict[str, Any]:
    path = root / relative
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing", "path": relative}
    if stat.S_ISLNK(info.st_mode):
        return {
            "kind": "symlink",
            "path": relative,
            "target_sha256": sha256_bytes(os.fsencode(os.readlink(path))),
        }
    if stat.S_ISREG(info.st_mode):
        return {
            "kind": "file",
            "path": relative,
            "sha256": sha256_file(path),
            "size": info.st_size,
        }
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "directory_or_gitlink", "path": relative}
    return {"kind": f"mode_{info.st_mode:o}", "path": relative}


def _manifest(root: Path, paths: Iterable[str]) -> dict[str, Any]:
    entries = [_content_entry(root, relative) for relative in paths]
    return {"count": len(entries), "sha256": _sha256_json(entries)}


def capture_legacy_baseline(legacy_root: Path) -> dict[str, Any]:
    root = legacy_root.resolve(strict=True)
    observed_root = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8", "strict").strip()).resolve(strict=True)
    if os.path.normcase(str(observed_root)) != os.path.normcase(str(root)):
        raise ContractError("legacy root is not the exact Git worktree root")
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tracked_paths = _paths(_git(root, "ls-files", "-z"))
    untracked_paths = _paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    index_path = Path(_git(root, "rev-parse", "--git-path", "index").decode("utf-8", "strict").strip())
    if not index_path.is_absolute():
        index_path = root / index_path
    if not index_path.is_file():
        raise IntegrityError("legacy Git index is missing")
    core: dict[str, Any] = {
        "branch": _git(root, "symbolic-ref", "--quiet", "--short", "HEAD").decode("utf-8", "strict").strip(),
        "cached_diff_sha256": hashlib.sha256(_git(root, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv")).hexdigest(),
        "hash_policy": _HASH_POLICY,
        "head": _git(root, "rev-parse", "HEAD").decode("ascii", "strict").strip(),
        "index_sha256": sha256_file(index_path),
        "index_stage_sha256": hashlib.sha256(_git(root, "ls-files", "-s", "-z")).hexdigest(),
        "legacy_root": str(root),
        "schema_version": "1.0.0",
        "status_count": len([item for item in status.split(b"\0") if item]),
        "status_sha256": hashlib.sha256(status).hexdigest(),
        "tracked_content": _manifest(root, tracked_paths),
        "unstaged_diff_sha256": hashlib.sha256(_git(root, "diff", "--binary", "--no-ext-diff", "--no-textconv")).hexdigest(),
        "untracked_content": _manifest(root, untracked_paths),
    }
    return {**core, "baseline_id": _sha256_json(core)}


def load_legacy_baseline(config_path: Path) -> dict[str, Any]:
    """Load and authenticate a historical legacy-worktree capture."""

    try:
        expected = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError("legacy baseline configuration is invalid") from exc
    required = {
        "baseline_id", "branch", "cached_diff_sha256", "hash_policy", "head",
        "index_sha256", "index_stage_sha256", "legacy_root", "schema_version",
        "status_count", "status_sha256", "tracked_content", "unstaged_diff_sha256",
        "untracked_content",
    }
    if not isinstance(expected, dict) or set(expected) != required:
        raise ContractError("legacy baseline configuration schema is invalid")
    expected_core = {key: value for key, value in expected.items() if key != "baseline_id"}
    if _sha256_json(expected_core) != expected["baseline_id"]:
        raise IntegrityError("legacy baseline configuration hash is invalid")
    return expected


def verify_legacy_baseline(config_path: Path) -> dict[str, Any]:
    """Compare a current legacy worktree with a previously captured baseline."""

    expected = load_legacy_baseline(config_path)
    required = set(expected)
    observed = capture_legacy_baseline(Path(str(expected["legacy_root"])))
    if canonical_json_bytes(observed) != canonical_json_bytes(expected):
        changed = sorted(key for key in required if observed.get(key) != expected.get(key))
        raise IntegrityError("legacy worktree changed: " + ", ".join(changed))
    return observed
