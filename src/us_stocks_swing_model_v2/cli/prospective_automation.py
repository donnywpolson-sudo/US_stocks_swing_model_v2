from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from ..clock import TrustedClock
from ..prospective_automation import (
    acceptance_status,
    automation_status,
    build_structural_recovery_plan,
    dry_run_daily_capture,
    execute_structural_recovery,
    load_automation_policy,
    run_daily_capture,
    supersede_legacy_soak_and_initialize_acceptance,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Owner-operated ALPACA_FREE_BOUNDED_V1 prospective automation",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-policy")
    dry_run = subparsers.add_parser("dry-run")
    dry_run.add_argument("--local-date", type=date.fromisoformat)
    live = subparsers.add_parser("run-daily")
    live.add_argument("--execute-network", action="store_true")
    initialize = subparsers.add_parser("initialize-acceptance")
    initialize.add_argument("--remediation-commit", required=True)
    recovery_plan = subparsers.add_parser("plan-structural-recovery")
    recovery_plan.add_argument("--remediation-commit", required=True)
    recovery = subparsers.add_parser("recover-structural-failure")
    recovery.add_argument("--remediation-commit", required=True)
    recovery.add_argument("--approved-recovery-plan-id", required=True)
    subparsers.add_parser("acceptance-status")
    subparsers.add_parser("status")
    return parser


def main() -> int:
    args = _parser().parse_args()
    root = _root()
    if args.command == "validate-policy":
        policy = load_automation_policy(root)
        _print({
            "state": "PASS",
            "policy_id": policy["policy_id"],
            "required_consecutive_sessions": policy["required_consecutive_sessions"],
            "background_monitor_blocking": policy["background_monitor"]["blocking"],
            "network_requests": 0,
        })
        return 0
    if args.command == "dry-run":
        _print(dry_run_daily_capture(repository_root=root, local_date=args.local_date))
        return 0
    if args.command == "run-daily":
        if not args.execute_network:
            _print(dry_run_daily_capture(repository_root=root))
            return 0
        _print(run_daily_capture(repository_root=root, execute_network=True))
        return 0
    if args.command == "initialize-acceptance":
        _print(supersede_legacy_soak_and_initialize_acceptance(
            repository_root=root,
            remediation_commit=args.remediation_commit,
            initialized_at=TrustedClock.production().now(),
        ))
        return 0
    if args.command == "plan-structural-recovery":
        _print(build_structural_recovery_plan(
            repository_root=root,
            remediation_commit=args.remediation_commit,
        ))
        return 0
    if args.command == "recover-structural-failure":
        _print(execute_structural_recovery(
            repository_root=root,
            remediation_commit=args.remediation_commit,
            approved_recovery_plan_id=args.approved_recovery_plan_id,
            recorded_at=TrustedClock.production().now(),
        ))
        return 0
    if args.command == "acceptance-status":
        _print(acceptance_status(repository_root=root))
        return 0
    if args.command == "status":
        _print(automation_status(repository_root=root))
        return 0
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
