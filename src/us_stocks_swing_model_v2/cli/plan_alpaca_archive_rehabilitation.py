from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..alpaca_archive_rehabilitation import (
    verify_rehabilitated_alpaca_release,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Verify the immutable V2 accepted release that replaced the retired "
            "external Alpaca archive; never writes or publishes"
        )
    )
    value.add_argument("--repo-root", type=Path, default=_repo_root())
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    verification = verify_rehabilitated_alpaca_release(args.repo_root)
    print(json.dumps(verification, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
