from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..legacy_cleanup import APPROVED_CLEANUP_PLAN_ID, execute_purge, prepare_purge_execution


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-shot hash-bound legacy purge")
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--expected-executor-commit")
    parser.add_argument("--owner-confirmation")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.plan_id != APPROVED_CLEANUP_PLAN_ID:
        parser.error("--plan-id differs from the approved cleanup plan")
    if not args.execute:
        print(json.dumps(prepare_purge_execution(_repo_root(), plan_id=args.plan_id)["plan"], sort_keys=True))
        return 0
    if not args.expected_executor_commit or not args.owner_confirmation:
        parser.error("--execute requires --expected-executor-commit and --owner-confirmation")
    result = execute_purge(_repo_root(), plan_id=args.plan_id, expected_executor_commit=args.expected_executor_commit, owner_confirmation=args.owner_confirmation, created_at=TrustedClock.production().now().isoformat().replace("+00:00", "Z"))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
