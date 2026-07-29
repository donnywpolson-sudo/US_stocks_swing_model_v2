"""Deterministic, fail-closed orchestration for a separately authorized Master Audit.

The runner never discovers inputs.  A caller must provide one canonical,
hash-bound invocation manifest containing every file, release, command, and
secret-scan surface.  Loading and validating a manifest is read-only.  Report
publication is a separate, explicit option and is the runner's only write.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import subprocess
import sys
from typing import Callable, Mapping, Sequence

from .audit_controls import AUDIT_SURFACES, SecretScanResult, scan_declared_audit_surfaces
from .common import (
    atomic_write_new,
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .mechanical_readiness import (
    assess_stock_mechanical_readiness,
    verify_stock_mechanical_readiness_publication,
)
from .releases import verify_accepted_release


SCHEMA_VERSION = 1
COMMAND_ORDER = (
    "preflight",
    "authority_read",
    "contract_discovery",
    "accepted_release_verification",
    "mechanical_readiness_verification",
    "secret_scan",
    "pytest",
    "master_classification",
    "report_publication",
)
EXPECTED_EXIT_POLICIES = {"REQUIRED_SUCCESS", "REPORTABLE_NONZERO"}
REPORT_OUTCOMES = ("SUPPORTABLE", "BLOCKED", "INSUFFICIENT_EVIDENCE")
_FORBIDDEN_SECRET_FILENAMES = {
    ".env",
    "api.env",
    "credentials",
    "credentials.json",
    "secrets",
    "secrets.json",
}
_WILDCARD_CHARACTERS = frozenset("*?[]")
_ProcessRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class AuditProcessError(RuntimeError):
    """A declared audit process failed in a non-reportable way."""


def _exact_dict(value: object, expected: set[str], field: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ContractError(f"{field} must be an exact object")
    result = dict(value)
    if set(result) != expected:
        raise ContractError(
            f"{field} fields differ: missing={sorted(expected-set(result))}, "
            f"extra={sorted(set(result)-expected)}"
        )
    return result


def _exact_list(value: object, field: str) -> list[object]:
    if type(value) is not list or not value:
        raise ContractError(f"{field} must be an exact nonempty list")
    return list(value)


def _exact_array(value: object, field: str) -> list[object]:
    if type(value) is not list:
        raise ContractError(f"{field} must be an exact list")
    return list(value)


def _exact_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError(f"{field} must be nonempty exact text")
    return value


def _exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{field} must be an exact boolean")
    return value


def _bounded_positive_int(value: object, field: str, maximum: int = 1_800) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ContractError(f"{field} must be an integer from 1 through {maximum}")
    return value


def _relative_path(value: object, field: str) -> str:
    raw = _exact_text(value, field)
    if any(character in raw for character in _WILDCARD_CHARACTERS):
        raise ContractError(f"{field} cannot contain wildcard syntax")
    path = safe_relative_path(raw)
    if any(part.casefold() == "latest" for part in path.parts):
        raise ContractError(f"{field} cannot select a latest path")
    return path.as_posix()


def _absolute_path(value: object, field: str) -> Path:
    raw = _exact_text(value, field)
    if any(character in raw for character in _WILDCARD_CHARACTERS):
        raise ContractError(f"{field} cannot contain wildcard syntax")
    path = Path(raw)
    if not path.is_absolute():
        raise ContractError(f"{field} must be absolute")
    if any(part.casefold() == "latest" for part in path.parts):
        raise ContractError(f"{field} cannot select a latest path")
    return path


def _require_unique(values: Sequence[str], field: str) -> None:
    if len(set(values)) != len(values):
        raise ContractError(f"{field} entries must be unique")


def _require_git_sha1(value: object, field: str) -> str:
    raw = _exact_text(value, field)
    if len(raw) != 40 or any(character not in "0123456789abcdef" for character in raw):
        raise ContractError(
            f"{field} must be an exact lowercase 40-character Git SHA-1 object ID"
        )
    return raw


def windows_ancestor_chain(value: str) -> tuple[str, ...]:
    """Return ancestors through the volume root without degrading ``C:\\`` to ``C:``."""

    path = PureWindowsPath(value)
    if not path.is_absolute() or not path.anchor:
        raise ContractError("Windows containment path must be absolute")
    volume_root = PureWindowsPath(path.anchor)
    current = path
    result: list[str] = []
    while True:
        result.append(str(current))
        if current == volume_root:
            break
        parent = current.parent
        if parent == current:
            raise ContractError("ancestor traversal ended before the Windows volume root")
        current = parent
    if PureWindowsPath(result[-1]) != volume_root:
        raise ContractError("Windows ancestor chain does not end at its volume root")
    return tuple(result)


@dataclass(frozen=True)
class FileBinding:
    path: str
    sha256: str

    @classmethod
    def from_dict(cls, value: object, field: str) -> FileBinding:
        item = _exact_dict(value, {"path", "sha256"}, field)
        return cls(
            path=_relative_path(item["path"], f"{field}.path"),
            sha256=require_sha256(item["sha256"], f"{field}.sha256"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class SecretFileBinding:
    path: str
    sha256: str | None

    @classmethod
    def from_dict(cls, value: object, field: str) -> SecretFileBinding:
        item = _exact_dict(value, {"path", "sha256"}, field)
        path = _relative_path(item["path"], f"{field}.path")
        forbidden = _is_forbidden_secret_filename(Path(path).name)
        if forbidden:
            if item["sha256"] is not None:
                raise ContractError(
                    f"{field}.sha256 must be null for a forbidden secret filename"
                )
            digest = None
        else:
            digest = require_sha256(item["sha256"], f"{field}.sha256")
        return cls(path=path, sha256=digest)

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class ReleaseBinding:
    directory: str
    manifest_sha256: str

    @classmethod
    def from_dict(cls, value: object, field: str) -> ReleaseBinding:
        item = _exact_dict(value, {"directory", "manifest_sha256"}, field)
        return cls(
            directory=_relative_path(item["directory"], f"{field}.directory"),
            manifest_sha256=require_sha256(
                item["manifest_sha256"], f"{field}.manifest_sha256"
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "directory": self.directory,
            "manifest_sha256": self.manifest_sha256,
        }


@dataclass(frozen=True)
class CommandBinding:
    step: str
    timeout_seconds: int
    run_limit: int
    expected_exit: str
    argv: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object, field: str) -> CommandBinding:
        item = _exact_dict(
            value,
            {"step", "timeout_seconds", "run_limit", "expected_exit", "argv"},
            field,
        )
        step = _exact_text(item["step"], f"{field}.step")
        timeout = _bounded_positive_int(
            item["timeout_seconds"], f"{field}.timeout_seconds"
        )
        if item["run_limit"] != 1 or type(item["run_limit"]) is not int:
            raise ContractError(f"{field}.run_limit must be exactly one")
        expected_exit = _exact_text(item["expected_exit"], f"{field}.expected_exit")
        if expected_exit not in EXPECTED_EXIT_POLICIES:
            raise ContractError(f"{field}.expected_exit is unsupported")
        if type(item["argv"]) is not list:
            raise ContractError(f"{field}.argv must be an exact list")
        argv = tuple(_exact_text(part, f"{field}.argv") for part in item["argv"])
        if step in {"contract_discovery", "pytest"}:
            if not argv:
                raise ContractError(f"{field}.argv cannot be empty for a process step")
        elif argv:
            raise ContractError(f"{field}.argv must be empty for an internal step")
        if step == "pytest" and expected_exit != "REPORTABLE_NONZERO":
            raise ContractError("pytest must declare REPORTABLE_NONZERO")
        if step != "pytest" and expected_exit != "REQUIRED_SUCCESS":
            raise ContractError(f"{step} must declare REQUIRED_SUCCESS")
        return cls(
            step=step,
            timeout_seconds=timeout,
            run_limit=1,
            expected_exit=expected_exit,
            argv=argv,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.step,
            "timeout_seconds": self.timeout_seconds,
            "run_limit": self.run_limit,
            "expected_exit": self.expected_exit,
            "argv": list(self.argv),
        }


@dataclass(frozen=True)
class RepositoryBinding:
    root: Path
    git_directory: Path
    commit: str
    tree: str
    require_clean: bool
    python_executable: Path
    python_version: str

    @classmethod
    def from_dict(cls, value: object) -> RepositoryBinding:
        item = _exact_dict(
            value,
            {
                "root",
                "git_directory",
                "commit",
                "tree",
                "require_clean",
                "python_executable",
                "python_version",
            },
            "repository",
        )
        root = _absolute_path(item["root"], "repository.root")
        git_directory = _absolute_path(
            item["git_directory"], "repository.git_directory"
        )
        python_executable = _absolute_path(
            item["python_executable"], "repository.python_executable"
        )
        return cls(
            root=root,
            git_directory=git_directory,
            commit=_require_git_sha1(item["commit"], "repository.commit"),
            tree=_require_git_sha1(item["tree"], "repository.tree"),
            require_clean=_exact_bool(item["require_clean"], "repository.require_clean"),
            python_executable=python_executable,
            python_version=_exact_text(
                item["python_version"], "repository.python_version"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "git_directory": str(self.git_directory),
            "commit": self.commit,
            "tree": self.tree,
            "require_clean": self.require_clean,
            "python_executable": str(self.python_executable),
            "python_version": self.python_version,
        }


@dataclass(frozen=True)
class MechanicalReadinessBinding:
    foundation_release_directory: str
    rebuild_complete_release_directory: str
    historical_research_ready_release_directory: str

    @classmethod
    def from_dict(cls, value: object) -> MechanicalReadinessBinding:
        item = _exact_dict(
            value,
            {
                "foundation_release_directory",
                "rebuild_complete_release_directory",
                "historical_research_ready_release_directory",
            },
            "mechanical_readiness",
        )
        return cls(
            foundation_release_directory=_relative_path(
                item["foundation_release_directory"],
                "mechanical_readiness.foundation_release_directory",
            ),
            rebuild_complete_release_directory=_relative_path(
                item["rebuild_complete_release_directory"],
                "mechanical_readiness.rebuild_complete_release_directory",
            ),
            historical_research_ready_release_directory=_relative_path(
                item["historical_research_ready_release_directory"],
                "mechanical_readiness.historical_research_ready_release_directory",
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "foundation_release_directory": self.foundation_release_directory,
            "rebuild_complete_release_directory": self.rebuild_complete_release_directory,
            "historical_research_ready_release_directory": (
                self.historical_research_ready_release_directory
            ),
        }


@dataclass(frozen=True)
class ReportBinding:
    output_root: str
    publication_enabled: bool
    allowed_outcomes: tuple[str, ...]

    @classmethod
    def from_dict(cls, value: object) -> ReportBinding:
        item = _exact_dict(
            value,
            {"output_root", "publication_enabled", "allowed_outcomes"},
            "report",
        )
        outcomes = tuple(
            _exact_text(entry, "report.allowed_outcomes")
            for entry in _exact_list(item["allowed_outcomes"], "report.allowed_outcomes")
        )
        if outcomes != REPORT_OUTCOMES:
            raise ContractError("report.allowed_outcomes must use the exact audit order")
        return cls(
            output_root=_relative_path(item["output_root"], "report.output_root"),
            publication_enabled=_exact_bool(
                item["publication_enabled"], "report.publication_enabled"
            ),
            allowed_outcomes=outcomes,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "output_root": self.output_root,
            "publication_enabled": self.publication_enabled,
            "allowed_outcomes": list(self.allowed_outcomes),
        }


@dataclass(frozen=True)
class MasterAuditInvocation:
    manifest_id: str
    target_state: str
    repository: RepositoryBinding
    master: FileBinding
    meta: FileBinding
    authorities: tuple[FileBinding, ...]
    configuration: tuple[FileBinding, ...]
    lockfiles: tuple[FileBinding, ...]
    evidence_files: tuple[FileBinding, ...]
    accepted_release_root: str
    accepted_releases: tuple[ReleaseBinding, ...]
    component_manifests: tuple[FileBinding, ...]
    mechanical_readiness: MechanicalReadinessBinding
    secret_surfaces: Mapping[str, tuple[SecretFileBinding, ...]]
    empty_surface_roots: Mapping[str, tuple[str, ...]]
    commands: tuple[CommandBinding, ...]
    report: ReportBinding

    @classmethod
    def from_dict(cls, value: object) -> MasterAuditInvocation:
        item = _exact_dict(
            value,
            {
                "schema_version",
                "manifest_id",
                "target_state",
                "repository",
                "specifications",
                "file_census",
                "accepted_release_root",
                "accepted_releases",
                "component_manifests",
                "mechanical_readiness",
                "secret_surfaces",
                "commands",
                "report",
            },
            "manifest",
        )
        if item["schema_version"] != SCHEMA_VERSION or type(item["schema_version"]) is not int:
            raise ContractError(f"manifest.schema_version must equal {SCHEMA_VERSION}")
        specifications = _exact_dict(
            item["specifications"], {"master", "meta"}, "specifications"
        )
        census = _exact_dict(
            item["file_census"],
            {"authorities", "configuration", "lockfiles", "evidence"},
            "file_census",
        )

        def files(raw: object, field: str) -> tuple[FileBinding, ...]:
            result = tuple(
                FileBinding.from_dict(entry, f"{field}[{index}]")
                for index, entry in enumerate(_exact_list(raw, field))
            )
            _require_unique([entry.path for entry in result], field)
            return result

        authorities = files(census["authorities"], "file_census.authorities")
        configuration = files(census["configuration"], "file_census.configuration")
        lockfiles = files(census["lockfiles"], "file_census.lockfiles")
        if len(lockfiles) != 2:
            raise ContractError("file_census.lockfiles must enumerate exactly two files")
        evidence_files = files(census["evidence"], "file_census.evidence")
        accepted_releases = tuple(
            ReleaseBinding.from_dict(entry, f"accepted_releases[{index}]")
            for index, entry in enumerate(
                _exact_list(item["accepted_releases"], "accepted_releases")
            )
        )
        _require_unique(
            [entry.directory for entry in accepted_releases], "accepted_releases"
        )
        component_manifests = files(
            item["component_manifests"], "component_manifests"
        )

        raw_surfaces = _exact_list(item["secret_surfaces"], "secret_surfaces")
        parsed_surfaces = [
            _exact_dict(
                entry,
                {"surface", "files", "empty_roots"},
                f"secret_surfaces[{index}]",
            )
            for index, entry in enumerate(raw_surfaces)
        ]
        surface_names = tuple(
            _exact_text(entry["surface"], "secret_surfaces.surface")
            for entry in parsed_surfaces
        )
        if surface_names != AUDIT_SURFACES:
            raise ContractError("secret_surfaces must use the exact six-surface order")
        secret_surfaces: dict[str, tuple[SecretFileBinding, ...]] = {}
        empty_surface_roots: dict[str, tuple[str, ...]] = {}
        seen_secret_paths: set[str] = set()
        seen_empty_roots: set[str] = set()
        for surface, surface_item in zip(AUDIT_SURFACES, parsed_surfaces, strict=True):
            raw_files = _exact_array(
                surface_item["files"], f"secret_surfaces.{surface}.files"
            )
            raw_empty_roots = _exact_array(
                surface_item["empty_roots"],
                f"secret_surfaces.{surface}.empty_roots",
            )
            if bool(raw_files) is bool(raw_empty_roots):
                raise ContractError(
                    "each secret surface requires either files or explicit empty roots"
                )
            entries = tuple(
                SecretFileBinding.from_dict(
                    entry, f"secret_surfaces.{surface}[{index}]"
                )
                for index, entry in enumerate(
                    raw_files
                )
            )
            for entry in entries:
                if entry.path in seen_secret_paths:
                    raise ContractError(
                        "secret-scan files cannot appear in more than one surface"
                    )
                seen_secret_paths.add(entry.path)
            secret_surfaces[surface] = entries
            roots = tuple(
                _relative_path(
                    entry, f"secret_surfaces.{surface}.empty_roots[{index}]"
                )
                for index, entry in enumerate(raw_empty_roots)
            )
            for root in roots:
                if root in seen_empty_roots:
                    raise ContractError("empty-surface roots must be unique")
                seen_empty_roots.add(root)
            empty_surface_roots[surface] = roots

        commands = tuple(
            CommandBinding.from_dict(entry, f"commands[{index}]")
            for index, entry in enumerate(_exact_list(item["commands"], "commands"))
        )
        if tuple(command.step for command in commands) != COMMAND_ORDER:
            raise ContractError("commands differ from the exact required order")

        invocation = cls(
            manifest_id=require_sha256(item["manifest_id"], "manifest.manifest_id"),
            target_state=_exact_text(item["target_state"], "manifest.target_state"),
            repository=RepositoryBinding.from_dict(item["repository"]),
            master=FileBinding.from_dict(specifications["master"], "specifications.master"),
            meta=FileBinding.from_dict(specifications["meta"], "specifications.meta"),
            authorities=authorities,
            configuration=configuration,
            lockfiles=lockfiles,
            evidence_files=evidence_files,
            accepted_release_root=_relative_path(
                item["accepted_release_root"], "accepted_release_root"
            ),
            accepted_releases=accepted_releases,
            component_manifests=component_manifests,
            mechanical_readiness=MechanicalReadinessBinding.from_dict(
                item["mechanical_readiness"]
            ),
            secret_surfaces=secret_surfaces,
            empty_surface_roots=empty_surface_roots,
            commands=commands,
            report=ReportBinding.from_dict(item["report"]),
        )
        expected_id = sha256_bytes(canonical_json_bytes(invocation.unsigned_dict()))
        if invocation.manifest_id != expected_id:
            raise IntegrityError("manifest_id differs from the canonical manifest content")
        invocation._validate_process_commands()
        return invocation

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "target_state": self.target_state,
            "repository": self.repository.as_dict(),
            "specifications": {
                "master": self.master.as_dict(),
                "meta": self.meta.as_dict(),
            },
            "file_census": {
                "authorities": [entry.as_dict() for entry in self.authorities],
                "configuration": [entry.as_dict() for entry in self.configuration],
                "lockfiles": [entry.as_dict() for entry in self.lockfiles],
                "evidence": [entry.as_dict() for entry in self.evidence_files],
            },
            "accepted_release_root": self.accepted_release_root,
            "accepted_releases": [
                entry.as_dict() for entry in self.accepted_releases
            ],
            "component_manifests": [
                entry.as_dict() for entry in self.component_manifests
            ],
            "mechanical_readiness": self.mechanical_readiness.as_dict(),
            "secret_surfaces": [
                {
                    "surface": surface,
                    "files": [
                        entry.as_dict() for entry in self.secret_surfaces[surface]
                    ],
                    "empty_roots": list(self.empty_surface_roots[surface]),
                }
                for surface in AUDIT_SURFACES
            ],
            "commands": [entry.as_dict() for entry in self.commands],
            "report": self.report.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "manifest_id": self.manifest_id}

    def _validate_process_commands(self) -> None:
        discovery = self.commands[COMMAND_ORDER.index("contract_discovery")]
        executable = Path(discovery.argv[0]).name.casefold()
        if executable not in {"rg", "rg.exe"}:
            raise ContractError("contract_discovery must execute only rg")
        if any(argument in {"--glob", "-g"} for argument in discovery.argv[1:]):
            raise ContractError("contract_discovery cannot add manifest-time glob discovery")
        pytest_command = self.commands[COMMAND_ORDER.index("pytest")]
        expected = (
            str(self.repository.python_executable),
            "-m",
            "pytest",
            "-q",
        )
        if pytest_command.argv != expected:
            raise ContractError("pytest argv differs from the exact project command")


@dataclass(frozen=True)
class AuditExecutionResult:
    manifest_id: str
    target_state: str
    step_results: tuple[dict[str, object], ...]
    report_path: Path | None
    report_sha256: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "manifest_id": self.manifest_id,
            "target_state": self.target_state,
            "step_results": list(self.step_results),
            "report_path": None if self.report_path is None else str(self.report_path),
            "report_sha256": self.report_sha256,
        }


def build_invocation_payload(unsigned: Mapping[str, object]) -> dict[str, object]:
    """Return canonical invocation content with its exact manifest ID."""

    value = dict(unsigned)
    if "manifest_id" in value:
        raise ContractError("unsigned invocation cannot already contain manifest_id")
    return {**value, "manifest_id": sha256_bytes(canonical_json_bytes(value))}


def load_invocation_manifest(
    path: Path, *, expected_file_sha256: str
) -> MasterAuditInvocation:
    """Load one canonical, hash-bound manifest without executing audit steps."""

    expected = require_sha256(expected_file_sha256, "expected_file_sha256")
    manifest_path = Path(path)
    reject_link(manifest_path)
    if not manifest_path.is_file() or manifest_path.stat().st_nlink != 1:
        raise ContractError("invocation manifest must be an ordinary single-link file")
    raw = manifest_path.read_bytes()
    if sha256_bytes(raw) != expected:
        raise IntegrityError("invocation manifest file hash mismatch")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("invocation manifest is not UTF-8 JSON") from exc
    invocation = MasterAuditInvocation.from_dict(decoded)
    if raw != canonical_json_bytes(invocation.as_dict()):
        raise IntegrityError("invocation manifest bytes are not canonical JSON")
    return invocation


def _is_forbidden_secret_filename(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _FORBIDDEN_SECRET_FILENAMES or lowered.endswith(".env")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(right)
    )


def _bound_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    parts = safe_relative_path(relative).parts
    return require_contained_path(root.joinpath(*parts), root, must_exist=must_exist)


def _verify_file_binding(root: Path, binding: FileBinding) -> Path:
    path = _bound_path(root, binding.path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ContractError(f"bound file is not an ordinary single-link file: {binding.path}")
    if sha256_file(path) != binding.sha256:
        raise IntegrityError(f"bound file hash mismatch: {binding.path}")
    return path


def _verify_secret_binding(root: Path, binding: SecretFileBinding) -> Path:
    path = _bound_path(root, binding.path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ContractError(
            f"secret-scan file is not an ordinary single-link file: {binding.path}"
        )
    if binding.sha256 is None:
        if not _is_forbidden_secret_filename(path.name):
            raise ContractError("only forbidden secret filenames can omit a hash")
    elif sha256_file(path) != binding.sha256:
        raise IntegrityError(f"secret-scan file hash mismatch: {binding.path}")
    return path


def _verify_empty_surface_root(root: Path, relative: str) -> str:
    path = _bound_path(root, relative, must_exist=False)
    if not path.exists():
        return "ABSENT"
    reject_link(path)
    if not path.is_dir():
        raise ContractError("empty-surface root must be absent or a directory")
    for child in path.rglob("*"):
        reject_link(child)
        if child.is_file():
            raise ContractError("empty-surface root contains an unexpected file")
        if not child.is_dir():
            raise ContractError("empty-surface root contains an unsupported entry")
    return "EMPTY_DIRECTORY"


def validate_repository_preflight(
    invocation: MasterAuditInvocation,
    *,
    cwd: Path | None = None,
    process_runner: _ProcessRunner = subprocess.run,
) -> dict[str, object]:
    """Validate exact repository identity and every predeclared file identity."""

    root = invocation.repository.root
    actual = Path.cwd() if cwd is None else Path(cwd)
    if not _same_path(actual, root):
        raise IntegrityError("current directory differs from the manifest repository root")
    require_contained_path(root, root)
    reject_link(root)
    git_directory = invocation.repository.git_directory
    require_contained_path(git_directory, root)
    reject_link(git_directory)
    if not git_directory.is_dir():
        raise ContractError("Git directory must be an ordinary directory")
    if os.name == "nt":
        windows_ancestor_chain(str(root))

    def git(*arguments: str) -> str:
        try:
            completed = process_runner(
                ["git", *arguments],
                cwd=root,
                capture_output=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AuditProcessError(f"Git preflight failed to launch: {arguments}") from exc
        if completed.returncode != 0:
            raise AuditProcessError(
                f"Git preflight exited {completed.returncode}: {arguments}"
            )
        return completed.stdout.decode("utf-8", errors="strict").strip()

    git_root = Path(git("rev-parse", "--show-toplevel").replace("/", os.sep))
    absolute_git_dir = Path(
        git("rev-parse", "--absolute-git-dir").replace("/", os.sep)
    )
    if not _same_path(git_root, root) or not _same_path(
        absolute_git_dir, git_directory
    ):
        raise IntegrityError("Git identity differs from the invocation manifest")
    if git("rev-parse", "HEAD") != invocation.repository.commit:
        raise IntegrityError("Git commit differs from the invocation manifest")
    if git("rev-parse", "HEAD^{tree}") != invocation.repository.tree:
        raise IntegrityError("Git tree differs from the invocation manifest")
    if invocation.repository.require_clean and git("status", "--short"):
        raise IntegrityError("worktree is not clean")
    if not _same_path(Path(sys.executable), invocation.repository.python_executable):
        raise IntegrityError("selected Python executable differs from the manifest")
    if platform.python_version() != invocation.repository.python_version:
        raise IntegrityError("selected Python version differs from the manifest")

    bindings = (
        (invocation.master, invocation.meta)
        + invocation.authorities
        + invocation.configuration
        + invocation.lockfiles
        + invocation.evidence_files
        + invocation.component_manifests
    )
    for binding in bindings:
        _verify_file_binding(root, binding)
    for entries in invocation.secret_surfaces.values():
        for binding in entries:
            _verify_secret_binding(root, binding)
    empty_root_states: list[dict[str, str]] = []
    for surface in AUDIT_SURFACES:
        for relative in invocation.empty_surface_roots[surface]:
            empty_root_states.append(
                {
                    "surface": surface,
                    "relative_path": relative,
                    "state": _verify_empty_surface_root(root, relative),
                }
            )
    for release in invocation.accepted_releases:
        directory = _bound_path(root, release.directory)
        manifest_path = _bound_path(root, f"{release.directory}/release_manifest.json")
        if not directory.is_dir() or sha256_file(manifest_path) != release.manifest_sha256:
            raise IntegrityError(
                f"accepted release manifest mismatch: {release.directory}"
            )
    return {
        "step": "preflight",
        "status": "PASSED",
        "commit": invocation.repository.commit,
        "tree": invocation.repository.tree,
        "bound_file_count": len(bindings),
        "accepted_release_count": len(invocation.accepted_releases),
        "empty_surface_roots": empty_root_states,
    }


def _run_declared_process(
    command: CommandBinding,
    *,
    cwd: Path,
    process_runner: _ProcessRunner,
) -> dict[str, object]:
    try:
        completed = process_runner(
            list(command.argv),
            cwd=cwd,
            capture_output=True,
            timeout=command.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuditProcessError(f"{command.step} timed out") from exc
    except OSError as exc:
        raise AuditProcessError(f"{command.step} failed to launch") from exc
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    result = {
        "step": command.step,
        "exit_code": completed.returncode,
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
    }
    if completed.returncode == 0:
        return {**result, "status": "PASSED"}
    if command.expected_exit == "REPORTABLE_NONZERO":
        return {**result, "status": "REPORTABLE_TEST_FAILURE"}
    raise AuditProcessError(f"{command.step} exited {completed.returncode}")


def _verify_releases(invocation: MasterAuditInvocation) -> dict[str, object]:
    root = invocation.repository.root
    accepted_root = _bound_path(root, invocation.accepted_release_root)
    release_ids: list[str] = []
    for binding in invocation.accepted_releases:
        directory = _bound_path(root, binding.directory)
        manifest = verify_accepted_release(directory, accepted_root=accepted_root)
        if sha256_file(directory / "release_manifest.json") != binding.manifest_sha256:
            raise IntegrityError(
                f"post-verification manifest mismatch: {binding.directory}"
            )
        release_ids.append(manifest.release_id)
    return {
        "step": "accepted_release_verification",
        "status": "PASSED",
        "release_ids": release_ids,
    }


def _verify_mechanical_readiness(
    invocation: MasterAuditInvocation,
) -> dict[str, object]:
    root = invocation.repository.root
    accepted_root = _bound_path(root, invocation.accepted_release_root)
    binding = invocation.mechanical_readiness
    foundation = _bound_path(root, binding.foundation_release_directory)
    rebuild = _bound_path(root, binding.rebuild_complete_release_directory)
    historical = _bound_path(
        root, binding.historical_research_ready_release_directory
    )
    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=foundation,
        accepted_release_root=accepted_root,
    )
    verified = verify_stock_mechanical_readiness_publication(
        foundation_release_directory=foundation,
        accepted_release_root=accepted_root,
        rebuild_complete_release_directory=rebuild,
        historical_research_ready_release_directory=historical,
    )
    if verified.assessment_id != assessment.assessment_id:
        raise IntegrityError("mechanical-readiness assessment IDs differ")
    return {
        "step": "mechanical_readiness_verification",
        "status": "PASSED",
        "assessment_id": assessment.assessment_id,
    }


def _scan_secrets(invocation: MasterAuditInvocation) -> tuple[dict[str, object], SecretScanResult]:
    root = invocation.repository.root
    surfaces = {
        surface: tuple(binding.path for binding in invocation.secret_surfaces[surface])
        for surface in AUDIT_SURFACES
    }
    result = scan_declared_audit_surfaces(
        root,
        surfaces,
        empty_surface_roots=dict(invocation.empty_surface_roots),
    )
    return (
        {
            "step": "secret_scan",
            "status": "PASSED" if result.passed else "FINDINGS",
            "scan_id": result.scan_id,
            "surface_counts": dict(result.surface_counts),
            "findings": [
                {
                    "surface": finding.surface,
                    "relative_path": finding.relative_path,
                    "category": finding.category,
                    "line_number": finding.line_number,
                }
                for finding in result.findings
            ],
        },
        result,
    )


def publish_content_addressed_report(
    *,
    repository_root: Path,
    output_root: str,
    report_bytes: bytes,
) -> tuple[Path, str]:
    """Atomically publish exact Markdown bytes without overwriting a prior report."""

    if type(report_bytes) is not bytes or not report_bytes:
        raise ContractError("report_bytes must be exact nonempty bytes")
    root = Path(repository_root)
    require_contained_path(root, root)
    output = _bound_path(root, output_root, must_exist=False)
    if output.exists():
        reject_link(output)
        if not output.is_dir():
            raise ContractError("report output root must be a directory")
    report_sha256 = sha256_bytes(report_bytes)
    destination = require_contained_path(
        output / f"{report_sha256}.md", root, must_exist=False
    )
    if destination.exists():
        raise IntegrityError("content-addressed report collision")
    atomic_write_new(destination, report_bytes)
    if sha256_file(destination) != report_sha256:
        raise IntegrityError("published report hash verification failed")
    return destination, report_sha256


def execute_invocation(
    invocation: MasterAuditInvocation,
    *,
    process_runner: _ProcessRunner = subprocess.run,
    report_bytes: bytes | None = None,
    publish_report: bool = False,
) -> AuditExecutionResult:
    """Execute the exact manifest once; no implicit command or discovery is allowed."""

    if publish_report and not invocation.report.publication_enabled:
        raise PermissionError("manifest does not enable report publication")
    if publish_report and report_bytes is None:
        raise ContractError("report publication requires exact report bytes")
    if not publish_report and report_bytes is not None:
        raise ContractError("report bytes require explicit publication")

    root = invocation.repository.root
    commands = {command.step: command for command in invocation.commands}
    results: list[dict[str, object]] = [
        validate_repository_preflight(
            invocation, cwd=root, process_runner=process_runner
        )
    ]

    for binding in (
        invocation.authorities + invocation.configuration + invocation.lockfiles
    ):
        _verify_file_binding(root, binding).read_bytes()
    results.append(
        {
            "step": "authority_read",
            "status": "PASSED",
            "file_count": (
                len(invocation.authorities)
                + len(invocation.configuration)
                + len(invocation.lockfiles)
            ),
        }
    )
    results.append(
        _run_declared_process(
            commands["contract_discovery"],
            cwd=root,
            process_runner=process_runner,
        )
    )
    results.append(_verify_releases(invocation))
    results.append(_verify_mechanical_readiness(invocation))
    secret_result, _ = _scan_secrets(invocation)
    results.append(secret_result)
    results.append(
        _run_declared_process(
            commands["pytest"], cwd=root, process_runner=process_runner
        )
    )
    _verify_file_binding(root, invocation.master).read_bytes()
    results.append(
        {
            "step": "master_classification",
            "status": "EVIDENCE_READY_FOR_INDEPENDENT_REVIEW",
        }
    )

    report_path: Path | None = None
    report_sha256: str | None = None
    if publish_report:
        report_path, report_sha256 = publish_content_addressed_report(
            repository_root=root,
            output_root=invocation.report.output_root,
            report_bytes=report_bytes or b"",
        )
        report_status = "PUBLISHED"
    else:
        report_status = "NOT_REQUESTED"
    results.append(
        {
            "step": "report_publication",
            "status": report_status,
            "report_sha256": report_sha256,
        }
    )
    return AuditExecutionResult(
        manifest_id=invocation.manifest_id,
        target_state=invocation.target_state,
        step_results=tuple(results),
        report_path=report_path,
        report_sha256=report_sha256,
    )
