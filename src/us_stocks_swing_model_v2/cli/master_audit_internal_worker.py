"""Private canonical-JSON subprocess worker for Master Audit internal steps."""

from __future__ import annotations

import base64
import binascii
import json
import sys
import time

from ..common import canonical_json_bytes
from ..errors import ContractError
from ..master_audit_runner import (
    _INTERNAL_STEPS,
    _MAX_INTERNAL_REQUEST_BYTES,
    _MAX_INTERNAL_RESULT_BYTES,
    MasterAuditInvocation,
    _execute_internal_step_direct,
)


def _exact_request(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ContractError("worker request must be an exact object")
    result = dict(value)
    expected = {
        "schema_version",
        "invocation",
        "step",
        "timeout_seconds",
        "publish_report",
        "report_bytes_base64",
    }
    if set(result) != expected:
        raise ContractError("worker request fields differ")
    if result["schema_version"] != 1 or type(result["schema_version"]) is not int:
        raise ContractError("worker request schema version differs")
    if type(result["step"]) is not str or result["step"] not in _INTERNAL_STEPS:
        raise ContractError("worker request step is unsupported")
    if (
        type(result["timeout_seconds"]) is not int
        or result["timeout_seconds"] <= 0
    ):
        raise ContractError("worker timeout must be a positive integer")
    if type(result["publish_report"]) is not bool:
        raise ContractError("worker publication flag must be Boolean")
    report_value = result["report_bytes_base64"]
    if report_value is not None and type(report_value) is not str:
        raise ContractError("worker report payload must be Base64 text or null")
    return result


def _decode_report(request: dict[str, object]) -> bytes | None:
    encoded = request["report_bytes_base64"]
    if request["step"] != "report_publication":
        if encoded is not None or request["publish_report"] is not False:
            raise ContractError(
                "only report_publication can receive report material"
            )
        return None
    if encoded is None:
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ContractError("worker report payload is not strict Base64") from exc


def _execute(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > _MAX_INTERNAL_REQUEST_BYTES:
        raise ContractError("worker request size is outside the bounded limit")
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("worker request is not UTF-8 JSON") from exc
    if canonical_json_bytes(decoded) != raw:
        raise ContractError("worker request is not canonical JSON")
    request = _exact_request(decoded)
    invocation = MasterAuditInvocation.from_dict(request["invocation"])
    command = next(
        entry for entry in invocation.commands if entry.step == request["step"]
    )
    if command.timeout_seconds != request["timeout_seconds"]:
        raise ContractError("worker timeout differs from the invocation")
    report_bytes = _decode_report(request)
    outcome = _execute_internal_step_direct(
        invocation,
        step=command.step,
        report_bytes=report_bytes,
        publish_report=request["publish_report"],
        deadline_monotonic=time.monotonic() + command.timeout_seconds - 0.25,
    )
    return {"ok": True, "outcome": outcome.as_dict(), "error_type": None}


def main() -> int:
    raw = sys.stdin.buffer.read(_MAX_INTERNAL_REQUEST_BYTES + 1)
    try:
        payload = _execute(raw)
    except Exception as exc:
        payload = {
            "ok": False,
            "outcome": None,
            "error_type": type(exc).__name__,
        }
    encoded = canonical_json_bytes(payload)
    if len(encoded) > _MAX_INTERNAL_RESULT_BYTES:
        return 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
