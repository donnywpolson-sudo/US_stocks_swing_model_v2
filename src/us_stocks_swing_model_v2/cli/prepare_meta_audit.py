"""Prepare or validate a non-authorizing Meta Audit envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..common import canonical_json_bytes
from ..meta_audit_harness import (
    build_envelope_payload,
    load_envelope,
    prepare_v2_envelope,
    run_host_validation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--unsigned", type=Path)
    mode.add_argument("--manifest", type=Path)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--validate-host", action="store_true")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    parser.add_argument("--controller", default="META_MASTER_AUDIT.md")
    parser.add_argument("--target", default="MASTER_AUDIT.md")
    parser.add_argument(
        "--corpus-policy", default="config/meta_audit_reference_corpus.json"
    )
    parser.add_argument(
        "--script", default="tools/meta_audit/Invoke-MetaAuditEvidence.ps1"
    )
    parser.add_argument(
        "--powershell",
        type=Path,
        default=Path(
            r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.unsigned is not None:
        if args.manifest_sha256 is not None:
            raise SystemExit("--manifest-sha256 is valid only with --manifest")
        unsigned = json.loads(args.unsigned.read_text(encoding="utf-8"))
        sys.stdout.buffer.write(canonical_json_bytes(build_envelope_payload(unsigned)))
        return 0
    if args.validate_host:
        if args.manifest_sha256 is not None:
            raise SystemExit("--manifest-sha256 is valid only with --manifest")
        result = run_host_validation(
            powershell_executable=args.powershell,
            script_path=args.repository_root.resolve() / args.script,
        )
        sys.stdout.buffer.write(canonical_json_bytes(result))
        return 0
    if args.prepare:
        if args.manifest_sha256 is not None:
            raise SystemExit("--manifest-sha256 is valid only with --manifest")
        envelope = prepare_v2_envelope(
            root=args.repository_root,
            controller_path=args.controller,
            target_path=args.target,
            corpus_policy_path=args.corpus_policy,
            script_path=args.script,
            powershell_executable=args.powershell,
        )
        sys.stdout.buffer.write(canonical_json_bytes(envelope))
        return 0
    if args.manifest_sha256 is None:
        raise SystemExit("--manifest requires --manifest-sha256")
    envelope = load_envelope(
        args.manifest, expected_file_sha256=args.manifest_sha256
    )
    envelope_id = (
        envelope.envelope_id
        if hasattr(envelope, "envelope_id")
        else envelope["envelope_id"]
    )
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "envelope_id": envelope_id,
                "mode": "VALIDATION_ONLY_NO_WRITES",
                "valid": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
