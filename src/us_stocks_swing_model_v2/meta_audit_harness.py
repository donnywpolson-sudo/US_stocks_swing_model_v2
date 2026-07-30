"""Content-addressed, non-authorizing Meta Audit envelope contracts.

The module prepares and validates exact reviewer process manifests.  It does
not execute a Meta Audit, read a target, or grant project authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .common import canonical_json_bytes, require_sha256, safe_relative_path, sha256_bytes
from .errors import ContractError, IntegrityError


SCHEMA_VERSION = 1
EXPECTED_EXIT = "REQUIRED_SUCCESS"
OUTPUT_DESTINATION = "CONVERSATION_ONLY"
POWERSHELL_FLAGS = ("-NoLogo", "-NoProfile", "-NonInteractive", "-File")
MODES = (
    "Preflight",
    "PlanBatches",
    "ReadReferenceBatch",
    "ReadTargetBatch",
    "FinalPreflight",
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
                raise ContractError("read command mode differs from batch applicability")
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


def load_envelope(path: Path, *, expected_file_sha256: str) -> MetaAuditEnvelope:
    expected = require_sha256(expected_file_sha256, "expected_file_sha256")
    raw = Path(path).read_bytes()
    if sha256_bytes(raw) != expected:
        raise IntegrityError("Meta Audit envelope file hash mismatch")
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("Meta Audit envelope must be UTF-8 JSON") from exc
    envelope = MetaAuditEnvelope.from_dict(decoded)
    if raw != canonical_json_bytes(envelope.as_dict()):
        raise IntegrityError("Meta Audit envelope is not canonical JSON")
    return envelope
