from __future__ import annotations

import argparse
import json
from pathlib import Path

from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.external_strategy_census import HistoricalTrialCensusAssessment
from us_stocks_swing_model_v2.external_strategy_intake import (
    ExternalStrategySpec,
    build_external_strategy_intake_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-v2-external-strategy-intake")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--census-assessment", type=Path)
    parser.add_argument("--accepted-root", type=Path)
    parser.add_argument("--accepted-release", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        spec = ExternalStrategySpec.from_bytes(args.spec.read_bytes())
        census = None if args.census_assessment is None else HistoricalTrialCensusAssessment.from_bytes(args.census_assessment.read_bytes())
        if bool(args.accepted_release) != (args.accepted_root is not None):
            raise ContractError("accepted root and release directories must be supplied together")
        plan = build_external_strategy_intake_plan(
            spec,
            repository_root=args.repo_root,
            census_assessment=census,
            accepted_release_directories=args.accepted_release,
            accepted_release_root=args.accepted_root,
        )
        print(json.dumps(plan, indent=2))
        return 0
    except (OSError, ContractError) as exc:
        print(json.dumps({"status": "INVALID_SPEC", "reason": str(exc)}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
