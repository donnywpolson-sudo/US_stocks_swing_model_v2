from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.alpaca_sip_qualification_publisher import CONFIRMATION_TOKEN, CONFIRMATION_VALUE, build_publication_plan, publish_receipt


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or publish one non-active Alpaca SIP qualification receipt")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-plan-id")
    args = parser.parse_args(argv)
    plan = build_publication_plan(repo_root=_root())
    if not args.execute:
        print(json.dumps({"schema_version": 1, "mode": "PLAN_ONLY_NO_WRITES", "publication_plan": plan}, indent=2, sort_keys=True))
        return 0
    if not args.approved_plan_id:
        parser.error("--execute requires --approved-plan-id")
    result = publish_receipt(approved_plan_id=args.approved_plan_id, owner_confirmation=os.environ.get(CONFIRMATION_TOKEN, ""), clock=TrustedClock.production(), repo_root=_root())
    print(json.dumps({"schema_version": 1, "mode": "PUBLISHED_NON_ACTIVE_QUALIFICATION_RECEIPT", **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
