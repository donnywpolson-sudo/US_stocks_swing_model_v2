from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from ..alpaca_corporate_action_preflight import build_corporate_action_preflight


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan-only caveated Alpaca corporate-actions preflight; never calls a provider or writes.")
    parser.add_argument("--release-directory", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--symbols", required=True, help="comma-separated uppercase symbols")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args(argv)
    symbols = tuple(args.symbols.split(","))
    print(json.dumps(build_corporate_action_preflight(release_directory=args.release_directory, accepted_root=_repo_root() / "data" / "vault" / "accepted", start=args.start, end=args.end, symbols=symbols, max_pages=args.max_pages, created_at=args.created_at, repo_root=_repo_root()), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
