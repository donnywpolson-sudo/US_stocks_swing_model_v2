"""Plan-only preflight for the selected external trial-registry backend."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

from ..s3_object_lock_trial_registry import S3ObjectLockRegistryPolicy


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan-only S3 Object Lock trial-registry preflight; never reads credentials or S3."
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=_repo_root() / "config" / "trial_registry_s3_object_lock_policy.json",
    )
    args = parser.parse_args(argv)
    policy = S3ObjectLockRegistryPolicy.load(args.policy, repository_root=_repo_root())
    print(
        json.dumps(
            {
                "mode": "S3_OBJECT_LOCK_TRIAL_REGISTRY_PLAN_ONLY",
                "policy_id": policy.policy_id,
                "backend": "AWS_S3_OBJECT_LOCK_COMPLIANCE",
                "policy_status": policy.status,
                "network_requests": 0,
                "credentials_read": 0,
                "sdk_available": importlib.util.find_spec("boto3") is not None,
                "writes": 0,
                "production_registration_ready": policy.status == "CONFIGURED",
                "future_external_action": {
                    "operation": "one S3 PutObject followed by one GetObject",
                    "retention_mode": "COMPLIANCE",
                    "retry": False,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
