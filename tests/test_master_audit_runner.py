from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import platform
import subprocess
import sys

import pytest

import us_stocks_swing_model_v2.master_audit_runner as runner
from us_stocks_swing_model_v2.audit_controls import AUDIT_SURFACES
from us_stocks_swing_model_v2.common import (
    canonical_json_bytes,
    sha256_bytes,
    sha256_file,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.master_audit_runner import (
    AuditProcessError,
    CommandBinding,
    MasterAuditInvocation,
    build_invocation_payload,
    load_invocation_manifest,
    publish_content_addressed_report,
    windows_ancestor_chain,
)


def _write(root: Path, relative: str, payload: bytes = b"ordinary evidence\n") -> dict[str, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {"path": relative, "sha256": sha256_bytes(payload)}


def _unsigned_manifest(root: Path) -> dict[str, object]:
    (root / ".git").mkdir(parents=True)
    master = _write(root, "MASTER_AUDIT.md", b"master\n")
    meta = _write(root, "META_MASTER_AUDIT.md", b"meta\n")
    authority = _write(root, "AGENTS.md")
    configuration = _write(root, "config/sources.json", b"{}\n")
    lock_one = _write(root, "requirements.lock")
    lock_two = _write(root, "requirements-test.lock")
    evidence = _write(root, "evidence/receipt.json", b"{}\n")
    component = _write(
        root,
        "accepted/component/abc/release_manifest.json",
        b'{"release_id":"abc"}\n',
    )
    secret_surfaces: list[dict[str, object]] = []
    for surface in AUDIT_SURFACES:
        if surface == "admitted_evidence":
            relative = f"scan/{surface}/api.env"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"must never be read")
            files: list[dict[str, object]] = [{"path": relative, "sha256": None}]
        else:
            binding = _write(root, f"scan/{surface}/evidence.txt")
            files = [binding]
        secret_surfaces.append(
            {"surface": surface, "files": files, "empty_roots": []}
        )
    commands = [
        {
            "step": step,
            "timeout_seconds": 30,
            "run_limit": 1,
            "expected_exit": (
                "REPORTABLE_NONZERO" if step == "pytest" else "REQUIRED_SUCCESS"
            ),
            "argv": (
                ["rg", "release|identity", "src", "config", "tests"]
                if step == "contract_discovery"
                else [sys.executable, "-m", "pytest", "-q"]
                if step == "pytest"
                else []
            ),
        }
        for step in runner.COMMAND_ORDER
    ]
    return {
        "schema_version": 1,
        "target_state": "HISTORICAL_RESEARCH_READY",
        "repository": {
            "root": str(root),
            "git_directory": str(root / ".git"),
            "commit": "1" * 40,
            "tree": "2" * 40,
            "require_clean": True,
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
        },
        "specifications": {"master": master, "meta": meta},
        "file_census": {
            "authorities": [authority],
            "configuration": [configuration],
            "lockfiles": [lock_one, lock_two],
            "evidence": [evidence],
        },
        "accepted_release_root": "accepted",
        "accepted_releases": [
            {
                "directory": "accepted/component/abc",
                "manifest_sha256": component["sha256"],
            }
        ],
        "component_manifests": [component],
        "mechanical_readiness": {
            "foundation_release_directory": "accepted/foundation/abc",
            "rebuild_complete_release_directory": "accepted/rebuild/abc",
            "historical_research_ready_release_directory": "accepted/historical/abc",
        },
        "secret_surfaces": secret_surfaces,
        "commands": commands,
        "report": {
            "output_root": "reports/generated/master_audit",
            "publication_enabled": False,
            "allowed_outcomes": [
                "SUPPORTABLE",
                "BLOCKED",
                "INSUFFICIENT_EVIDENCE",
            ],
        },
    }


def _invocation(root: Path) -> MasterAuditInvocation:
    return MasterAuditInvocation.from_dict(
        build_invocation_payload(_unsigned_manifest(root))
    )


def test_windows_ancestor_chain_preserves_drive_root() -> None:
    chain = windows_ancestor_chain(
        r"C:\Users\donny\Desktop\US_stocks_swing_model_v2"
    )
    assert chain[0] == r"C:\Users\donny\Desktop\US_stocks_swing_model_v2"
    assert chain[-1] == "C:\\"
    assert "C:" not in chain


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", ""),
        ("commit", "A" * 40),
        ("commit", "g" * 40),
        ("commit", "1" * 39),
        ("commit", "1" * 41),
        ("commit", "1" * 64),
        ("tree", ""),
        ("tree", "B" * 40),
        ("tree", "z" * 40),
        ("tree", "2" * 39),
        ("tree", "2" * 41),
        ("tree", "2" * 64),
    ],
)
def test_repository_requires_exact_lowercase_git_sha1_object_ids(
    tmp_path: Path, field: str, value: str
) -> None:
    unsigned = _unsigned_manifest(tmp_path)
    unsigned["repository"][field] = value  # type: ignore[index]
    with pytest.raises(ContractError, match=rf"repository\.{field}"):
        MasterAuditInvocation.from_dict(build_invocation_payload(unsigned))


def test_repository_git_ids_do_not_weaken_sha256_file_bindings(
    tmp_path: Path,
) -> None:
    unsigned = _unsigned_manifest(tmp_path)
    unsigned["repository"]["commit"] = "a" * 40  # type: ignore[index]
    unsigned["repository"]["tree"] = "b" * 40  # type: ignore[index]
    unsigned["specifications"]["master"]["sha256"] = "c" * 40  # type: ignore[index]
    with pytest.raises(
        ContractError, match=r"specifications\.master\.sha256.*SHA-256"
    ):
        MasterAuditInvocation.from_dict(build_invocation_payload(unsigned))


def test_manifest_requires_exact_two_lockfiles_and_command_order(tmp_path: Path) -> None:
    unsigned = _unsigned_manifest(tmp_path)
    unsigned["file_census"]["lockfiles"].append(  # type: ignore[index,union-attr]
        _write(tmp_path, "not-a-lockfile.txt")
    )
    with pytest.raises(ContractError, match="exactly two"):
        MasterAuditInvocation.from_dict(build_invocation_payload(unsigned))

    reordered = _unsigned_manifest(tmp_path / "order")
    reordered["commands"][1], reordered["commands"][2] = (  # type: ignore[index]
        reordered["commands"][2],  # type: ignore[index]
        reordered["commands"][1],  # type: ignore[index]
    )
    with pytest.raises(ContractError, match="exact required order"):
        MasterAuditInvocation.from_dict(build_invocation_payload(reordered))


@pytest.mark.parametrize(
    "bad_path",
    [
        "config/*.json",
        "accepted/latest/release_manifest.json",
        "../other-repository/evidence.json",
    ],
)
def test_manifest_rejects_fallback_and_pattern_paths(
    tmp_path: Path, bad_path: str
) -> None:
    unsigned = _unsigned_manifest(tmp_path)
    unsigned["file_census"]["configuration"][0]["path"] = bad_path  # type: ignore[index]
    with pytest.raises(ContractError):
        MasterAuditInvocation.from_dict(build_invocation_payload(unsigned))


def test_hash_bound_canonical_manifest_rejects_tampering(tmp_path: Path) -> None:
    payload = build_invocation_payload(_unsigned_manifest(tmp_path))
    raw = canonical_json_bytes(payload)
    manifest = tmp_path / "invocation.json"
    manifest.write_bytes(raw)
    loaded = load_invocation_manifest(
        manifest, expected_file_sha256=sha256_bytes(raw)
    )
    assert loaded.manifest_id == payload["manifest_id"]

    tampered = json.loads(raw)
    tampered["target_state"] = "CANDIDATE_SEALED"
    manifest.write_bytes(canonical_json_bytes(tampered))
    with pytest.raises(IntegrityError, match="file hash mismatch"):
        load_invocation_manifest(manifest, expected_file_sha256=sha256_bytes(raw))


def test_forbidden_secret_filename_is_preflighted_without_hashing_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    invocation = _invocation(tmp_path)
    forbidden = invocation.secret_surfaces["admitted_evidence"][0]
    original = runner.sha256_file

    def guarded(path: Path) -> str:
        if path.name == "api.env":
            raise AssertionError("forbidden secret bytes were hashed")
        return original(path)

    monkeypatch.setattr(runner, "sha256_file", guarded)
    resolved = runner._verify_secret_binding(tmp_path, forbidden)
    assert resolved.name == "api.env"


def test_manifest_accepts_explicit_empty_roots_and_rejects_omitted_surface_proof(
    tmp_path: Path,
) -> None:
    unsigned = _unsigned_manifest(tmp_path)
    logs = unsigned["secret_surfaces"][1]  # type: ignore[index]
    logs["files"] = []
    logs["empty_roots"] = ["logs"]
    invocation = MasterAuditInvocation.from_dict(build_invocation_payload(unsigned))
    assert invocation.secret_surfaces["logs"] == ()
    assert invocation.empty_surface_roots["logs"] == ("logs",)

    omitted = deepcopy(unsigned)
    omitted["secret_surfaces"][1]["empty_roots"] = []  # type: ignore[index]
    with pytest.raises(
        ContractError,
        match="either files or explicit empty roots",
    ):
        MasterAuditInvocation.from_dict(build_invocation_payload(omitted))


def test_empty_surface_preflight_rejects_unexpected_files(tmp_path: Path) -> None:
    empty = tmp_path / "logs"
    empty.mkdir()
    assert runner._verify_empty_surface_root(tmp_path, "logs") == "EMPTY_DIRECTORY"
    (empty / "unexpected.log").write_text("unexpected", encoding="utf-8")
    with pytest.raises(ContractError, match="unexpected file"):
        runner._verify_empty_surface_root(tmp_path, "logs")


def test_process_timeout_and_exit_policy_are_fail_closed(tmp_path: Path) -> None:
    required = CommandBinding(
        step="contract_discovery",
        timeout_seconds=1,
        run_limit=1,
        expected_exit="REQUIRED_SUCCESS",
        argv=("rg", "x", "src"),
    )
    reportable = CommandBinding(
        step="pytest",
        timeout_seconds=1,
        run_limit=1,
        expected_exit="REPORTABLE_NONZERO",
        argv=(sys.executable, "-m", "pytest", "-q"),
    )

    def timed_out(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd="rg", timeout=1)

    with pytest.raises(AuditProcessError, match="timed out"):
        runner._run_declared_process(
            required, cwd=tmp_path, process_runner=timed_out
        )

    def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=[], returncode=2, stdout=b"x", stderr=b"y")

    with pytest.raises(AuditProcessError, match="exited 2"):
        runner._run_declared_process(required, cwd=tmp_path, process_runner=failed)
    result = runner._run_declared_process(
        reportable, cwd=tmp_path, process_runner=failed
    )
    assert result["status"] == "REPORTABLE_TEST_FAILURE"
    assert "stdout" not in result
    assert "stderr" not in result
    assert result["stdout_sha256"] == sha256_bytes(b"x")
    assert result["stderr_sha256"] == sha256_bytes(b"y")
    assert result["stdout_bytes"] == 1
    assert result["stderr_bytes"] == 1


def test_content_addressed_report_is_atomic_and_never_overwrites(
    tmp_path: Path,
) -> None:
    report = b"# Master Audit\n\nOutcome: BLOCKED\n"
    path, digest = publish_content_addressed_report(
        repository_root=tmp_path,
        output_root="reports/generated/master_audit",
        report_bytes=report,
    )
    assert path.name == f"{digest}.md"
    assert path.read_bytes() == report
    assert sha256_file(path) == digest
    with pytest.raises(IntegrityError, match="collision"):
        publish_content_addressed_report(
            repository_root=tmp_path,
            output_root="reports/generated/master_audit",
            report_bytes=report,
        )


def test_report_publication_requires_both_manifest_and_caller_enablement(
    tmp_path: Path,
) -> None:
    invocation = _invocation(tmp_path)
    with pytest.raises(PermissionError, match="does not enable"):
        runner.execute_invocation(
            invocation,
            report_bytes=b"report",
            publish_report=True,
        )
    with pytest.raises(ContractError, match="explicit publication"):
        runner.execute_invocation(
            invocation,
            report_bytes=b"report",
            publish_report=False,
        )


def test_manifest_id_changes_with_any_semantic_change(tmp_path: Path) -> None:
    first = build_invocation_payload(_unsigned_manifest(tmp_path / "first"))
    second_unsigned = deepcopy(_unsigned_manifest(tmp_path / "second"))
    second_unsigned["target_state"] = "REBUILD_COMPLETE"
    second = build_invocation_payload(second_unsigned)
    assert first["manifest_id"] != second["manifest_id"]
