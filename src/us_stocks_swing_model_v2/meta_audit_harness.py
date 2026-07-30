"""Content-addressed, non-authorizing Meta Audit envelope contracts.

The module prepares and validates exact reviewer process manifests.  It does
not execute a Meta Audit, read a target, or grant project authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, safe_relative_path, sha256_bytes
from .errors import ContractError, IntegrityError


SCHEMA_VERSION = 1
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


@dataclass(frozen=True)
class ReadBatch:
    batch_ordinal: int
    phase: str
    path: str
    start_line: int
    line_count: int
    max_rendered_utf8_bytes: int
    file_sha256: str
    file_git_blob: str
    target: bool

    @classmethod
    def from_dict(cls, value: object, name: str) -> ReadBatch:
        fields = {
            "batch_ordinal",
            "phase",
            "path",
            "start_line",
            "line_count",
            "max_rendered_utf8_bytes",
            "file_sha256",
            "file_git_blob",
            "target",
        }
        item = _exact_dict(value, fields, name)
        return cls(
            batch_ordinal=_positive_int(item["batch_ordinal"], f"{name}.batch_ordinal"),
            phase=_text(item["phase"], f"{name}.phase"),
            path=_relative_path(item["path"], f"{name}.path"),
            start_line=_positive_int(item["start_line"], f"{name}.start_line"),
            line_count=_positive_int(item["line_count"], f"{name}.line_count"),
            max_rendered_utf8_bytes=_positive_int(
                item["max_rendered_utf8_bytes"],
                f"{name}.max_rendered_utf8_bytes",
            ),
            file_sha256=require_sha256(
                item["file_sha256"], f"{name}.file_sha256"
            ),
            file_git_blob=_git_sha1(
                item["file_git_blob"], f"{name}.file_git_blob"
            ),
            target=_bool(item["target"], f"{name}.target"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_ordinal": self.batch_ordinal,
            "phase": self.phase,
            "path": self.path,
            "start_line": self.start_line,
            "line_count": self.line_count,
            "max_rendered_utf8_bytes": self.max_rendered_utf8_bytes,
            "file_sha256": self.file_sha256,
            "file_git_blob": self.file_git_blob,
            "target": self.target,
        }


@dataclass(frozen=True)
class CommandBinding:
    command_ordinal: int
    mode: str
    batch_ordinal: int | None
    argv: tuple[str, ...]
    cwd: Path
    environment_additions: tuple[tuple[str, str], ...]
    run_limit: int
    timeout_seconds: int
    expected_exit: str
    output_max_utf8_bytes: int

    @classmethod
    def from_dict(cls, value: object, name: str) -> CommandBinding:
        fields = {
            "command_ordinal",
            "mode",
            "batch_ordinal",
            "argv",
            "cwd",
            "environment_additions",
            "run_limit",
            "timeout_seconds",
            "expected_exit",
            "output_max_utf8_bytes",
        }
        item = _exact_dict(value, fields, name)
        mode = _text(item["mode"], f"{name}.mode")
        if mode not in MODES:
            raise ContractError(f"{name}.mode is unsupported")
        batch_ordinal: int | None
        if item["batch_ordinal"] is None:
            batch_ordinal = None
        else:
            batch_ordinal = _positive_int(
                item["batch_ordinal"], f"{name}.batch_ordinal"
            )
        if type(item["argv"]) is not list:
            raise ContractError(f"{name}.argv must be an exact list")
        argv = tuple(_text(entry, f"{name}.argv") for entry in item["argv"])
        if type(item["environment_additions"]) is not dict:
            raise ContractError(f"{name}.environment_additions must be an object")
        environment = tuple(
            sorted(
                (
                    _text(key, f"{name}.environment key"),
                    _text(value, f"{name}.environment value"),
                )
                for key, value in item["environment_additions"].items()
            )
        )
        if item["run_limit"] != 1 or type(item["run_limit"]) is not int:
            raise ContractError(f"{name}.run_limit must equal one")
        if item["expected_exit"] != EXPECTED_EXIT:
            raise ContractError(f"{name}.expected_exit must require success")
        return cls(
            command_ordinal=_positive_int(
                item["command_ordinal"], f"{name}.command_ordinal"
            ),
            mode=mode,
            batch_ordinal=batch_ordinal,
            argv=argv,
            cwd=_absolute_path(item["cwd"], f"{name}.cwd"),
            environment_additions=environment,
            run_limit=1,
            timeout_seconds=_positive_int(
                item["timeout_seconds"], f"{name}.timeout_seconds"
            ),
            expected_exit=EXPECTED_EXIT,
            output_max_utf8_bytes=_positive_int(
                item["output_max_utf8_bytes"],
                f"{name}.output_max_utf8_bytes",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "command_ordinal": self.command_ordinal,
            "mode": self.mode,
            "batch_ordinal": self.batch_ordinal,
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "environment_additions": dict(self.environment_additions),
            "run_limit": 1,
            "timeout_seconds": self.timeout_seconds,
            "expected_exit": EXPECTED_EXIT,
            "output_max_utf8_bytes": self.output_max_utf8_bytes,
        }


@dataclass(frozen=True)
class BarrierBinding:
    name: str
    after_command_ordinal: int
    before_command_ordinal: int

    @classmethod
    def from_dict(cls, value: object, name: str) -> BarrierBinding:
        item = _exact_dict(
            value,
            {"name", "after_command_ordinal", "before_command_ordinal"},
            name,
        )
        after = _positive_int(
            item["after_command_ordinal"], f"{name}.after_command_ordinal"
        )
        before = _positive_int(
            item["before_command_ordinal"], f"{name}.before_command_ordinal"
        )
        if before != after + 1:
            raise ContractError(f"{name} must separate adjacent commands")
        return cls(
            name=_text(item["name"], f"{name}.name"),
            after_command_ordinal=after,
            before_command_ordinal=before,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "after_command_ordinal": self.after_command_ordinal,
            "before_command_ordinal": self.before_command_ordinal,
        }


@dataclass(frozen=True)
class MetaAuditEnvelope:
    envelope_id: str
    repository: RepositoryBinding
    host: HostBinding
    script: FileBinding
    target: FileBinding
    controller: FileBinding
    reference_census_count: int
    reference_census_sha256: str
    read_batches: tuple[ReadBatch, ...]
    commands: tuple[CommandBinding, ...]
    barriers: tuple[BarrierBinding, ...]
    output_destination: str
    output_retained: bool
    prohibitions: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> MetaAuditEnvelope:
        fields = {
            "schema_version",
            "envelope_id",
            "repository",
            "host",
            "script",
            "target",
            "controller",
            "reference_census",
            "read_batches",
            "commands",
            "barriers",
            "output",
            "prohibitions",
        }
        item = _exact_dict(value, fields, "envelope")
        if item["schema_version"] != SCHEMA_VERSION:
            raise ContractError("envelope.schema_version is unsupported")
        census = _exact_dict(
            item["reference_census"], {"count", "sha256"}, "reference_census"
        )
        output = _exact_dict(
            item["output"], {"destination", "retained"}, "output"
        )
        if type(item["read_batches"]) is not list:
            raise ContractError("read_batches must be an exact list")
        if type(item["commands"]) is not list:
            raise ContractError("commands must be an exact list")
        if type(item["barriers"]) is not list:
            raise ContractError("barriers must be an exact list")
        if type(item["prohibitions"]) is not list:
            raise ContractError("prohibitions must be an exact list")
        result = cls(
            envelope_id=require_sha256(item["envelope_id"], "envelope.envelope_id"),
            repository=RepositoryBinding.from_dict(item["repository"]),
            host=HostBinding.from_dict(item["host"]),
            script=FileBinding.from_dict(item["script"], "script"),
            target=FileBinding.from_dict(item["target"], "target"),
            controller=FileBinding.from_dict(item["controller"], "controller"),
            reference_census_count=_positive_int(
                census["count"], "reference_census.count"
            ),
            reference_census_sha256=require_sha256(
                census["sha256"], "reference_census.sha256"
            ),
            read_batches=tuple(
                ReadBatch.from_dict(entry, f"read_batches[{index}]")
                for index, entry in enumerate(item["read_batches"])
            ),
            commands=tuple(
                CommandBinding.from_dict(entry, f"commands[{index}]")
                for index, entry in enumerate(item["commands"])
            ),
            barriers=tuple(
                BarrierBinding.from_dict(entry, f"barriers[{index}]")
                for index, entry in enumerate(item["barriers"])
            ),
            output_destination=_text(output["destination"], "output.destination"),
            output_retained=_bool(output["retained"], "output.retained"),
            prohibitions=tuple(
                _text(entry, "prohibitions entry") for entry in item["prohibitions"]
            ),
        )
        result._validate()
        expected_id = sha256_bytes(canonical_json_bytes(result.unsigned_dict()))
        if result.envelope_id != expected_id:
            raise IntegrityError("envelope_id differs from canonical unsigned content")
        return result

    def _validate(self) -> None:
        if self.output_destination != OUTPUT_DESTINATION or self.output_retained:
            raise ContractError("Meta Audit output must be conversation-only")
        if self.prohibitions != PROHIBITIONS:
            raise ContractError("prohibitions differ from the exact safe contract")
        batch_ordinals = tuple(batch.batch_ordinal for batch in self.read_batches)
        if batch_ordinals != tuple(range(1, len(self.read_batches) + 1)):
            raise ContractError("read batch ordinals must be contiguous")
        command_ordinals = tuple(command.command_ordinal for command in self.commands)
        if command_ordinals != tuple(range(1, len(self.commands) + 1)):
            raise ContractError("command ordinals must be contiguous")
        if len(self.commands) != len(self.read_batches) + 3:
            raise ContractError("commands must bind preflight, plan, every batch, and final")
        if tuple(command.mode for command in self.commands[:2]) != (
            "Preflight",
            "PlanBatches",
        ):
            raise ContractError("commands must begin with preflight and batch planning")
        if self.commands[-1].mode != "FinalPreflight":
            raise ContractError("commands must end with final preflight")
        script_path = self.repository.root / PurePosixPath(self.script.path)
        for command in self.commands:
            expected_argv = (
                str(self.host.powershell_executable),
                *POWERSHELL_FLAGS,
                str(script_path),
                "-Mode",
                command.mode,
                "-EnvelopePath",
                "{ENVELOPE_PATH}",
                "-EnvelopeSha256",
                "{ENVELOPE_SHA256}",
                "-CommandOrdinal",
                str(command.command_ordinal),
            )
            if command.argv != expected_argv:
                raise ContractError("command argv differs from the exact literal script")
            if command.cwd != self.repository.root or command.environment_additions:
                raise ContractError("commands must use the exact root and no environment additions")
        by_ordinal = {batch.batch_ordinal: batch for batch in self.read_batches}
        bound_batches: list[ReadBatch] = []
        for command in self.commands[2:-1]:
            if command.batch_ordinal is None or command.batch_ordinal not in by_ordinal:
                raise ContractError("read command lacks its exact batch")
            batch = by_ordinal[command.batch_ordinal]
            expected_mode = "ReadTargetBatch" if batch.target else "ReadReferenceBatch"
            if command.mode != expected_mode:
                raise ContractError(
                    "BATCH_APPLICABILITY_MISMATCH: "
                    "read command mode differs from batch applicability"
                )
            if command.output_max_utf8_bytes != batch.max_rendered_utf8_bytes:
                raise ContractError("read command output limit differs from its batch")
            bound_batches.append(batch)
        if tuple(bound_batches) != self.read_batches:
            raise ContractError("commands do not bind every batch in exact order")
        target_path = self.target.path
        for batch in self.read_batches:
            if batch.target != (batch.path == target_path):
                raise ContractError("target applicability differs from the bound target path")
        reference_commands = tuple(
            command
            for command in self.commands
            if command.mode == "ReadReferenceBatch"
        )
        target_commands = tuple(
            command for command in self.commands if command.mode == "ReadTargetBatch"
        )
        if not reference_commands or not target_commands:
            raise ContractError("both reference and target batches are required")
        first_target = next(
            (
                command.command_ordinal
                for command in self.commands
                if command.mode == "ReadTargetBatch"
            ),
            None,
        )
        last_reference = reference_commands[-1].command_ordinal
        last_target = target_commands[-1].command_ordinal
        if first_target is None or first_target != last_reference + 1:
            raise ContractError("target batches must follow all reference batches")
        expected_barriers = (
            BarrierBinding("B01_BLIND_CENSUS_FROZEN", last_reference, first_target),
            BarrierBinding(
                "B02_MAPPING_COMPLETE", last_target, self.commands[-1].command_ordinal
            ),
        )
        if self.barriers != expected_barriers:
            raise ContractError("barriers differ from the exact blind-first order")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "repository": self.repository.as_dict(),
            "host": self.host.as_dict(),
            "script": self.script.as_dict(),
            "target": self.target.as_dict(),
            "controller": self.controller.as_dict(),
            "reference_census": {
                "count": self.reference_census_count,
                "sha256": self.reference_census_sha256,
            },
            "read_batches": [batch.as_dict() for batch in self.read_batches],
            "commands": [command.as_dict() for command in self.commands],
            "barriers": [barrier.as_dict() for barrier in self.barriers],
            "output": {
                "destination": self.output_destination,
                "retained": self.output_retained,
            },
            "prohibitions": list(self.prohibitions),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "envelope_id": self.envelope_id}


def build_envelope_payload(unsigned: Mapping[str, object]) -> dict[str, object]:
    value = dict(unsigned)
    if "envelope_id" in value:
        raise ContractError("unsigned envelope cannot contain envelope_id")
    payload = {**value, "envelope_id": sha256_bytes(canonical_json_bytes(value))}
    return MetaAuditEnvelope.from_dict(payload).as_dict()


def load_envelope(
    path: Path, *, expected_file_sha256: str
) -> MetaAuditEnvelope | dict[str, object]:
    expected = require_sha256(expected_file_sha256, "expected_file_sha256")
    raw = Path(path).read_bytes()
    if sha256_bytes(raw) != expected:
        raise IntegrityError("Meta Audit envelope file hash mismatch")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("Meta Audit envelope must be UTF-8 JSON") from exc
    if type(decoded) is dict and decoded.get("schema_version") == SCHEMA_VERSION_V2:
        envelope_v2 = validate_v2_envelope_payload(decoded)
        if raw != canonical_json_bytes(envelope_v2):
            raise IntegrityError("Meta Audit envelope is not canonical JSON")
        return envelope_v2
    envelope_v1 = MetaAuditEnvelope.from_dict(decoded)
    if raw != canonical_json_bytes(envelope_v1.as_dict()):
        raise IntegrityError("Meta Audit envelope is not canonical JSON")
    return envelope_v1


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


def run_host_validation(
    *, powershell_executable: Path, script_path: Path, timeout_seconds: int = 30
) -> dict[str, object]:
    outputs: dict[str, object] = {}
    for mode in ("HostProfile", "SelfTest"):
        result = subprocess.run(
            [
                str(powershell_executable),
                *POWERSHELL_FLAGS[:-1],
                "-File",
                str(script_path),
                "-Mode",
                mode,
            ],
            cwd=script_path.parents[2],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0 or result.stderr:
            raise ContractError(
                f"HOST_VALIDATION_FAILED: {mode} exited {result.returncode}"
            )
        try:
            outputs[mode] = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError(
                f"HOST_VALIDATION_OUTPUT_INVALID: {mode} was not UTF-8 JSON"
            ) from exc
    if outputs["SelfTest"].get("mode") != "SELF_TEST_NO_WRITES":
        raise ContractError("HOST_SELF_TEST_MISMATCH: expected metadata-only result")
    return {"host": outputs["HostProfile"], "self_test": outputs["SelfTest"]}


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    if result.returncode != 0:
        raise ContractError(f"GIT_METADATA_FAILED: {' '.join(arguments)}")
    return result.stdout.strip()


def load_reference_corpus_policy(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("CORPUS_POLICY_INVALID: policy is not UTF-8 JSON") from exc
    fields = {
        "schema_version",
        "governed_roots",
        "governed_files",
        "excluded_paths",
        "expected_path_count",
        "expected_paths_sha256",
    }
    item = _exact_dict(value, fields, "corpus_policy")
    if item["schema_version"] != 1:
        raise ContractError("CORPUS_POLICY_INVALID: unsupported schema")
    for field in ("governed_roots", "governed_files", "excluded_paths"):
        if type(item[field]) is not list:
            raise ContractError(f"CORPUS_POLICY_INVALID: {field} must be a list")
        item[field] = [
            _relative_path(entry, f"corpus_policy.{field}") for entry in item[field]
        ]
        if item[field] != sorted(set(item[field])):
            raise ContractError(f"CORPUS_POLICY_INVALID: {field} must be sorted unique")
    _positive_int(item["expected_path_count"], "corpus_policy.expected_path_count")
    require_sha256(
        item["expected_paths_sha256"], "corpus_policy.expected_paths_sha256"
    )
    return item


def discover_reference_paths(
    *, root: Path, policy: Mapping[str, object], target_path: str
) -> tuple[str, ...]:
    tracked = tuple(
        line
        for line in _git_output(root, "ls-files").splitlines()
        if line
    )
    governed_roots = tuple(f"{path}/" for path in policy["governed_roots"])
    governed_files = set(policy["governed_files"])
    excluded = set(policy["excluded_paths"])
    selected = tuple(
        path
        for path in tracked
        if (
            path in governed_files
            or any(path.startswith(prefix) for prefix in governed_roots)
        )
        and path not in excluded
    )
    if target_path in selected:
        raise ContractError("TARGET_IN_REFERENCE_CORPUS: target must be excluded")
    forbidden_names = {"api.env", ".env", "id_rsa", "id_ed25519"}
    for path in selected:
        if PurePosixPath(path).name.lower() in forbidden_names:
            raise ContractError("FORBIDDEN_REFERENCE_PATH: secret-like path selected")
    if len(selected) != policy["expected_path_count"]:
        raise ContractError("CORPUS_CENSUS_MISMATCH: path count differs")
    paths_sha256 = sha256_bytes(canonical_json_bytes(list(selected)))
    if paths_sha256 != policy["expected_paths_sha256"]:
        raise IntegrityError("CORPUS_CENSUS_MISMATCH: path hash differs")
    return selected


def _tracked_file_binding(root: Path, relative_path: str) -> FileBinding:
    safe = _relative_path(relative_path, "tracked_file.path")
    path = root / PurePosixPath(safe)
    data = path.read_bytes()
    binding = FileBinding(
        path=safe,
        bytes=len(data),
        sha256=sha256_bytes(data),
        git_blob=git_blob_sha1_bytes(data),
    )
    head_blob = _git_output(root, "rev-parse", f"HEAD:{safe}")
    if head_blob != binding.git_blob:
        raise IntegrityError(f"TRACKED_FILE_MISMATCH: {safe} differs from HEAD")
    return binding


def prepare_v2_envelope(
    *,
    root: Path,
    controller_path: str,
    target_path: str,
    corpus_policy_path: str,
    script_path: str,
    powershell_executable: Path,
) -> dict[str, object]:
    root = root.resolve()
    if Path(_git_output(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ContractError("REPOSITORY_ROOT_MISMATCH: Git root differs")
    if _git_output(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ContractError("REPOSITORY_NOT_CLEAN: preparation requires a clean tree")
    host_validation = run_host_validation(
        powershell_executable=powershell_executable,
        script_path=root / PurePosixPath(script_path),
    )
    bound_powershell = Path(host_validation["host"]["powershell_executable"])
    policy = load_reference_corpus_policy(
        root / PurePosixPath(corpus_policy_path)
    )
    reference_paths = discover_reference_paths(
        root=root,
        policy=policy,
        target_path=_relative_path(target_path, "target_path"),
    )
    references = tuple(
        _tracked_file_binding(root, path) for path in reference_paths
    )
    target = _tracked_file_binding(root, target_path)
    controller = _tracked_file_binding(root, controller_path)
    script = _tracked_file_binding(root, script_path)
    corpus_policy = _tracked_file_binding(root, corpus_policy_path)
    groups = build_maximal_read_groups(
        root=root,
        reference_bindings=references,
        target_binding=target,
    )
    reference_records = [binding.as_dict() for binding in references]
    reference_census_sha256 = sha256_bytes(
        canonical_json_bytes(reference_records)
    )
    paths_sha256 = sha256_bytes(canonical_json_bytes(list(reference_paths)))
    repository = {
        "root": str(root),
        "branch": _git_output(root, "branch", "--show-current"),
        "head": _git_output(root, "rev-parse", "HEAD"),
        "tree": _git_output(root, "rev-parse", "HEAD^{tree}"),
        "require_clean": True,
    }
    reviewer_binding = sha256_bytes(
        canonical_json_bytes(
            {
                "repository": repository,
                "target": target.as_dict(),
                "groups": [group.as_dict() for group in groups],
                "policy": corpus_policy.as_dict(),
            }
        )
    )
    commands: list[dict[str, object]] = [
        _v2_command(
            ordinal=1,
            mode="Preflight",
            group_ordinal=None,
            root=root,
            powershell_executable=bound_powershell,
            script_path=root / PurePosixPath(script.path),
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        ),
        _v2_command(
            ordinal=2,
            mode="PlanGroups",
            group_ordinal=None,
            root=root,
            powershell_executable=bound_powershell,
            script_path=root / PurePosixPath(script.path),
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        ),
    ]
    for group in groups:
        commands.append(
            _v2_command(
                ordinal=len(commands) + 1,
                mode="ReadGroup",
                group_ordinal=group.group_ordinal,
                root=root,
                powershell_executable=bound_powershell,
                script_path=root / PurePosixPath(script.path),
                timeout_seconds=60,
                output_max_utf8_bytes=group.rendered_utf8_bytes,
            )
        )
    commands.append(
        _v2_command(
            ordinal=len(commands) + 1,
            mode="FinalPreflight",
            group_ordinal=None,
            root=root,
            powershell_executable=bound_powershell,
            script_path=root / PurePosixPath(script.path),
            timeout_seconds=30,
            output_max_utf8_bytes=4_000,
        )
    )
    first_target_group = next(
        group.group_ordinal for group in groups if group.phase == "TARGET"
    )
    unsigned = {
        "schema_version": SCHEMA_VERSION_V2,
        "repository": repository,
        "host": host_validation["host"],
        "script": script.as_dict(),
        "target": target.as_dict(),
        "controller": controller.as_dict(),
        "corpus_policy": corpus_policy.as_dict(),
        "reference_census": {
            "count": len(references),
            "sha256": reference_census_sha256,
            "paths_sha256": paths_sha256,
        },
        "read_groups": [group.as_dict() for group in groups],
        "commands": commands,
        "barriers": [
            {
                "name": "B01_BLIND_CENSUS_FROZEN",
                "after_command_ordinal": first_target_group + 1,
                "before_command_ordinal": first_target_group + 2,
            },
            {
                "name": "B02_MAPPING_COMPLETE",
                "after_command_ordinal": len(groups) + 2,
                "before_command_ordinal": len(groups) + 3,
            },
        ],
        "reviewer_independence": {
            "reviewer_instance_binding": reviewer_binding,
            "no_inherited_turns": True,
            "no_prior_target_access": True,
            "target_access_barrier": "B01_BLIND_CENSUS_FROZEN",
            "final_attestation_required": True,
        },
        "failure_class": "READ_ONLY_INVOCATION",
        "encoding": {"name": "UTF-8", "bom": False, "console": "UTF-8"},
        "output": {"destination": OUTPUT_DESTINATION, "retained": False},
        "prohibitions": list(PROHIBITIONS),
    }
    return build_v2_envelope_payload(unsigned)
