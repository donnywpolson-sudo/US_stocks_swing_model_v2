from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..clock import TrustedClock
from ..providers.identity_readiness import (
    assess_identity_inputs,
    build_alpaca_assets_request_plan,
    guarded_capture_alpaca_assets,
)
from ..providers.snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan one bounded Alpaca asset capture or assess already-landed "
            "Alpaca/Nasdaq identity inputs; activation is never performed"
        )
    )
    mode = value.add_mutually_exclusive_group()
    mode.add_argument(
        "--execute-network",
        action="store_true",
        help="capture one Alpaca asset response under a separately approved plan",
    )
    mode.add_argument(
        "--assess-pair",
        nargs=2,
        type=Path,
        metavar=("ALPACA_ASSET_SNAPSHOT", "NASDAQ_SNAPSHOT"),
        help="offline assessment of two already-landed source snapshots",
    )
    value.add_argument(
        "--approved-plan-id",
        help="exact Alpaca asset request plan ID required by --execute-network",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = _repo_root()
    if args.assess_pair is not None:
        assessment = assess_identity_inputs(
            alpaca_snapshot_directory=args.assess_pair[0],
            nasdaq_snapshot_directory=args.assess_pair[1],
            repo_root=root,
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "OFFLINE_IDENTITY_INPUT_ASSESSMENT_NO_WRITES",
                    "assessment": assessment.summary(),
                    "network_calls": 0,
                    "identity_release_publication": False,
                    "source_activation": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    plan = build_alpaca_assets_request_plan(root)
    if not args.execute_network:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "PLAN_ONLY_NO_WRITES",
                    "request_plan": plan.as_dict(),
                    "network_authorized": False,
                    "identity_release_publication": False,
                    "source_activation": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.approved_plan_id:
        parser().error("--execute-network requires --approved-plan-id")
    source_config = json.loads(
        (root / "config" / "sources.json").read_text(encoding="utf-8")
    )
    expected_store = root / "data" / "vault" / "qualification" / "as_received"
    if (
        source_config.get("project") != "US_stocks_swing_model_v2"
        or Path(str(source_config.get("snapshot_store_root"))) != expected_store
    ):
        raise ValueError("identity snapshot store differs from source configuration")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    store = AsReceivedSnapshotStore(
        expected_store,
        allowed_root=root,
        acquisition_registry=registry,
    )
    snapshot = guarded_capture_alpaca_assets(
        approved_plan_id=args.approved_plan_id,
        snapshot_store=store,
        api_key_id=os.environ.get("APCA_API_KEY_ID", ""),
        api_secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
        clock=TrustedClock.production(),
        repo_root=root,
        network_enabled=True,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "CAPTURED_ONE_ALPACA_ASSET_SNAPSHOT_NOT_ACTIVE",
                "request_plan_id": plan.plan_id,
                "snapshot_id": snapshot.snapshot_id,
                "snapshot_directory": str(snapshot.root),
                "raw_sha256": snapshot.raw_sha256,
                "retrieved_at": snapshot.retrieved_at.isoformat().replace(
                    "+00:00", "Z"
                ),
                "identity_release_publication": False,
                "source_activation": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
