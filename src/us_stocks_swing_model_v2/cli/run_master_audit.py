from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..common import (
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
)
from ..master_audit_runner import (
    StepEvidenceEmitter,
    execute_invocation,
    load_invocation_manifest,
    validate_repository_preflight,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Validate or execute one exact manifest-driven read-only Master Audit; "
            "never discovers inputs, uses providers, activates sources, or runs research"
        )
    )
    value.add_argument("--manifest", type=Path, required=True)
    value.add_argument("--manifest-sha256", required=True)
    value.add_argument(
        "--execute",
        action="store_true",
        help="execute only the exact ordered commands declared by the manifest",
    )
    value.add_argument(
        "--publish-report",
        action="store_true",
        help="publish exact report bytes; requires --execute and report bindings",
    )
    value.add_argument("--report-source", type=Path)
    value.add_argument("--report-sha256")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    invocation = load_invocation_manifest(
        args.manifest,
        expected_file_sha256=args.manifest_sha256,
    )
    if not args.execute:
        if args.publish_report or args.report_source is not None or args.report_sha256:
            parser().error("report arguments require --execute")
        preflight = validate_repository_preflight(invocation)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "VALIDATION_ONLY_NO_WRITES",
                    "manifest_id": invocation.manifest_id,
                    "target_state": invocation.target_state,
                    "preflight": preflight,
                    "publication_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.publish_report:
        if args.report_source is None or args.report_sha256 is None:
            parser().error("--publish-report requires --report-source and --report-sha256")
        expected = require_sha256(args.report_sha256, "report_sha256")
        report_source = args.report_source
        if not report_source.is_absolute():
            report_source = invocation.repository.root / report_source
        report_source = require_contained_path(
            report_source, invocation.repository.root
        )
        reject_link(report_source)
        if not report_source.is_file() or report_source.stat().st_nlink != 1:
            raise ValueError("report source must be an ordinary single-link file")
        report_bytes = report_source.read_bytes()
        if sha256_bytes(report_bytes) != expected:
            raise ValueError("report source hash differs from --report-sha256")
    else:
        if args.report_source is not None or args.report_sha256 is not None:
            parser().error("report source arguments require --publish-report")
        report_bytes = None

    evidence_emitter = StepEvidenceEmitter(
        manifest_id=invocation.manifest_id,
        stream=sys.stderr.buffer,
    )
    try:
        result = execute_invocation(
            invocation,
            evidence_emitter=evidence_emitter,
            report_bytes=report_bytes,
            publish_report=args.publish_report,
        )
    except Exception:
        return 1
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
