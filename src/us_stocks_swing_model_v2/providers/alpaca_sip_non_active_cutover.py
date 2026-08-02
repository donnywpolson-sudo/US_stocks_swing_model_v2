"""Plan-first non-active source cutover; execution never enables the pipeline."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any, Mapping

from ..common import atomic_write, canonical_json_bytes, require_sha256, sha256_bytes, sha256_file
from ..errors import ContractError, IntegrityError
from ..releases import verify_accepted_release

POLICY_PATH = "config/alpaca_sip_non_active_cutover_policy.json"
CONFIRMATION_TOKEN = "ALPACA_SIP_NON_ACTIVE_CUTOVER_APPROVED"
CONFIRMATION_VALUE = "YES"


def _root() -> Path: return Path(__file__).resolve().parents[3]
def _json(path: Path) -> dict[str, Any]:
    try: value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise IntegrityError("cutover JSON is unreadable") from exc
    if type(value) is not dict: raise IntegrityError("cutover JSON must be an object")
    return value
def _git(root: Path, *args: str) -> str:
    try: return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc: raise IntegrityError("non-active SIP cutover requires a clean Git closure") from exc
def _repository(root: Path) -> dict[str, str]:
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root or _git(root, "branch", "--show-current") != "main" or _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("non-active SIP cutover requires the exact clean main repository")
    return {"commit": _git(root, "rev-parse", "HEAD"), "tree": _git(root, "rev-parse", "HEAD^{tree}")}
def load_policy(root: Path) -> dict[str, Any]:
    value = _json(root / POLICY_PATH)
    expected={"schema_version","project","policy_type","receipt_release_id","receipt_id","receipt_relative_path","identity_release_id","identity_snapshot_id","calendar_release_id","sources_path","target"}
    if set(value)!=expected or value.get("schema_version")!=1 or value.get("project")!="US_stocks_swing_model_v2" or value.get("policy_type")!="ALPACA_SIP_NON_ACTIVE_SOURCE_CUTOVER" or value.get("sources_path")!="config/sources.json" or value.get("target")!={"qualified_feed":"sip","status":"qualified_sip_not_active","enabled_for_active_pipeline":False}: raise ContractError("non-active SIP cutover policy differs")
    for key in ("receipt_release_id","receipt_id","identity_release_id","identity_snapshot_id","calendar_release_id"): require_sha256(value[key], key)
    return value
def _target_sources(root: Path, policy: Mapping[str, Any]) -> tuple[dict[str, Any], bytes]:
    value=_json(root / str(policy["sources_path"])); source=value.get("sources",{}).get("alpaca_basic_delayed_sip")
    if not isinstance(source,dict) or source.get("status")!="pending_single_sip_requalification" or source.get("qualification_receipt") is not None or source.get("enabled_for_active_pipeline") is not False: raise IntegrityError("source is not pending the exact non-active cutover")
    source["request_contract"]["qualified_feed"]="sip"; source["qualification_receipt"]=policy["receipt_relative_path"]; source["status"]="qualified_sip_not_active"; source["enabled_for_active_pipeline"]=False
    return value, (json.dumps(value, indent=2)+"\n").encode()
def build_cutover_plan(*, repo_root: Path | None=None) -> dict[str, object]:
    root=(repo_root or _root()).resolve(strict=True); policy=load_policy(root); repository=_repository(root)
    receipt=root / str(policy["receipt_relative_path"]); manifest=verify_accepted_release(receipt.parent, accepted_root=root / "data/vault/accepted")
    payload=_json(receipt)
    if manifest.release_id!=policy["receipt_release_id"] or payload.get("receipt_id")!=policy["receipt_id"] or payload.get("activation_authorized") is not False: raise IntegrityError("qualification receipt differs or authorizes activation")
    _, target=_target_sources(root,policy); old=root / str(policy["sources_path"])
    unsigned={"schema_version":1,"mode":"ALPACA_SIP_NON_ACTIVE_CUTOVER_PLAN_ONLY","repository":repository,"policy_sha256":sha256_file(root/POLICY_PATH),"sources_path":str(policy["sources_path"]),"sources_before_sha256":sha256_file(old),"sources_after_sha256":sha256_bytes(target),"receipt_release_id":policy["receipt_release_id"],"receipt_id":policy["receipt_id"],"identity_release_id":policy["identity_release_id"],"identity_snapshot_id":policy["identity_snapshot_id"],"calendar_release_id":policy["calendar_release_id"],"source_activation":False,"enabled_for_active_pipeline":False,"network_calls":0}
    return {**unsigned,"cutover_plan_id":sha256_bytes(canonical_json_bytes(unsigned))}
def execute_cutover(*, approved_plan_id: str, owner_confirmation: str, repo_root: Path | None=None) -> dict[str, object]:
    if owner_confirmation!=CONFIRMATION_VALUE: raise PermissionError("non-active SIP cutover confirmation differs")
    root=(repo_root or _root()).resolve(strict=True); plan=build_cutover_plan(repo_root=root)
    if plan["cutover_plan_id"]!=approved_plan_id: raise PermissionError("approved non-active SIP cutover plan differs")
    policy=load_policy(root); _, target=_target_sources(root,policy); path=root / str(policy["sources_path"])
    atomic_write(path,target)
    if sha256_file(path)!=plan["sources_after_sha256"]: raise IntegrityError("non-active source cutover write differs")
    return {"cutover_plan_id":approved_plan_id,"sources_sha256":plan["sources_after_sha256"],"source_activation":False,"enabled_for_active_pipeline":False}
