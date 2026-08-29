"""Content-addressed, non-authorizing Meta Audit envelope contracts.

The module prepares and validates exact reviewer process manifests.  It does
not execute a Meta Audit, read a target, or grant project authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, safe_relative_path, sha256_bytes
from .errors import ContractError, IntegrityError


SCHEMA_VERSION_V2 = 2
EXPECTED_EXIT = "REQUIRED_SUCCESS"
OUTPUT_DESTINATION = "CONVERSATION_ONLY"
POWERSHELL_FLAGS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-File")
MODES = (
    "Preflight",
    "PlanBatches",
    "ReadReferenceBatch",
    "ReadTargetBatch",
    "PlanGroups",
    "ReadGroup",
    "FinalPreflight",
)
MAX_GROUP_LINES = 400
MAX_GROUP_UTF8_BYTES = 20_000
READ_GROUP_FOOTER = "===== META_AUDIT_GROUP_COMPLETE =====\n"
LOCAL_CORRECTION_BUDGET = 2
FAILURE_CLASSES = (
    "LOCAL_CORRECTABLE",
    "READ_ONLY_INVOCATION",
    "MUTATING_OR_EXTERNAL",
)
PROHIBITIONS = (
    "TESTS",
    "PYTHON_IMPORTS",
    "PROJECT_READINESS_AUDIT",
    "MASTER_AUDIT_EXECUTION",
    "FILE_WRITES",
    "RETAINED_REPORT",
    "PROVIDER_ACTIVITY",
    "DATA_ACCESS",
    "DATA_MUTATION",
    "RESEARCH",
    "TRAINING",
    "EVALUATION",
    "PREDICTION",
    "ACTIVATION",
    "TRADING",
)


def _exact_dict(value: object, fields: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ContractError(f"{name} must be an exact object")
    result = dict(value)
    if set(result) != fields:
        raise ContractError(f"{name} fields differ from the exact contract")
    return result


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value:
        raise ContractError(f"{name} must be nonempty text")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ContractError(f"{name} must be a positive integer")
    return value


def _bool(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{name} must be Boolean")
    return value


def _absolute_path(value: object, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute():
        raise ContractError(f"{name} must be absolute")
    return path


def _relative_path(value: object, name: str) -> str:
    text = _text(value, name)
    try:
        return safe_relative_path(text).as_posix()
    except ContractError as exc:
        raise ContractError(f"{name} must be a safe relative path") from exc


def _git_sha1(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 40 or any(character not in "0123456789abcdef" for character in text):
        raise ContractError(f"{name} must be a lowercase Git SHA-1")
    return text


@dataclass(frozen=True)
class FileBinding:
    path: str
    bytes: int
    sha256: str
    git_blob: str

    @classmethod
    def from_dict(cls, value: object, name: str) -> FileBinding:
        item = _exact_dict(value, {"path", "bytes", "sha256", "git_blob"}, name)
        return cls(
            path=_relative_path(item["path"], f"{name}.path"),
            bytes=_positive_int(item["bytes"], f"{name}.bytes"),
            sha256=require_sha256(item["sha256"], f"{name}.sha256"),
            git_blob=_git_sha1(item["git_blob"], f"{name}.git_blob"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "git_blob": self.git_blob,
        }


@dataclass(frozen=True)
class HostBinding:
    powershell_executable: Path
    powershell_sha256: str
    powershell_file_version: str
    ps_version: str
    ps_edition: str
    clr_version: str
    is_64bit_process: bool
    sha256_hash_data_available: bool
    sha1_hash_data_available: bool
    path_get_relative_path_available: bool

    @classmethod
    def from_dict(cls, value: object) -> HostBinding:
        fields = {
            "powershell_executable",
            "powershell_sha256",
            "powershell_file_version",
            "ps_version",
            "ps_edition",
            "clr_version",
            "is_64bit_process",
            "sha256_hash_data_available",
            "sha1_hash_data_available",
            "path_get_relative_path_available",
        }
        item = _exact_dict(value, fields, "host")
        return cls(
            powershell_executable=_absolute_path(
                item["powershell_executable"], "host.powershell_executable"
            ),
            powershell_sha256=require_sha256(
                item["powershell_sha256"], "host.powershell_sha256"
            ),
            powershell_file_version=_text(
                item["powershell_file_version"], "host.powershell_file_version"
            ),
            ps_version=_text(item["ps_version"], "host.ps_version"),
            ps_edition=_text(item["ps_edition"], "host.ps_edition"),
            clr_version=_text(item["clr_version"], "host.clr_version"),
            is_64bit_process=_bool(
                item["is_64bit_process"], "host.is_64bit_process"
            ),
            sha256_hash_data_available=_bool(
                item["sha256_hash_data_available"],
                "host.sha256_hash_data_available",
            ),
            sha1_hash_data_available=_bool(
                item["sha1_hash_data_available"], "host.sha1_hash_data_available"
            ),
            path_get_relative_path_available=_bool(
                item["path_get_relative_path_available"],
                "host.path_get_relative_path_available",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "powershell_executable": str(self.powershell_executable),
            "powershell_sha256": self.powershell_sha256,
            "powershell_file_version": self.powershell_file_version,
            "ps_version": self.ps_version,
            "ps_edition": self.ps_edition,
            "clr_version": self.clr_version,
            "is_64bit_process": self.is_64bit_process,
            "sha256_hash_data_available": self.sha256_hash_data_available,
            "sha1_hash_data_available": self.sha1_hash_data_available,
            "path_get_relative_path_available": self.path_get_relative_path_available,
        }


@dataclass(frozen=True)
class RepositoryBinding:
    root: Path
    branch: str
    head: str
    tree: str
    require_clean: bool

    @classmethod
    def from_dict(cls, value: object) -> RepositoryBinding:
        item = _exact_dict(
            value, {"root", "branch", "head", "tree", "require_clean"}, "repository"
        )
        return cls(
            root=_absolute_path(item["root"], "repository.root"),
            branch=_text(item["branch"], "repository.branch"),
            head=_git_sha1(item["head"], "repository.head"),
            tree=_git_sha1(item["tree"], "repository.tree"),
            require_clean=_bool(item["require_clean"], "repository.require_clean"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "branch": self.branch,
            "head": self.head,
            "tree": self.tree,
            "require_clean": self.require_clean,
        }


def git_blob_sha1_bytes(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _strict_lf_lines(data: bytes, path: str) -> tuple[bytes, ...]:
    if b"\r" in data:
        raise ContractError(f"NON_LF_TEXT: {path} contains CR bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError(f"NON_UTF8_TEXT: {path} is not strict UTF-8") from exc
    lines = data.split(b"\n")
    if data.endswith(b"\n"):
        lines = lines[:-1]
    if not lines:
        raise ContractError(f"EMPTY_REFERENCE_TEXT: {path} has no readable lines")
    return tuple(lines)


@dataclass(frozen=True)
class ReadSliceV2:
    path: str
    start_line: int
    line_count: int
    file_sha256: str
    file_git_blob: str
    target: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "start_line": self.start_line,
            "line_count": self.line_count,
            "file_sha256": self.file_sha256,
            "file_git_blob": self.file_git_blob,
            "target": self.target,
        }


@dataclass(frozen=True)
class ReadGroupV2:
    group_ordinal: int
    phase: str
    slices: tuple[ReadSliceV2, ...]
    rendered_line_count: int
    rendered_utf8_bytes: int

    def as_dict(self) -> dict[str, object]:
        return {
            "group_ordinal": self.group_ordinal,
            "phase": self.phase,
            "slices": [item.as_dict() for item in self.slices],
            "rendered_line_count": self.rendered_line_count,
            "rendered_utf8_bytes": self.rendered_utf8_bytes,
        }


def _slice_rendered_bytes(item: ReadSliceV2, lines: tuple[bytes, ...]) -> int:
    end_line = item.start_line + item.line_count - 1
    header = (
        f"===== {item.path} lines {item.start_line}-{end_line} =====\n".encode(
            "utf-8"
        )
    )
    total = len(header)
    for line_number in range(item.start_line, end_line + 1):
        total += len(f"{line_number:06d}: ".encode("ascii"))
        total += len(lines[line_number - 1]) + 1
    return total


def _group_metrics(
    slices: tuple[ReadSliceV2, ...],
    line_map: Mapping[str, tuple[bytes, ...]],
) -> tuple[int, int]:
    line_count = sum(item.line_count for item in slices)
    rendered_bytes = sum(
        _slice_rendered_bytes(item, line_map[item.path]) for item in slices
    )
    if slices:
        rendered_bytes += len(READ_GROUP_FOOTER.encode("ascii"))
    return line_count, rendered_bytes


def _pack_phase(
    bindings: tuple[FileBinding, ...],
    *,
    root: Path,
    target: bool,
    phase: str,
) -> tuple[ReadGroupV2, ...]:
    line_map: dict[str, tuple[bytes, ...]] = {}
    for binding in bindings:
        path = root / PurePosixPath(binding.path)
        data = path.read_bytes()
        if len(data) != binding.bytes or sha256_bytes(data) != binding.sha256:
            raise IntegrityError(f"REFERENCE_IDENTITY_MISMATCH: {binding.path}")
        if git_blob_sha1_bytes(data) != binding.git_blob:
            raise IntegrityError(f"REFERENCE_GIT_BLOB_MISMATCH: {binding.path}")
        line_map[binding.path] = _strict_lf_lines(data, binding.path)

    groups: list[ReadGroupV2] = []
    current: tuple[ReadSliceV2, ...] = ()

    def flush() -> None:
        nonlocal current
        if not current:
            return
        lines, rendered = _group_metrics(current, line_map)
        groups.append(
            ReadGroupV2(
                group_ordinal=len(groups) + 1,
                phase=phase,
                slices=current,
                rendered_line_count=lines,
                rendered_utf8_bytes=rendered,
            )
        )
        current = ()

    for binding in bindings:
        total_lines = len(line_map[binding.path])
        next_line = 1
        while next_line <= total_lines:
            if (
                current
                and current[-1].path == binding.path
                and current[-1].start_line + current[-1].line_count == next_line
            ):
                prior = current[-1]
                candidate_slice = ReadSliceV2(
                    path=prior.path,
                    start_line=prior.start_line,
                    line_count=prior.line_count + 1,
                    file_sha256=prior.file_sha256,
                    file_git_blob=prior.file_git_blob,
                    target=target,
                )
                candidate = (*current[:-1], candidate_slice)
            else:
                candidate = (
                    *current,
                    ReadSliceV2(
                        path=binding.path,
                        start_line=next_line,
                        line_count=1,
                        file_sha256=binding.sha256,
                        file_git_blob=binding.git_blob,
                        target=target,
                    ),
                )
            lines, rendered = _group_metrics(candidate, line_map)
            if lines <= MAX_GROUP_LINES and rendered <= MAX_GROUP_UTF8_BYTES:
                current = candidate
                next_line += 1
                continue
            if not current:
                raise ContractError(
                    f"UNRENDERABLE_LINE: {binding.path}:{next_line} exceeds "
                    "the bounded group output"
                )
            flush()
    flush()
    return tuple(groups)


def build_maximal_read_groups(
    *,
    root: Path,
    reference_bindings: tuple[FileBinding, ...],
    target_binding: FileBinding,
) -> tuple[ReadGroupV2, ...]:
    references = _pack_phase(
        reference_bindings,
        root=root,
        target=False,
        phase="REFERENCE",
    )
    targets = _pack_phase(
        (target_binding,),
        root=root,
        target=True,
        phase="TARGET",
    )
    combined = (*references, *targets)
    return tuple(
        ReadGroupV2(
            group_ordinal=index,
            phase=group.phase,
            slices=group.slices,
            rendered_line_count=group.rendered_line_count,
            rendered_utf8_bytes=group.rendered_utf8_bytes,
        )
        for index, group in enumerate(combined, start=1)
    )


def _v2_command(
    *,
    ordinal: int,
    mode: str,
    group_ordinal: int | None,
    root: Path,
    powershell_executable: Path,
    script_path: Path,
    timeout_seconds: int,
    output_max_utf8_bytes: int,
) -> dict[str, object]:
    return {
        "command_ordinal": ordinal,
        "mode": mode,
        "group_ordinal": group_ordinal,
        "argv": [
            str(powershell_executable),
            *POWERSHELL_FLAGS,
            str(script_path),
            "-Mode",
            mode,
            "-EnvelopePath",
            "{ENVELOPE_PATH}",
            "-EnvelopeSha256",
            "{ENVELOPE_SHA256}",
            "-CommandOrdinal",
            str(ordinal),
        ],
        "cwd": str(root),
        "environment_additions": {},
        "run_limit": 1,
        "timeout_seconds": timeout_seconds,
        "expected_exit": EXPECTED_EXIT,
        "output_max_utf8_bytes": output_max_utf8_bytes,
    }


def build_v2_envelope_payload(unsigned: Mapping[str, object]) -> dict[str, object]:
    value = dict(unsigned)
    if "envelope_id" in value:
        raise ContractError("unsigned envelope cannot contain envelope_id")
    value["schema_version"] = SCHEMA_VERSION_V2
    payload = {**value, "envelope_id": sha256_bytes(canonical_json_bytes(value))}
    return validate_v2_envelope_payload(payload)


def validate_v2_envelope_payload(value: object) -> dict[str, object]:
    fields = {
        "schema_version",
        "envelope_id",
        "repository",
        "host",
        "script",
        "target",
        "controller",
        "corpus_policy",
        "reference_census",
        "read_groups",
        "commands",
        "barriers",
        "reviewer_independence",
        "failure_class",
        "encoding",
        "output",
        "prohibitions",
    }
    item = _exact_dict(value, fields, "envelope_v2")
    if item["schema_version"] != SCHEMA_VERSION_V2:
        raise ContractError("UNSUPPORTED_SCHEMA: envelope_v2 must use schema 2")
    envelope_id = require_sha256(item["envelope_id"], "envelope_v2.envelope_id")
    repository = RepositoryBinding.from_dict(item["repository"])
    host = HostBinding.from_dict(item["host"])
    script = FileBinding.from_dict(item["script"], "script")
    target = FileBinding.from_dict(item["target"], "target")
    controller = FileBinding.from_dict(item["controller"], "controller")
    corpus_policy = FileBinding.from_dict(item["corpus_policy"], "corpus_policy")
    census = _exact_dict(
        item["reference_census"],
        {"count", "sha256", "paths_sha256"},
        "reference_census",
    )
    _positive_int(census["count"], "reference_census.count")
    require_sha256(census["sha256"], "reference_census.sha256")
    require_sha256(census["paths_sha256"], "reference_census.paths_sha256")
    if type(item["read_groups"]) is not list or not item["read_groups"]:
        raise ContractError("MISSING_READ_GROUPS: read_groups must be nonempty")
    groups: list[dict[str, object]] = []
    phases: list[str] = []
    for index, raw_group in enumerate(item["read_groups"], start=1):
        group = _exact_dict(
            raw_group,
            {
                "group_ordinal",
                "phase",
                "slices",
                "rendered_line_count",
                "rendered_utf8_bytes",
            },
            f"read_groups[{index - 1}]",
        )
        if group["group_ordinal"] != index:
            raise ContractError("GROUP_ORDER_MISMATCH: group ordinals are not contiguous")
        phase = _text(group["phase"], "read_group.phase")
        if phase not in {"REFERENCE", "TARGET"}:
            raise ContractError("GROUP_PHASE_INVALID: unsupported read-group phase")
        phases.append(phase)
        line_count = _positive_int(
            group["rendered_line_count"], "read_group.rendered_line_count"
        )
        rendered_bytes = _positive_int(
            group["rendered_utf8_bytes"], "read_group.rendered_utf8_bytes"
        )
        if line_count > MAX_GROUP_LINES or rendered_bytes > MAX_GROUP_UTF8_BYTES:
            raise ContractError("GROUP_OUTPUT_LIMIT_EXCEEDED: read group is oversized")
        if type(group["slices"]) is not list or not group["slices"]:
            raise ContractError("EMPTY_READ_GROUP: read group lacks slices")
        slices: list[dict[str, object]] = []
        for raw_slice in group["slices"]:
            slice_item = _exact_dict(
                raw_slice,
                {
                    "path",
                    "start_line",
                    "line_count",
                    "file_sha256",
                    "file_git_blob",
                    "target",
                },
                "read_group.slice",
            )
            path = _relative_path(slice_item["path"], "read_group.slice.path")
            target_applicable = _bool(
                slice_item["target"], "read_group.slice.target"
            )
            if target_applicable != (phase == "TARGET") or target_applicable != (
                path == target.path
            ):
                raise ContractError(
                    "BATCH_APPLICABILITY_MISMATCH: slice phase or target differs"
                )
            slices.append(
                {
                    "path": path,
                    "start_line": _positive_int(
                        slice_item["start_line"], "read_group.slice.start_line"
                    ),
                    "line_count": _positive_int(
                        slice_item["line_count"], "read_group.slice.line_count"
                    ),
                    "file_sha256": require_sha256(
                        slice_item["file_sha256"], "read_group.slice.file_sha256"
                    ),
                    "file_git_blob": _git_sha1(
                        slice_item["file_git_blob"],
                        "read_group.slice.file_git_blob",
                    ),
                    "target": target_applicable,
                }
            )
        groups.append(
            {
                "group_ordinal": index,
                "phase": phase,
                "slices": slices,
                "rendered_line_count": line_count,
                "rendered_utf8_bytes": rendered_bytes,
            }
        )
    if "TARGET" not in phases or "REFERENCE" not in phases:
        raise ContractError("MISSING_REVIEW_PHASE: reference and target groups required")
    first_target_index = phases.index("TARGET")
    if any(phase != "REFERENCE" for phase in phases[:first_target_index]) or any(
        phase != "TARGET" for phase in phases[first_target_index:]
    ):
        raise ContractError("BLIND_ORDER_MISMATCH: target groups must follow references")

    independence = _exact_dict(
        item["reviewer_independence"],
        {
            "reviewer_instance_binding",
            "no_inherited_turns",
            "no_prior_target_access",
            "target_access_barrier",
            "final_attestation_required",
        },
        "reviewer_independence",
    )
    require_sha256(
        independence["reviewer_instance_binding"],
        "reviewer_independence.reviewer_instance_binding",
    )
    for field in (
        "no_inherited_turns",
        "no_prior_target_access",
        "final_attestation_required",
    ):
        if _bool(independence[field], f"reviewer_independence.{field}") is not True:
            raise ContractError("REVIEWER_INDEPENDENCE_REQUIRED: binding must be true")
    if independence["target_access_barrier"] != "B01_BLIND_CENSUS_FROZEN":
        raise ContractError("TARGET_BARRIER_MISMATCH: B01 is required")
    if item["failure_class"] != "READ_ONLY_INVOCATION":
        raise ContractError("FAILURE_CLASS_MISMATCH: Meta Audit must be read-only")
    encoding = _exact_dict(
        item["encoding"], {"name", "bom", "console"}, "encoding"
    )
    if encoding != {"name": "UTF-8", "bom": False, "console": "UTF-8"}:
        raise ContractError("ENCODING_MISMATCH: exact UTF-8 binding is required")
    output = _exact_dict(item["output"], {"destination", "retained"}, "output")
    if output != {"destination": OUTPUT_DESTINATION, "retained": False}:
        raise ContractError("OUTPUT_DESTINATION_MISMATCH: conversation-only required")
    if item["prohibitions"] != list(PROHIBITIONS):
        raise ContractError("PROHIBITIONS_MISMATCH: exact safe contract required")
    if type(item["commands"]) is not list:
        raise ContractError("COMMAND_CENSUS_INVALID: commands must be a list")
    commands = item["commands"]
    if len(commands) != len(groups) + 3:
        raise ContractError("COMMAND_CENSUS_INVALID: commands differ from groups")
    expected_modes = ["Preflight", "PlanGroups", *("ReadGroup" for _ in groups), "FinalPreflight"]
    script_path = repository.root / PurePosixPath(script.path)
    for index, (raw_command, expected_mode) in enumerate(
        zip(commands, expected_modes, strict=True), start=1
    ):
        command = _exact_dict(
            raw_command,
            {
                "command_ordinal",
                "mode",
                "group_ordinal",
                "argv",
                "cwd",
                "environment_additions",
                "run_limit",
                "timeout_seconds",
                "expected_exit",
                "output_max_utf8_bytes",
            },
            "command_v2",
        )
        expected_group = index - 2 if expected_mode == "ReadGroup" else None
        if (
            command["command_ordinal"] != index
            or command["mode"] != expected_mode
            or command["group_ordinal"] != expected_group
        ):
            raise ContractError("COMMAND_ORDER_MISMATCH: command differs from group")
        expected_argv = [
            str(host.powershell_executable),
            *POWERSHELL_FLAGS,
            str(script_path),
            "-Mode",
            expected_mode,
            "-EnvelopePath",
            "{ENVELOPE_PATH}",
            "-EnvelopeSha256",
            "{ENVELOPE_SHA256}",
            "-CommandOrdinal",
            str(index),
        ]
        if command["argv"] != expected_argv:
            raise ContractError("COMMAND_ARGV_MISMATCH: literal argv differs")
        if (
            command["cwd"] != str(repository.root)
            or command["environment_additions"] != {}
            or command["run_limit"] != 1
            or command["expected_exit"] != EXPECTED_EXIT
        ):
            raise ContractError("COMMAND_CONTRACT_MISMATCH: command policy differs")
        _positive_int(command["timeout_seconds"], "command.timeout_seconds")
        output_limit = _positive_int(
            command["output_max_utf8_bytes"], "command.output_max_utf8_bytes"
        )
        if expected_group is not None and output_limit != groups[expected_group - 1][
            "rendered_utf8_bytes"
        ]:
            raise ContractError("GROUP_OUTPUT_BINDING_MISMATCH: output limit differs")
    expected_barriers = [
        {
            "name": "B01_BLIND_CENSUS_FROZEN",
            "after_command_ordinal": first_target_index + 2,
            "before_command_ordinal": first_target_index + 3,
        },
        {
            "name": "B02_MAPPING_COMPLETE",
            "after_command_ordinal": len(groups) + 2,
            "before_command_ordinal": len(groups) + 3,
        },
    ]
    if item["barriers"] != expected_barriers:
        raise ContractError("BARRIER_MISMATCH: exact B01/B02 boundaries required")

    normalized = {
        "schema_version": SCHEMA_VERSION_V2,
        "repository": repository.as_dict(),
        "host": host.as_dict(),
        "script": script.as_dict(),
        "target": target.as_dict(),
        "controller": controller.as_dict(),
        "corpus_policy": corpus_policy.as_dict(),
        "reference_census": {
            "count": census["count"],
            "sha256": census["sha256"],
            "paths_sha256": census["paths_sha256"],
        },
        "read_groups": groups,
        "commands": commands,
        "barriers": expected_barriers,
        "reviewer_independence": independence,
        "failure_class": "READ_ONLY_INVOCATION",
        "encoding": encoding,
        "output": output,
        "prohibitions": list(PROHIBITIONS),
    }
    expected_id = sha256_bytes(canonical_json_bytes(normalized))
    if envelope_id != expected_id:
        raise IntegrityError("envelope_id differs from canonical unsigned content")
    return {**normalized, "envelope_id": envelope_id}


def build_reviewer_dispatch(
    envelope: Mapping[str, object],
    *,
    envelope_path: Path,
    envelope_sha256: str,
) -> dict[str, object]:
    """Build a content-addressed, target-content-free reviewer transport packet."""

    normalized = validate_v2_envelope_payload(dict(envelope))
    path = envelope_path.resolve()
    if not path.is_absolute():
        raise ContractError("DISPATCH_ENVELOPE_PATH_INVALID: path must be absolute")
    file_sha256 = require_sha256(
        envelope_sha256, "reviewer_dispatch.envelope_sha256"
    )
    groups = normalized["read_groups"]
    commands: list[dict[str, object]] = []
    for source in normalized["commands"]:
        command = dict(source)
        group_ordinal = command["group_ordinal"]
        phase = None
        footer = None
        if group_ordinal is not None:
            phase = groups[group_ordinal - 1]["phase"]
            footer = READ_GROUP_FOOTER
        argv = [
            (
                str(path)
                if value == "{ENVELOPE_PATH}"
                else file_sha256
                if value == "{ENVELOPE_SHA256}"
                else value
            )
            for value in command["argv"]
        ]
        commands.append(
            {
                "command_ordinal": command["command_ordinal"],
                "mode": command["mode"],
                "group_ordinal": group_ordinal,
                "phase": phase,
                "argv": argv,
                "cwd": command["cwd"],
                "environment_additions": command["environment_additions"],
                "run_limit": command["run_limit"],
                "timeout_seconds": command["timeout_seconds"],
                "expected_exit": command["expected_exit"],
                "output_max_utf8_bytes": command["output_max_utf8_bytes"],
                "required_stdout_footer": footer,
            }
        )
    unsigned = {
        "schema_version": 1,
        "mode": "REVIEWER_DISPATCH_NO_WRITES",
        "envelope": {
            "path": str(path),
            "sha256": file_sha256,
            "envelope_id": normalized["envelope_id"],
        },
        "repository": normalized["repository"],
        "script": normalized["script"],
        "commands": commands,
        "barriers": normalized["barriers"],
        "reviewer_independence": normalized["reviewer_independence"],
        "failure_class": normalized["failure_class"],
        "output": normalized["output"],
        "prohibitions": normalized["prohibitions"],
        "transport": {
            "dispatch_must_precede_reviewer_creation": True,
            "reviewer_envelope_read_outside_declared_commands": False,
            "read_group_completion_footer": READ_GROUP_FOOTER,
            "missing_footer_disposition": "STOP_INCOMPLETE_NO_RETRY",
        },
    }
    return {
        **unsigned,
        "dispatch_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def canonical_reviewer_dispatch_bytes(value: object) -> bytes:
    """Validate and serialize the complete reviewer dispatch without projection."""

    fields = {
        "schema_version",
        "mode",
        "envelope",
        "repository",
        "script",
        "commands",
        "barriers",
        "reviewer_independence",
        "failure_class",
        "output",
        "prohibitions",
        "transport",
        "dispatch_id",
    }
    item = _exact_dict(value, fields, "reviewer_dispatch")
    if item["schema_version"] != 1:
        raise ContractError("REVIEWER_DISPATCH_SCHEMA_MISMATCH")
    if item["mode"] != "REVIEWER_DISPATCH_NO_WRITES":
        raise ContractError("REVIEWER_DISPATCH_MODE_MISMATCH")

    envelope = _exact_dict(
        item["envelope"], {"path", "sha256", "envelope_id"}, "dispatch.envelope"
    )
    envelope_path = _absolute_path(envelope["path"], "dispatch.envelope.path")
    envelope_sha256 = require_sha256(
        envelope["sha256"], "dispatch.envelope.sha256"
    )
    require_sha256(envelope["envelope_id"], "dispatch.envelope.envelope_id")
    repository = RepositoryBinding.from_dict(item["repository"])
    if not repository.require_clean:
        raise ContractError("REVIEWER_DISPATCH_REPOSITORY_MISMATCH")
    script = FileBinding.from_dict(item["script"], "dispatch.script")

    if type(item["commands"]) is not list or len(item["commands"]) < 5:
        raise ContractError("REVIEWER_DISPATCH_COMMAND_CENSUS_MISMATCH")
    commands = item["commands"]
    powershell_executable: Path | None = None
    read_phases: list[str] = []
    for index, raw_command in enumerate(commands, start=1):
        command = _exact_dict(
            raw_command,
            {
                "command_ordinal",
                "mode",
                "group_ordinal",
                "phase",
                "argv",
                "cwd",
                "environment_additions",
                "run_limit",
                "timeout_seconds",
                "expected_exit",
                "output_max_utf8_bytes",
                "required_stdout_footer",
            },
            "reviewer_dispatch.command",
        )
        if index == 1:
            expected_mode = "Preflight"
        elif index == 2:
            expected_mode = "PlanGroups"
        elif index == len(commands):
            expected_mode = "FinalPreflight"
        else:
            expected_mode = "ReadGroup"
        expected_group = index - 2 if expected_mode == "ReadGroup" else None
        if (
            command["command_ordinal"] != index
            or command["mode"] != expected_mode
            or command["group_ordinal"] != expected_group
        ):
            raise ContractError("REVIEWER_DISPATCH_COMMAND_ORDER_MISMATCH")

        phase = command["phase"]
        if expected_mode == "ReadGroup":
            if phase not in {"REFERENCE", "TARGET"}:
                raise ContractError("REVIEWER_DISPATCH_PHASE_MISMATCH")
            read_phases.append(phase)
            expected_footer = READ_GROUP_FOOTER
            expected_timeout = 60
        else:
            if phase is not None:
                raise ContractError("REVIEWER_DISPATCH_PHASE_MISMATCH")
            expected_footer = None
            expected_timeout = 30
        if command["required_stdout_footer"] != expected_footer:
            raise ContractError("REVIEWER_DISPATCH_FOOTER_MISMATCH")
        if (
            command["cwd"] != str(repository.root)
            or command["environment_additions"] != {}
            or command["run_limit"] != 1
            or command["expected_exit"] != EXPECTED_EXIT
            or command["timeout_seconds"] != expected_timeout
        ):
            raise ContractError("REVIEWER_DISPATCH_COMMAND_CONTRACT_MISMATCH")
        output_limit = _positive_int(
            command["output_max_utf8_bytes"],
            "reviewer_dispatch.command.output_max_utf8_bytes",
        )
        if expected_mode == "ReadGroup":
            if output_limit > MAX_GROUP_UTF8_BYTES:
                raise ContractError("REVIEWER_DISPATCH_OUTPUT_LIMIT_MISMATCH")
        elif output_limit != 4_000:
            raise ContractError("REVIEWER_DISPATCH_OUTPUT_LIMIT_MISMATCH")

        if type(command["argv"]) is not list or not command["argv"]:
            raise ContractError("REVIEWER_DISPATCH_ARGV_MISMATCH")
        argv = [
            _text(argument, "reviewer_dispatch.command.argv")
            for argument in command["argv"]
        ]
        if powershell_executable is None:
            powershell_executable = _absolute_path(
                argv[0], "reviewer_dispatch.command.argv[0]"
            )
        expected_argv = [
            str(powershell_executable),
            *POWERSHELL_FLAGS,
            str(repository.root / PurePosixPath(script.path)),
            "-Mode",
            expected_mode,
            "-EnvelopePath",
            str(envelope_path),
            "-EnvelopeSha256",
            envelope_sha256,
            "-CommandOrdinal",
            str(index),
        ]
        if argv != expected_argv:
            raise ContractError("REVIEWER_DISPATCH_ARGV_MISMATCH")

    if "REFERENCE" not in read_phases or "TARGET" not in read_phases:
        raise ContractError("REVIEWER_DISPATCH_PHASE_MISMATCH")
    first_target_index = read_phases.index("TARGET")
    if any(phase != "REFERENCE" for phase in read_phases[:first_target_index]) or any(
        phase != "TARGET" for phase in read_phases[first_target_index:]
    ):
        raise ContractError("REVIEWER_DISPATCH_PHASE_MISMATCH")
    first_target_command = first_target_index + 3
    expected_barriers = [
        {
            "name": "B01_BLIND_CENSUS_FROZEN",
            "after_command_ordinal": first_target_command - 1,
            "before_command_ordinal": first_target_command,
        },
        {
            "name": "B02_MAPPING_COMPLETE",
            "after_command_ordinal": len(commands) - 1,
            "before_command_ordinal": len(commands),
        },
    ]
    if item["barriers"] != expected_barriers:
        raise ContractError("REVIEWER_DISPATCH_BARRIER_MISMATCH")

    independence = _exact_dict(
        item["reviewer_independence"],
        {
            "reviewer_instance_binding",
            "no_inherited_turns",
            "no_prior_target_access",
            "target_access_barrier",
            "final_attestation_required",
        },
        "reviewer_dispatch.reviewer_independence",
    )
    require_sha256(
        independence["reviewer_instance_binding"],
        "reviewer_dispatch.reviewer_instance_binding",
    )
    if (
        independence["no_inherited_turns"] is not True
        or independence["no_prior_target_access"] is not True
        or independence["final_attestation_required"] is not True
        or independence["target_access_barrier"] != "B01_BLIND_CENSUS_FROZEN"
    ):
        raise ContractError("REVIEWER_DISPATCH_INDEPENDENCE_MISMATCH")
    if item["failure_class"] != "READ_ONLY_INVOCATION":
        raise ContractError("REVIEWER_DISPATCH_FAILURE_CLASS_MISMATCH")
    if item["output"] != {"destination": OUTPUT_DESTINATION, "retained": False}:
        raise ContractError("REVIEWER_DISPATCH_OUTPUT_MISMATCH")
    if item["prohibitions"] != list(PROHIBITIONS):
        raise ContractError("REVIEWER_DISPATCH_PROHIBITIONS_MISMATCH")
    if item["transport"] != {
        "dispatch_must_precede_reviewer_creation": True,
        "reviewer_envelope_read_outside_declared_commands": False,
        "read_group_completion_footer": READ_GROUP_FOOTER,
        "missing_footer_disposition": "STOP_INCOMPLETE_NO_RETRY",
    }:
        raise ContractError("REVIEWER_DISPATCH_TRANSPORT_MISMATCH")

    dispatch_id = require_sha256(item["dispatch_id"], "reviewer_dispatch.dispatch_id")
    unsigned = {key: value for key, value in item.items() if key != "dispatch_id"}
    if dispatch_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("REVIEWER_DISPATCH_ID_MISMATCH")
    return canonical_json_bytes(item)
