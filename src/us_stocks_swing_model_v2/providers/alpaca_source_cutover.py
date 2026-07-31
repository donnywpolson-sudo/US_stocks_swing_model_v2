from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..common import (
    atomic_write,
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..environment import validate_environment_lock
from ..errors import ContractError, IntegrityError
from ..releases import verify_accepted_release
from .alpaca_qualification_publisher import (
    PAYLOAD_FILENAME,
    verify_alpaca_qualification_release,
)
from .alpaca_qualification_readiness import (
    PROJECT,
    load_alpaca_feed_qualification_policy,
)


ACTIVATION_CONFIRMATION_TOKEN = "ALPACA_SOURCE_ACTIVATION_APPROVED"
ACTIVATION_CONFIRMATION_VALUE = "YES"
FIXTURE_SCOPE = "ALPACA_SOURCE_CUTOVER_FIXTURE"
BASE_COMMIT = "7650811add138d744890c1ca7662b0cc01bd08bd"
BASE_TREE = "600c5d479789ed87ae6fbe78be9cfa743e24eb94"
QUALIFICATION_RELEASE_ID = (
    "8bce929303039efa69c6d9456dcb9b64b593a7397d0f7ffd479dbc358a5b33a2"
)
QUALIFICATION_RECEIPT_ID = (
    "92387485915f02368f830e4816d3cb6284e334fec73f29b63aa6e62663d948d5"
)
QUALIFICATION_PUBLICATION_PLAN_ID = (
    "38baceccab7e0cac4f6a1ff7abdb94be9ae38a1e078ecbc600a4e21e9e42db24"
)
QUALIFICATION_RECEIPT_FILE_SHA256 = (
    "8088dc87605560b993c6ae88734b95d3a9efa684dc46c051890e2ae2af139fc4"
)
QUALIFICATION_MANIFEST_FILE_SHA256 = (
    "792ef032297b8ff4cf65c3d33b9dae8f4344f6351836f4b579f517a41f3103aa"
)
CODE_CLOSURE_PATHS = (
    "pyproject.toml",
    "src/us_stocks_swing_model_v2/cli/activate_alpaca_source.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_source_cutover.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_qualification_publisher.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_qualification_readiness.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_feed_qualification_policy.json",
    "config/environment.lock.json",
    "config/sources.json",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_file(path: Path, *, label: str) -> tuple[bytes, dict[str, Any]]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    return raw, value


def _run_git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError(
            "Alpaca source cutover requires a valid committed Git closure"
        ) from exc
    return completed.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    if Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("Alpaca source cutover Git root differs")
    if _run_git(root, "branch", "--show-current") != "main":
        raise IntegrityError("Alpaca source cutover requires main")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("Alpaca source cutover requires a clean committed tree")
    if _run_git(root, "rev-parse", f"{BASE_COMMIT}^{{tree}}") != BASE_TREE:
        raise IntegrityError("Alpaca source cutover base tree differs")
    try:
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError(
            "Alpaca source cutover is not descended from its reviewed base"
        ) from exc
    if int(_run_git(root, "rev-list", "--count", f"{BASE_COMMIT}..HEAD")) != 1:
        raise IntegrityError(
            "Alpaca source cutover requires exactly one implementation commit"
        )
    head = _run_git(root, "rev-parse", "HEAD")
    tree = _run_git(root, "rev-parse", "HEAD^{tree}")
    if any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        for value in (head, tree)
    ):
        raise IntegrityError("Alpaca source cutover Git identity is malformed")
    return {"head": head, "tree": tree}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for relative in paths:
        path = require_contained_path(root / relative, root)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(f"Alpaca source cutover closure is absent: {relative}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def _validate_inactive_source(
    config: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
) -> None:
    baseline = policy["source_config_baseline"]
    if (
        config.get("schema_version") != 1
        or config.get("project") != PROJECT
        or type(config.get("sources")) is not dict
    ):
        raise ContractError("Alpaca source configuration identity differs")
    source = config["sources"].get(baseline["source_key"])
    if (
        type(source) is not dict
        or source.get("enabled_for_active_pipeline") is not False
        or source.get("status") != baseline["status"]
        or source.get("qualification_receipt") is not None
        or type(source.get("request_contract")) is not dict
        or source["request_contract"].get("qualified_feed") is not None
    ):
        raise ContractError("Alpaca source is not in its exact inactive baseline")
    request = source["request_contract"]
    if request != {
        "qualified_feed": None,
        "qualification_candidates": ["sip", "iex"],
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "minimum_end_lag_minutes": 20,
        "sort": "asc",
    }:
        raise ContractError("Alpaca inactive request contract differs")


def _receipt_relative_path(release_id: str, policy: Mapping[str, Any]) -> str:
    return policy["source_cutover"]["qualification_receipt_path_template"].format(
        release_id=release_id
    )


def _activated_config(
    config: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    release_id: str,
) -> dict[str, Any]:
    _validate_inactive_source(config, policy=policy)
    activated = copy.deepcopy(dict(config))
    source_key = policy["source_cutover"]["source_key"]
    source = activated["sources"][source_key]
    mutations = policy["source_cutover"]["mutations"]
    source["enabled_for_active_pipeline"] = mutations["enabled_for_active_pipeline"]
    source["request_contract"]["qualified_feed"] = mutations["qualified_feed"]
    source["qualification_receipt"] = _receipt_relative_path(release_id, policy)
    source["status"] = mutations["status"]
    _validate_activated_source(
        activated,
        baseline=config,
        policy=policy,
        release_id=release_id,
    )
    return activated


def _validate_activated_source(
    config: Mapping[str, Any],
    *,
    baseline: Mapping[str, Any],
    policy: Mapping[str, Any],
    release_id: str,
) -> None:
    expected = copy.deepcopy(dict(baseline))
    source_key = policy["source_cutover"]["source_key"]
    expected_source = expected["sources"][source_key]
    mutations = policy["source_cutover"]["mutations"]
    expected_source["enabled_for_active_pipeline"] = True
    expected_source["request_contract"]["qualified_feed"] = "sip"
    expected_source["qualification_receipt"] = _receipt_relative_path(
        release_id, policy
    )
    expected_source["status"] = mutations["status"]
    if config != expected:
        raise IntegrityError("Alpaca source cutover changed undeclared configuration")
    source = config["sources"][source_key]
    if (
        source["enabled_for_active_pipeline"] is not True
        or source["request_contract"]["qualified_feed"] != "sip"
        or source["status"] != "active_sip_qualified_pending_canonical_bars"
        or source["qualification_receipt"]
        != _receipt_relative_path(release_id, policy)
    ):
        raise IntegrityError("Alpaca source cutover activation fields differ")


def _render_config(value: Mapping[str, Any], *, baseline_raw: bytes) -> bytes:
    newline = "\r\n" if b"\r\n" in baseline_raw else "\n"
    rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    expanded_candidates = (
        '        "qualification_candidates": [\n'
        '          "sip",\n'
        '          "iex"\n'
        "        ],"
    )
    if expanded_candidates not in rendered:
        raise IntegrityError("Alpaca qualification candidate rendering differs")
    rendered = rendered.replace(
        expanded_candidates,
        '        "qualification_candidates": ["sip", "iex"],',
        1,
    )
    return rendered.replace("\n", newline).encode("utf-8")


def _release_binding(
    release_directory: Path,
    *,
    accepted_root: Path,
    synthetic: bool,
    enforce_production_identity: bool,
) -> tuple[dict[str, Any], dict[str, str]]:
    manifest = verify_accepted_release(
        release_directory,
        accepted_root=accepted_root,
    )
    receipt = verify_alpaca_qualification_release(
        release_directory,
        accepted_root=accepted_root,
        synthetic=synthetic,
    )
    manifest_hash = sha256_file(release_directory / "release_manifest.json")
    receipt_hash = sha256_file(release_directory / PAYLOAD_FILENAME)
    if enforce_production_identity and (
        manifest.release_id != QUALIFICATION_RELEASE_ID
        or receipt["receipt_id"] != QUALIFICATION_RECEIPT_ID
        or receipt["publication_plan_id"] != QUALIFICATION_PUBLICATION_PLAN_ID
        or receipt_hash != QUALIFICATION_RECEIPT_FILE_SHA256
        or manifest_hash != QUALIFICATION_MANIFEST_FILE_SHA256
    ):
        raise IntegrityError("Alpaca source cutover release identity differs")
    if (
        receipt["selected_feed"] != "sip"
        or receipt["selection_reason"] != "both_pass_prefer_sip"
        or receipt["authorities"]["source_activation"] is not False
        or receipt["authorities"]["canonical_bars"] is not False
    ):
        raise IntegrityError("Alpaca source cutover receipt boundary differs")
    return receipt, {
        "release_id": manifest.release_id,
        "receipt_id": receipt["receipt_id"],
        "publication_plan_id": receipt["publication_plan_id"],
        "receipt_file_sha256": receipt_hash,
        "manifest_file_sha256": manifest_hash,
    }


def _cutover_plan_from_context(
    *,
    policy: Mapping[str, Any],
    repository: Mapping[str, str],
    release: Mapping[str, str],
    source_config_path: Path,
    baseline_raw: bytes,
    baseline_config: Mapping[str, Any],
    code_closure: Mapping[str, object],
    config_closure: Mapping[str, object],
    environment_id: str,
    synthetic_permit_id: str | None = None,
) -> dict[str, Any]:
    _validate_inactive_source(baseline_config, policy=policy)
    activated = _activated_config(
        baseline_config,
        policy=policy,
        release_id=release["release_id"],
    )
    proposed = _render_config(activated, baseline_raw=baseline_raw)
    relative_receipt = _receipt_relative_path(release["release_id"], policy)
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": (
            "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
            if synthetic_permit_id is not None
            else "ACTIVATE_ONE_VERIFIED_ALPACA_SIP_SOURCE"
        ),
        "publisher_base_commit": BASE_COMMIT,
        "publisher_base_tree": BASE_TREE,
        "cutover_code_commit": repository["head"],
        "cutover_tree": repository["tree"],
        "policy_id": policy["policy_id"],
        "qualification_release": dict(release),
        "source_config_path": str(source_config_path),
        "source_config_before_sha256": sha256_bytes(baseline_raw),
        "source_config_after_sha256": sha256_bytes(proposed),
        "source_config_after_size": len(proposed),
        "qualification_receipt_path": relative_receipt,
        "source_key": policy["source_cutover"]["source_key"],
        "mutations": {
            "enabled_for_active_pipeline": True,
            "qualified_feed": "sip",
            "qualification_receipt": relative_receipt,
            "status": "active_sip_qualified_pending_canonical_bars",
        },
        "code_closure": dict(code_closure),
        "config_closure": dict(config_closure),
        "environment_id": environment_id,
        "activation_count": 1,
        "config_file_mutations": 1,
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "source_activation": False,
            "canonical_bars": False,
            "provider_calls": False,
            "credential_access": False,
            "research": False,
        },
        "stop_conditions": [
            "repository_or_plan_mismatch",
            "accepted_release_or_receipt_mismatch",
            "source_config_drift",
            "atomic_write_failure",
            "post_write_verification_failure",
        ],
    }
    return {
        **unsigned,
        "activation_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_alpaca_source_cutover_plan(
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Build the exact one-file source activation plan without writing."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    repository = _repository_binding(root)
    policy = load_alpaca_feed_qualification_policy(root)
    release_directory = (
        root
        / "data"
        / "vault"
        / "accepted"
        / "alpaca_feed_qualification"
        / QUALIFICATION_RELEASE_ID
    )
    accepted_root = root / "data" / "vault" / "accepted"
    receipt, release = _release_binding(
        release_directory,
        accepted_root=accepted_root,
        synthetic=False,
        enforce_production_identity=True,
    )
    if receipt["policy_id"] != policy["policy_id"]:
        raise IntegrityError("Alpaca source cutover policy differs from receipt")
    receipt_config_hashes = {
        entry["path"]: entry["sha256"]
        for entry in receipt["config_closure"]["files"]
    }
    baseline = policy["source_config_baseline"]
    source_path = require_contained_path(root / baseline["path"], root)
    baseline_raw, baseline_config = _json_file(
        source_path,
        label="Alpaca source configuration",
    )
    if (
        sha256_bytes(baseline_raw) != baseline["file_sha256"]
        or receipt_config_hashes.get(baseline["path"]) != baseline["file_sha256"]
    ):
        raise IntegrityError("Alpaca source baseline differs from publication evidence")
    return _cutover_plan_from_context(
        policy=policy,
        repository=repository,
        release=release,
        source_config_path=source_path,
        baseline_raw=baseline_raw,
        baseline_config=baseline_config,
        code_closure=_closure(root, CODE_CLOSURE_PATHS),
        config_closure=_closure(root, CONFIG_CLOSURE_PATHS),
        environment_id=validate_environment_lock(
            root / "config" / "environment.lock.json"
        ),
    )


@dataclass(frozen=True)
class AlpacaSourceCutover:
    activation_plan_id: str
    release_id: str
    receipt_id: str
    source_config_path: Path
    source_config_sha256: str


def _apply_prevalidated_plan(
    *,
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    source_config_path: Path,
) -> AlpacaSourceCutover:
    raw, baseline = _json_file(
        source_config_path,
        label="Alpaca source configuration",
    )
    if sha256_bytes(raw) != plan["source_config_before_sha256"]:
        raise IntegrityError("Alpaca source configuration changed after planning")
    activated = _activated_config(
        baseline,
        policy=policy,
        release_id=plan["qualification_release"]["release_id"],
    )
    proposed = _render_config(activated, baseline_raw=raw)
    if (
        sha256_bytes(proposed) != plan["source_config_after_sha256"]
        or len(proposed) != plan["source_config_after_size"]
    ):
        raise IntegrityError("Alpaca source cutover proposed bytes changed")
    atomic_write(source_config_path, proposed)
    written, loaded = _json_file(
        source_config_path,
        label="activated Alpaca source configuration",
    )
    if written != proposed:
        raise IntegrityError("Alpaca source cutover written bytes differ")
    _validate_activated_source(
        loaded,
        baseline=baseline,
        policy=policy,
        release_id=plan["qualification_release"]["release_id"],
    )
    return AlpacaSourceCutover(
        activation_plan_id=str(plan["activation_plan_id"]),
        release_id=str(plan["qualification_release"]["release_id"]),
        receipt_id=str(plan["qualification_release"]["receipt_id"]),
        source_config_path=source_config_path,
        source_config_sha256=sha256_bytes(written),
    )


def _require_expected_activation_worktree(root: Path) -> None:
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all") != (
        "M config/sources.json"
    ):
        raise IntegrityError("Alpaca source cutover produced an unexpected worktree")


def activate_alpaca_source(
    *,
    approved_plan_id: str,
    owner_confirmation: str,
    repo_root: Path | None = None,
) -> AlpacaSourceCutover:
    """Apply one separately approved exact source activation plan."""

    require_sha256(approved_plan_id, "approved Alpaca activation plan ID")
    if owner_confirmation != ACTIVATION_CONFIRMATION_VALUE:
        raise PermissionError("Alpaca source activation owner confirmation differs")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    plan = build_alpaca_source_cutover_plan(repo_root=root)
    if plan["activation_plan_id"] != approved_plan_id:
        raise PermissionError("approved Alpaca activation plan ID differs")
    policy = load_alpaca_feed_qualification_policy(root)
    result = _apply_prevalidated_plan(
        plan=plan,
        policy=policy,
        source_config_path=Path(plan["source_config_path"]),
    )
    _require_expected_activation_worktree(root)
    return result


def build_alpaca_source_cutover_fixture_plan(
    *,
    source_config_path: Path,
    release_directory: Path,
    accepted_root: Path,
    permit: SyntheticOnlyPermit,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_alpaca_feed_qualification_policy(root)
    receipt, release = _release_binding(
        Path(release_directory),
        accepted_root=Path(accepted_root),
        synthetic=True,
        enforce_production_identity=False,
    )
    if receipt["policy_id"] != policy["policy_id"]:
        raise IntegrityError("synthetic Alpaca cutover policy differs")
    raw, config = _json_file(
        Path(source_config_path),
        label="synthetic Alpaca source configuration",
    )
    if sha256_bytes(raw) != policy["source_config_baseline"]["file_sha256"]:
        raise IntegrityError("synthetic Alpaca cutover requires the exact source baseline")
    code_entries = [{"path": "synthetic/cutover.py", "sha256": "a" * 64}]
    config_entries = [{"path": "synthetic/sources.json", "sha256": sha256_bytes(raw)}]
    return _cutover_plan_from_context(
        policy=policy,
        repository={"head": "b" * 40, "tree": "c" * 40},
        release=release,
        source_config_path=Path(source_config_path),
        baseline_raw=raw,
        baseline_config=config,
        code_closure={
            "files": code_entries,
            "closure_sha256": sha256_bytes(canonical_json_bytes(code_entries)),
        },
        config_closure={
            "files": config_entries,
            "closure_sha256": sha256_bytes(canonical_json_bytes(config_entries)),
        },
        environment_id="d" * 64,
        synthetic_permit_id=verified.permit_id,
    )


def apply_alpaca_source_cutover_fixture(
    *,
    plan: Mapping[str, Any],
    source_config_path: Path,
    release_directory: Path,
    accepted_root: Path,
    permit: SyntheticOnlyPermit,
    repo_root: Path | None = None,
) -> AlpacaSourceCutover:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    if (
        plan.get("mode") != "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
        or plan.get("synthetic_permit_id") != verified.permit_id
    ):
        raise ContractError("synthetic Alpaca cutover plan differs from its permit")
    config_path = Path(source_config_path)
    accepted = Path(accepted_root)
    if config_path.parent.parent.resolve() != accepted.parent.resolve():
        raise ContractError("synthetic Alpaca cutover requires one fixture root")
    receipt, release = _release_binding(
        Path(release_directory),
        accepted_root=accepted,
        synthetic=True,
        enforce_production_identity=False,
    )
    if (
        release != plan["qualification_release"]
        or receipt["publication_plan_id"] != release["publication_plan_id"]
    ):
        raise IntegrityError("synthetic Alpaca cutover release changed after planning")
    policy = load_alpaca_feed_qualification_policy(
        Path(repo_root or _repo_root()).resolve(strict=True)
    )
    return _apply_prevalidated_plan(
        plan=plan,
        policy=policy,
        source_config_path=config_path,
    )
