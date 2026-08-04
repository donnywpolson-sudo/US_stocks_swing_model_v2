"""Plan, capture, or publish the fixed prospective SIP smoke lane."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..prospective_sip_smoke import (
    PUBLICATION_WORK_ROOT,
    build_prospective_sip_smoke_candidate,
    build_prospective_sip_smoke_plan,
    build_prospective_sip_smoke_publication_plan,
    execute_prospective_sip_smoke_capture,
    load_prospective_sip_smoke_capture,
    load_prospective_sip_smoke_plan_package,
    publish_prospective_sip_smoke,
    write_prospective_sip_smoke_plan_package,
)


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fixed 2026-08-03 AAPL/SPY prospective SIP smoke lane")
    parser.add_argument("--identity-release", type=Path)
    parser.add_argument("--calendar-release", type=Path)
    parser.add_argument("--write-plan-package", action="store_true")
    parser.add_argument("--plan-package", type=Path)
    parser.add_argument("--snapshot-directory", type=Path)
    parser.add_argument("--execute-network", action="store_true")
    parser.add_argument("--approved-plan-id")
    parser.add_argument("--plan-publication", action="store_true")
    parser.add_argument("--execute-publication", action="store_true")
    parser.add_argument("--approved-publication-plan-id")
    parser.add_argument("--owner-confirmation")
    args = parser.parse_args(argv)
    root = _root()
    publication_mode = args.plan_publication or args.execute_publication
    if publication_mode:
        if args.execute_network or args.identity_release or args.calendar_release or args.write_plan_package:
            parser.error("publication modes use only acquisition evidence")
        if not args.plan_package or not args.snapshot_directory:
            parser.error("publication modes require --plan-package and --snapshot-directory")
        plan, candidate, receipt_hash = load_prospective_sip_smoke_capture(
            plan_package=args.plan_package, snapshot_directory=args.snapshot_directory, repository_root=root
        )
        publication = build_prospective_sip_smoke_publication_plan(
            candidate,
            plan=plan,
            acquisition_receipt_sha256=receipt_hash,
            accepted_root=root / "data/vault/accepted",
            work_root=root / PUBLICATION_WORK_ROOT,
            repository_root=root,
        )
        if args.plan_publication:
            if args.execute_publication or args.approved_publication_plan_id or args.owner_confirmation:
                parser.error("--plan-publication cannot authorize execution")
            print(json.dumps(publication, indent=2, sort_keys=True))
            return 0
        if not args.approved_publication_plan_id or args.owner_confirmation is None:
            parser.error("--execute-publication requires --approved-publication-plan-id and --owner-confirmation")
        destination = publish_prospective_sip_smoke(
            candidate=candidate,
            plan=plan,
            acquisition_receipt_sha256=receipt_hash,
            approved_publication_plan_id=args.approved_publication_plan_id,
            owner_confirmation=args.owner_confirmation,
            accepted_root=root / "data/vault/accepted",
            work_root=root / PUBLICATION_WORK_ROOT,
            repository_root=root,
        )
        print(json.dumps({"publication_plan_id": publication["publication_plan_id"], "release_directory": str(destination), "source_activation": False}, indent=2, sort_keys=True))
        return 0
    if args.execute_network:
        if args.identity_release or args.calendar_release or args.write_plan_package or args.snapshot_directory:
            parser.error("--execute-network uses only --plan-package")
        if not args.plan_package or not args.approved_plan_id:
            parser.error("--execute-network requires --plan-package and --approved-plan-id")
        snapshot = execute_prospective_sip_smoke_capture(
            plan_package=args.plan_package,
            approved_plan_id=args.approved_plan_id,
            api_key_id=os.environ.get("APCA_API_KEY_ID", ""),
            api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
            clock=TrustedClock.production(),
            repository_root=root,
        )
        plan = load_prospective_sip_smoke_plan_package(plan_package=args.plan_package, repository_root=root)
        candidate = build_prospective_sip_smoke_candidate(snapshot, plan=plan)
        print(json.dumps({"plan_id": plan["prospective_sip_smoke_plan_id"], "snapshot_id": snapshot.snapshot_id, "candidate_id": candidate.candidate_id, "published": False, "source_activation": False}, indent=2, sort_keys=True))
        return 0
    if args.plan_package or args.snapshot_directory or args.approved_plan_id or args.approved_publication_plan_id or args.owner_confirmation:
        parser.error("acquisition evidence and approval arguments require an execution mode")
    if not args.identity_release or not args.calendar_release:
        parser.error("plan generation requires --identity-release and --calendar-release")
    plan = build_prospective_sip_smoke_plan(
        identity_release_directory=args.identity_release,
        calendar_release_directory=args.calendar_release,
        repository_root=root,
        clock=TrustedClock.production(),
    )
    if args.write_plan_package:
        result = write_prospective_sip_smoke_plan_package(plan=plan, repository_root=root)
        print(json.dumps({"plan_id": plan["prospective_sip_smoke_plan_id"], "plan_package": result["directory"], "execution_authorized": False, "network_calls": 0}, indent=2, sort_keys=True))
        return 0
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
