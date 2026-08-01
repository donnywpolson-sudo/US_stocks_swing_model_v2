"""Create a no-write plan for a prospective evidence epoch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..prospective_evidence import build_prospective_epoch_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan one prospective evidence epoch; never captures, publishes, or trains."
    )
    parser.add_argument("--identity-release", required=True, type=Path)
    parser.add_argument("--bars-release", required=True, type=Path)
    parser.add_argument("--actions-release", required=True, type=Path)
    parser.add_argument("--calendar-release", required=True, type=Path)
    parser.add_argument("--accepted-root", type=Path, default=_repo_root() / "data" / "vault" / "accepted")
    args = parser.parse_args(argv)
    plan = build_prospective_epoch_plan(
        identity_release_directory=args.identity_release,
        bars_release_directory=args.bars_release,
        actions_release_directory=args.actions_release,
        calendar_release_directory=args.calendar_release,
        accepted_root=args.accepted_root,
        repository_root=_repo_root(),
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
