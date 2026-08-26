from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..clock import TrustedClock
from ..migration import (
    ControlledRebuildAuthorization,
    execute_copy_plan,
    load_migration_approval,
    load_migration_config,
    plan_migration,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Plan or execute exact hash-verified migration copies")
    value.add_argument("--config", type=Path, required=True)
    value.add_argument(
        "--detailed-plan",
        action="store_true",
        help="include every reviewed source/destination/hash entry in dry-run output",
    )
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
    args = parser().parse_args(argv)
    if args.controlled_rebuild_authorization is not None:
        raise PermissionError(
            "controlled rebuild authorization is retired historical evidence only"
        )
    config = load_migration_config(args.config)
    plan = plan_migration(config)
    summary: dict[str, object] = {
        "mode": "execute" if args.execute else "dry_run",
        **plan.concise_summary(),
    }
    if args.detailed_plan:
        summary["entries"] = [entry.as_dict() for entry in plan]
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.execute:
        if args.approval is None:
            raise PermissionError("--execute requires: --approval")
        approval = load_migration_approval(args.approval, plan)
        if args.controlled_rebuild_authorization is not None:
            controlled = ControlledRebuildAuthorization.load(
                args.controlled_rebuild_authorization
            )
            execute_copy_plan(
                plan,
                approval=approval,
                controlled_rebuild_authorization=controlled,
                clock=TrustedClock.production(),
                execute=True,
            )
        else:
            raise PermissionError(
                "--execute requires: --controlled-rebuild-authorization"
            )
    elif any(
        value is not None
        for value in (
            args.approval,
            args.controlled_rebuild_authorization,
        )
    ):
        raise PermissionError("authorization inputs are accepted only with --execute")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
