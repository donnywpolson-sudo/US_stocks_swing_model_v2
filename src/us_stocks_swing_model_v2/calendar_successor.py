"""One-shot, production-safe publication of a pinned XNYS calendar successor."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from .clock import TrustedClock, require_trusted_clock
from .common import (
    canonical_json_bytes,
    iso_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .errors import ContractError, IntegrityError
from .exchange_calendar import (
    EXCHANGE_CALENDARS_VERSION,
    calendar_environment_hash,
    calendar_policy_hash,
    publish_xnys_calendar_release,
)


POLICY_PATH = "config/xnys_calendar_successor_policy.json"
CONFIRMATION_TOKEN = "XNYS_CALENDAR_SUCCESSOR_PUBLICATION_APPROVED"
CONFIRMATION_VALUE = "YES"
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/calendar_successor.py",
    "src/us_stocks_swing_model_v2/exchange_calendar.py",
    "src/us_stocks_swing_model_v2/cli/publish_xnys_calendar_successor.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
SPENT_FAILURE_STATE = "SPENT_PREWRITE_NONEMPTY_LEGACY_STAGING_ROOT"
PRESERVED_STAGING_FILES = ("provenance.json", "sessions.parquet")
RECOVERY_CONTRACT = {
    "schema_version": 1,
    "spent_plan_id": "3efad51aeea48654ddf7de1c8ea008f21b14443fb17e41a04195b411d8e396d1",
    "failure_state": SPENT_FAILURE_STATE,
    "preserved_staging_root": "data/w/xnys_calendar_successor",
    "preserved_files": [
        {
            "path": "provenance.json",
            "size": 481,
            "sha256": "c3f07fead5aa4e36d308082d2bbc0e1b8874532db99cf61468a01365fea68826",
        },
        {
            "path": "sessions.parquet",
            "size": 101990,
            "sha256": "9268af4703b5709409e3e119b34737542aaeaaee1311ede80d337d44227a2e69",
        },
    ],
    "new_staging_partition": "environment_sha256",
    "cleanup_allowed": False,
    "retry_authorized": False,
}


def _root(repository_root: Path | None) -> Path:
    return Path(repository_root or Path(__file__).resolve().parents[2]).resolve(strict=True)


def _clean_repository(root: Path) -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True,
                text=True, encoding="utf-8", timeout=30,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise IntegrityError("calendar successor requires a valid committed Git closure") from exc
    if Path(run("rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("calendar successor Git root differs")
    if run("status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("calendar successor requires a clean committed tree")
    return {"commit": run("rev-parse", "HEAD"), "tree": run("rev-parse", "HEAD^{tree}")}


def _load_policy(root: Path) -> dict[str, Any]:
    try:
        policy = json.loads((root / POLICY_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("calendar successor policy is unreadable") from exc
    expected = {
        "schema_version", "project", "mode", "calendar_policy", "calendar_name",
        "calendar_package", "calendar_version", "requested_start", "requested_end",
        "outputs", "recovery", "execution",
    }
    if (
        type(policy) is not dict or set(policy) != expected
        or policy["schema_version"] != 1 or policy["project"] != "US_stocks_swing_model_v2"
        or policy["mode"] != "XNYS_CALENDAR_SUCCESSOR_PLAN_ONLY"
        or policy["calendar_policy"] != "config/xnys_calendar_policy.json"
        or policy["calendar_name"] != "XNYS"
        or policy["calendar_package"] != "exchange-calendars"
        or policy["calendar_version"] != EXCHANGE_CALENDARS_VERSION
        or policy["requested_start"] != "2000-01-01" or policy["requested_end"] != "2035-12-31"
        or policy["outputs"] != {"accepted_root": "data/vault/accepted", "work_root": "data/w/xnys_calendar_successor"}
        or policy["recovery"] != RECOVERY_CONTRACT
        or policy["execution"] != {"owner_confirmation_token": CONFIRMATION_TOKEN, "owner_confirmation_value": CONFIRMATION_VALUE, "publication_count": 1, "network_calls": 0, "source_activation": False}
    ):
        raise ContractError("calendar successor policy differs")
    return policy


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    files = [{"path": item, "sha256": sha256_file(root / item)} for item in paths]
    return {"files": files, "sha256": sha256_bytes(canonical_json_bytes(files))}


def _verify_spent_attempt_evidence(
    root: Path,
    recovery: Mapping[str, Any],
) -> dict[str, object]:
    expected_keys = {
        "schema_version",
        "spent_plan_id",
        "failure_state",
        "preserved_staging_root",
        "preserved_files",
        "new_staging_partition",
        "cleanup_allowed",
        "retry_authorized",
    }
    if (
        type(recovery) is not dict
        or set(recovery) != expected_keys
        or recovery["schema_version"] != 1
        or recovery["failure_state"] != SPENT_FAILURE_STATE
        or recovery["new_staging_partition"] != "environment_sha256"
        or recovery["cleanup_allowed"] is not False
        or recovery["retry_authorized"] is not False
    ):
        raise ContractError("calendar successor recovery contract differs")
    require_sha256(recovery["spent_plan_id"], "calendar recovery spent plan ID")
    files = recovery["preserved_files"]
    if (
        type(files) is not list
        or [item.get("path") for item in files if type(item) is dict]
        != list(PRESERVED_STAGING_FILES)
        or any(
            type(item) is not dict
            or set(item) != {"path", "size", "sha256"}
            or isinstance(item["size"], bool)
            or not isinstance(item["size"], int)
            or item["size"] < 0
            for item in files
        )
    ):
        raise ContractError("calendar successor preserved-file contract differs")
    stage = require_contained_path(
        root / str(recovery["preserved_staging_root"]),
        root / "data",
    )
    reject_link(stage)
    try:
        observed_names = sorted(item.name for item in stage.iterdir())
    except OSError as exc:
        raise IntegrityError("calendar successor preserved staging root is unreadable") from exc
    if observed_names != list(PRESERVED_STAGING_FILES):
        raise IntegrityError("calendar successor preserved staging census differs")
    verified: list[dict[str, object]] = []
    for item in files:
        require_sha256(item["sha256"], f"calendar recovery {item['path']} hash")
        candidate = require_contained_path(stage / item["path"], stage)
        reject_link(candidate)
        if (
            not candidate.is_file()
            or candidate.stat().st_size != item["size"]
            or sha256_file(candidate) != item["sha256"]
        ):
            raise IntegrityError(
                f"calendar successor preserved file differs: {item['path']}"
            )
        verified.append(dict(item))
    unsigned = {
        "schema_version": 1,
        "spent_plan_id": recovery["spent_plan_id"],
        "failure_state": recovery["failure_state"],
        "preserved_staging_root": str(stage),
        "preserved_files": verified,
        "preservation_state": "VERIFIED_UNCHANGED",
        "cleanup_allowed": False,
        "retry_authorized": False,
    }
    return {
        **unsigned,
        "recovery_binding_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_calendar_successor_plan(*, repository_root: Path | None = None) -> dict[str, object]:
    root = _root(repository_root)
    policy = _load_policy(root)
    repository = _clean_repository(root)
    environment_sha256 = calendar_environment_hash()
    recovery = _verify_spent_attempt_evidence(root, policy["recovery"])
    preserved_root = Path(recovery["preserved_staging_root"])
    work_root = require_contained_path(
        preserved_root / environment_sha256,
        root / "data",
        must_exist=False,
    )
    if work_root.exists():
        raise IntegrityError("calendar successor environment staging child already exists")
    unsigned = {
        "schema_version": 1,
        "mode": "PUBLISH_ONE_XNYS_CALENDAR_SUCCESSOR",
        "repository": repository,
        "calendar": {
            "name": policy["calendar_name"], "package": policy["calendar_package"],
            "version": policy["calendar_version"], "start": policy["requested_start"],
            "end": policy["requested_end"], "source_epoch": "xnys_exchange_calendars_4_13_2",
        },
        "code_closure": _closure(root, CODE_CLOSURE_PATHS),
        "successor_policy_sha256": sha256_file(root / POLICY_PATH),
        "calendar_policy_sha256": calendar_policy_hash(),
        "environment_sha256": environment_sha256,
        "recovery": recovery,
        "outputs": {"accepted_root": str(root / policy["outputs"]["accepted_root"]), "work_root": str(work_root), "publication_count": 1},
        "authorities": {"network_calls": 0, "source_activation": False, "calendar_publication": False, "cleanup": False, "spent_plan_retry": False},
    }
    return {**unsigned, "calendar_successor_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def publish_calendar_successor(*, approved_plan_id: str, owner_confirmation: str, clock: TrustedClock, repository_root: Path | None = None) -> Path:
    root = _root(repository_root)
    if owner_confirmation != CONFIRMATION_VALUE:
        raise PermissionError("calendar successor owner confirmation differs")
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise ContractError("calendar successor publication requires production system UTC")
    plan = build_calendar_successor_plan(repository_root=root)
    if approved_plan_id != plan["calendar_successor_plan_id"]:
        raise PermissionError("approved calendar successor plan differs")
    calendar = plan["calendar"]
    return publish_xnys_calendar_release(
        staging_root=Path(plan["outputs"]["work_root"]), release_root=Path(plan["outputs"]["accepted_root"]),
        start=date.fromisoformat(calendar["start"]), end=date.fromisoformat(calendar["end"]),
        created_at=iso_z(trusted_clock.now()), code_hash=plan["code_closure"]["sha256"],
        config_hash=plan["calendar_policy_sha256"], environment_hash=plan["environment_sha256"],
        publication_allowed_root=root / "data", production_clock=trusted_clock,
    )
