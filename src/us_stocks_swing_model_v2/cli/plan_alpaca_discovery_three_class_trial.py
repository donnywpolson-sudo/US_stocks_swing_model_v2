from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..alpaca_discovery_proxy_feature_wfa import build_feature_wfa_plan
from ..alpaca_discovery_three_class_trial import build_three_class_trial_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan-only fixed three-class Alpaca discovery trial.")
    parser.add_argument("--feature-release-directory", type=Path, required=True)
    parser.add_argument("--proxy-outcome-release-directory", type=Path, required=True)
    parser.add_argument("--accepted-root", type=Path, default=_repo_root() / "data" / "vault" / "accepted")
    args = parser.parse_args(argv)
    wfa_plan = build_feature_wfa_plan(args.feature_release_directory, proxy_outcome_release_directory=args.proxy_outcome_release_directory, accepted_root=args.accepted_root, repo_root=_repo_root())
    print(json.dumps(build_three_class_trial_plan(wfa_plan, repo_root=_repo_root()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
