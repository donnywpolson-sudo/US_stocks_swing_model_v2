from __future__ import annotations

import argparse
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Retired hash-copy interface")
    value.add_argument("--config", type=Path, required=True)
    value.add_argument(
        "--controlled-rebuild-authorization",
        type=Path,
        help="retired historical controlled-rebuild input; always rejected",
    )
    value.add_argument("--execute", action="store_true", help="requires HASH_COPY_APPROVED=YES")
    value.add_argument(
        "--approval",
        type=Path,
        help="exact reviewed approval JSON; mandatory with --execute",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    parser().parse_args(argv)
    raise PermissionError("hash-copy migration is retired historical evidence only")


if __name__ == "__main__":
    raise SystemExit(main())
