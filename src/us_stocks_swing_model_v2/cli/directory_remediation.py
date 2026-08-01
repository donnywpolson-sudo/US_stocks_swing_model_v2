from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..directory_remediation import execute_directory_remediation, write_remediation_plan


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Directory-only legacy purge remediation")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--execute-plan", action="store_true")
    parser.add_argument("--plan-id")
    parser.add_argument("--owner-confirmation")
    parser.add_argument("--execute-remediation", action="store_true")
    args = parser.parse_args(argv)
    now = TrustedClock.production().now().isoformat().replace("+00:00", "Z")
    if args.execute_plan:
        result = write_remediation_plan(_root(), expected_commit=args.expected_commit, created_at=now)
    elif args.execute_remediation and args.plan_id and args.owner_confirmation:
        result = execute_directory_remediation(_root(), plan_id=args.plan_id, expected_commit=args.expected_commit, confirmation=args.owner_confirmation, created_at=now)
    else:
        parser.error("select --execute-plan or supply plan ID and confirmation with --execute-remediation")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
