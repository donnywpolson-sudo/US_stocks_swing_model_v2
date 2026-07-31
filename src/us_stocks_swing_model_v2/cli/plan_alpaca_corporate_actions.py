from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from ..alpaca_corporate_action_preflight import (
    build_corporate_action_preflight,
    execute_corporate_action_preflight,
)
from ..clock import TrustedClock


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or execute one approved, caveated Alpaca corporate-actions sample.")
    parser.add_argument("--release-directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated uppercase symbols")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--execute-network", action="store_true")
    parser.add_argument("--approved-plan-id")
    args = parser.parse_args(argv)
    root = _repo_root()
    symbols = tuple(args.symbols.split(","))
    plan = build_corporate_action_preflight(release_directory=(root / args.release_directory).resolve(), accepted_root=root / "data" / "vault" / "accepted", start=args.start, end=args.end, symbols=symbols, max_pages=args.max_pages, created_at=args.created_at, repo_root=root)
    if not args.execute_network:
        if args.approved_plan_id is not None:
            parser.error("--approved-plan-id requires --execute-network")
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    if not args.approved_plan_id:
        parser.error("--execute-network requires --approved-plan-id")
    if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
        raise PermissionError("--execute-network requires FREE_SOURCE_QUALIFICATION_APPROVED=YES")
    result = execute_corporate_action_preflight(
        plan=plan,
        approved_plan_id=args.approved_plan_id,
        api_key_id=os.environ.get("APCA_API_KEY_ID", ""),
        api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
        clock=TrustedClock.production(),
        repo_root=root,
    )
    print(json.dumps({"mode": "CAPTURED_AND_VERIFIED_NOT_PUBLISHED", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
