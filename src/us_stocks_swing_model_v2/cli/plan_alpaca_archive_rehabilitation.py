from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..alpaca_archive_rehabilitation import (
    build_alpaca_archive_rehabilitation_plan,
    load_alpaca_archive_rehabilitation_policy,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Inspect the exact retired Alpaca SIP archive and emit one "
            "metadata-only rehabilitation plan; never writes or publishes"
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    policy, _policy_id = load_alpaca_archive_rehabilitation_policy(args.repo_root)
    plan = build_alpaca_archive_rehabilitation_plan(
        Path(policy["legacy_archive_root"]),
        repository_root=args.repo_root,
    )
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
