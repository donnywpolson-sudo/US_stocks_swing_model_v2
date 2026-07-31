from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..alpaca_legacy_discovery_downstream import build_downstream_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan-only Alpaca legacy-discovery downstream boundaries; never opens bar rows or writes.")
    parser.add_argument("--release-directory", type=Path, required=True)
    parser.add_argument("--accepted-root", type=Path, default=_repo_root() / "data" / "vault" / "accepted")
    args = parser.parse_args(argv)
    print(json.dumps(build_downstream_plan(args.release_directory, accepted_root=args.accepted_root, repo_root=_repo_root()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
