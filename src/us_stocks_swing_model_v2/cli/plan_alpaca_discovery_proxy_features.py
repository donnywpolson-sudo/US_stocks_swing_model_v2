from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..alpaca_discovery_proxy_feature_wfa import build_feature_release_plan, publish_feature_release


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or run the caveated Alpaca feature-only build.")
    parser.add_argument("--source-release-directory", type=Path, required=True)
    parser.add_argument("--calendar-release-directory", type=Path, required=True)
    parser.add_argument("--accepted-root", type=Path, default=_repo_root() / "data" / "vault" / "accepted")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approved-feature-build-plan-id")
    parser.add_argument("--work-root", type=Path, default=_repo_root() / "data" / "w" / "alpaca_discovery_proxy_features")
    parser.add_argument("--created-at")
    args = parser.parse_args(argv)
    if args.execute:
        if not args.approved_feature_build_plan_id or not args.created_at:
            parser.error("--execute requires --approved-feature-build-plan-id and --created-at")
        published = publish_feature_release(args.source_release_directory, calendar_release_directory=args.calendar_release_directory, accepted_root=args.accepted_root, work_root=args.work_root, created_at=args.created_at, approved_feature_build_plan_id=args.approved_feature_build_plan_id, repo_root=_repo_root())
        print(json.dumps({"published_release_directory": str(published)}, sort_keys=True))
        return 0
    print(json.dumps(build_feature_release_plan(args.source_release_directory, calendar_release_directory=args.calendar_release_directory, accepted_root=args.accepted_root, repo_root=_repo_root()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
