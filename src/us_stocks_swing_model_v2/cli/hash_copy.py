from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..governance import load_external_authority, load_signed_authorization_receipt
from ..migration import (
    ControlledRebuildAuthorization,
    execute_copy_plan,
    load_migration_approval,
    load_migration_config,
    plan_migration,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan or execute exact hash-verified migration copies")
    value.add_argument("--config", type=Path, required=True)
    value.add_argument(
        "--detailed-plan",
        action="store_true",
        help="include every reviewed source/destination/hash entry in dry-run output",
    )
    authority_mode = value.add_mutually_exclusive_group()
    authority_mode.add_argument(
        "--authorization",
        type=Path,
        help="externally signed exact copy authorization receipt",
    )
    authority_mode.add_argument(
        "--controlled-rebuild-authorization",
        type=Path,
        help="repository-pinned authority for the completed controlled rebuild",
    )
    value.add_argument(
        "--authority-registry",
        type=Path,
        help="exact reviewed config/authorization_authorities.json",
    )
    value.add_argument("--authority-key-id", help="active external authority key ID")
    value.add_argument("--public-key-file", type=Path, help="external RSA public JWK")
    value.add_argument("--execute", action="store_true", help="requires HASH_COPY_APPROVED=YES")
    value.add_argument(
        "--approval",
        type=Path,
        help="exact reviewed approval JSON; mandatory with --execute",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = load_migration_config(args.config)
    plan = plan_migration(config)
    summary: dict[str, object] = {
        "mode": "execute" if args.execute else "dry_run",
        **plan.concise_summary(),
    }
    if args.detailed_plan:
        summary["entries"] = [entry.as_dict() for entry in plan]
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.execute:
        if args.approval is None:
            raise PermissionError("--execute requires: --approval")
        approval = load_migration_approval(args.approval, plan)
        if args.controlled_rebuild_authorization is not None:
            forbidden_external = {
                "--authority-registry": args.authority_registry,
                "--authority-key-id": args.authority_key_id,
                "--public-key-file": args.public_key_file,
            }
            present = [name for name, value in forbidden_external.items() if value is not None]
            if present:
                raise PermissionError(
                    "controlled-rebuild mode cannot mix external authority inputs: "
                    + ", ".join(present)
                )
            controlled = ControlledRebuildAuthorization.load(
                args.controlled_rebuild_authorization
            )
            execute_copy_plan(
                plan,
                approval=approval,
                controlled_rebuild_authorization=controlled,
                clock=TrustedClock.production(),
                execute=True,
            )
        else:
            required = {
                "--authorization": args.authorization,
                "--authority-registry": args.authority_registry,
                "--authority-key-id": args.authority_key_id,
                "--public-key-file": args.public_key_file,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise PermissionError(f"--execute requires: {', '.join(missing)}")
            authorization = load_signed_authorization_receipt(args.authorization)
            authority = load_external_authority(
                args.authority_registry,
                key_id=args.authority_key_id,
                verification_key=args.public_key_file.read_bytes(),
            )
            execute_copy_plan(
                plan,
                approval=approval,
                authorization=authorization,
                authorization_authority=authority,
                clock=TrustedClock.production(),
                execute=True,
            )
    elif any(
        value is not None
        for value in (
            args.approval,
            args.authorization,
            args.controlled_rebuild_authorization,
            args.authority_registry,
            args.authority_key_id,
            args.public_key_file,
        )
    ):
        raise PermissionError("authorization inputs are accepted only with --execute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
