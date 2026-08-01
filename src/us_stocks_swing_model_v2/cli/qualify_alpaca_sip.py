"""Plan or execute the one-request Alpaca SIP qualification lane."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.alpaca_sip_single_feed_qualification import build_qualification_plan, execute_qualification_capture, verify_qualification_snapshot


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan-only, bounded Alpaca SIP source qualification")
    modes = value.add_mutually_exclusive_group()
    modes.add_argument("--execute-network", action="store_true")
    modes.add_argument("--verify-snapshot", type=Path)
    value.add_argument("--approved-plan-id")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = _repo_root()
    clock = TrustedClock.production()
    plan = build_qualification_plan(repo_root=root, clock=clock)
    if not args.execute_network and args.verify_snapshot is None:
        print(json.dumps({"schema_version": 1, "mode": "PLAN_ONLY_NO_NETWORK_NO_WRITES", "qualification_plan": plan, "network_calls": 0, "snapshot_writes": 0, "qualification_receipt_publication": False, "source_activation": False}, indent=2, sort_keys=True))
        return 0
    if args.verify_snapshot is not None:
        assessment = verify_qualification_snapshot(snapshot_directory=args.verify_snapshot, plan=plan, repo_root=root)
        print(json.dumps({"schema_version": 1, "mode": "OFFLINE_VERIFIED_NO_WRITES", "assessment": assessment, "network_calls": 0, "qualification_receipt_publication": False, "source_activation": False}, indent=2, sort_keys=True))
        return 0
    if not args.approved_plan_id:
        parser().error("--execute-network requires --approved-plan-id")
    if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
        raise PermissionError("--execute-network requires FREE_SOURCE_QUALIFICATION_APPROVED=YES")
    key, secret = os.environ.get("APCA_API_KEY_ID", ""), os.environ.get("APCA_API_SECRET_KEY", "")
    if not key or not secret:
        raise PermissionError("Alpaca credentials are absent from the process environment")
    snapshot, assessment = execute_qualification_capture(plan=plan, approved_plan_id=args.approved_plan_id, api_key_id=key, api_secret_key=secret, clock=clock, repo_root=root)
    print(json.dumps({"schema_version": 1, "mode": "CAPTURED_AND_ASSESSED_NOT_PUBLISHED", "snapshot_id": snapshot.snapshot_id, "snapshot_directory": str(snapshot.root), "raw_sha256": snapshot.raw_sha256, "assessment": assessment, "network_calls": 1, "qualification_receipt_publication": False, "source_activation": False, "canonical_bars": False, "training_or_evaluation": False}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
