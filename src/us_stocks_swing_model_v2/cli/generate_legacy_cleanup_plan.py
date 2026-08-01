from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..legacy_cleanup import write_cleanup_plan


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate one local no-deletion cleanup plan")
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("--execute is required to write the local cleanup plan package")
    result = write_cleanup_plan(
        _repo_root(), expected_commit=args.expected_commit,
        created_at=TrustedClock.production().now().isoformat().replace("+00:00", "Z"),
    )
    print(json.dumps({"cleanup_plan_id": result["plan"]["cleanup_plan_id"], "directory": result["directory"], "execution_authorized": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
