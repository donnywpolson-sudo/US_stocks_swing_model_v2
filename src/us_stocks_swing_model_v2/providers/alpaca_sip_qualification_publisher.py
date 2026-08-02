"""Plan-first publication of one non-active Alpaca SIP qualification receipt."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from ..clock import TrustedClock, require_trusted_clock
from ..common import assert_exact_tree, atomic_write, canonical_json_bytes, iso_z, require_sha256, sha256_bytes, sha256_file
from ..errors import ContractError, IntegrityError
from ..releases import AtomicReleasePublisher, build_manifest, verify_accepted_release
from .alpaca_sip_single_feed_qualification import build_qualification_plan, verify_qualification_snapshot


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = "config/alpaca_sip_qualification_receipt_publication_policy.json"
DATASET = "alpaca_feed_qualification"
PAYLOAD = "alpaca_feed_qualification_receipt.json"
CONFIRMATION_TOKEN = "ALPACA_SIP_QUALIFICATION_RECEIPT_PUBLICATION_APPROVED"
CONFIRMATION_VALUE = "YES"
CODE_PATHS = (
    "src/us_stocks_swing_model_v2/providers/alpaca_sip_qualification_publisher.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_sip_single_feed_qualification.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/releases.py",
    "src/us_stocks_swing_model_v2/cli/publish_alpaca_sip_qualification_receipt.py",
)
CONFIG_PATHS = (POLICY_PATH, "config/sources.json", "config/network_acquisition_registry.json", "config/environment.lock.json", "config/alpaca_sip_single_feed_qualification_policy.json")


def _root() -> Path:
    return Path(__file__).resolve().parents[3]


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError("qualification receipt publication requires a valid Git closure") from exc
    return result.stdout.strip()


def _repository(root: Path) -> dict[str, str]:
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root or _git(root, "branch", "--show-current") != "main":
        raise IntegrityError("qualification receipt publication requires the exact main repository")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("qualification receipt publication requires a clean committed closure")
    return {"commit": _git(root, "rev-parse", "HEAD"), "tree": _git(root, "rev-parse", "HEAD^{tree}")}


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise IntegrityError(f"{label} must be an object")
    return value


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    files = [{"path": item, "sha256": sha256_file(root / item)} for item in sorted(paths)]
    return {"files": files, "closure_sha256": sha256_bytes(canonical_json_bytes(files))}


def load_policy(root: Path) -> tuple[dict[str, Any], str]:
    policy = _json(root / POLICY_PATH, "SIP qualification publication policy")
    expected = {"schema_version", "project", "policy_type", "qualification_plan_id", "assessment_id", "snapshot_id", "raw_sha256", "calendar_release_id", "snapshot_directory", "outputs", "authorities"}
    outputs = {"accepted_root": "data/vault/accepted", "work_root": "data/w/alpaca_feed_qualification", "dataset": DATASET, "payload_filename": PAYLOAD}
    authorities = {"receipt_publication": False, "source_activation": False, "config_sources_mutation": False, "network_calls": False, "canonical_bars": False, "training_or_evaluation": False}
    if set(policy) != expected or policy.get("schema_version") != 1 or policy.get("project") != PROJECT or policy.get("policy_type") != "ALPACA_SIP_QUALIFICATION_RECEIPT_PUBLICATION" or policy.get("outputs") != outputs or policy.get("authorities") != authorities:
        raise ContractError("SIP qualification publication policy differs")
    for name in ("qualification_plan_id", "assessment_id", "snapshot_id", "raw_sha256", "calendar_release_id"):
        require_sha256(policy[name], name)
    if type(policy["snapshot_directory"]) is not str:
        raise ContractError("qualification publication snapshot path differs")
    return policy, sha256_file(root / POLICY_PATH)


def _source_is_pending(root: Path) -> None:
    source = _json(root / "config/sources.json", "sources policy").get("sources", {}).get("alpaca_basic_delayed_sip")
    if not isinstance(source, dict) or source.get("enabled_for_active_pipeline") is not False or source.get("status") != "pending_single_sip_requalification" or source.get("qualification_receipt") is not None or source.get("request_contract", {}).get("qualified_feed") is not None:
        raise IntegrityError("SIP qualification receipt publication may not alter or bypass pending source state")


def _assessment(root: Path, policy: Mapping[str, Any]) -> dict[str, object]:
    qualification = build_qualification_plan(repo_root=root, clock=TrustedClock.production())
    if qualification["qualification_plan_id"] != policy["qualification_plan_id"]:
        raise IntegrityError("qualification plan differs from receipt-publication policy")
    assessment = verify_qualification_snapshot(snapshot_directory=root / str(policy["snapshot_directory"]), plan=qualification, repo_root=root)
    if assessment.get("assessment_id") != policy["assessment_id"] or assessment.get("qualification_plan_id") != policy["qualification_plan_id"]:
        raise IntegrityError("recomputed SIP assessment differs")
    snapshot = assessment.get("snapshot")
    qualification_result = assessment.get("qualification")
    if not isinstance(snapshot, dict) or not isinstance(qualification_result, dict) or snapshot.get("snapshot_id") != policy["snapshot_id"] or snapshot.get("raw_sha256") != policy["raw_sha256"] or qualification_result.get("calendar_release_id") != policy["calendar_release_id"] or qualification_result.get("state") != "PASS" or qualification_result.get("bar_count") != 10 or assessment.get("activation_authorized") is not False:
        raise IntegrityError("SIP qualification evidence is not the exact non-active PASS assessment")
    return assessment


def build_publication_plan(*, repo_root: Path | None = None) -> dict[str, object]:
    root = (repo_root or _root()).resolve(strict=True)
    policy, policy_hash = load_policy(root)
    repository = _repository(root)
    _source_is_pending(root)
    assessment = _assessment(root, policy)
    accepted = (root / policy["outputs"]["accepted_root"]).resolve(strict=True)
    work = (root / policy["outputs"]["work_root"]).resolve(strict=False)
    unsigned = {"schema_version": 1, "project": PROJECT, "mode": "ALPACA_SIP_QUALIFICATION_RECEIPT_PUBLICATION_PLAN_ONLY", "repository": repository, "policy_sha256": policy_hash, "qualification_plan_id": policy["qualification_plan_id"], "assessment_id": policy["assessment_id"], "snapshot_id": policy["snapshot_id"], "raw_sha256": policy["raw_sha256"], "calendar_release_id": policy["calendar_release_id"], "assessment": assessment, "accepted_root": str(accepted), "work_root": str(work), "dataset": DATASET, "payload_filename": PAYLOAD, "publication_count": 1, "network_calls": 0, "source_activation": False, "config_sources_mutation": False, "training_or_evaluation": False, "code_closure": _closure(root, CODE_PATHS), "config_closure": _closure(root, CONFIG_PATHS)}
    return {**unsigned, "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_plan(plan: Mapping[str, object]) -> str:
    plan_id = plan.get("publication_plan_id")
    require_sha256(plan_id, "qualification receipt publication plan ID")
    unsigned = {key: value for key, value in plan.items() if key != "publication_plan_id"}
    if sha256_bytes(canonical_json_bytes(unsigned)) != plan_id or plan.get("mode") != "ALPACA_SIP_QUALIFICATION_RECEIPT_PUBLICATION_PLAN_ONLY" or plan.get("dataset") != DATASET or plan.get("source_activation") is not False or plan.get("config_sources_mutation") is not False or plan.get("network_calls") != 0:
        raise IntegrityError("qualification receipt publication plan differs")
    return str(plan_id)


def _receipt(plan: Mapping[str, object], created_at: str) -> dict[str, object]:
    unsigned = {"schema_version": 1, "project": PROJECT, "receipt_class": "ALPACA_SIP_SINGLE_FEED_QUALIFICATION", "status": "SIP_PASS_RECEIPT_PUBLISHED_NOT_ACTIVE", "created_at": created_at, "publication_plan_id": plan["publication_plan_id"], "qualification_plan_id": plan["qualification_plan_id"], "assessment_id": plan["assessment_id"], "snapshot_id": plan["snapshot_id"], "raw_sha256": plan["raw_sha256"], "calendar_release_id": plan["calendar_release_id"], "selected_feed_candidate": "sip", "activation_authorized": False, "authorities": {"receipt_publication": True, "source_activation": False, "config_sources_mutation": False, "network_calls": False, "canonical_bars": False, "training_or_evaluation": False}, "prohibitions": ["source_activation", "config_sources_mutation", "network_calls", "canonical_bars", "training_or_evaluation"]}
    return {**unsigned, "receipt_id": sha256_bytes(canonical_json_bytes(unsigned))}


def publish_receipt(*, approved_plan_id: str, owner_confirmation: str, clock: TrustedClock, repo_root: Path | None = None) -> dict[str, object]:
    if owner_confirmation != CONFIRMATION_VALUE:
        raise PermissionError("qualification receipt publication confirmation differs")
    root = (repo_root or _root()).resolve(strict=True)
    plan = build_publication_plan(repo_root=root)
    if _validate_plan(plan) != approved_plan_id:
        raise PermissionError("approved qualification receipt publication plan differs")
    trusted = require_trusted_clock(clock)
    if not trusted.trust_eligible:
        raise PermissionError("qualification receipt publication requires production UTC")
    receipt = _receipt(plan, iso_z(trusted.now()))
    work = Path(str(plan["work_root"])) / approved_plan_id[:20]
    stage = work / "stage"
    stage.mkdir(parents=True, exist_ok=True)
    payload_path = stage / PAYLOAD
    payload = canonical_json_bytes(receipt)
    if payload_path.exists() and payload_path.read_bytes() != payload:
        raise IntegrityError("qualification receipt stage differs")
    if not payload_path.exists():
        atomic_write(payload_path, payload)
    assert_exact_tree(stage, {PAYLOAD}, set())
    manifest = build_manifest(stage, (PAYLOAD,), project=PROJECT, dataset=DATASET, source_epoch="alpaca_sip_qualification_v1", role="qualification_evidence_only", quality_state="QUALIFICATION_EVIDENCE", created_at=receipt["created_at"], row_count=10, event_start="2026-07-27", event_end="2026-07-31", upstream_release_ids=(str(plan["calendar_release_id"]),), schema_fingerprint=sha256_bytes(canonical_json_bytes(sorted(receipt))), code_hash=str(plan["code_closure"]["closure_sha256"]), config_hash=str(plan["config_closure"]["closure_sha256"]), environment_hash=sha256_file(root / "config/environment.lock.json"))
    destination = AtomicReleasePublisher(Path(str(plan["accepted_root"]))).publish(stage, manifest)
    verify_accepted_release(destination, accepted_root=Path(str(plan["accepted_root"])), expected=manifest)
    _source_is_pending(root)
    return {"publication_plan_id": approved_plan_id, "receipt_id": receipt["receipt_id"], "release_id": manifest.release_id, "release_directory": str(destination), "source_activation": False}
