from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..providers.alpaca_historical_backfill_publication import (
    build_historical_backfill_publication_plan,
    publication_plan_summary,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Revalidate the complete Alpaca historical-backfill corpus and emit "
            "one no-network, no-write immutable-release publication plan with an "
            "exact deterministic release identity. Publication requires separate "
            "approval and explicit execution."
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    value.add_argument(
        "--created-at",
        required=True,
        help="exact canonical UTC Z timestamp bound into the prospective plan",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_historical_backfill_publication_plan(
        repository_root=args.repo_root,
        created_at=args.created_at,
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "PLAN_ONLY_NO_NETWORK_NO_WRITES",
                "publication_plan": publication_plan_summary(plan),
                "publication_authorized": False,
                "release_builder_implemented": True,
                "publication_implemented": True,
                "network_calls": 0,
                "credential_access": False,
                "source_activation": False,
                "training_or_research_authorized": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
