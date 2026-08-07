"""Content-addressed, no-write Master Audit planning and read interface."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Mapping


if __package__ in {None, ""}:  # Support the exact direct-script audit command.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from us_stocks_swing_model_v2.common import (  # type: ignore[import-not-found]
        canonical_json_bytes,
        reject_link,
        require_contained_path,
        require_sha256,
        safe_relative_path,
        sha256_bytes,
        sha256_file,
    )
    from us_stocks_swing_model_v2.errors import (  # type: ignore[import-not-found]
        ContractError,
        IntegrityError,
    )
    from us_stocks_swing_model_v2.releases import (  # type: ignore[import-not-found]
        verify_accepted_release,
    )
else:
    from .common import (
        canonical_json_bytes,
        reject_link,
        require_contained_path,
        require_sha256,
        safe_relative_path,
        sha256_bytes,
        sha256_file,
    )
    from .errors import ContractError, IntegrityError
    from .releases import verify_accepted_release


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/master_audit_policy.json"
SCHEMA_VERSION = 1
TARGET_STATES = ("REBUILD_COMPLETE", "HISTORICAL_RESEARCH_READY")
MODES = (
    "HostProfile",
    "Preflight",
    "PlanGroups",
    "ReadGroup",
    "VerifyReleases",
    "FinalPreflight",
)
GROUP_FOOTER = "===== MASTER_AUDIT_GROUP_COMPLETE =====\n"
SECRET_BASENAMES = {
    ".env",
    "api.env",
    "credentials",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:/")


@dataclass(frozen=True)
class FileBinding:
    path: str
    bytes: int
    sha256: str
    line_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "line_count": self.line_count,
        }


@dataclass(frozen=True)
class ReadSlice:
    path: str
    start_line: int
    end_line: int
    file_sha256: str

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "file_sha256": self.file_sha256,
        }


@dataclass(frozen=True)
class ReadGroup:
    ordinal: int
    slice: ReadSlice
    numbered_line_count: int
    rendered_utf8_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "slice": self.slice.as_dict(),
            "numbered_line_count": self.numbered_line_count,
            "rendered_utf8_bytes": self.rendered_utf8_bytes,
            "required_footer": GROUP_FOOTER.rstrip("\n"),
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _windows_key(value: Path | str) -> str:
    text = str(value).replace("\\", "/")
    if WINDOWS_DRIVE.match(text):
        text = text[0].lower() + text[1:]
    return text.rstrip("/").casefold()


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise ContractError(f"Git command failed: git {' '.join(arguments)}: {error}")
    return completed.stdout.decode("utf-8", errors="strict").strip()


def _json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise ContractError(f"{label} must be an object")
    return value


def _exact_keys(value: Mapping[str, object], keys: set[str], label: str) -> None:
    if set(value) != keys:
        raise ContractError(f"{label} fields differ")


def _text_list(value: object, label: str, *, sorted_unique: bool = True) -> list[str]:
    if type(value) is not list or any(type(item) is not str or not item for item in value):
        raise ContractError(f"{label} must be a list of nonempty strings")
    result = list(value)
    if sorted_unique and result != sorted(set(result)):
        raise ContractError(f"{label} must be sorted and unique")
    return result


def load_policy(root: Path) -> dict[str, object]:
    policy = _json(root / POLICY_PATH, "Master Audit policy")
    _exact_keys(
        policy,
        {
            "schema_version",
            "project",
            "mode",
            "expected_repository_root",
            "specification_path",
            "tracked_corpus",
            "group_limits",
            "runtime_files",
            "common_review_paths",
            "targets",
            "commands",
            "output",
            "prohibitions",
        },
        "Master Audit policy",
    )
    if (
        policy["schema_version"] != 1
        or policy["project"] != PROJECT
        or policy["mode"] != "MASTER_AUDIT_PLAN_ONLY_NO_WRITES"
    ):
        raise ContractError("Master Audit policy identity differs")
    if type(policy["expected_repository_root"]) is not str:
        raise ContractError("Master Audit expected root differs")
    safe_relative_path(str(policy["specification_path"]))
    _text_list(policy["runtime_files"], "runtime_files")
    _text_list(policy["common_review_paths"], "common_review_paths")
    _text_list(policy["prohibitions"], "prohibitions")

    tracked = policy["tracked_corpus"]
    limits = policy["group_limits"]
    commands = policy["commands"]
    output = policy["output"]
    targets = policy["targets"]
    if not all(type(item) is dict for item in (tracked, limits, commands, output, targets)):
        raise ContractError("Master Audit nested policy fields differ")
    _exact_keys(tracked, {"expected_path_count", "expected_paths_sha256"}, "tracked_corpus")
    _exact_keys(limits, {"maximum_numbered_lines", "maximum_rendered_utf8_bytes"}, "group_limits")
    _exact_keys(
        commands,
        {
            "metadata_timeout_seconds",
            "read_group_timeout_seconds",
            "release_verification_timeout_seconds",
            "full_test_timeout_seconds",
            "git_diff_timeout_seconds",
        },
        "commands",
    )
    _exact_keys(output, {"destination", "retained_report", "generated_artifact_write"}, "output")
    if (
        type(tracked["expected_path_count"]) is not int
        or tracked["expected_path_count"] <= 0
    ):
        raise ContractError("tracked corpus count must be positive")
    require_sha256(tracked["expected_paths_sha256"], "tracked corpus paths hash")
    if any(type(limits[name]) is not int or limits[name] <= 0 for name in limits):
        raise ContractError("group limits must be positive integers")
    if any(type(commands[name]) is not int or commands[name] <= 0 for name in commands):
        raise ContractError("command timeouts must be positive integers")
    if output != {
        "destination": "CONVERSATION_ONLY",
        "retained_report": False,
        "generated_artifact_write": False,
    }:
        raise ContractError("Master Audit output policy differs")
    if set(targets) != set(TARGET_STATES):
        raise ContractError("Master Audit target states differ")
    for target, target_policy in targets.items():
        if type(target_policy) is not dict:
            raise ContractError(f"{target} policy must be an object")
        _exact_keys(
            target_policy,
            {"requires_completed_target", "review_paths", "accepted_releases"},
            f"{target} policy",
        )
        _text_list(target_policy["review_paths"], f"{target}.review_paths")
        _text_list(target_policy["accepted_releases"], f"{target}.accepted_releases")
    if targets["REBUILD_COMPLETE"]["requires_completed_target"] is not None:
        raise ContractError("REBUILD_COMPLETE prerequisite differs")
    if targets["HISTORICAL_RESEARCH_READY"]["requires_completed_target"] != "REBUILD_COMPLETE":
        raise ContractError("HISTORICAL_RESEARCH_READY prerequisite differs")
    return policy


def _validate_root(root: Path, policy: Mapping[str, object]) -> Path:
    resolved = Path(root).resolve(strict=True)
    reject_link(resolved)
    expected = str(policy["expected_repository_root"])
    if _windows_key(resolved) != _windows_key(expected):
        raise ContractError("Master Audit repository root differs")
    git_root = Path(_git(resolved, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if _windows_key(git_root) != _windows_key(resolved):
        raise ContractError("Master Audit Git root differs")
    return resolved


def _secret_like(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name.casefold()
    return name in SECRET_BASENAMES or name.endswith(SECRET_SUFFIXES)


def _tracked_paths(root: Path, policy: Mapping[str, object]) -> list[str]:
    raw = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if raw.returncode != 0:
        raise ContractError("tracked corpus census failed")
    decoded = raw.stdout.decode("utf-8", errors="strict")
    paths = sorted(item for item in decoded.split("\0") if item)
    if paths != sorted(set(paths)):
        raise ContractError("tracked corpus paths are not unique")
    for path in paths:
        safe_relative_path(path)
        if _secret_like(path):
            raise ContractError(f"tracked secret-like path is prohibited: {path}")
    tracked = policy["tracked_corpus"]
    assert isinstance(tracked, dict)
    if len(paths) != tracked["expected_path_count"]:
        raise IntegrityError("tracked corpus path count differs")
    if sha256_bytes(canonical_json_bytes(paths)) != tracked["expected_paths_sha256"]:
        raise IntegrityError("tracked corpus paths hash differs")
    return paths


def _file_binding(root: Path, relative_path: str) -> FileBinding:
    safe = safe_relative_path(relative_path).as_posix()
    path = require_contained_path(root / PurePosixPath(safe), root)
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ContractError(f"audit corpus entry must be an ordinary single-link file: {safe}")
    data = path.read_bytes()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(f"audit corpus entry is not UTF-8 text: {safe}") from exc
    if "\x00" in text:
        raise ContractError(f"audit corpus entry contains NUL: {safe}")
    return FileBinding(
        path=safe,
        bytes=len(data),
        sha256=sha256_bytes(data),
        line_count=len(text.splitlines()),
    )


def _file_lines(root: Path, binding: FileBinding) -> list[str]:
    path = require_contained_path(root / PurePosixPath(binding.path), root)
    data = path.read_bytes()
    if sha256_bytes(data) != binding.sha256 or len(data) != binding.bytes:
        raise IntegrityError(f"audit corpus file changed: {binding.path}")
    return data.decode("utf-8", errors="strict").splitlines()


def _render_slice(path: str, start: int, end: int, lines: list[str]) -> str:
    header = f"===== {path}:{start}-{end} =====\n"
    body = "".join(
        f"{line_number:06d}|{lines[line_number - 1]}\n"
        for line_number in range(start, end + 1)
    )
    return header + body + GROUP_FOOTER


def build_read_groups(
    root: Path,
    bindings: Iterable[FileBinding],
    *,
    maximum_lines: int,
    maximum_bytes: int,
) -> tuple[ReadGroup, ...]:
    groups: list[ReadGroup] = []
    for binding in bindings:
        lines = _file_lines(root, binding)
        if not lines:
            rendered = f"===== {binding.path}:EMPTY =====\n{GROUP_FOOTER}"
            if len(rendered.encode("utf-8")) > maximum_bytes:
                raise ContractError(f"empty audit group exceeds byte limit: {binding.path}")
            groups.append(
                ReadGroup(
                    ordinal=len(groups) + 1,
                    slice=ReadSlice(binding.path, 0, 0, binding.sha256),
                    numbered_line_count=0,
                    rendered_utf8_bytes=len(rendered.encode("utf-8")),
                )
            )
            continue
        start = 1
        while start <= len(lines):
            end = start
            while end <= len(lines):
                if end - start + 1 > maximum_lines:
                    break
                rendered = _render_slice(binding.path, start, end, lines)
                if len(rendered.encode("utf-8")) > maximum_bytes:
                    break
                end += 1
            final_end = end - 1
            if final_end < start:
                raise ContractError(f"unrenderable audit line: {binding.path}:{start}")
            rendered = _render_slice(binding.path, start, final_end, lines)
            groups.append(
                ReadGroup(
                    ordinal=len(groups) + 1,
                    slice=ReadSlice(binding.path, start, final_end, binding.sha256),
                    numbered_line_count=final_end - start + 1,
                    rendered_utf8_bytes=len(rendered.encode("utf-8")),
                )
            )
            start = final_end + 1
    return tuple(groups)


def _accepted_release_binding(root: Path, relative_directory: str) -> dict[str, object]:
    safe = safe_relative_path(relative_directory).as_posix()
    parts = PurePosixPath(safe).parts
    if len(parts) != 5 or parts[:3] != ("data", "vault", "accepted"):
        raise ContractError("Master Audit accepted release path shape differs")
    dataset, expected_id = parts[3], parts[4]
    require_sha256(expected_id, "accepted release path ID")
    accepted_root = root / "data" / "vault" / "accepted"
    directory = require_contained_path(root / PurePosixPath(safe), accepted_root)
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if manifest.dataset != dataset or manifest.release_id != expected_id:
        raise IntegrityError("Master Audit accepted release identity differs")
    return {
        "relative_directory": safe,
        "release_id": manifest.release_id,
        "dataset": manifest.dataset,
        "source_epoch": manifest.source_epoch,
        "role": manifest.role,
        "quality_state": manifest.quality_state,
        "manifest_sha256": sha256_file(directory / "release_manifest.json"),
        "payload_file_count": len(manifest.files),
        "payload_bytes": sum(item.size for item in manifest.files),
    }


def _command_contract(
    *,
    root: Path,
    target_state: str,
    group_count: int,
    policy: Mapping[str, object],
) -> dict[str, object]:
    timeouts = policy["commands"]
    assert isinstance(timeouts, dict)
    python = str(Path(sys.executable).resolve(strict=True))
    script = str((root / "src/us_stocks_swing_model_v2/master_audit.py").resolve(strict=True))
    git = shutil.which("git")
    if not git:
        raise ContractError("Git executable is unavailable")
    git = str(Path(git).resolve(strict=True))
    base = [
        python,
        script,
        "--target-state",
        target_state,
        "--approved-envelope-id",
        "{ENVELOPE_ID}",
    ]
    commands: list[dict[str, object]] = []
    for mode in ("HostProfile", "Preflight", "PlanGroups"):
        commands.append(
            {
                "name": mode,
                "argv": [*base, "--mode", mode],
                "run_limit": 1,
                "timeout_seconds": timeouts["metadata_timeout_seconds"],
                "expected_exit": "REQUIRED_SUCCESS",
                "output_max_utf8_bytes": 20000,
            }
        )
    commands.append(
        {
            "name": "ReadGroup",
            "argv_template": [*base, "--mode", "ReadGroup", "--group", "{GROUP_ORDINAL}"],
            "group_ordinal_start": 1,
            "group_ordinal_end": group_count,
            "run_limit_each": 1,
            "timeout_seconds_each": timeouts["read_group_timeout_seconds"],
            "expected_exit": "REQUIRED_SUCCESS",
            "output_max_utf8_bytes_each": policy["group_limits"]["maximum_rendered_utf8_bytes"],
            "required_footer": GROUP_FOOTER.rstrip("\n"),
        }
    )
    commands.extend(
        [
            {
                "name": "VerifyReleases",
                "argv": [*base, "--mode", "VerifyReleases"],
                "run_limit": 1,
                "timeout_seconds": timeouts["release_verification_timeout_seconds"],
                "expected_exit": "REQUIRED_SUCCESS",
                "output_max_utf8_bytes": 100000,
            },
            {
                "name": "RunFullTests",
                "argv": [python, "-m", "pytest", "-q", "--tb=line"],
                "cwd": str(root),
                "run_limit": 1,
                "timeout_seconds": timeouts["full_test_timeout_seconds"],
                "expected_exit": "REPORTABLE_NONZERO",
                "output_max_utf8_bytes": 200000,
            },
            {
                "name": "GitDiffCheck",
                "argv": [git, "diff", "--check"],
                "cwd": str(root),
                "run_limit": 1,
                "timeout_seconds": timeouts["git_diff_timeout_seconds"],
                "expected_exit": "REQUIRED_SUCCESS",
                "output_max_utf8_bytes": 20000,
            },
            {
                "name": "FinalPreflight",
                "argv": [*base, "--mode", "FinalPreflight"],
                "run_limit": 1,
                "timeout_seconds": timeouts["metadata_timeout_seconds"],
                "expected_exit": "REQUIRED_SUCCESS",
                "output_max_utf8_bytes": 20000,
            },
        ]
    )
    for command in commands:
        command.setdefault("cwd", str(root))
        command["environment_additions"] = {}
    return {
        "ordering": [command["name"] for command in commands],
        "commands": commands,
        "undeclared_commands_allowed": False,
        "retry_allowed": False,
    }


def build_envelope(root: Path, target_state: str) -> dict[str, object]:
    if target_state not in TARGET_STATES:
        raise ContractError("Master Audit target state differs")
    if platform.python_version() != "3.11.9":
        raise ContractError("Master Audit requires the exact Python 3.11.9 runtime")
    raw_root = Path(root)
    policy = load_policy(raw_root.resolve(strict=True))
    root = _validate_root(raw_root, policy)
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ContractError("Master Audit requires a clean ordinary worktree")

    paths = _tracked_paths(root, policy)
    bindings = tuple(_file_binding(root, path) for path in paths)
    binding_map = {binding.path: binding for binding in bindings}
    target_policy = policy["targets"][target_state]
    common = _text_list(policy["common_review_paths"], "common_review_paths")
    specific = _text_list(target_policy["review_paths"], f"{target_state}.review_paths")
    review_paths = sorted(set((*common, *specific)))
    if any(path not in binding_map for path in review_paths):
        missing = sorted(path for path in review_paths if path not in binding_map)
        raise IntegrityError(f"Master Audit review path is not tracked: {missing}")
    review_bindings = tuple(binding_map[path] for path in review_paths)
    limits = policy["group_limits"]
    groups = build_read_groups(
        root,
        review_bindings,
        maximum_lines=limits["maximum_numbered_lines"],
        maximum_bytes=limits["maximum_rendered_utf8_bytes"],
    )
    releases = tuple(
        _accepted_release_binding(root, path)
        for path in _text_list(
            target_policy["accepted_releases"], f"{target_state}.accepted_releases"
        )
    )
    specification = binding_map[str(policy["specification_path"])]
    runtime_files = [binding_map[path].as_dict() for path in policy["runtime_files"]]
    repository = {
        "root": str(root),
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "require_clean": True,
    }
    unsigned: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "MASTER_AUDIT_ENVELOPE_NO_WRITES",
        "project": PROJECT,
        "target_state": target_state,
        "requires_completed_target": target_policy["requires_completed_target"],
        "repository": repository,
        "runtime": {
            "python_executable": str(Path(sys.executable).resolve(strict=True)),
            "python_version": platform.python_version(),
            "required_python_version": "3.11.9",
            "files": runtime_files,
        },
        "policy": {
            "path": POLICY_PATH,
            "sha256": sha256_file(root / POLICY_PATH),
        },
        "specification": specification.as_dict(),
        "tracked_corpus": {
            "path_count": len(paths),
            "paths_sha256": sha256_bytes(canonical_json_bytes(paths)),
            "files_sha256": sha256_bytes(
                canonical_json_bytes([binding.as_dict() for binding in bindings])
            ),
            "files": [binding.as_dict() for binding in bindings],
        },
        "qualitative_review": {
            "path_count": len(review_bindings),
            "files": [binding.as_dict() for binding in review_bindings],
            "group_count": len(groups),
            "groups": [group.as_dict() for group in groups],
        },
        "accepted_releases": list(releases),
        "reviewer_independence": {
            "fresh_reviewer": True,
            "inherited_turns": 0,
            "prior_target_access": False,
            "transport_probe_before_creation": True,
        },
        "output": policy["output"],
        "authorities": {
            "network_calls": False,
            "secret_reads": False,
            "writes": False,
            "release_publication": False,
            "research": False,
            "training": False,
            "evaluation": False,
            "prediction": False,
            "activation": False,
            "commit": False,
            "push": False,
            "august_raw_capture": False,
        },
        "prohibitions": policy["prohibitions"],
    }
    unsigned["command_contract"] = _command_contract(
        root=root,
        target_state=target_state,
        group_count=len(groups),
        policy=policy,
    )
    envelope_id = sha256_bytes(canonical_json_bytes(unsigned))
    return {**unsigned, "envelope_id": envelope_id}


def _approved_envelope(
    root: Path, target_state: str, approved_envelope_id: str
) -> dict[str, object]:
    approved = require_sha256(approved_envelope_id, "approved Master Audit envelope ID")
    envelope = build_envelope(root, target_state)
    if envelope["envelope_id"] != approved:
        raise IntegrityError("approved Master Audit envelope differs from live state")
    return envelope


def build_dispatch(envelope: Mapping[str, object]) -> dict[str, object]:
    envelope_id = require_sha256(envelope.get("envelope_id"), "Master Audit envelope ID")
    contract = envelope.get("command_contract")
    repository = envelope.get("repository")
    review = envelope.get("qualitative_review")
    if not all(type(item) is dict for item in (contract, repository, review)):
        raise ContractError("Master Audit envelope dispatch fields differ")

    def replace(value: object) -> object:
        if type(value) is str:
            return value.replace("{ENVELOPE_ID}", envelope_id)
        if type(value) is list:
            return [replace(item) for item in value]
        if type(value) is dict:
            return {key: replace(item) for key, item in value.items()}
        return value

    unsigned = {
        "schema_version": 1,
        "mode": "MASTER_AUDIT_COMPACT_DISPATCH_NO_WRITES",
        "target_state": envelope["target_state"],
        "envelope_id": envelope_id,
        "repository": repository,
        "specification_sha256": envelope["specification"]["sha256"],
        "tracked_corpus": {
            "path_count": envelope["tracked_corpus"]["path_count"],
            "paths_sha256": envelope["tracked_corpus"]["paths_sha256"],
            "files_sha256": envelope["tracked_corpus"]["files_sha256"],
        },
        "qualitative_review": {
            "path_count": review["path_count"],
            "group_count": review["group_count"],
        },
        "accepted_release_count": len(envelope["accepted_releases"]),
        "command_contract": replace(contract),
        "reviewer_independence": envelope["reviewer_independence"],
        "output": envelope["output"],
        "authorities": envelope["authorities"],
        "prohibitions": envelope["prohibitions"],
        "transport": {
            "target_free_probe_before_reviewer_creation": True,
            "packet_must_arrive_unchanged": True,
            "missing_group_footer_disposition": "INCOMPLETE_NO_RETRY",
        },
    }
    return {**unsigned, "dispatch_id": sha256_bytes(canonical_json_bytes(unsigned))}


def render_group(root: Path, envelope: Mapping[str, object], ordinal: int) -> str:
    review = envelope["qualitative_review"]
    groups = review["groups"]
    if type(ordinal) is not int or ordinal < 1 or ordinal > len(groups):
        raise ContractError("Master Audit group ordinal is out of range")
    item = groups[ordinal - 1]
    slice_data = item["slice"]
    binding = FileBinding(
        path=slice_data["path"],
        bytes=next(
            file["bytes"] for file in review["files"] if file["path"] == slice_data["path"]
        ),
        sha256=slice_data["file_sha256"],
        line_count=next(
            file["line_count"]
            for file in review["files"]
            if file["path"] == slice_data["path"]
        ),
    )
    lines = _file_lines(root, binding)
    if slice_data["start_line"] == 0:
        rendered = f"===== {binding.path}:EMPTY =====\n{GROUP_FOOTER}"
    else:
        rendered = _render_slice(
            binding.path,
            slice_data["start_line"],
            slice_data["end_line"],
            lines,
        )
    if len(rendered.encode("utf-8")) != item["rendered_utf8_bytes"]:
        raise IntegrityError("Master Audit rendered group size differs")
    return rendered


def _summary(envelope: Mapping[str, object], mode: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "target_state": envelope["target_state"],
        "envelope_id": envelope["envelope_id"],
        "repository": envelope["repository"],
        "tracked_path_count": envelope["tracked_corpus"]["path_count"],
        "review_path_count": envelope["qualitative_review"]["path_count"],
        "read_group_count": envelope["qualitative_review"]["group_count"],
        "accepted_release_count": len(envelope["accepted_releases"]),
        "writes": 0,
        "network_calls": 0,
    }


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Plan or read one exact no-write Master Audit envelope"
    )
    value.add_argument("--target-state", choices=TARGET_STATES, required=True)
    action = value.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--dispatch", action="store_true")
    action.add_argument("--mode", choices=MODES)
    value.add_argument("--approved-envelope-id")
    value.add_argument("--group", type=int)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = _repo_root()
    if args.plan:
        if args.approved_envelope_id or args.group is not None:
            parser().error("--plan does not accept approval or group arguments")
        print(json.dumps(build_envelope(root, args.target_state), indent=2, sort_keys=True))
        return 0
    if not args.approved_envelope_id:
        parser().error("--dispatch and --mode require --approved-envelope-id")
    envelope = _approved_envelope(root, args.target_state, args.approved_envelope_id)
    if args.dispatch:
        if args.group is not None:
            parser().error("--dispatch does not accept --group")
        print(json.dumps(build_dispatch(envelope), indent=2, sort_keys=True))
        return 0
    if args.mode == "ReadGroup":
        if args.group is None:
            parser().error("ReadGroup requires --group")
        sys.stdout.write(render_group(root, envelope, args.group))
        return 0
    if args.group is not None:
        parser().error("--group is valid only for ReadGroup")
    if args.mode == "VerifyReleases":
        output = {
            **_summary(envelope, "MASTER_AUDIT_RELEASES_VERIFIED_NO_WRITES"),
            "accepted_releases": envelope["accepted_releases"],
        }
    elif args.mode == "PlanGroups":
        output = {
            **_summary(envelope, "MASTER_AUDIT_GROUP_PLAN_NO_WRITES"),
            "groups": envelope["qualitative_review"]["groups"],
        }
    elif args.mode == "HostProfile":
        output = {
            **_summary(envelope, "MASTER_AUDIT_HOST_PROFILE_NO_WRITES"),
            "runtime": envelope["runtime"],
        }
    elif args.mode == "FinalPreflight":
        output = _summary(envelope, "MASTER_AUDIT_FINAL_PREFLIGHT_NO_WRITES")
    else:
        output = _summary(envelope, "MASTER_AUDIT_PREFLIGHT_NO_WRITES")
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
