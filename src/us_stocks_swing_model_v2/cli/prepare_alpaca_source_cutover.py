from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..providers.alpaca_qualification_readiness import (
    build_alpaca_feed_cutover_design,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Prepare one no-write Alpaca qualification-receipt and source-cutover "
            "design; never publishes a receipt or activates a source"
        )
    )
    value.add_argument("--repo-root", type=Path)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    design = build_alpaca_feed_cutover_design(args.repo_root)
    print(json.dumps(design, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
