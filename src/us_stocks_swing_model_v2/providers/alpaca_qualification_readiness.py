from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ..common import (
    canonical_json_bytes,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, IntegrityError
from .alpaca import AlpacaBarsRequest, assess_landed_alpaca_pair
from .snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry


PROJECT = "US_stocks_swing_model_v2"
POLICY_PATH = Path("config/alpaca_feed_qualification_policy.json")
POLICY_TYPE = "ALPACA_FEED_QUALIFICATION_RECEIPT_AND_CUTOVER_DESIGN"
EXPECTED_ASSESSMENT_ID = (
    "3789bb3002d89dcab395a1a4ba6243af028926c37798645069a789a7869ff9e1"
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _json_object(path: Path, *, label: str) -> dict[str, Any]:
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise IntegrityError(f"{label} must be an independent plain file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(payload) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    return payload


def _validate_policy_shape(policy: Mapping[str, Any]) -> None:
    expected_fields = {
        "schema_version",
        "project",
        "policy_type",
        "assessment",
        "window",
        "request_contract",
        "network_registry",
        "calendar",
        "snapshots",
        "source_config_baseline",
        "receipt_publication",
        "source_cutover",
        "prohibitions",
        "policy_id",
    }
    if set(policy) != expected_fields:
        raise ContractError("Alpaca qualification policy fields differ")
    if (
        type(policy["schema_version"]) is not int
        or policy["schema_version"] != 1
        or policy["project"] != PROJECT
        or policy["policy_type"] != POLICY_TYPE
    ):
        raise ContractError("Alpaca qualification policy identity differs")
    assessment = policy["assessment"]
    if (
        type(assessment) is not dict
        or set(assessment)
        != {
            "assessment_id",
            "selected_feed_candidate",
            "selection_reason",
            "activation_authorized",
        }
        or assessment["assessment_id"] != EXPECTED_ASSESSMENT_ID
        or assessment["selected_feed_candidate"] != "sip"
        or assessment["selection_reason"] != "both_pass_prefer_sip"
        or assessment["activation_authorized"] is not False
    ):
        raise ContractError("Alpaca qualification assessment binding differs")
    window = policy["window"]
    if window != {
        "symbols": ["AAPL", "SPY"],
        "start": "2026-07-23T04:00:00Z",
        "end": "2026-07-30T03:59:59Z",
        "sessions": [
            "2026-07-23",
            "2026-07-24",
            "2026-07-27",
            "2026-07-28",
            "2026-07-29",
        ],
    }:
        raise ContractError("Alpaca qualification window differs")
    if policy["request_contract"] != {
        "endpoint": "https://data.alpaca.markets/v2/stocks/bars",
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "sort": "asc",
        "limit": 10000,
        "max_pages": 1,
        "minimum_end_lag_minutes": 20,
    }:
        raise ContractError("Alpaca qualification request contract differs")
    registry = policy["network_registry"]
    if (
        type(registry) is not dict
        or set(registry) != {"path", "registry_id", "file_sha256"}
        or registry["path"] != "config/network_acquisition_registry.json"
    ):
        raise ContractError("Alpaca qualification registry binding differs")
    calendar = policy["calendar"]
    if (
        type(calendar) is not dict
        or set(calendar) != {"release_id", "relative_directory", "manifest_sha256"}
    ):
        raise ContractError("Alpaca qualification calendar binding differs")
    for name in ("registry_id", "file_sha256"):
        require_sha256(registry[name], f"Alpaca policy network_registry.{name}")
    for name in ("release_id", "manifest_sha256"):
        require_sha256(calendar[name], f"Alpaca policy calendar.{name}")
    snapshots = policy["snapshots"]
    if type(snapshots) is not dict or set(snapshots) != {"sip", "iex"}:
        raise ContractError("Alpaca qualification snapshot census differs")
    for feed, snapshot in snapshots.items():
        if (
            type(snapshot) is not dict
            or set(snapshot)
            != {
                "snapshot_id",
                "raw_sha256",
                "receipt_file_sha256",
                "relative_directory",
                "bar_count",
            }
            or type(snapshot["bar_count"]) is not int
            or snapshot["bar_count"] != 10
            or f"alpaca_{feed}_qualification" not in snapshot["relative_directory"]
        ):
            raise ContractError(f"Alpaca {feed} snapshot binding differs")
        for name in ("snapshot_id", "raw_sha256", "receipt_file_sha256"):
            require_sha256(snapshot[name], f"Alpaca policy {feed}.{name}")
    baseline = policy["source_config_baseline"]
    if (
        type(baseline) is not dict
        or set(baseline)
        != {
            "path",
            "file_sha256",
            "source_key",
            "enabled_for_active_pipeline",
            "qualified_feed",
            "status",
        }
        or baseline.get("path") != "config/sources.json"
        or baseline.get("source_key") != "alpaca_basic_delayed_sip"
        or baseline.get("enabled_for_active_pipeline") is not False
        or baseline.get("qualified_feed") is not None
        or baseline.get("status")
        != "empty_pending_bounded_sip_vs_iex_qualification"
    ):
        raise ContractError("Alpaca source baseline differs")
    require_sha256(baseline["file_sha256"], "Alpaca policy source baseline hash")
    publication = policy["receipt_publication"]
    if (
        type(publication) is not dict
        or set(publication)
        != {
            "accepted_root",
            "work_root",
            "dataset",
            "source_epoch",
            "role",
            "quality_state",
            "payload_filename",
            "receipt_class",
            "status",
            "provenance",
            "receipt_fields",
            "publication_count",
            "source_active",
            "network_calls",
        }
        or publication.get("accepted_root") != "data/vault/accepted"
        or publication.get("work_root") != "data/w/alpaca_feed_qualification"
        or publication.get("dataset") != "alpaca_feed_qualification"
        or publication.get("source_epoch") != "alpaca_basic_sip_20260723_20260729"
        or publication.get("role") != "qualification_evidence_only"
        or publication.get("quality_state") != "QUALIFICATION_EVIDENCE"
        or publication.get("payload_filename")
        != "alpaca_feed_qualification_receipt.json"
        or publication.get("receipt_class")
        != "ALPACA_SIP_IEX_FEED_QUALIFICATION"
        or publication.get("status") != "PASS_SELECTED_SIP_NOT_ACTIVE"
        or publication.get("provenance")
        != "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        or publication.get("receipt_fields")
        != [
            "schema_version",
            "project",
            "receipt_class",
            "status",
            "created_at",
            "publication_plan_id",
            "policy_id",
            "assessment_id",
            "selected_feed",
            "selection_reason",
            "window",
            "request_contract",
            "network_registry_id",
            "calendar_release_id",
            "snapshots",
            "qualifications",
            "code_closure",
            "config_closure",
            "environment_id",
            "provenance",
            "authorities",
            "prohibitions",
            "receipt_id",
        ]
        or publication.get("publication_count") != 1
        or publication.get("source_active") is not False
        or publication.get("network_calls") != 0
    ):
        raise ContractError("Alpaca receipt publication contract differs")
    cutover = policy["source_cutover"]
    if (
        type(cutover) is not dict
        or set(cutover)
        != {
            "config_path",
            "source_key",
            "requires_verified_accepted_release",
            "qualification_receipt_field",
            "qualification_receipt_path_template",
            "mutations",
            "preserve_request_contract",
            "activation_requires_separate_authorization",
            "canonical_bars_authorized",
            "research_authorized",
        }
        or cutover.get("config_path") != "config/sources.json"
        or cutover.get("source_key") != "alpaca_basic_delayed_sip"
        or cutover.get("requires_verified_accepted_release") is not True
        or cutover.get("qualification_receipt_field") != "qualification_receipt"
        or cutover.get("qualification_receipt_path_template")
        != "data/vault/accepted/alpaca_feed_qualification/{release_id}/alpaca_feed_qualification_receipt.json"
        or cutover.get("preserve_request_contract") is not True
        or cutover.get("activation_requires_separate_authorization") is not True
        or cutover.get("canonical_bars_authorized") is not False
        or cutover.get("research_authorized") is not False
        or cutover.get("mutations")
        != {
            "enabled_for_active_pipeline": True,
            "qualified_feed": "sip",
            "status": "active_sip_qualified_pending_canonical_bars",
        }
    ):
        raise ContractError("Alpaca source cutover contract differs")
    if policy["prohibitions"] != [
        "provider_call",
        "credential_access",
        "receipt_publication",
        "source_activation",
        "canonical_bars",
        "research_execution",
        "audit_execution",
    ]:
        raise ContractError("Alpaca qualification prohibitions differ")
    require_sha256(policy["policy_id"], "Alpaca qualification policy_id")


def load_alpaca_feed_qualification_policy(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = require_contained_path(root / POLICY_PATH, root)
    policy = _json_object(path, label="Alpaca feed qualification policy")
    _validate_policy_shape(policy)
    unsigned = {key: value for key, value in policy.items() if key != "policy_id"}
    if policy["policy_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("Alpaca qualification policy ID differs")
    return policy


def _repository_binding(root: Path) -> dict[str, str]:
    commands = {
        "root": ["git", "rev-parse", "--show-toplevel"],
        "branch": ["git", "branch", "--show-current"],
        "head": ["git", "rev-parse", "HEAD"],
        "tree": ["git", "rev-parse", "HEAD^{tree}"],
        "status": ["git", "status", "--porcelain"],
    }
    values = {
        name: subprocess.run(
            argv,
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        for name, argv in commands.items()
    }
    if Path(values["root"]).resolve() != root or values["branch"] != "main":
        raise ContractError("Alpaca cutover design repository identity differs")
    if values["status"]:
        raise ContractError("Alpaca cutover design requires a clean repository")
    return {"head": values["head"], "tree": values["tree"]}


def _design_from_context(
    *,
    policy: Mapping[str, Any],
    assessment: Mapping[str, Any],
    repository: Mapping[str, str],
) -> dict[str, Any]:
    if (
        assessment.get("assessment_id") != policy["assessment"]["assessment_id"]
        or assessment.get("selected_feed_candidate") != "sip"
        or assessment.get("selection_reason") != "both_pass_prefer_sip"
        or assessment.get("activation_authorized") is not False
    ):
        raise ContractError("Alpaca assessment does not satisfy the cutover policy")
    unsigned = {
        "schema_version": 1,
        "mode": "ALPACA_QUALIFICATION_RECEIPT_AND_CUTOVER_PLAN_ONLY_NO_WRITES",
        "policy_id": policy["policy_id"],
        "assessment_id": assessment["assessment_id"],
        "repository": dict(repository),
        "selected_feed_candidate": "sip",
        "receipt_publication": policy["receipt_publication"],
        "source_cutover": policy["source_cutover"],
        "authorities": {
            "receipt_publication": False,
            "source_activation": False,
            "canonical_bars": False,
            "network_calls": False,
            "credential_access": False,
            "research": False,
        },
    }
    return {**unsigned, "design_id": sha256_bytes(canonical_json_bytes(unsigned))}


def build_alpaca_feed_cutover_design(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_alpaca_feed_qualification_policy(root)
    baseline = policy["source_config_baseline"]
    source_path = require_contained_path(root / baseline["path"], root)
    if sha256_file(source_path) != baseline["file_sha256"]:
        raise IntegrityError("Alpaca source configuration differs from its baseline")
    source_config = _json_object(source_path, label="source configuration")
    source = source_config.get("sources", {}).get(baseline["source_key"])
    if (
        type(source) is not dict
        or source.get("enabled_for_active_pipeline") is not False
        or source.get("request_contract", {}).get("qualified_feed") is not None
        or source.get("status") != baseline["status"]
    ):
        raise ContractError("Alpaca source is not in the expected non-active state")

    registry_binding = policy["network_registry"]
    registry_path = require_contained_path(root / registry_binding["path"], root)
    if sha256_file(registry_path) != registry_binding["file_sha256"]:
        raise IntegrityError("Alpaca network registry file differs")
    registry = NetworkAcquisitionRegistry.load(
        registry_path,
        allowed_root=root / "config",
    )
    if registry.registry_id != registry_binding["registry_id"]:
        raise IntegrityError("Alpaca network registry ID differs")

    allowed_root = (root / "data" / "vault").resolve()
    store = AsReceivedSnapshotStore(
        allowed_root / "qualification" / "as_received",
        allowed_root=allowed_root,
        acquisition_registry=registry,
    )
    loaded = {}
    for feed, binding in policy["snapshots"].items():
        directory = require_contained_path(root / binding["relative_directory"], root)
        snapshot = store.load(directory)
        if (
            snapshot.snapshot_id != binding["snapshot_id"]
            or snapshot.raw_sha256 != binding["raw_sha256"]
            or sha256_file(directory / "receipt.json")
            != binding["receipt_file_sha256"]
        ):
            raise IntegrityError(f"Alpaca {feed} snapshot identity differs")
        loaded[feed] = snapshot

    calendar = policy["calendar"]
    calendar_directory = require_contained_path(
        root / calendar["relative_directory"],
        root,
    )
    if (
        sha256_file(calendar_directory / "release_manifest.json")
        != calendar["manifest_sha256"]
    ):
        raise IntegrityError("Alpaca qualification calendar manifest differs")
    window = policy["window"]
    requested_at = min(snapshot.retrieved_at for snapshot in loaded.values())
    request = AlpacaBarsRequest(
        symbols=tuple(window["symbols"]),
        start=datetime.fromisoformat(window["start"].replace("Z", "+00:00")),
        end=datetime.fromisoformat(window["end"].replace("Z", "+00:00")),
        requested_at=requested_at,
    )
    assessment = assess_landed_alpaca_pair(
        request,
        sip_snapshot=loaded["sip"],
        iex_snapshot=loaded["iex"],
        network_registry_id=registry.registry_id,
        calendar_release_directory=calendar_directory,
        accepted_release_root=allowed_root / "accepted",
    )
    return _design_from_context(
        policy=policy,
        assessment=assessment,
        repository=_repository_binding(root),
    )
