from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..providers.alpaca_historical_backfill_publication import (
    PUBLICATION_CONFIRMATION_TOKEN,
    PUBLICATION_CONFIRMATION_VALUE,
    publish_historical_backfill_release,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Publish one separately approved, immutable Alpaca historical-backfill "
            "legacy-discovery release. It never calls a provider or activates a source."
        )
    )
    value.add_argument("--created-at", required=True)
    value.add_argument("--approved-plan-id", required=True)
    value.add_argument("--execute", action="store_true")
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not args.execute:
        parser().error("publication requires --execute")
    if os.environ.get(PUBLICATION_CONFIRMATION_TOKEN) != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError(
            f"--execute requires {PUBLICATION_CONFIRMATION_TOKEN}="
            f"{PUBLICATION_CONFIRMATION_VALUE}"
        )
    result = publish_historical_backfill_release(
        approved_plan_id=args.approved_plan_id,
        created_at=args.created_at,
        repository_root=_repo_root(),
        owner_confirmation=os.environ[PUBLICATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "PUBLISHED_LEGACY_DISCOVERY_ONLY",
                "publication_plan_id": result.publication_plan_id,
                "release_id": result.release_id,
                "release_directory": str(result.release_directory),
                "work_directory": str(result.work_directory),
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
