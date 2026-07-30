"""Prepare or validate a non-authorizing Meta Audit envelope."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from ..common import canonical_json_bytes
from ..meta_audit_harness import build_envelope_payload, load_envelope


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--unsigned", type=Path)
    mode.add_argument("--manifest", type=Path)
    parser.add_argument("--manifest-sha256")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.unsigned is not None:
        if args.manifest_sha256 is not None:
            raise SystemExit("--manifest-sha256 is valid only with --manifest")
        unsigned = json.loads(args.unsigned.read_text(encoding="utf-8"))
        sys.stdout.buffer.write(canonical_json_bytes(build_envelope_payload(unsigned)))
        return 0
    if args.manifest_sha256 is None:
        raise SystemExit("--manifest requires --manifest-sha256")
    envelope = load_envelope(
        args.manifest, expected_file_sha256=args.manifest_sha256
    )
    sys.stdout.buffer.write(
        canonical_json_bytes(
            {
                "envelope_id": envelope.envelope_id,
                "mode": "VALIDATION_ONLY_NO_WRITES",
                "valid": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
