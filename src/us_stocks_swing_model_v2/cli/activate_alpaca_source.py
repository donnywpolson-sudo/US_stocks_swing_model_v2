from __future__ import annotations

import argparse
import json
import os

from ..providers.alpaca_source_cutover import (
    ACTIVATION_CONFIRMATION_TOKEN,
    ACTIVATION_CONFIRMATION_VALUE,
    activate_alpaca_source,
    build_alpaca_source_cutover_plan,
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description=(
            "Plan or apply the exact verified Alpaca SIP source cutover; "
            "never calls a provider or builds canonical bars"
        )
    )
    value.add_argument(
        "--execute",
        action="store_true",
        help=(
            "activate exactly one source config; also requires the exact "
            "approved plan ID and owner confirmation environment token"
        ),
    )
    value.add_argument(
        "--approved-plan-id",
        help="exact activation plan ID from a separately reviewed no-write plan",
    )
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = build_alpaca_source_cutover_plan()
    if not args.execute:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "PLAN_ONLY_NO_WRITES",
                    "activation_plan": plan,
                    "activation_authorized": False,
                    "config_file_mutations": 0,
                    "canonical_bars": False,
                    "network_calls": 0,
                    "credential_access": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    if not args.approved_plan_id:
        parser().error("--execute requires --approved-plan-id")
    if os.environ.get(ACTIVATION_CONFIRMATION_TOKEN) != ACTIVATION_CONFIRMATION_VALUE:
        raise PermissionError(
            f"--execute also requires {ACTIVATION_CONFIRMATION_TOKEN}="
            f"{ACTIVATION_CONFIRMATION_VALUE}"
        )
    if args.approved_plan_id != plan["activation_plan_id"]:
        raise PermissionError("approved Alpaca activation plan ID differs")
    result = activate_alpaca_source(
        approved_plan_id=args.approved_plan_id,
        owner_confirmation=os.environ[ACTIVATION_CONFIRMATION_TOKEN],
    )
    print(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "ACTIVATED_VERIFIED_ALPACA_SIP_SOURCE",
                "activation_plan_id": result.activation_plan_id,
                "release_id": result.release_id,
                "receipt_id": result.receipt_id,
                "source_config_path": str(result.source_config_path),
                "source_config_sha256": result.source_config_sha256,
                "source_active": True,
                "qualified_feed": "sip",
                "canonical_bars": False,
                "network_calls": 0,
                "credential_access": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
