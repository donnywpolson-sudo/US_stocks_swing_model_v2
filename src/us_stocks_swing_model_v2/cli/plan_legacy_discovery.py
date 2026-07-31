from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..legacy_discovery_bridge import build_legacy_discovery_bridge_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Prepare one metadata-only, no-write proxy legacy-discovery bridge "
            "plan; never reads historical rows, computes outcomes, or trains"
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--foundation-set-directory",
        type=Path,
        required=True,
        help="exact accepted stock_historical_foundation_set release directory",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_legacy_discovery_bridge_plan(
        args.foundation_set_directory,
        accepted_root=args.accepted_root,
        repo_root=args.repo_root,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
