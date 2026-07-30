from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.alpaca_qualification_publisher import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    build_alpaca_qualification_publication_plan,
    publish_alpaca_qualification_receipt,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan or publish one non-active Alpaca SIP/IEX qualification "
            "receipt; never calls a provider or activates a source"
        )
    )
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--work-root",
        type=Path,
        default=_repo_root() / "data" / "w" / "alpaca_feed_qualification",
    )
    value.add_argument(
        "--execute",
        action="store_true",
        help=(
            "publish exactly one non-active receipt; also requires the owner "
            "confirmation environment token and --approved-plan-id"
        ),
    )
    value.add_argument(
        "--approved-plan-id",
        help="exact plan ID from a separately reviewed plan-only invocation",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_alpaca_qualification_publication_plan(
        accepted_root=args.accepted_root,
        work_root=args.work_root,
    )
    if not args.execute:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "PLAN_ONLY_NO_WRITES",
                    "publication_plan": plan,
                    "publication_authorized": False,
                    "source_activation": False,
                    "canonical_bars": False,
                    "network_calls": 0,
                    "credential_access": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.approved_plan_id:
        parser().error("--execute requires --approved-plan-id")
    if os.environ.get(PUBLICATION_CONFIRMATION_TOKEN) != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError(
            f"--execute also requires {PUBLICATION_CONFIRMATION_TOKEN}="
            f"{PUBLICATION_CONFIRMATION_VALUE}"
        )
    if args.approved_plan_id != plan["publication_plan_id"]:
        raise PermissionError("approved Alpaca publication plan ID differs")
    result = publish_alpaca_qualification_receipt(
        approved_plan_id=args.approved_plan_id,
        accepted_root=args.accepted_root,
        work_root=args.work_root,
        clock=TrustedClock.production(),
        owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "PUBLISHED_NON_ACTIVE_ALPACA_QUALIFICATION_RECEIPT",
                "publication_plan_id": result.publication_plan_id,
                "receipt_id": result.receipt_id,
                "release_id": result.release_id,
                "release_directory": str(result.release_directory),
                "local_integrity_record": result.local_integrity_record.as_dict(),
                "source_activation": False,
                "canonical_bars": False,
                "network_calls": 0,
                "credential_access": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
