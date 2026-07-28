from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.identity_publisher import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    build_identity_release_publication_plan,
    publish_identity_release,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan or publish one immutable Nasdaq/Alpaca identity release; "
            "never captures a provider response or activates a source"
        )
    )
    value.add_argument("--alpaca-assets-snapshot", required=True, type=Path)
    value.add_argument("--nasdaq-snapshot", required=True, type=Path)
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--work-root",
        type=Path,
        default=_repo_root() / "data" / "w" / "nasdaq_identity",
    )
    value.add_argument(
        "--execute",
        action="store_true",
        help=(
            "publish exactly one non-active identity release; also requires "
            "the owner confirmation token and --approved-plan-id"
        ),
    )
    value.add_argument(
        "--approved-plan-id",
        help="exact plan ID from a separately reviewed plan-only invocation",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_identity_release_publication_plan(
        alpaca_snapshot_directory=args.alpaca_assets_snapshot,
        nasdaq_snapshot_directory=args.nasdaq_snapshot,
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
                    "network_calls": 0,
                    "source_activation": False,
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
        raise PermissionError("approved identity publication plan ID differs")
    result = publish_identity_release(
        approved_plan_id=args.approved_plan_id,
        alpaca_snapshot_directory=args.alpaca_assets_snapshot,
        nasdaq_snapshot_directory=args.nasdaq_snapshot,
        accepted_root=args.accepted_root,
        work_root=args.work_root,
        clock=TrustedClock.production(),
        owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "PUBLISHED_NON_ACTIVE_IDENTITY_RELEASE",
                "publication_plan_id": result.publication_plan_id,
                "receipt_id": result.receipt_id,
                "release_id": result.release_id,
                "release_directory": str(result.release_directory),
                "network_calls": 0,
                "source_activation": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
