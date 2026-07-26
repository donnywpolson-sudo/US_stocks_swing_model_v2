from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..foundation_orchestrator import (
    plan_stock_historical_foundation,
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
        "--calendar-receipt",
        type=Path,
        default=_repo_root() / "config" / "xnys_calendar_release_receipt.json",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
