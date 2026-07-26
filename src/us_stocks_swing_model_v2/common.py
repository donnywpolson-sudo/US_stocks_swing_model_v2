from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ContractError


SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(f"value is not canonical-JSON serializable: {exc}") from exc
    return encoded.encode("utf-8") + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: object, field: str) -> str:
    """Return an exact lowercase SHA-256 string or fail without coercion."""

    if not isinstance(value, str) or SHA256_HEX.fullmatch(value) is None:
        raise ContractError(f"{field} must be exact lowercase SHA-256")
    return value


def require_aware_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return require_aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, field: str = "timestamp") -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError(f"{field} is not ISO-8601: {value!r}") from exc
    return require_aware_utc(parsed, field)


def parse_utc_z(value: str, field: str = "timestamp") -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ContractError(f"{field} must use canonical UTC Z encoding")
    parsed = parse_timestamp(value, field)
    if iso_z(parsed) != value:
        raise ContractError(f"{field} is not canonical UTC encoding")
    return parsed


def safe_relative_path(value: str) -> PurePosixPath:
    candidate = PurePosixPath(value.replace("\\", "/"))
    if candidate.is_absolute() or not candidate.parts:
        raise ContractError(f"path must be relative: {value!r}")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise ContractError(f"unsafe relative path: {value!r}")
    return candidate


def reject_link(path: Path) -> None:
    if path.is_symlink():
        raise ContractError(f"links are prohibited: {path}")
    if os.name == "nt" and os.path.lexists(path):
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
        reparse_flag = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if reparse_flag and attrs & reparse_flag:
            raise ContractError(f"junction/reparse points are prohibited: {path}")


def require_contained_path(path: Path, allowed_root: Path, *, must_exist: bool = True) -> Path:
    """Reject lexical escapes and links/reparse points in every existing ancestor."""
    candidate = Path(path)
    root = Path(allowed_root)
    if not candidate.is_absolute() or not root.is_absolute():
        raise ContractError("contained paths and roots must be absolute")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"path escapes its approved root: {candidate}") from exc
    current = root
    reject_link(current)
    if not current.exists():
        raise ContractError(f"approved root does not exist: {root}")
    for part in relative.parts:
        current = current / part
        reject_link(current)
    if must_exist and not candidate.exists():
        raise ContractError(f"required contained path does not exist: {candidate}")
    resolved_root = root.resolve(strict=True)
    if candidate.exists():
        resolved = candidate.resolve(strict=True)
        if resolved != resolved_root and resolved_root not in resolved.parents:
            raise ContractError(f"resolved path escapes its approved root: {candidate}")
    return candidate


def assert_exact_tree(root: Path, expected_files: set[str], expected_directories: set[str]) -> None:
    directory = Path(root)
    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    for path in directory.rglob("*"):
        reject_link(path)
        relative = path.relative_to(directory).as_posix()
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise ContractError(f"hardlinked tree entry is prohibited: {relative}")
            actual_files.add(relative)
        elif path.is_dir():
            actual_directories.add(relative)
        else:
            raise ContractError(f"non-file/non-directory tree entry is prohibited: {relative}")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ContractError(
            "tree differs from manifest: "
            f"missing_files={sorted(expected_files-actual_files)}, "
            f"extra_files={sorted(actual_files-expected_files)}, "
            f"missing_dirs={sorted(expected_directories-actual_directories)}, "
            f"extra_dirs={sorted(actual_directories-expected_directories)}"
        )


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short enough for Windows' legacy path limit.
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix=".aw.", suffix=".tmp", dir=path.parent, delete=False
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()
