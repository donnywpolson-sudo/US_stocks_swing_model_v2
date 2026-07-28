from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.nasdaq_bootstrap_publisher import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    build_nasdaq_bootstrap_publication_plan,
    publish_nasdaq_bootstrap_receipt,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan or publish one non-active Nasdaq bootstrap baseline receipt; "
            "never calls a provider or activates a source"
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
        default=_repo_root() / "data" / "w" / "nasdaq_bootstrap",
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
    plan = build_nasdaq_bootstrap_publication_plan(
        accepted_root=args.accepted_root,
        work_root=args.work_root,
    )
    if not args.execute:
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "mode": "PLAN_ONLY_NO_WRITES",
                    "publication_plan": plan,
                    "publication_authorized": False,
                    "source_activation": False,
                    "network_calls": 0,
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
        raise PermissionError("approved Nasdaq publication plan ID differs")
    result = publish_nasdaq_bootstrap_receipt(
        approved_plan_id=args.approved_plan_id,
        accepted_root=args.accepted_root,
        work_root=args.work_root,
        clock=TrustedClock.production(),
        owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "PUBLISHED_NON_ACTIVE_BOOTSTRAP_BASELINE",
                "publication_plan_id": result.publication_plan_id,
                "receipt_id": result.receipt_id,
                "release_id": result.release_id,
                "release_directory": str(result.release_directory),
                "local_integrity_record": result.local_integrity_record.as_dict(),
                "source_activation": False,
                "network_calls": 0,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
