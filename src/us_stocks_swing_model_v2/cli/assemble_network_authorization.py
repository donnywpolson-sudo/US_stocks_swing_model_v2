from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..common import atomic_write, canonical_json_bytes
from ..errors import EvaluationAuthorizationError
from ..governance import load_external_authority
from ..providers.network_authorization import (
    assemble_network_authorization_receipt,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Assemble and verify one externally signed network authorization"
    )
    value.add_argument("--request", type=Path, required=True)
    value.add_argument("--detached-signature", type=Path, required=True)
    value.add_argument("--authority-registry", type=Path, required=True)
    value.add_argument("--authority-key-id", required=True)
    value.add_argument("--public-key-file", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.output.exists():
        raise EvaluationAuthorizationError(
            "network authorization output already exists"
        )
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise EvaluationAuthorizationError(
            "network authorization request must be a JSON object"
        )
    signature = args.detached_signature.read_text(encoding="ascii").strip()
    authority = load_external_authority(
        args.authority_registry,
        key_id=args.authority_key_id,
        verification_key=args.public_key_file.read_bytes(),
    )
    receipt = assemble_network_authorization_receipt(
        request,
        signature_hex=signature,
        authority=authority,
        clock=TrustedClock.production(),
    )
    atomic_write(args.output, canonical_json_bytes(receipt.as_dict()))
    print(receipt.receipt_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
