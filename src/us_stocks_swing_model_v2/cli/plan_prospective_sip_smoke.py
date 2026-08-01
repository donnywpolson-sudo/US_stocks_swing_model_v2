from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..prospective_sip_smoke import build_prospective_sip_smoke_plan


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan one prospective AAPL/SPY SIP smoke capture; never captures or publishes")
    parser.add_argument("--identity-release", required=True, type=Path)
    parser.add_argument("--calendar-release", required=True, type=Path)
    args = parser.parse_args(argv)
    plan = build_prospective_sip_smoke_plan(identity_release_directory=args.identity_release, calendar_release_directory=args.calendar_release, repository_root=_root(), clock=TrustedClock.production())
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
