"""Print a zero-write prospective corporate-action capture plan."""
from __future__ import annotations
import argparse
import json
from datetime import date
from pathlib import Path
from ..prospective_corporate_actions import build_prospective_corporate_action_capture_plan

def _root() -> Path:
    return Path(__file__).resolve().parents[3]

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan prospective corporate-action/delisting capture; never executes it")
    parser.add_argument("--identity-release", required=True, type=Path)
    parser.add_argument("--bars-release", required=True, type=Path)
    parser.add_argument("--calendar-release", required=True, type=Path)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--effective-start", required=True, type=date.fromisoformat)
    parser.add_argument("--effective-end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    root = _root()
    plan = build_prospective_corporate_action_capture_plan(repository_root=root, accepted_root=root / "data/vault/accepted", identity_release_directory=args.identity_release, bars_release_directory=args.bars_release, calendar_release_directory=args.calendar_release, symbols=tuple(args.symbols.split(",")), effective_start_session=args.effective_start, effective_end_session=args.effective_end)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0
