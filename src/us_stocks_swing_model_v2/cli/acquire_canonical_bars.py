from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.alpaca_canonical_bars import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    build_canonical_bars_acquisition_plan,
    build_canonical_bars_candidate,
    build_canonical_bars_publication_plan,
    execute_canonical_bars_acquisition,
    publish_canonical_bars,
)
from ..providers.snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan, execute, verify, or separately publish the first bounded "
            "active-SIP canonical-bars capture"
        )
    )
    modes = value.add_mutually_exclusive_group()
    modes.add_argument("--execute-network", action="store_true")
    modes.add_argument("--verify-snapshot", type=Path)
    modes.add_argument("--execute-publication", type=Path)
    value.add_argument(
        "--approved-plan-id",
        help="exact acquisition plan ID required for network execution",
    )
    value.add_argument(
        "--approved-publication-plan-id",
        help="exact publication plan ID required for publication execution",
    )
    return value


def _load_candidate(
    snapshot_directory: Path,
    plan: dict[str, object],
) -> object:
    root = _repo_root()
    policy_path = root / "config" / "alpaca_canonical_bars_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    registry = NetworkAcquisitionRegistry.load(
        root / policy["network_registry"],
        allowed_root=root,
    )
    store = AsReceivedSnapshotStore(
        root / policy["outputs"]["snapshot_store"],
        allowed_root=root / "data",
        acquisition_registry=registry,
    )
    snapshot = store.load(snapshot_directory)
    return build_canonical_bars_candidate(snapshot, acquisition_plan=plan)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = _repo_root()
    clock = TrustedClock.production()
    plan = build_canonical_bars_acquisition_plan(repo_root=root, clock=clock)
    if not args.execute_network and args.verify_snapshot is None and args.execute_publication is None:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "PLAN_ONLY_NO_NETWORK_NO_WRITES",
                    "acquisition_plan": plan,
                    "network_calls": 0,
                    "credential_access": False,
                    "snapshot_writes": 0,
                    "canonical_release_publication": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    policy = json.loads(
        (root / "config" / "alpaca_canonical_bars_policy.json").read_text(
            encoding="utf-8"
        )
    )
    accepted_root = root / policy["outputs"]["accepted_root"]
    work_root = root / policy["outputs"]["work_root"]
    if args.execute_network:
        if not args.approved_plan_id:
            parser().error("--execute-network requires --approved-plan-id")
        if args.approved_plan_id != plan["acquisition_plan_id"]:
            raise PermissionError("approved canonical bars acquisition plan differs")
        if os.environ.get("FREE_SOURCE_QUALIFICATION_APPROVED") != "YES":
            raise PermissionError(
                "--execute-network requires FREE_SOURCE_QUALIFICATION_APPROVED=YES"
            )
        api_key_id = os.environ.get("APCA_API_KEY_ID", "")
        api_secret_key = os.environ.get("APCA_API_SECRET_KEY", "")
        if not api_key_id or not api_secret_key:
            raise PermissionError("Alpaca credentials are absent from the process environment")
        snapshot, candidate, publication_plan = execute_canonical_bars_acquisition(
            acquisition_plan=plan,
            approved_acquisition_plan_id=args.approved_plan_id,
            api_key_id=api_key_id,
            api_secret_key=api_secret_key,
            clock=clock,
            repo_root=root,
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "CAPTURED_AND_VERIFIED_NOT_PUBLISHED",
                    "acquisition_plan_id": candidate.acquisition_plan_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "snapshot_directory": str(snapshot.root),
                    "raw_sha256": snapshot.raw_sha256,
                    "requested_at": snapshot.requested_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "retrieved_at": snapshot.retrieved_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "candidate_id": candidate.candidate_id,
                    "row_count": candidate.row_count,
                    "symbols": list(candidate.symbols),
                    "sessions": [item.isoformat() for item in candidate.sessions],
                    "publication_plan": publication_plan,
                    "canonical_release_published": False,
                    "network_calls": 1,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    snapshot_directory = args.verify_snapshot or args.execute_publication
    candidate = _load_candidate(snapshot_directory, plan)
    publication_plan = build_canonical_bars_publication_plan(
        candidate,
        acquisition_plan=plan,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if args.verify_snapshot is not None:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "OFFLINE_VERIFIED_NO_WRITES",
                    "snapshot_id": candidate.snapshot_id,
                    "candidate_id": candidate.candidate_id,
                    "row_count": candidate.row_count,
                    "publication_plan": publication_plan,
                    "network_calls": 0,
                    "credential_access": False,
                    "canonical_release_published": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.approved_publication_plan_id:
        parser().error(
            "--execute-publication requires --approved-publication-plan-id"
        )
    if (
        os.environ.get(PUBLICATION_CONFIRMATION_TOKEN)
        != PUBLICATION_CONFIRMATION_VALUE
    ):
        raise PermissionError(
            f"--execute-publication requires {PUBLICATION_CONFIRMATION_TOKEN}=YES"
        )
    result = publish_canonical_bars(
        candidate,
        acquisition_plan=plan,
        approved_publication_plan_id=args.approved_publication_plan_id,
        accepted_root=accepted_root,
        work_root=work_root,
        owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "PUBLISHED_CANONICAL_BARS_NOT_RESEARCH_AUTHORITY",
                "publication_plan_id": result.publication_plan_id,
                "release_id": result.release_id,
                "receipt_id": result.receipt_id,
                "release_directory": str(result.release_directory),
                "work_directory": str(result.work_directory),
                "network_calls": 0,
                "credential_access": False,
                "source_activation": False,
                "research": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
