from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.alpaca_historical_backfill import (
    build_historical_backfill_plan,
    execute_historical_backfill_group,
    plan_summary,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Emit a deterministic, no-network, no-write Alpaca SIP historical "
            "backfill plan for the current identity cohort missing from the "
            "accepted rehabilitated archive"
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    value.add_argument(
        "--execute-group",
        type=int,
        help="execute exactly one separately approved plan group",
    )
    value.add_argument("--approved-plan-id")
    value.add_argument("--approved-group-request-plan-ids-sha256")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_historical_backfill_plan(repo_root=args.repo_root)
    approval_values = (
        args.approved_plan_id,
        args.approved_group_request_plan_ids_sha256,
    )
    if args.execute_group is None:
        if any(value is not None for value in approval_values):
            parser().error("execution approvals require --execute-group")
        print(json.dumps(plan_summary(plan), indent=2, sort_keys=True))
        return 0
    if any(value is None for value in approval_values):
        parser().error(
            "--execute-group requires --approved-plan-id and "
            "--approved-group-request-plan-ids-sha256"
        )
    if args.approved_plan_id != plan["backfill_plan_id"]:
        raise PermissionError("approved historical backfill plan ID differs")
    groups = [
        group
        for group in plan["execution_groups"]
        if group["group_index"] == args.execute_group
    ]
    if (
        len(groups) != 1
        or groups[0]["request_plan_ids_sha256"]
        != args.approved_group_request_plan_ids_sha256
    ):
        raise PermissionError("approved historical backfill execution group differs")
    if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
        raise PermissionError(
            "--execute-group requires FREE_SOURCE_QUALIFICATION_APPROVED=YES"
        )
    api_key_id = os.environ.get("APCA_API_KEY_ID", "")
    api_secret_key = os.environ.get("APCA_API_SECRET_KEY", "")
    if not api_key_id or not api_secret_key:
        raise PermissionError("Alpaca credentials are absent from the process environment")
    snapshots, assessment = execute_historical_backfill_group(
        backfill_plan=plan,
        approved_backfill_plan_id=args.approved_plan_id,
        group_index=args.execute_group,
        approved_group_request_plan_ids_sha256=(
            args.approved_group_request_plan_ids_sha256
        ),
        api_key_id=api_key_id,
        api_secret_key=api_secret_key,
        clock=TrustedClock.production(),
        repo_root=args.repo_root,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "BACKFILL_GROUP_CAPTURED_AND_VERIFIED_NOT_PUBLISHED",
                "backfill_plan_id": plan["backfill_plan_id"],
                "group_index": args.execute_group,
                "snapshot_count": len(snapshots),
                "snapshots": [
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "raw_sha256": snapshot.raw_sha256,
                        "directory": str(snapshot.root),
                    }
                    for snapshot in snapshots
                ],
                "assessment": assessment,
                "publication": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
