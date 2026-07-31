from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..alpaca_archive_rehabilitation_publisher import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    build_rehabilitation_publication_plan,
    publish_rehabilitation_release,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan or publish one immutable PIT-unresolved Alpaca legacy-discovery "
            "release; never calls a provider, activates a source, or authorizes "
            "research"
        )
    )
    value.add_argument(
        "--created-at",
        required=True,
        help=(
            "exact canonical UTC Z timestamp bound into the prospective release "
            "and reused unchanged for separately authorized execution"
        ),
    )
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--work-root",
        type=Path,
        default=_repo_root() / "data" / "w" / "alpaca_archive_rehabilitation",
    )
    value.add_argument(
        "--execute",
        action="store_true",
        help=(
            "publish exactly one caveated release; also requires the exact "
            "approved plan ID and owner confirmation environment token"
        ),
    )
    value.add_argument(
        "--approved-plan-id",
        help="exact plan ID from a separately reviewed plan-only invocation",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute:
        if not args.approved_plan_id:
            parser().error("--execute requires --approved-plan-id")
        if (
            os.environ.get(PUBLICATION_CONFIRMATION_TOKEN)
            != PUBLICATION_CONFIRMATION_VALUE
        ):
            raise PermissionError(
                f"--execute also requires {PUBLICATION_CONFIRMATION_TOKEN}="
                f"{PUBLICATION_CONFIRMATION_VALUE}"
            )
        result = publish_rehabilitation_release(
            approved_plan_id=args.approved_plan_id,
            created_at=args.created_at,
            accepted_root=args.accepted_root,
            work_root=args.work_root,
            owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
        )
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "mode": "PUBLISHED_PIT_UNRESOLVED_LEGACY_DISCOVERY",
                    "publication_plan_id": result.publication_plan_id,
                    "release_id": result.release_id,
                    "receipt_id": result.receipt_id,
                    "release_directory": str(result.release_directory),
                    "work_directory": str(result.work_directory),
                    "network_calls": 0,
                    "source_activation": False,
                    "training_or_research_authorized": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    if args.approved_plan_id is not None:
        parser().error("--approved-plan-id is valid only with --execute")
    plan = build_rehabilitation_publication_plan(
        accepted_root=args.accepted_root,
        work_root=args.work_root,
        created_at=args.created_at,
    )
    print(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "PLAN_ONLY_NO_WRITES",
                "publication_plan": plan,
                "publication_authorized": False,
                "network_calls": 0,
                "source_activation": False,
                "training_or_research_authorized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
