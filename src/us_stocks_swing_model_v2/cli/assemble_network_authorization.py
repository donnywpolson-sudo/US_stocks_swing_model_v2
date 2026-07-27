from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..common import (
    atomic_write_new,
    canonical_json_bytes,
    require_contained_path,
)
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
    value.add_argument(
        "--allowed-output-root",
        type=Path,
        required=True,
        help="existing absolute root containing every generated output",
    )
    value.add_argument(
        "--authority-registry",
        type=Path,
        required=True,
        help="must be the exact reviewed config/authorization_authorities.json",
    )
    value.add_argument("--authority-key-id", required=True)
    value.add_argument("--public-key-file", type=Path, required=True)
    value.add_argument("--output", type=Path)
    return value


def _bounded_new_output(path: Path, *, allowed_root: Path) -> Path:
    candidate = require_contained_path(
        Path(path),
        Path(allowed_root),
        must_exist=False,
    )
    if candidate.exists():
        raise EvaluationAuthorizationError(
            "network authorization output already exists"
        )
    return candidate


def _write_new_output(path: Path, payload: bytes) -> None:
    try:
        atomic_write_new(path, payload)
    except FileExistsError as exc:
        raise EvaluationAuthorizationError(
            "network authorization output already exists"
        ) from exc


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
    if args.signing_payload_output is not None and args.output is not None:
        raise EvaluationAuthorizationError(
            "--output is valid only with --detached-signature"
        )
    signing_payload_output = (
        _bounded_new_output(
            args.signing_payload_output,
            allowed_root=args.allowed_output_root,
        )
        if args.signing_payload_output is not None
        else None
    )
    receipt_output = (
        _bounded_new_output(
            args.output,
            allowed_root=args.allowed_output_root,
        )
        if args.output is not None
        else None
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
    if signing_payload_output is not None:
        _write_new_output(
            signing_payload_output,
            network_authorization_signing_payload(
                request,
                authority=authority,
            ),
        )
        print(signing_payload_output)
        return 0
    assert args.detached_signature is not None
    assert receipt_output is not None
    signature = args.detached_signature.read_text(encoding="ascii").strip()
    receipt = assemble_network_authorization_receipt(
        request,
        signature_hex=signature,
        authority=authority,
        clock=TrustedClock.production(),
    )
    _write_new_output(
        receipt_output,
        canonical_json_bytes(receipt.as_dict()),
    )
    print(receipt.receipt_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
