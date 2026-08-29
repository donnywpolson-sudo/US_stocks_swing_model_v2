from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import canonical_json_bytes, require_contained_path, sha256_bytes
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


POLICY_PATH = Path("config/alpaca_archive_rehabilitation_policy.json")
RETIRED_MODE = "ALPACA_LEGACY_ARCHIVE_REHABILITATION_RETIRED_ACCEPTED_RELEASE_ONLY"


def _strict_mapping(
    value: object, expected_keys: set[str], field: str
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected_keys:
        raise ContractError(f"{field} schema differs")
    return value


def load_alpaca_archive_rehabilitation_policy(
    repository_root: Path,
) -> tuple[dict[str, Any], str]:
    root = Path(repository_root).resolve(strict=True)
    path = root / POLICY_PATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("Alpaca archive rehabilitation policy is unreadable") from exc
    required = {
        "schema_version",
        "policy_version",
        "project",
        "mode",
        "accepted_release",
        "input_contract",
        "evidence_boundary",
        "prospective_release",
        "legacy_universe_boundary",
        "authorities",
        "stop_conditions",
    }
    policy = _strict_mapping(payload, required, "rehabilitation policy")
    if (
        policy["schema_version"] != 2
        or policy["policy_version"] != "2.0.0"
        or policy["project"] != "US_stocks_swing_model_v2"
        or policy["mode"] != RETIRED_MODE
    ):
        raise ContractError("rehabilitation policy identity differs")
    authorities = policy["authorities"]
    if (
        type(authorities) is not dict
        or not authorities
        or any(value is not False for value in authorities.values())
    ):
        raise ContractError("rehabilitation policy grants authority")
    boundary = policy["legacy_universe_boundary"]
    if (
        type(boundary) is not dict
        or boundary != {
            "selection_state": "legacy_universe_selection_unresolved",
            "trusted_membership_claim": False,
            "active_source_eligible": False,
            "training_or_evaluation_eligible": False,
        }
    ):
        raise ContractError("rehabilitation policy weakens the legacy-universe boundary")
    accepted = policy["accepted_release"]
    if (
        type(accepted) is not dict
        or set(accepted)
        != {
            "accepted_root",
            "relative_directory",
            "release_id",
            "required_file_count",
            "required_payload_bytes",
        }
        or accepted["accepted_root"] != "data/vault/accepted"
        or accepted["relative_directory"]
        != (
            "alpaca_legacy_daily_bars/"
            "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1"
        )
        or accepted["release_id"]
        != "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1"
        or accepted["required_file_count"] != 201
        or accepted["required_payload_bytes"] != 99_868_172
    ):
        raise ContractError("rehabilitation accepted-release binding differs")
    return policy, sha256_bytes(canonical_json_bytes(policy))


def verify_rehabilitated_alpaca_release(
    repository_root: Path,
) -> dict[str, Any]:
    root = Path(repository_root).resolve(strict=True)
    policy, policy_id = load_alpaca_archive_rehabilitation_policy(root)
    binding = policy["accepted_release"]
    accepted_root = require_contained_path(
        root / binding["accepted_root"], root, must_exist=True
    )
    release_dir = require_contained_path(
        accepted_root / binding["relative_directory"],
        accepted_root,
        must_exist=True,
    )
    manifest = verify_accepted_release(
        release_dir,
        accepted_root=accepted_root,
    )
    payload_bytes = sum(entry.size for entry in manifest.files)
    required_paths = {"bars.parquet", "rehabilitation_receipt.json", "source_evidence_manifest.json"}
    manifest_paths = {entry.path for entry in manifest.files}
    prospective = policy["prospective_release"]
    if (
        manifest.release_id != binding["release_id"]
        or manifest.dataset != prospective["dataset"]
        or manifest.source_epoch != prospective["source_epoch"]
        or manifest.role != prospective["role"]
        or manifest.quality_state != prospective["quality_state"]
        or manifest.row_count != policy["input_contract"]["expected_row_count"]
        or len(manifest.files) != binding["required_file_count"]
        or payload_bytes != binding["required_payload_bytes"]
        or not required_paths <= manifest_paths
    ):
        raise IntegrityError("rehabilitated accepted release differs from policy")
    unsigned = {
        "schema_version": 2,
        "project": "US_stocks_swing_model_v2",
        "mode": RETIRED_MODE,
        "policy_id": policy_id,
        "accepted_release": {
            "directory": release_dir.relative_to(root).as_posix(),
            "release_id": manifest.release_id,
            "dataset": manifest.dataset,
            "role": manifest.role,
            "quality_state": manifest.quality_state,
            "row_count": manifest.row_count,
            "file_count": len(manifest.files),
            "payload_bytes": payload_bytes,
        },
        "legacy_universe_boundary": policy["legacy_universe_boundary"],
        "evidence_boundary": policy["evidence_boundary"],
        "prospective_release": prospective,
        "authorities": policy["authorities"],
        "stop_conditions": policy["stop_conditions"],
    }
    return {
        **unsigned,
        "verification_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }
