from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..providers.alpaca_historical_backfill import (
    build_historical_backfill_plan,
    plan_summary,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Emit a deterministic, no-network, no-write Alpaca SIP historical "
            "backfill plan for the current identity cohort missing from the "
            "accepted rehabilitated archive"
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_historical_backfill_plan(repo_root=args.repo_root)
    print(json.dumps(plan_summary(plan), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
