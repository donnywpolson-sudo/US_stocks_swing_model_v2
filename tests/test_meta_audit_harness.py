from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.meta_audit_harness import (
    EXPECTED_EXIT,
    OUTPUT_DESTINATION,
    POWERSHELL_FLAGS,
    PROHIBITIONS,
    MetaAuditEnvelope,
    build_envelope_payload,
    load_envelope,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)
SCRIPT = REPOSITORY_ROOT / "tools" / "meta_audit" / "Invoke-MetaAuditEvidence.ps1"


def _git_blob(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _file_binding(root: Path, path: str) -> dict[str, object]:
    data = (root / path).read_bytes()
    return {
        "path": path,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "git_blob": _git_blob(data),
    }


def _host_profile(script: Path) -> dict[str, object]:
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            "-Mode",
            "HostProfile",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return json.loads(result.stdout)


def _command(
    root: Path,
    host: dict[str, object],
    ordinal: int,
    mode: str,
    *,
    batch_ordinal: int | None,
    output_max_utf8_bytes: int,
) -> dict[str, object]:
    script = root / "tools" / "meta_audit" / SCRIPT.name
    return {
        "command_ordinal": ordinal,
        "mode": mode,
        "batch_ordinal": batch_ordinal,
        "argv": [
            str(host["powershell_executable"]),
            *POWERSHELL_FLAGS,
            str(script),
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
        "timeout_seconds": 30,
        "expected_exit": EXPECTED_EXIT,
        "output_max_utf8_bytes": output_max_utf8_bytes,
    }


def _unsigned_envelope(
    root: Path,
    host: dict[str, object],
) -> dict[str, object]:
    reference_limit = 2_000
    target_limit = 2_000
    return {
        "schema_version": 1,
        "repository": {
            "root": str(root),
            "branch": "main",
            "head": "1" * 40,
            "tree": "2" * 40,
            "require_clean": True,
        },
        "host": host,
        "script": _file_binding(
            root, f"tools/meta_audit/{SCRIPT.name}"
        ),
        "target": _file_binding(root, "MASTER_AUDIT.md"),
        "controller": _file_binding(root, "META_MASTER_AUDIT.md"),
        "reference_census": {"count": 1, "sha256": "3" * 64},
        "read_batches": [
            {
                "batch_ordinal": 1,
                "phase": "REFERENCE",
                "path": "reference.txt",
                "start_line": 1,
                "line_count": 2,
                "max_rendered_utf8_bytes": reference_limit,
                "file_sha256": _file_binding(root, "reference.txt")["sha256"],
                "file_git_blob": _file_binding(root, "reference.txt")["git_blob"],
                "target": False,
            },
            {
                "batch_ordinal": 2,
                "phase": "TARGET",
                "path": "MASTER_AUDIT.md",
                "start_line": 1,
                "line_count": 2,
                "max_rendered_utf8_bytes": target_limit,
                "file_sha256": _file_binding(root, "MASTER_AUDIT.md")["sha256"],
                "file_git_blob": _file_binding(root, "MASTER_AUDIT.md")["git_blob"],
                "target": True,
            },
        ],
        "commands": [
            _command(
                root,
                host,
                1,
                "Preflight",
                batch_ordinal=None,
                output_max_utf8_bytes=2_000,
            ),
            _command(
                root,
                host,
                2,
                "PlanBatches",
                batch_ordinal=None,
                output_max_utf8_bytes=2_000,
            ),
            _command(
                root,
                host,
                3,
                "ReadReferenceBatch",
                batch_ordinal=1,
                output_max_utf8_bytes=reference_limit,
            ),
            _command(
                root,
                host,
                4,
                "ReadTargetBatch",
                batch_ordinal=2,
                output_max_utf8_bytes=target_limit,
            ),
            _command(
                root,
                host,
                5,
                "FinalPreflight",
                batch_ordinal=None,
                output_max_utf8_bytes=2_000,
            ),
        ],
        "barriers": [
            {
                "name": "B01_BLIND_CENSUS_FROZEN",
                "after_command_ordinal": 3,
                "before_command_ordinal": 4,
            },
            {
                "name": "B02_MAPPING_COMPLETE",
                "after_command_ordinal": 4,
                "before_command_ordinal": 5,
            },
        ],
        "output": {"destination": OUTPUT_DESTINATION, "retained": False},
        "prohibitions": list(PROHIBITIONS),
    }


@pytest.fixture
def harness_root(tmp_path: Path) -> Path:
    script = tmp_path / "tools" / "meta_audit" / SCRIPT.name
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    (tmp_path / "reference.txt").write_text(
        "reference one\nreference two\nreference three\n",
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "MASTER_AUDIT.md").write_text(
        "target one\ntarget two\ntarget three\n",
        encoding="utf-8",
        newline="\n",
    )
    (tmp_path / "META_MASTER_AUDIT.md").write_text(
        "controller\n",
        encoding="utf-8",
        newline="\n",
    )
    return tmp_path


def test_envelope_is_canonical_content_addressed_and_loadable(
    harness_root: Path,
) -> None:
    host = _host_profile(harness_root / "tools" / "meta_audit" / SCRIPT.name)
    payload = build_envelope_payload(_unsigned_envelope(harness_root, host))
    raw = canonical_json_bytes(payload)
    path = harness_root / "envelope.json"
    path.write_bytes(raw)

    loaded = load_envelope(path, expected_file_sha256=sha256_bytes(raw))

    assert loaded.envelope_id == payload["envelope_id"]
    assert loaded.output_destination == OUTPUT_DESTINATION
    assert loaded.output_retained is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda value: value["commands"][2]["argv"].append("--drift"),
            "argv differs",
        ),
        (
            lambda value: value["commands"][2].__setitem__(
                "output_max_utf8_bytes", 1
            ),
            "output limit differs",
        ),
        (
            lambda value: value["read_batches"][0].__setitem__("target", True),
            "batch applicability",
        ),
        (
            lambda value: value["barriers"][0].__setitem__(
                "before_command_ordinal", 5
            ),
            "adjacent commands",
        ),
    ],
)
def test_envelope_rejects_execution_or_order_drift(
    harness_root: Path,
    mutation: object,
    message: str,
) -> None:
    host = _host_profile(harness_root / "tools" / "meta_audit" / SCRIPT.name)
    unsigned = _unsigned_envelope(harness_root, host)
    mutation(unsigned)

    with pytest.raises(ContractError, match=message):
        build_envelope_payload(unsigned)


def test_envelope_rejects_missing_blind_or_target_phase(harness_root: Path) -> None:
    host = _host_profile(harness_root / "tools" / "meta_audit" / SCRIPT.name)
    payload = build_envelope_payload(_unsigned_envelope(harness_root, host))
    payload["read_batches"][0]["target"] = True
    payload["read_batches"][0]["path"] = "MASTER_AUDIT.md"
    payload["commands"][2]["mode"] = "ReadTargetBatch"
    payload["commands"][2]["argv"][7] = "ReadTargetBatch"
    unsigned = {key: value for key, value in payload.items() if key != "envelope_id"}

    with pytest.raises(ContractError, match="both reference and target"):
        build_envelope_payload(unsigned)


def test_load_rejects_wrong_file_hash(harness_root: Path) -> None:
    path = harness_root / "envelope.json"
    path.write_text("{}\n", encoding="utf-8", newline="\n")

    with pytest.raises(IntegrityError, match="file hash mismatch"):
        load_envelope(path, expected_file_sha256="4" * 64)


def test_windows_powershell_host_profile_and_self_test_are_compatible() -> None:
    profile = _host_profile(SCRIPT)
    assert profile["ps_edition"] == "Desktop"
    assert profile["sha256_hash_data_available"] is False
    assert profile["sha1_hash_data_available"] is False
    assert profile["path_get_relative_path_available"] is False

    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(SCRIPT),
            "-Mode",
            "SelfTest",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["mode"] == "SELF_TEST_NO_WRITES"


def test_exact_script_reads_one_bounded_batch_and_fails_on_self_drift(
    harness_root: Path,
) -> None:
    script = harness_root / "tools" / "meta_audit" / SCRIPT.name
    host = _host_profile(script)
    payload = build_envelope_payload(_unsigned_envelope(harness_root, host))
    raw = canonical_json_bytes(payload)
    envelope = harness_root / "envelope.json"
    envelope.write_bytes(raw)
    envelope_sha256 = sha256_bytes(raw)
    command = [
        str(POWERSHELL),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        "-Mode",
        "ReadReferenceBatch",
        "-EnvelopePath",
        str(envelope),
        "-EnvelopeSha256",
        envelope_sha256,
        "-CommandOrdinal",
        "3",
    ]

    result = subprocess.run(
        command,
        cwd=harness_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "lines 1-2" in result.stdout
    assert "000001: reference one" in result.stdout
    assert "reference three" not in result.stdout

    script.write_bytes(script.read_bytes() + b"\n")
    drifted = subprocess.run(
        command,
        cwd=harness_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert drifted.returncode != 0
    assert "script identity differs" in drifted.stderr


def test_git_attributes_pin_powershell_to_lf() -> None:
    assert "*.ps1 text eol=lf" in (REPOSITORY_ROOT / ".gitattributes").read_text(
        encoding="utf-8"
    ).splitlines()
