from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..mechanical_readiness import (
    assess_stock_mechanical_readiness,
    publish_stock_mechanical_readiness,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Assess or publish non-authorizing stock-v2 mechanical readiness; "
            "never calls providers or runs historical research, models, labels, "
            "WFA, or candidates"
        )
    )
    value.add_argument("--foundation-release", type=Path, required=True)
    value.add_argument(
        "--accepted-root",
        type=Path,
        default=_repo_root() / "data" / "vault" / "accepted",
    )
    value.add_argument(
        "--work-root",
        type=Path,
        default=_repo_root() / "data" / "w" / "readiness",
    )
    value.add_argument("--created-at", help="canonical UTC Z timestamp; required with --execute")
    value.add_argument(
        "--execute",
        action="store_true",
        help="publish only the two non-authorizing mechanical milestone receipts",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.execute:
        required = {"--created-at": args.created_at}
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser().error(f"--execute requires: {', '.join(missing)}")
        result = publish_stock_mechanical_readiness(
            foundation_release_directory=args.foundation_release,
            accepted_release_root=args.accepted_root,
            readiness_work_root=args.work_root,
            created_at=args.created_at,
            clock=TrustedClock.production(),
        )
        payload: dict[str, object] = {
            "schema_version": 1,
            "mode": "PUBLISH_NON_AUTHORIZING_MECHANICAL_MILESTONES",
            "assessment_id": result.assessment_id,
            "local_action_record": (
                None
                if result.local_action_record is None
                else result.local_action_record.as_dict()
            ),
            "rebuild_complete_release_directory": str(
                result.rebuild_complete_release_directory
            ),
            "historical_research_ready_release_directory": str(
                result.historical_research_ready_release_directory
            ),
            "real_history_authorized": False,
            "candidate_eligible": False,
            "alpha_claim": False,
        }
    else:
        assessment = assess_stock_mechanical_readiness(
            foundation_release_directory=args.foundation_release,
            accepted_release_root=args.accepted_root,
        )
        payload = {
            "schema_version": 1,
            "mode": "ASSESS_ONLY_NO_WRITES",
            **assessment.as_dict(),
        }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
