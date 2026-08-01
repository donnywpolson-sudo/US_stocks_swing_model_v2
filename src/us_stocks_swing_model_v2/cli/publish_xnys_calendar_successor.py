from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..calendar_successor import CONFIRMATION_TOKEN, build_calendar_successor_plan, publish_calendar_successor
from ..clock import TrustedClock


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or publish one non-active XNYS calendar successor")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-id")
    args = parser.parse_args(argv)
    plan = build_calendar_successor_plan(repository_root=_root())
    if not args.execute:
        print(json.dumps({"mode": "PLAN_ONLY_NO_WRITES", "calendar_successor_plan": plan, "publication_authorized": False}, indent=2, sort_keys=True))
        return 0
    if not args.approved_plan_id:
        parser.error("--execute requires --approved-plan-id")
    release = publish_calendar_successor(approved_plan_id=args.approved_plan_id, owner_confirmation=os.environ.get(CONFIRMATION_TOKEN, ""), clock=TrustedClock.production(), repository_root=_root())
    print(json.dumps({"mode": "PUBLISHED_XNYS_CALENDAR_SUCCESSOR", "release_directory": str(release), "network_calls": 0, "source_activation": False}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
