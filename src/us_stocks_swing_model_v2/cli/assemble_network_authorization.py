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
    network_authorization_signing_payload,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Assemble and verify one externally signed network authorization"
    )
    value.add_argument("--request", type=Path, required=True)
    value.add_argument("--detached-signature", type=Path)
    value.add_argument("--signing-payload-output", type=Path)
    value.add_argument("--authority-registry", type=Path, required=True)
    value.add_argument("--authority-key-id", required=True)
    value.add_argument("--public-key-file", type=Path, required=True)
    value.add_argument("--output", type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if bool(args.detached_signature) == bool(args.signing_payload_output):
        raise EvaluationAuthorizationError(
            "choose exactly one of detached signature or signing-payload output"
        )
    if args.detached_signature is not None and args.output is None:
        raise EvaluationAuthorizationError(
            "receipt assembly requires --output"
        )
    if args.output is not None and args.output.exists():
        raise EvaluationAuthorizationError(
            "network authorization output already exists"
        )
    request = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise EvaluationAuthorizationError(
            "network authorization request must be a JSON object"
        )
    authority = load_external_authority(
        args.authority_registry,
        key_id=args.authority_key_id,
        verification_key=args.public_key_file.read_bytes(),
    )
    if args.signing_payload_output is not None:
        if args.signing_payload_output.exists():
            raise EvaluationAuthorizationError(
                "signing payload output already exists"
            )
        atomic_write(
            args.signing_payload_output,
            network_authorization_signing_payload(
                request,
                authority=authority,
            ),
        )
        print(args.signing_payload_output)
        return 0
    assert args.detached_signature is not None
    assert args.output is not None
    signature = args.detached_signature.read_text(encoding="ascii").strip()
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
