from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..common import (
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..environment import validate_environment_lock
from ..errors import ContractError, IntegrityError
from .alpaca_historical_backfill import (
    build_historical_backfill_complete_corpus_plan,
    load_historical_backfill_policy,
)


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_historical_backfill_publication_policy.json"
MODE = "ALPACA_HISTORICAL_BACKFILL_PUBLICATION_PLAN_ONLY"
DATASET = "alpaca_historical_daily_bars"
SOURCE_EPOCH = "alpaca_sip_current_identity_seeded_20160104_20260710_v1"
ROLE = "legacy_discovery_only"
QUALITY_STATE = "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED"

CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_historical_backfill.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_historical_backfill_publication.py",
    "src/us_stocks_swing_model_v2/cli/plan_alpaca_historical_backfill_publication.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_historical_backfill_policy.json",
    POLICY_PATH,
    "config/alpaca_historical_backfill_network_registry.json",
    "config/environment.lock.json",
    "config/sources.json",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise IntegrityError(f"{label} must be one object")
    return value


def _closure(root: Path, paths: Iterable[str]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for relative in sorted(paths):
        path = root / relative
        require_contained_path(path, root)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(f"publication closure file is absent: {relative}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def load_historical_backfill_publication_policy(
    repo_root: Path | None = None,
) -> tuple[dict[str, Any], str]:
    root = (repo_root or _repo_root()).resolve(strict=True)
    policy = _json_object(root / POLICY_PATH, label="backfill publication policy")
    policy_id = sha256_bytes(canonical_json_bytes(policy))
    if (
        policy.get("schema_version") != 1
        or policy.get("project") != PROJECT
        or policy.get("mode") != MODE
        or policy.get("release_contract", {}).get("dataset") != DATASET
        or policy.get("release_contract", {}).get("source_epoch") != SOURCE_EPOCH
        or policy.get("release_contract", {}).get("role") != ROLE
        or policy.get("release_contract", {}).get("quality_state") != QUALITY_STATE
    ):
        raise ContractError("backfill publication policy contract differs")
    implementation = policy.get("implementation")
    if implementation != {
        "plan_only": True,
        "release_builder_implemented": False,
        "publication_execution_implemented": False,
        "release_id_deferred_until_deterministic_builder": True,
    }:
        raise ContractError("backfill publication implementation state differs")
    authorities = policy.get("authorities")
    if not isinstance(authorities, dict) or any(authorities.values()):
        raise ContractError("backfill publication policy grants authority")
    backfill_policy, backfill_policy_id = load_historical_backfill_policy(root)
    if policy.get("backfill_policy_id") != backfill_policy_id:
        raise IntegrityError("backfill publication policy binding differs")
    if backfill_policy.get("quality_state") != QUALITY_STATE:
        raise IntegrityError("backfill publication quality binding differs")
    return policy, policy_id


def _validated_complete_corpus(
    corpus: Mapping[str, object],
    *,
    policy: Mapping[str, Any],
) -> str:
    corpus_id = require_sha256(corpus.get("complete_corpus_id"), "complete corpus ID")
    unsigned = {key: value for key, value in corpus.items() if key != "complete_corpus_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != corpus_id:
        raise IntegrityError("historical backfill complete-corpus ID differs")
    expected = policy["completeness_contract"]
    if (
        corpus.get("plan_type")
        != "ALPACA_SIP_HISTORICAL_BACKFILL_COMPLETE_CORPUS"
        or corpus.get("group_count") != expected["expected_group_count"]
        or corpus.get("unit_count") != expected["expected_unit_count"]
        or not isinstance(corpus.get("page_count"), int)
        or corpus["page_count"] <= 0
        or not isinstance(corpus.get("raw_bytes"), int)
        or corpus["raw_bytes"] <= 0
        or corpus.get("evidence_boundary", {}).get("quality_state") != QUALITY_STATE
        or corpus.get("evidence_boundary", {}).get("survivorship_safe") is not False
        or any(corpus.get("authorities", {}).values())
    ):
        raise IntegrityError("historical backfill complete-corpus contract differs")
    return corpus_id


def build_historical_backfill_publication_plan_from_corpus(
    *,
    complete_corpus: Mapping[str, object],
    policy: Mapping[str, Any],
    publication_policy_id: str,
    accepted_root: Path,
    work_root: Path,
    created_at: str,
    code_closure_sha256: str,
    config_closure_sha256: str,
    environment_id: str,
) -> dict[str, object]:
    corpus_id = _validated_complete_corpus(complete_corpus, policy=policy)
    parse_utc_z(created_at, "backfill publication created_at")
    for label, value in (
        ("publication_policy_id", publication_policy_id),
        ("code_closure_sha256", code_closure_sha256),
        ("config_closure_sha256", config_closure_sha256),
        ("environment_id", environment_id),
    ):
        require_sha256(value, label)
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("backfill publication roots must be absolute")
    release = policy["release_contract"]
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": MODE,
        "publication_policy_id": publication_policy_id,
        "backfill_plan_id": complete_corpus["backfill_plan_id"],
        "complete_corpus_id": corpus_id,
        "repository": dict(complete_corpus["repository"]),
        "code_closure_sha256": code_closure_sha256,
        "config_closure_sha256": config_closure_sha256,
        "environment_id": environment_id,
        "created_at": created_at,
        "accepted_root": str(accepted),
        "work_root": str(work),
        "input_census": {
            "group_count": complete_corpus["group_count"],
            "unit_count": complete_corpus["unit_count"],
            "page_count": complete_corpus["page_count"],
            "raw_bytes": complete_corpus["raw_bytes"],
            "group_continuation_ids_sha256": complete_corpus[
                "group_continuation_ids_sha256"
            ],
            "unit_assessment_ids_sha256": complete_corpus[
                "unit_assessment_ids_sha256"
            ],
            "selected_snapshot_ids_sha256": complete_corpus[
                "selected_snapshot_ids_sha256"
            ],
            "page_evidence_census_sha256": complete_corpus[
                "page_evidence_census_sha256"
            ],
        },
        "release_contract": dict(release),
        "prospective_release": {
            "dataset": DATASET,
            "path_template": str(accepted / DATASET / "<release_id>"),
            "release_id": None,
            "release_id_disposition": (
                "DEFERRED_UNTIL_DETERMINISTIC_SHARD_BUILDER_IS_IMPLEMENTED"
            ),
        },
        "implementation": dict(policy["implementation"]),
        "authorities": dict(policy["authorities"]),
        "stop_conditions": list(policy["stop_conditions"]),
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_historical_backfill_publication_plan(
    *,
    repository_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    created_at: str,
) -> dict[str, object]:
    """Revalidate the complete real corpus and emit a no-write publication plan."""

    root = (repository_root or _repo_root()).resolve(strict=True)
    policy, policy_id = load_historical_backfill_publication_policy(root)
    complete = build_historical_backfill_complete_corpus_plan(repo_root=root)
    expected_accepted = (root / policy["outputs"]["accepted_root"]).resolve()
    expected_work = (root / policy["outputs"]["work_root"]).resolve()
    accepted = Path(accepted_root or expected_accepted).resolve()
    work = Path(work_root or expected_work).resolve()
    if accepted != expected_accepted or work != expected_work:
        raise ContractError("backfill publication roots differ from policy")
    require_contained_path(accepted, root / "data", must_exist=False)
    require_contained_path(work, root / "data", must_exist=False)
    return build_historical_backfill_publication_plan_from_corpus(
        complete_corpus=complete,
        policy=policy,
        publication_policy_id=policy_id,
        accepted_root=accepted,
        work_root=work,
        created_at=created_at,
        code_closure_sha256=_closure(root, CODE_CLOSURE_PATHS)["closure_sha256"],
        config_closure_sha256=_closure(root, CONFIG_CLOSURE_PATHS)[
            "closure_sha256"
        ],
        environment_id=validate_environment_lock(
            root / "config/environment.lock.json"
        ),
    )


def publication_plan_summary(plan: Mapping[str, object]) -> dict[str, object]:
    plan_id = require_sha256(plan.get("publication_plan_id"), "publication plan ID")
    unsigned = {key: value for key, value in plan.items() if key != "publication_plan_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id:
        raise IntegrityError("historical backfill publication plan ID differs")
    return {
        key: plan[key]
        for key in (
            "publication_plan_id",
            "backfill_plan_id",
            "complete_corpus_id",
            "repository",
            "created_at",
            "input_census",
            "release_contract",
            "prospective_release",
            "implementation",
            "authorities",
            "stop_conditions",
        )
    }
