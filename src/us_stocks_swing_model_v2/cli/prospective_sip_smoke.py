"""Plan, capture, or verify the fixed prospective SIP smoke lane."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from ..clock import TrustedClock
from ..prospective_sip_smoke import build_prospective_sip_smoke_plan, execute_prospective_sip_smoke_capture, build_prospective_sip_smoke_candidate

def _root() -> Path: return Path(__file__).resolve().parents[3]
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed 2026-08-03 AAPL/SPY prospective SIP smoke lane")
    parser.add_argument("--identity-release", type=Path, required=True); parser.add_argument("--calendar-release", type=Path, required=True); parser.add_argument("--execute-network", action="store_true"); parser.add_argument("--approved-plan-id")
    args = parser.parse_args(argv); root = _root(); plan = build_prospective_sip_smoke_plan(identity_release_directory=args.identity_release, calendar_release_directory=args.calendar_release, repository_root=root, clock=TrustedClock.production())
    if not args.execute_network: print(json.dumps(plan, indent=2, sort_keys=True)); return 0
    if not args.approved_plan_id: parser.error("--execute-network requires --approved-plan-id")
    snapshot = execute_prospective_sip_smoke_capture(plan=plan, approved_plan_id=args.approved_plan_id, api_key_id=os.environ.get("APCA_API_KEY_ID", ""), api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""), clock=TrustedClock.production(), repository_root=root)
    candidate = build_prospective_sip_smoke_candidate(snapshot, plan=plan)
    print(json.dumps({"plan_id": plan["prospective_sip_smoke_plan_id"], "snapshot_id": snapshot.snapshot_id, "candidate_id": candidate.candidate_id, "published": False, "source_activation": False}, indent=2, sort_keys=True)); return 0
