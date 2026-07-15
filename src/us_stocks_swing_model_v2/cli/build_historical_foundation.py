from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..foundation_orchestrator import (
    plan_stock_historical_foundation,
    run_stock_historical_foundation,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Verify or build the non-active stock historical foundation; "
            "never calls providers or executes research"
        )
    )
    value.add_argument("--migration-release", type=Path, required=True)
    value.add_argument("--created-at", required=True, help="canonical UTC Z timestamp")
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--work-root",
        type=Path,
        default=_repo_root() / "data" / "w",
    )
    value.add_argument(
        "--calendar-receipt",
        type=Path,
        default=_repo_root() / "config" / "xnys_calendar_release_receipt.json",
    )
    value.add_argument(
        "--execute",
        action="store_true",
        help="publish only non-active foundation releases after exact verification",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute:
        result = run_stock_historical_foundation(
            migration_release_directory=args.migration_release,
            accepted_release_root=args.accepted_root,
            derived_work_root=args.work_root,
            created_at=args.created_at,
            calendar_receipt_path=args.calendar_receipt,
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "mode": "EXECUTE_NON_ACTIVE_FOUNDATION_ONLY",
            "build_id": result.build_id,
            "migration_plan_id": result.migration_plan_id,
            "calendar_release_directory": str(result.calendar_release_directory),
            "hfdl_epoch_set_release_directory": str(
                result.hfdl_publication.epoch_set_release_directory
            ),
            "historical_bridge_set_release_directory": str(
                result.historical_foundation.bridge_set_release_directory
            ),
            "aggregate_set_release_directory": str(
                result.aggregate_set_release_directory
            ),
            "research_or_model_executed": False,
        }
    else:
        payload = plan_stock_historical_foundation(
            migration_release_directory=args.migration_release,
            accepted_release_root=args.accepted_root,
            created_at=args.created_at,
            calendar_receipt_path=args.calendar_receipt,
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
