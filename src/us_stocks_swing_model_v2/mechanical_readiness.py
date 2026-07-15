"""Non-authorizing mechanical readiness assessment and receipt publication.

The milestone names in this module certify only a reproducible code/data
mechanism.  They do not authorize real-history research and do not improve the
evidentiary role of legacy HFDL data.  In particular, historical PIT identity
and the exact legacy outcome-exposure count remain unresolved.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import importlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping

from .capabilities import SyntheticOnlyPermit
from .canonical.hfdl_legacy_publisher import (
    HFDL_EPOCHS,
    SYNTHETIC_CONTRACT_SCOPE,
    verify_hfdl_legacy_publication,
)
from .common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
    sha256_file,
)
from .environment import validate_environment_lock
from .errors import ContractError, IntegrityError
from .exchange_calendar import load_xnys_calendar_release
from .foundation_orchestrator import (
    AGGREGATE_COMPONENT_COUNT,
    AGGREGATE_DATASET,
    AGGREGATE_QUALITY,
    AGGREGATE_ROLE,
    AGGREGATE_SOURCE_EPOCH,
    _AGGREGATE_CONTRACT,
    _INDEX_FIELDS as FOUNDATION_INDEX_FIELDS,
    _RECEIPT_FIELDS as FOUNDATION_RECEIPT_FIELDS,
    _contract_id as foundation_contract_id,
    _implementation_hash as foundation_implementation_hash,
)
from .historical_foundation import OUTPUT_KINDS, load_hfdl_historical_foundation
from .releases import (
    AtomicReleasePublisher,
    ReleaseManifest,
    build_manifest,
    verify_accepted_release,
)
from .research.builder import OuterBuilderRequest
from .research.executor import (
    EXECUTOR_ENTRYPOINT,
    EXECUTOR_MECHANICS_VERSION,
    execute_synthetic_nested_wfa,
)


PROJECT = "US_stocks_swing_model_v2"
READINESS_VERSION = "1.0.0"
READINESS_STATE = "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
REBUILD_DATASET = "stock_rebuild_complete_receipt"
HISTORICAL_READY_DATASET = "stock_historical_research_ready_receipt"
RECEIPT_SOURCE_EPOCH = "mechanical_readiness_non_authorizing_v1"
RECEIPT_ROLE = "qualification_evidence_only"
RECEIPT_QUALITY = "QUALIFICATION_EVIDENCE"
REBUILD_MILESTONE = "REBUILD_COMPLETE"
HISTORICAL_READY_MILESTONE = "HISTORICAL_RESEARCH_READY"
MINIMUM_LEGACY_EXPOSURE_FLOOR = 62

_PIT_BLOCKERS = (
    "historical_membership",
    "historical_stock_etf_identity",
    "complete_delisting_outcomes",
    "survivorship_safe_security_master",
)
_FALSE_FOUNDATION_FLAGS = (
    "point_in_time_safe",
    "epochs_may_be_pooled",
    "provider_calls_made",
    "legacy_paths_read",
    "model_or_evaluation_inputs_read",
    "real_history_hypothesis_executed",
    "wfa_executed",
    "labels_emitted",
    "matured_outcomes_emitted",
    "alpha_evidence",
    "candidate_eligible",
)
_RECEIPT_FIELDS = {
    "schema_version",
    "project",
    "milestone",
    "milestone_scope",
    "readiness_state",
    "status",
    "created_at",
    "assessment_id",
    "foundation",
    "mechanics",
    "repository",
    "legacy_exposure",
    "pit_guard",
    "authority_and_claims",
    "upstream_milestone",
    "receipt_id",
}
_FOUNDATION_BINDING_FIELDS = {
    "dataset",
    "release_id",
    "manifest_sha256",
    "foundation_set_sha256",
    "foundation_index_sha256",
    "foundation_contract_id",
    "foundation_implementation_hash",
    "foundation_environment_hash",
    "migration_plan_id",
    "migration_inventory_sha256",
    "migration_completion_receipt_sha256",
    "calendar_release_id",
    "calendar_receipt_id",
    "hfdl_epoch_set_release_id",
    "bridge_set_release_id",
    "component_count",
    "component_release_ids",
    "component_bindings_sha256",
    "synthetic_permit_id",
    "historical_evidence_scope",
    "point_in_time_safe",
    "epochs_may_be_pooled",
    "legacy_paths_read_after_migration",
}
_AUTHORITY_AND_CLAIMS = {
    "execution_authority": False,
    "provider_calls_authorized": False,
    "provider_calls_made": False,
    "real_history_execution_authorized": False,
    "real_history_executed": False,
    "candidate_sealing_authorized": False,
    "candidate_eligible": False,
    "trusted_historical_gate_eligible": False,
    "alpha_evidence": False,
    "alpha_claim": False,
    "historical_pit_claim": False,
    "live_ready": False,
    "options_ready": False,
    "deployable_short_ready": False,
    "trading_authorized": False,
    "retrospective_pit_substitution_allowed": False,
}
_CODE_PATHS = (
    "src/us_stocks_swing_model_v2/mechanical_readiness.py",
    "src/us_stocks_swing_model_v2/cli/assess_mechanical_readiness.py",
    "src/us_stocks_swing_model_v2/foundation_orchestrator.py",
    "src/us_stocks_swing_model_v2/releases.py",
    "src/us_stocks_swing_model_v2/common.py",
    "src/us_stocks_swing_model_v2/locking.py",
    "src/us_stocks_swing_model_v2/environment.py",
    "src/us_stocks_swing_model_v2/capabilities.py",
    "src/us_stocks_swing_model_v2/exchange_calendar.py",
    "src/us_stocks_swing_model_v2/canonical/hfdl_legacy_publisher.py",
    "src/us_stocks_swing_model_v2/historical_foundation.py",
    "src/us_stocks_swing_model_v2/research/executor.py",
    "src/us_stocks_swing_model_v2/research/artifacts.py",
    "src/us_stocks_swing_model_v2/research/contracts.py",
    "src/us_stocks_swing_model_v2/research/splits.py",
    "src/us_stocks_swing_model_v2/research/builder.py",
    "src/us_stocks_swing_model_v2/research/evaluator.py",
    "src/us_stocks_swing_model_v2/inference.py",
)
_CONFIG_PATHS = (
    "config/research_readiness_contract.json",
    "config/controlled_rebuild_authorization.json",
    "config/environment.lock.json",
    "requirements.lock",
    "requirements.sha256.lock",
)
_FOREIGN_IMPORT_PREFIXES = (
    "futures_rebuild",
    "futures_intraday_model",
    "futures_intraday_model_v2",
)


@dataclass(frozen=True)
class MechanicalReadinessAssessment:
    foundation: Mapping[str, Any]
    mechanics: Mapping[str, Any]
    repository: Mapping[str, Any]
    legacy_exposure: Mapping[str, Any]
    pit_guard: Mapping[str, Any]
    isolation_attestation: Mapping[str, Any]
    assessment_id: str

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "project": PROJECT,
            "readiness_version": READINESS_VERSION,
            "readiness_state": READINESS_STATE,
            "foundation": dict(self.foundation),
            "mechanics": dict(self.mechanics),
            "repository": dict(self.repository),
            "legacy_exposure": dict(self.legacy_exposure),
            "pit_guard": dict(self.pit_guard),
            "isolation_attestation": dict(self.isolation_attestation),
            "authority_and_claims": dict(_AUTHORITY_AND_CLAIMS),
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.unsigned_dict(), "assessment_id": self.assessment_id}


@dataclass(frozen=True)
class MechanicalReadinessPublication:
    assessment_id: str
    rebuild_complete_release_directory: Path
    historical_research_ready_release_directory: Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _plain_file(path: Path, *, label: str) -> bytes:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise IntegrityError(f"{label} is unreadable") from exc


def _json_object(path: Path, *, canonical: bool, label: str) -> dict[str, Any]:
    raw = _plain_file(path, label=label)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    if canonical and raw != canonical_json_bytes(payload):
        raise IntegrityError(f"{label} is not canonical JSON")
    return payload


def _file_manifest(paths: tuple[str, ...]) -> tuple[dict[str, str], str]:
    root = _repo_root()
    manifest: dict[str, str] = {}
    for relative in paths:
        path = root.joinpath(*safe_relative_path(relative).parts)
        require_contained_path(path, root, must_exist=True)
        _plain_file(path, label=f"registered closure file {relative}")
        manifest[relative] = sha256_file(path)
    ordered = dict(sorted(manifest.items()))
    return ordered, sha256_bytes(canonical_json_bytes(ordered))


def _test_tree_binding() -> dict[str, Any]:
    root = _repo_root() / "tests"
    if not root.is_dir():
        raise IntegrityError("registered test tree is missing")
    manifest: dict[str, str] = {}
    for path in sorted(root.rglob("test_*.py")):
        require_contained_path(path, root, must_exist=True)
        _plain_file(path, label="registered test file")
        manifest[path.relative_to(_repo_root()).as_posix()] = sha256_file(path)
    if not manifest:
        raise IntegrityError("registered test tree is empty")
    return {
        "test_file_count": len(manifest),
        "test_tree_sha256": sha256_bytes(canonical_json_bytes(manifest)),
    }


def _import_names(tree: ast.AST) -> tuple[str, ...]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(node.args[0].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "importlib"
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(node.args[0].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.append(node.args[0].value)
    return tuple(found)


def _fit_call_count(tree: ast.AST) -> int:
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "fit")
            or (isinstance(node.func, ast.Name) and node.func.id == "fit")
        )
    )


def _source_tree_ast() -> tuple[dict[str, str], dict[str, ast.AST]]:
    root = _repo_root() / "src"
    hashes: dict[str, str] = {}
    trees: dict[str, ast.AST] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(_repo_root()).as_posix()
        raw = _plain_file(path, label="source isolation input")
        try:
            tree = ast.parse(raw, filename=str(path))
        except SyntaxError as exc:
            raise IntegrityError(f"source isolation input is invalid: {relative}") from exc
        hashes[relative] = sha256_bytes(raw)
        trees[relative] = tree
    if not hashes:
        raise IntegrityError("source isolation tree is empty")
    return hashes, trees


def build_mechanical_isolation_attestation() -> dict[str, Any]:
    """Re-derive static role/project isolation from the current source tree."""

    hashes, trees = _source_tree_ast()
    foreign: list[str] = []
    foreign_literals: list[str] = []
    for relative, tree in trees.items():
        for module in _import_names(tree):
            if module.startswith(_FOREIGN_IMPORT_PREFIXES):
                foreign.append(f"{relative}:{module}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                normalized = node.value.replace("/", "\\").lower()
                if any(
                    f"\\{prefix.lower()}" in normalized
                    for prefix in _FOREIGN_IMPORT_PREFIXES
                ):
                    foreign_literals.append(f"{relative}:{node.lineno}")
    evaluator_relative = "src/us_stocks_swing_model_v2/research/evaluator.py"
    inference_relative = "src/us_stocks_swing_model_v2/inference.py"
    evaluator_tree = trees[evaluator_relative]
    inference_tree = trees[inference_relative]
    evaluator_imports = _import_names(evaluator_tree)
    inference_imports = _import_names(inference_tree)
    evaluator_forbidden = sorted(
        value for value in evaluator_imports if "builder" in value or value.startswith("sklearn")
    )
    inference_forbidden = sorted(
        value
        for value in inference_imports
        if value.startswith("sklearn")
        or "research.evaluator" in value
        or value.endswith(".outcomes")
    )
    entry_module, entry_name = EXECUTOR_ENTRYPOINT.split(":", maxsplit=1)
    registered_entrypoint = getattr(importlib.import_module(entry_module), entry_name, None)
    builder_fields = tuple(OuterBuilderRequest.__dataclass_fields__)
    if (
        foreign
        or foreign_literals
        or evaluator_forbidden
        or inference_forbidden
        or _fit_call_count(evaluator_tree) != 0
        or _fit_call_count(inference_tree) != 0
        or registered_entrypoint is not execute_synthetic_nested_wfa
        or "audit_targets" in builder_fields
        or "outer_audit_targets" in builder_fields
    ):
        raise IntegrityError("mechanical role/project isolation does not pass")
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "attestation_scope": "STATIC_SYNTHETIC_MECHANICS_ROLE_ISOLATION",
        "source_python_file_count": len(hashes),
        "source_tree_sha256": sha256_bytes(canonical_json_bytes(hashes)),
        "executor_entrypoint": EXECUTOR_ENTRYPOINT,
        "executor_mechanics_version": EXECUTOR_MECHANICS_VERSION,
        "foreign_project_imports": [],
        "foreign_project_path_literals": [],
        "evaluator_fit_calls": 0,
        "evaluator_forbidden_imports": [],
        "inference_fit_calls": 0,
        "inference_forbidden_imports": [],
        "builder_request_fields": list(builder_fields),
        "builder_outer_audit_targets_present": False,
        "independent_human_judgment_claimed": False,
        "provider_calls_made": False,
        "real_history_executed": False,
        "model_fit_executed": False,
    }
    return {
        **unsigned,
        "attestation_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def verify_mechanical_isolation_attestation(value: Mapping[str, Any]) -> dict[str, Any]:
    expected = build_mechanical_isolation_attestation()
    if (
        not isinstance(value, Mapping)
        or canonical_json_bytes(dict(value)) != canonical_json_bytes(expected)
    ):
        raise IntegrityError("mechanical isolation attestation differs from current source")
    return expected


def _load_registered_contract() -> tuple[dict[str, Any], dict[str, Any]]:
    path = _repo_root() / "config" / "research_readiness_contract.json"
    contract = _json_object(path, canonical=False, label="research readiness contract")
    try:
        readiness = contract["readiness"]
        census = contract["trial_ledger"]["legacy_trial_census"]
        registered = contract["registered_mechanical_executor"]
        isolation = contract["role_isolation"]
        claims = contract["claims"]
    except (KeyError, TypeError) as exc:
        raise IntegrityError("research readiness contract lacks registered mechanics") from exc
    expected_registered = {
        "entrypoint": EXECUTOR_ENTRYPOINT,
        "mechanics_version": EXECUTOR_MECHANICS_VERSION,
        "implementation_status": "IMPLEMENTED_SYNTHETIC_ADVERSARIAL_TESTED",
        "evidence_role": "SYNTHETIC_MECHANICS_ONLY",
        "model_kind": "linear_distribution_v1",
        "target_semantics": "ABSOLUTE_NEXT_5_SESSION_RETURN",
        "fold_local_scaling": True,
        "ridge_fit": True,
        "hyperparameter_selection": "INNER_WEIGHTED_MEAN_SQUARED_ERROR_ONLY",
        "tie_break": "LOWEST_ALPHA",
        "outer_predictions_frozen_before_label_evaluation": True,
        "fit_and_audit_sample_ids_recorded_exactly": True,
        "rank_used_as_direction": False,
        "real_history_authorized": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
        "does_not_clear_pit_or_trial_census_blockers": True,
    }
    if canonical_json_bytes(registered) != canonical_json_bytes(expected_registered):
        raise IntegrityError("registered synthetic executor contract differs")
    if (
        type(contract.get("schema_version")) is not int
        or contract.get("schema_version") != 1
        or contract.get("project") != PROJECT
        or readiness.get("target_state") != HISTORICAL_READY_MILESTONE
        or readiness.get("historical_evidence_scope") != READINESS_STATE
        or readiness.get("candidate_eligibility") != "BLOCKED_PENDING_PROSPECTIVE_PIT"
        or readiness.get("alpha_claim") is not False
        or readiness.get("live_readiness") is not False
        or tuple(readiness.get("pit_blockers", ())) != _PIT_BLOCKERS
        or claims.get("options_execution_claim") is not False
        or claims.get("short_implementation_claim") is not False
        or claims.get("historical_pit_claim") is not False
        or claims.get("historical_alpha_claim") is not False
        or isolation.get("builder_outer_label_access") is not False
        or type(isolation.get("evaluator_fit_calls_allowed")) is not int
        or isolation.get("evaluator_fit_calls_allowed") != 0
        or type(isolation.get("inference_fit_calls_allowed")) is not int
        or isolation.get("inference_fit_calls_allowed") != 0
        or isolation.get("inference_outcome_or_evaluation_access") is not False
    ):
        raise IntegrityError("research readiness blockers or isolation contract differ")
    floor = census.get("documented_minimum_outcome_informed_attempts")
    if (
        type(floor) is not int
        or floor < MINIMUM_LEGACY_EXPOSURE_FLOOR
        or census.get("exact_count_state") != "INDETERMINATE"
        or census.get("uncertain_attempts_count_conservatively") is not True
        or census.get("effective_trial_count_reduction_allowed") is not False
        or census.get("trusted_gate_blocked_until_exact_census") is not True
        or census.get("unresolved_status") != "INVALID_TRIAL_CENSUS_UNRESOLVED"
        or "exact_count" in census
    ):
        raise IntegrityError("legacy exposure is not a conservative indeterminate floor")
    legacy = {
        "documented_lower_bound": floor,
        "minimum_required_lower_bound": MINIMUM_LEGACY_EXPOSURE_FLOOR,
        "count_semantics": "LOWER_BOUND_NOT_EXACT",
        "exact_count": None,
        "exact_count_state": "INDETERMINATE",
        "uncertain_attempts_count_conservatively": True,
        "effective_trial_count_reduction_allowed": False,
        "trusted_gate_eligible": False,
        "unresolved_status": "INVALID_TRIAL_CENSUS_UNRESOLVED",
        "fabricated_trial_events": False,
    }
    return contract, legacy


def _load_authorization() -> tuple[str, str]:
    path = _repo_root() / "config" / "controlled_rebuild_authorization.json"
    value = _json_object(path, canonical=False, label="controlled rebuild authorization")
    unsigned = {key: item for key, item in value.items() if key != "authorization_id"}
    # This pre-existing authorization uses compact canonical JSON without the
    # repository newline convention; retain its exact registered hash policy.
    expected_id = sha256_bytes(canonical_json_bytes(unsigned)[:-1])
    hard_pauses = value.get("hard_pauses")
    allowed = value.get("allowed_actions")
    if (
        value.get("project") != PROJECT
        or value.get("authorization_id") != expected_id
        or not isinstance(hard_pauses, list)
        or "real_history_hypothesis_or_wfa_execution" not in hard_pauses
        or "candidate_sealing" not in hard_pauses
        or "trading" not in hard_pauses
        or not isinstance(allowed, list)
        or any(
            item in allowed
            for item in (
                "real_history_hypothesis_or_wfa_execution",
                "candidate_sealing",
                "trading",
            )
        )
    ):
        raise IntegrityError("controlled rebuild authorization boundary differs")
    return expected_id, sha256_file(path)


def _run_git(*arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(_repo_root()), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise IntegrityError("production readiness requires a valid committed Git closure") from exc
    return completed.stdout.strip()


def _git_oid(value: str, *, object_format: str, field: str) -> str:
    length = 40 if object_format == "sha1" else 64 if object_format == "sha256" else 0
    if length == 0 or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise IntegrityError(f"{field} differs from the registered Git object format")
    return value


def _repository_binding(permit: SyntheticOnlyPermit | None) -> dict[str, Any]:
    code_manifest, code_hash = _file_manifest(_CODE_PATHS)
    config_manifest, config_hash = _file_manifest(_CONFIG_PATHS)
    tests = _test_tree_binding()
    source_hashes, _ = _source_tree_ast()
    common = {
        "code_file_count": len(code_manifest),
        "code_closure_sha256": code_hash,
        "config_file_count": len(config_manifest),
        "config_closure_sha256": config_hash,
        **tests,
        "source_tree_sha256": sha256_bytes(canonical_json_bytes(source_hashes)),
    }
    if permit is not None:
        permit.validate(SYNTHETIC_CONTRACT_SCOPE)
        return {
            "mode": "SYNTHETIC_TEST_ONLY_UNCOMMITTED_CLOSURE",
            "synthetic_permit_id": permit.permit_id,
            "repository_root": str(_repo_root()),
            "git_directory": None,
            "object_format": None,
            "head": None,
            "tree": None,
            "clean": False,
            **common,
        }
    root = Path(_run_git("rev-parse", "--show-toplevel")).resolve(strict=True)
    if root != _repo_root().resolve(strict=True):
        raise IntegrityError("Git toplevel differs from the stock-v2 repository")
    status = _run_git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise IntegrityError("production readiness requires a clean tracked repository")
    object_format = _run_git("rev-parse", "--show-object-format")
    git_raw = Path(_run_git("rev-parse", "--absolute-git-dir"))
    git_directory = git_raw.resolve(strict=True)
    legacy_git = (_repo_root().parent / "US_stocks_swing_model" / ".git")
    futures_git = (_repo_root().parent / "futures_intraday_model_v2" / ".git")
    for foreign in (legacy_git, futures_git):
        if foreign.exists() and git_directory == foreign.resolve(strict=True):
            raise IntegrityError("stock-v2 Git directory is not isolated")
    tracked = set(_run_git("ls-files").splitlines())
    test_paths = {
        path.relative_to(_repo_root()).as_posix()
        for path in (_repo_root() / "tests").rglob("test_*.py")
    }
    required_tracked = set(_CODE_PATHS) | set(_CONFIG_PATHS) | set(source_hashes) | test_paths
    if not required_tracked <= tracked:
        raise IntegrityError("production readiness closure contains untracked required files")
    return {
        "mode": "CLEAN_COMMITTED_PRODUCTION_CLOSURE",
        "synthetic_permit_id": None,
        "repository_root": str(root),
        "git_directory": str(git_directory),
        "object_format": object_format,
        "head": _git_oid(
            _run_git("rev-parse", "HEAD"),
            object_format=object_format,
            field="repository.head",
        ),
        "tree": _git_oid(
            _run_git("rev-parse", "HEAD^{tree}"),
            object_format=object_format,
            field="repository.tree",
        ),
        "clean": True,
        **common,
    }


def _component_binding(directory: Path, manifest: ReleaseManifest) -> dict[str, Any]:
    return {
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "relative_directory": f"{manifest.dataset}/{manifest.release_id}",
        "source_epoch": manifest.source_epoch,
        "role": manifest.role,
        "quality_state": manifest.quality_state,
        "row_count": manifest.row_count,
        "event_start": manifest.event_start,
        "event_end": manifest.event_end,
        "manifest_sha256": sha256_file(directory / "release_manifest.json"),
    }


def _foundation_rows(directory: Path) -> tuple[tuple[dict[str, Any], ...], bytes]:
    raw = _plain_file(directory / "foundation_index.jsonl", label="foundation index")
    lines = raw.splitlines(keepends=True)
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IntegrityError("foundation index is invalid JSONL") from exc
        if not isinstance(value, dict):
            raise IntegrityError("foundation index rows must be objects")
        rows.append(value)
    if raw != b"".join(canonical_json_bytes(row) for row in rows):
        raise IntegrityError("foundation index is not canonical JSONL")
    return tuple(rows), raw


def _receipt_component_bindings(receipt: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    try:
        bindings: list[dict[str, Any]] = [dict(receipt["calendar"]["release"])]
        bindings.extend(dict(receipt["hfdl"]["epochs"][epoch]) for epoch in HFDL_EPOCHS)
        bindings.append(dict(receipt["hfdl"]["epoch_set"]))
        for epoch in HFDL_EPOCHS:
            bindings.extend(
                dict(receipt["historical_foundation"]["epochs"][epoch][kind])
                for kind in OUTPUT_KINDS
            )
        bindings.append(dict(receipt["historical_foundation"]["bridge_set"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegrityError("foundation receipt component bindings differ") from exc
    return tuple(bindings)


def _verify_foundation(
    directory: Path,
    *,
    accepted_root: Path,
    synthetic_permit: SyntheticOnlyPermit | None,
) -> dict[str, Any]:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    if (
        manifest.project != PROJECT
        or manifest.dataset != AGGREGATE_DATASET
        or manifest.source_epoch != AGGREGATE_SOURCE_EPOCH
        or manifest.role != AGGREGATE_ROLE
        or manifest.quality_state != AGGREGATE_QUALITY
        or manifest.row_count != AGGREGATE_COMPONENT_COUNT
        or {entry.path for entry in manifest.files}
        != {"foundation_index.jsonl", "foundation_set.json"}
    ):
        raise IntegrityError("foundation aggregate contract differs")
    receipt = _json_object(
        directory / "foundation_set.json",
        canonical=True,
        label="foundation aggregate receipt",
    )
    rows, index_raw = _foundation_rows(directory)
    if set(receipt) != FOUNDATION_RECEIPT_FIELDS:
        raise IntegrityError("foundation aggregate receipt fields differ")
    synthetic_id = receipt.get("synthetic_permit_id")
    if synthetic_id is None:
        if synthetic_permit is not None:
            raise IntegrityError("production foundation cannot use a synthetic permit")
        expected_root = (_repo_root() / "data" / "vault" / "accepted").resolve(strict=True)
        if accepted_root.resolve(strict=True) != expected_root:
            raise IntegrityError("production readiness requires the repository accepted root")
    else:
        if synthetic_permit is None:
            raise IntegrityError("synthetic foundation cannot enter production readiness")
        synthetic_permit.validate(SYNTHETIC_CONTRACT_SCOPE)
        if synthetic_id != synthetic_permit.permit_id:
            raise IntegrityError("synthetic foundation permit substitution")
    if (
        manifest.code_hash != foundation_implementation_hash()
        or manifest.config_hash != foundation_contract_id()
        or manifest.environment_hash
        != sha256_file(_repo_root() / "config" / "environment.lock.json")
        or canonical_json_bytes(receipt.get("contract"))
        != canonical_json_bytes(_AGGREGATE_CONTRACT)
        or receipt.get("contract_id") != foundation_contract_id()
        or receipt.get("implementation_hash") != foundation_implementation_hash()
        or receipt.get("environment_hash") != manifest.environment_hash
        or receipt.get("publication_state") != "COMPLETE_NON_ACTIVE_HISTORICAL_FOUNDATION"
        or receipt.get("component_count") != AGGREGATE_COMPONENT_COUNT
        or receipt.get("historical_evidence_scope") != READINESS_STATE
        or any(receipt.get(field) is not False for field in _FALSE_FOUNDATION_FLAGS)
    ):
        raise IntegrityError("foundation aggregate contract differs")
    if (
        len(rows) != AGGREGATE_COMPONENT_COUNT
        or [row.get("sequence") for row in rows] != list(range(AGGREGATE_COMPONENT_COUNT))
        or any(type(row.get("sequence")) is not int for row in rows)
        or any(type(row.get("row_count")) is not int for row in rows)
        or any(set(row) != FOUNDATION_INDEX_FIELDS for row in rows)
    ):
        raise IntegrityError("foundation component denominator or sequence differs")
    expected_topology: list[tuple[str, str, str]] = [
        ("calendar", "xnys_exchange_calendars_4_13_2", "pinned_sessions")
    ]
    expected_topology.extend(("hfdl", epoch, "physical_epoch") for epoch in HFDL_EPOCHS)
    expected_topology.append(("hfdl", "hfdl_epoch_set_no_pooling", "epoch_set"))
    for epoch in HFDL_EPOCHS:
        expected_topology.extend(("bridge", epoch, kind) for kind in OUTPUT_KINDS)
    expected_topology.append(
        ("bridge", "hfdl_historical_foundation_no_pooling", "bridge_set")
    )
    if [
        (row.get("phase"), row.get("epoch"), row.get("kind")) for row in rows
    ] != expected_topology:
        raise IntegrityError("foundation component topology differs")
    receipt_bindings = _receipt_component_bindings(receipt)
    release_ids: list[str] = []
    for row, receipt_binding in zip(rows, receipt_bindings, strict=True):
        require_sha256(row.get("release_id"), "foundation.component.release_id")
        relative = safe_relative_path(str(row.get("relative_directory")))
        component_dir = accepted_root.joinpath(*relative.parts)
        component_manifest = verify_accepted_release(
            component_dir, accepted_root=accepted_root
        )
        observed = {
            "phase": row["phase"],
            "epoch": row["epoch"],
            "kind": row["kind"],
            **_component_binding(component_dir, component_manifest),
        }
        expected_row = {"sequence": row["sequence"], **observed}
        if (
            canonical_json_bytes(row) != canonical_json_bytes(expected_row)
            or canonical_json_bytes(receipt_binding) != canonical_json_bytes(observed)
        ):
            raise IntegrityError("foundation component binding substitution")
        release_ids.append(component_manifest.release_id)
    if (
        len(set(release_ids)) != AGGREGATE_COMPONENT_COUNT
        or sorted(release_ids) != receipt.get("component_release_ids")
        or tuple(sorted(release_ids)) != manifest.upstream_release_ids
        or sha256_bytes(index_raw) != receipt.get("index_sha256")
    ):
        raise IntegrityError("foundation aggregate component identity differs")
    calendar_binding = receipt["calendar"]["release"]
    calendar_dir = accepted_root / calendar_binding["dataset"] / calendar_binding["release_id"]
    load_xnys_calendar_release(calendar_dir, accepted_release_root=accepted_root)
    hfdl_binding = receipt["hfdl"]["epoch_set"]
    hfdl_dir = accepted_root / hfdl_binding["dataset"] / hfdl_binding["release_id"]
    hfdl = verify_hfdl_legacy_publication(
        hfdl_dir,
        accepted_release_root=accepted_root,
        synthetic_permit=synthetic_permit,
    )
    bridge_binding = receipt["historical_foundation"]["bridge_set"]
    bridge_dir = accepted_root / bridge_binding["dataset"] / bridge_binding["release_id"]
    bridge = load_hfdl_historical_foundation(
        bridge_dir,
        accepted_release_root=accepted_root,
        hfdl_synthetic_permit=synthetic_permit,
    )
    if (
        hfdl.epoch_set_release_directory.resolve(strict=True) != hfdl_dir.resolve(strict=True)
        or bridge.bridge_set_release_directory.resolve(strict=True)
        != bridge_dir.resolve(strict=True)
        or receipt.get("migration", {}).get("manifest_schema_version") != 2
        or receipt.get("migration", {}).get("payload_layout_version")
        != "flat_object_160bit_v1"
    ):
        raise IntegrityError("foundation deep publication binding differs")
    migration = receipt["migration"]
    foundation = {
        "dataset": manifest.dataset,
        "release_id": manifest.release_id,
        "manifest_sha256": sha256_file(directory / "release_manifest.json"),
        "foundation_set_sha256": sha256_file(directory / "foundation_set.json"),
        "foundation_index_sha256": sha256_bytes(index_raw),
        "foundation_contract_id": receipt["contract_id"],
        "foundation_implementation_hash": receipt["implementation_hash"],
        "foundation_environment_hash": receipt["environment_hash"],
        "migration_plan_id": migration["plan_id"],
        "migration_inventory_sha256": migration["inventory_sha256"],
        "migration_completion_receipt_sha256": migration[
            "completion_receipt_sha256"
        ],
        "calendar_release_id": calendar_binding["release_id"],
        "calendar_receipt_id": receipt["calendar"]["receipt_id"],
        "hfdl_epoch_set_release_id": hfdl_binding["release_id"],
        "bridge_set_release_id": bridge_binding["release_id"],
        "component_count": AGGREGATE_COMPONENT_COUNT,
        "component_release_ids": sorted(release_ids),
        "component_bindings_sha256": sha256_bytes(
            canonical_json_bytes(list(rows))
        ),
        "synthetic_permit_id": synthetic_id,
        "historical_evidence_scope": READINESS_STATE,
        "point_in_time_safe": False,
        "epochs_may_be_pooled": False,
        "legacy_paths_read_after_migration": False,
    }
    if set(foundation) != _FOUNDATION_BINDING_FIELDS:
        raise IntegrityError("foundation readiness binding fields differ")
    return foundation


def assess_stock_mechanical_readiness(
    *,
    foundation_release_directory: Path,
    accepted_release_root: Path,
    synthetic_permit: SyntheticOnlyPermit | None = None,
) -> MechanicalReadinessAssessment:
    """Verify all prerequisites without writing receipts or executing research."""

    foundation_path = Path(foundation_release_directory)
    accepted_root = Path(accepted_release_root)
    if not foundation_path.is_absolute() or not accepted_root.is_absolute():
        raise ContractError("readiness foundation and accepted roots must be absolute")
    foundation = _verify_foundation(
        foundation_path,
        accepted_root=accepted_root,
        synthetic_permit=synthetic_permit,
    )
    contract, legacy = _load_registered_contract()
    authorization_id, authorization_sha256 = _load_authorization()
    _, code_hash = _file_manifest(_CODE_PATHS)
    _, config_hash = _file_manifest(_CONFIG_PATHS)
    environment_path = _repo_root() / "config" / "environment.lock.json"
    environment_id = validate_environment_lock(environment_path)
    isolation = verify_mechanical_isolation_attestation(
        build_mechanical_isolation_attestation()
    )
    repository = _repository_binding(synthetic_permit)
    mechanics = {
        "registered_executor_entrypoint": EXECUTOR_ENTRYPOINT,
        "registered_executor_mechanics_version": EXECUTOR_MECHANICS_VERSION,
        "registered_executor_contract_sha256": sha256_bytes(
            canonical_json_bytes(contract["registered_mechanical_executor"])
        ),
        "readiness_contract_sha256": sha256_file(
            _repo_root() / "config" / "research_readiness_contract.json"
        ),
        "controlled_rebuild_authorization_id": authorization_id,
        "controlled_rebuild_authorization_sha256": authorization_sha256,
        "implementation_closure_sha256": code_hash,
        "config_closure_sha256": config_hash,
        "environment_id": environment_id,
        "environment_lock_sha256": sha256_file(environment_path),
        "isolation_attestation_id": isolation["attestation_id"],
        "synthetic_mechanics_registered": True,
        "synthetic_mechanics_execution_performed_by_assessor": False,
        "real_history_authorized": False,
        "alpha_evidence": False,
        "candidate_eligible": False,
    }
    pit_guard = {
        "historical_pit_identity_evidence": "UNRESOLVED_NOT_FABRICATED",
        "unresolved_blockers": list(_PIT_BLOCKERS),
        "genuinely_prospective_pit_evidence_required": True,
        "retrospective_membership_or_identity_substitution_allowed": False,
        "trusted_historical_gate": (
            "BLOCKED_PENDING_GENUINELY_PROSPECTIVE_PIT_AND_EXACT_LEGACY_CENSUS"
        ),
        "candidate_eligibility": "BLOCKED_PENDING_GENUINELY_PROSPECTIVE_PIT",
    }
    provisional = MechanicalReadinessAssessment(
        foundation=foundation,
        mechanics=mechanics,
        repository=repository,
        legacy_exposure=legacy,
        pit_guard=pit_guard,
        isolation_attestation=isolation,
        assessment_id="",
    )
    assessment = MechanicalReadinessAssessment(
        foundation=foundation,
        mechanics=mechanics,
        repository=repository,
        legacy_exposure=legacy,
        pit_guard=pit_guard,
        isolation_attestation=isolation,
        assessment_id=sha256_bytes(canonical_json_bytes(provisional.unsigned_dict())),
    )
    if assessment.assessment_id != sha256_bytes(
        canonical_json_bytes(assessment.unsigned_dict())
    ):
        raise IntegrityError("mechanical readiness assessment ID differs")
    return assessment


def _receipt(
    assessment: MechanicalReadinessAssessment,
    *,
    milestone: str,
    created_at: str,
    upstream_milestone: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if milestone == REBUILD_MILESTONE:
        scope = "COMMITTED_REBUILD_MECHANICS_ONLY"
    elif milestone == HISTORICAL_READY_MILESTONE:
        scope = "HISTORICAL_MECHANICAL_HARNESS_ONLY"
    else:
        raise ContractError("unknown mechanical readiness milestone")
    unsigned = {
        "schema_version": 1,
        "project": PROJECT,
        "milestone": milestone,
        "milestone_scope": scope,
        "readiness_state": READINESS_STATE,
        "status": "PASS_MECHANICAL_PREREQUISITES_NON_AUTHORIZING",
        "created_at": created_at,
        "assessment_id": assessment.assessment_id,
        "foundation": dict(assessment.foundation),
        "mechanics": dict(assessment.mechanics),
        "repository": dict(assessment.repository),
        "legacy_exposure": dict(assessment.legacy_exposure),
        "pit_guard": dict(assessment.pit_guard),
        "authority_and_claims": dict(_AUTHORITY_AND_CLAIMS),
        "upstream_milestone": dict(upstream_milestone) if upstream_milestone else None,
    }
    return {**unsigned, "receipt_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        if _plain_file(path, label="readiness stage receipt") != payload:
            raise IntegrityError("readiness stage receipt differs")
        return
    atomic_write(path, payload)


def _publish_receipt(
    receipt: Mapping[str, Any],
    *,
    dataset: str,
    filename: str,
    accepted_root: Path,
    stage: Path,
    upstream_release_ids: tuple[str, ...],
) -> Path:
    stage.mkdir(parents=True, exist_ok=True)
    receipt_path = stage / filename
    _write_exact(receipt_path, canonical_json_bytes(dict(receipt)))
    try:
        assert_exact_tree(stage, {filename}, set())
    except ContractError as exc:
        raise IntegrityError("readiness stage contains partial or extra evidence") from exc
    schema_fingerprint = sha256_bytes(
        canonical_json_bytes(
            {
                "receipt_fields": sorted(_RECEIPT_FIELDS),
                "milestone": receipt["milestone"],
            }
        )
    )
    manifest = build_manifest(
        stage,
        (filename,),
        project=PROJECT,
        dataset=dataset,
        source_epoch=RECEIPT_SOURCE_EPOCH,
        role=RECEIPT_ROLE,
        quality_state=RECEIPT_QUALITY,
        created_at=str(receipt["created_at"]),
        row_count=1,
        event_start=None,
        event_end=None,
        upstream_release_ids=upstream_release_ids,
        schema_fingerprint=schema_fingerprint,
        code_hash=str(receipt["mechanics"]["implementation_closure_sha256"]),
        config_hash=str(receipt["mechanics"]["config_closure_sha256"]),
        environment_hash=str(receipt["mechanics"]["environment_id"]),
    )
    return AtomicReleasePublisher(accepted_root).publish(stage, manifest)


def publish_stock_mechanical_readiness(
    *,
    foundation_release_directory: Path,
    accepted_release_root: Path,
    readiness_work_root: Path,
    created_at: str,
    synthetic_permit: SyntheticOnlyPermit | None = None,
) -> MechanicalReadinessPublication:
    """Publish two evidence receipts; never grant or perform research authority."""

    parse_utc_z(created_at, "mechanical_readiness.created_at")
    accepted_root = Path(accepted_release_root)
    work_root = Path(readiness_work_root)
    if not accepted_root.is_absolute() or not work_root.is_absolute():
        raise ContractError("readiness accepted and work roots must be absolute")
    if synthetic_permit is None:
        expected_work_parent = (_repo_root() / "data").resolve(strict=True)
        require_contained_path(work_root, expected_work_parent, must_exist=False)
        work_root.parent.mkdir(parents=True, exist_ok=True)
    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=Path(foundation_release_directory),
        accepted_release_root=accepted_root,
        synthetic_permit=synthetic_permit,
    )
    build_id = sha256_bytes(
        canonical_json_bytes(
            {
                "assessment_id": assessment.assessment_id,
                "created_at": created_at,
                "readiness_version": READINESS_VERSION,
            }
        )
    )
    build_root = work_root / build_id[:20]
    rebuild_receipt = _receipt(
        assessment,
        milestone=REBUILD_MILESTONE,
        created_at=created_at,
        upstream_milestone=None,
    )
    rebuild_dir = _publish_receipt(
        rebuild_receipt,
        dataset=REBUILD_DATASET,
        filename="rebuild_complete.json",
        accepted_root=accepted_root,
        stage=build_root / "r",
        upstream_release_ids=(str(assessment.foundation["release_id"]),),
    )
    rebuild_manifest = verify_accepted_release(rebuild_dir, accepted_root=accepted_root)
    upstream = {
        "milestone": REBUILD_MILESTONE,
        "release_id": rebuild_manifest.release_id,
        "receipt_id": rebuild_receipt["receipt_id"],
        "manifest_sha256": sha256_file(rebuild_dir / "release_manifest.json"),
    }
    historical_receipt = _receipt(
        assessment,
        milestone=HISTORICAL_READY_MILESTONE,
        created_at=created_at,
        upstream_milestone=upstream,
    )
    historical_dir = _publish_receipt(
        historical_receipt,
        dataset=HISTORICAL_READY_DATASET,
        filename="historical_research_ready.json",
        accepted_root=accepted_root,
        stage=build_root / "h",
        upstream_release_ids=(
            str(assessment.foundation["release_id"]),
            rebuild_manifest.release_id,
        ),
    )
    result = MechanicalReadinessPublication(
        assessment_id=assessment.assessment_id,
        rebuild_complete_release_directory=rebuild_dir,
        historical_research_ready_release_directory=historical_dir,
    )
    _verify_stock_mechanical_readiness_publication_against_assessment(
        assessment=assessment,
        foundation_release_directory=Path(foundation_release_directory),
        accepted_release_root=accepted_root,
        rebuild_complete_release_directory=rebuild_dir,
        historical_research_ready_release_directory=historical_dir,
    )
    return result


def _verify_receipt_release(
    directory: Path,
    *,
    accepted_root: Path,
    dataset: str,
    filename: str,
    expected: Mapping[str, Any],
    upstream_release_ids: tuple[str, ...],
) -> ReleaseManifest:
    manifest = verify_accepted_release(directory, accepted_root=accepted_root)
    receipt = _json_object(directory / filename, canonical=True, label="readiness receipt")
    if (
        set(receipt) != _RECEIPT_FIELDS
        or canonical_json_bytes(receipt) != canonical_json_bytes(dict(expected))
    ):
        raise IntegrityError("readiness receipt differs from current verified evidence")
    unsigned = dict(receipt)
    receipt_id = unsigned.pop("receipt_id")
    if receipt_id != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("readiness receipt ID differs from its content")
    expected_schema = sha256_bytes(
        canonical_json_bytes(
            {"receipt_fields": sorted(_RECEIPT_FIELDS), "milestone": receipt["milestone"]}
        )
    )
    if (
        manifest.project != PROJECT
        or manifest.dataset != dataset
        or manifest.source_epoch != RECEIPT_SOURCE_EPOCH
        or manifest.role != RECEIPT_ROLE
        or manifest.quality_state != RECEIPT_QUALITY
        or manifest.row_count != 1
        or manifest.event_start is not None
        or manifest.event_end is not None
        or manifest.upstream_release_ids != tuple(sorted(upstream_release_ids))
        or manifest.schema_fingerprint != expected_schema
        or manifest.code_hash != receipt["mechanics"]["implementation_closure_sha256"]
        or manifest.config_hash != receipt["mechanics"]["config_closure_sha256"]
        or manifest.environment_hash != receipt["mechanics"]["environment_id"]
        or {entry.path for entry in manifest.files} != {filename}
    ):
        raise IntegrityError("readiness release manifest differs")
    return manifest


def _verify_stock_mechanical_readiness_publication_against_assessment(
    *,
    assessment: MechanicalReadinessAssessment,
    foundation_release_directory: Path,
    accepted_release_root: Path,
    rebuild_complete_release_directory: Path,
    historical_research_ready_release_directory: Path,
) -> MechanicalReadinessPublication:
    rebuild_payload = _json_object(
        Path(rebuild_complete_release_directory) / "rebuild_complete.json",
        canonical=True,
        label="rebuild complete receipt",
    )
    created_at = rebuild_payload.get("created_at")
    parse_utc_z(created_at, "rebuild_complete.created_at")
    expected_rebuild = _receipt(
        assessment,
        milestone=REBUILD_MILESTONE,
        created_at=created_at,
        upstream_milestone=None,
    )
    rebuild_manifest = _verify_receipt_release(
        Path(rebuild_complete_release_directory),
        accepted_root=Path(accepted_release_root),
        dataset=REBUILD_DATASET,
        filename="rebuild_complete.json",
        expected=expected_rebuild,
        upstream_release_ids=(str(assessment.foundation["release_id"]),),
    )
    upstream = {
        "milestone": REBUILD_MILESTONE,
        "release_id": rebuild_manifest.release_id,
        "receipt_id": expected_rebuild["receipt_id"],
        "manifest_sha256": sha256_file(
            Path(rebuild_complete_release_directory) / "release_manifest.json"
        ),
    }
    historical_payload = _json_object(
        Path(historical_research_ready_release_directory)
        / "historical_research_ready.json",
        canonical=True,
        label="historical research ready receipt",
    )
    if historical_payload.get("created_at") != created_at:
        raise IntegrityError("readiness milestone timestamps differ")
    expected_historical = _receipt(
        assessment,
        milestone=HISTORICAL_READY_MILESTONE,
        created_at=created_at,
        upstream_milestone=upstream,
    )
    _verify_receipt_release(
        Path(historical_research_ready_release_directory),
        accepted_root=Path(accepted_release_root),
        dataset=HISTORICAL_READY_DATASET,
        filename="historical_research_ready.json",
        expected=expected_historical,
        upstream_release_ids=(
            str(assessment.foundation["release_id"]),
            rebuild_manifest.release_id,
        ),
    )
    return MechanicalReadinessPublication(
        assessment_id=assessment.assessment_id,
        rebuild_complete_release_directory=Path(rebuild_complete_release_directory),
        historical_research_ready_release_directory=Path(
            historical_research_ready_release_directory
        ),
    )


def verify_stock_mechanical_readiness_publication(
    *,
    foundation_release_directory: Path,
    accepted_release_root: Path,
    rebuild_complete_release_directory: Path,
    historical_research_ready_release_directory: Path,
    synthetic_permit: SyntheticOnlyPermit | None = None,
) -> MechanicalReadinessPublication:
    """Re-derive and verify both milestone releases without selecting a latest file."""

    assessment = assess_stock_mechanical_readiness(
        foundation_release_directory=Path(foundation_release_directory),
        accepted_release_root=Path(accepted_release_root),
        synthetic_permit=synthetic_permit,
    )
    return _verify_stock_mechanical_readiness_publication_against_assessment(
        assessment=assessment,
        foundation_release_directory=Path(foundation_release_directory),
        accepted_release_root=Path(accepted_release_root),
        rebuild_complete_release_directory=Path(rebuild_complete_release_directory),
        historical_research_ready_release_directory=Path(
            historical_research_ready_release_directory
        ),
    )
