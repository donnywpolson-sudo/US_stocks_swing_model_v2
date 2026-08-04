"""Plan or execute the bounded prospective corporate-action raw-evidence lane."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from ..clock import TrustedClock
from ..prospective_corporate_action_raw_capture import build_prospective_corporate_action_raw_capture_plan, execute_prospective_corporate_action_raw_capture


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or execute one bounded Alpaca corporate-action raw capture")
    parser.add_argument("--identity-release", type=Path, required=True)
    parser.add_argument("--bars-release", type=Path, required=True)
    parser.add_argument("--calendar-release", type=Path, required=True)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--process-date-start", type=date.fromisoformat, required=True)
    parser.add_argument("--process-date-end", type=date.fromisoformat, required=True)
    parser.add_argument("--execute-network", action="store_true")
    parser.add_argument("--approved-plan-id")
    args = parser.parse_args(argv)
    root = _root()
    plan = build_prospective_corporate_action_raw_capture_plan(repository_root=root, identity_release_directory=args.identity_release, bars_release_directory=args.bars_release, calendar_release_directory=args.calendar_release, symbols=tuple(args.symbols.split(",")), process_date_start=args.process_date_start, process_date_end=args.process_date_end)
    if not args.execute_network:
        if args.approved_plan_id:
            parser.error("--approved-plan-id requires --execute-network")
        print(json.dumps({"mode": "PLAN_ONLY_NO_NETWORK_NO_WRITES", "capture_plan": plan, "network_calls": 0, "snapshot_writes": 0, "release_publication": False, "source_activation": False}, indent=2, sort_keys=True))
        return 0
    if not args.approved_plan_id:
        parser.error("--execute-network requires --approved-plan-id")
    result = execute_prospective_corporate_action_raw_capture(plan=plan, approved_plan_id=args.approved_plan_id, api_key_id=os.environ.get("APCA_API_KEY_ID", ""), api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""), clock=TrustedClock.production(), repository_root=root)
    print(json.dumps({"mode": "CAPTURED_AND_VERIFIED_RAW_ONLY_NOT_PUBLISHED", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
