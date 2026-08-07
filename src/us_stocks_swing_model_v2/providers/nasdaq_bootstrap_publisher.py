from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..clock import TrustedClock, require_trusted_clock
from ..common import (
    assert_exact_tree,
    atomic_write,
    canonical_json_bytes,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, EvaluationAuthorizationError, IntegrityError
from ..governance import LocalIntegrityRecord, create_local_integrity_record
from ..releases import (
    AtomicReleasePublisher,
    build_manifest,
    verify_accepted_release,
)
from .nasdaq_bootstrap import (
    PROJECT,
    load_nasdaq_bootstrap_policy,
    verify_nasdaq_bootstrap_pair,
)
from .snapshots import AsReceivedSnapshotStore, NetworkAcquisitionRegistry


PUBLICATION_POLICY_PATH = Path("config/nasdaq_bootstrap_publication_policy.json")
PUBLICATION_CONFIRMATION_TOKEN = "NASDAQ_BOOTSTRAP_PUBLICATION_APPROVED"
PUBLICATION_CONFIRMATION_VALUE = "YES"
PUBLICATION_SCOPE = "NASDAQ_BOOTSTRAP_BASELINE_RECEIPT_PUBLICATION"
DATASET = "nasdaq_bootstrap_baseline"
PAYLOAD_FILENAME = "nasdaq_bootstrap_receipt.json"
RECEIPT_CLASS = "NASDAQ_TWO_CAPTURE_BOOTSTRAP_BASELINE"
PRODUCTION_STATUS = "PASS_BOOTSTRAP_BASELINE_PUBLISHED_NOT_ACTIVE"
SYNTHETIC_STATUS = "SYNTHETIC_BOOTSTRAP_PUBLICATION_MECHANICS_ONLY"
SOURCE_EPOCH = "nasdaqtraded_two_capture_bootstrap_20260728"
ROLE = "qualification_evidence_only"
QUALITY_STATE = "QUALIFICATION_EVIDENCE"
FIXTURE_SCOPE = "NASDAQ_BOOTSTRAP_PUBLICATION_FIXTURE"
RECEIPT_FIELDS = {
    "schema_version",
    "project",
    "receipt_class",
    "status",
    "created_at",
    "publication_plan_id",
    "assessment_id",
    "bootstrap_policy_id",
    "assessment_code_commit",
    "publisher_code_commit",
    "environment_id",
    "network_registry_id",
    "snapshot_a",
    "snapshot_b",
    "baseline",
    "preserved_historical_comparison",
    "provenance",
    "local_integrity_record",
    "authorities",
    "prohibitions",
    "receipt_id",
}
PROHIBITIONS = (
    "network_capture",
    "source_activation",
    "historical_membership_backfill",
    "historical_receipt_relabel",
    "model_fit",
    "research_execution",
)
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/cli/publish_nasdaq_bootstrap.py",
    "src/us_stocks_swing_model_v2/providers/nasdaq.py",
    "src/us_stocks_swing_model_v2/providers/nasdaq_bootstrap.py",
    "src/us_stocks_swing_model_v2/providers/nasdaq_bootstrap_publisher.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/governance.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/environment.lock.json",
    "config/nasdaq_bootstrap_policy.json",
    "config/nasdaq_bootstrap_publication_policy.json",
    "config/nasdaq_qualification_receipt.json",
    "config/network_acquisition_registry.json",
    "config/sources.json",
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


def _validate_publication_policy_shape(payload: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "project",
        "plan_type",
        "base_commit",
        "base_tree",
        "bootstrap_policy_id",
        "assessment_id",
        "snapshot_a",
        "snapshot_b",
        "preserved_historical_receipt",
        "destination",
        "receipt_contract",
        "implementation_changes",
        "execution_contract",
        "validation",
        "current_environment_id",
        "current_network_registry_id",
        "plan_id",
    }
    if set(payload) != expected:
        raise ContractError("Nasdaq bootstrap publication policy fields differ")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 2
        or payload["project"] != PROJECT
        or payload["plan_type"]
        != "NASDAQ_BOOTSTRAP_BASELINE_PUBLISHER_IMPLEMENTATION_ONLY"
    ):
        raise ContractError("Nasdaq bootstrap publication policy identity differs")
    for name in (
        "bootstrap_policy_id",
        "assessment_id",
        "current_environment_id",
        "current_network_registry_id",
        "plan_id",
    ):
        require_sha256(payload[name], f"Nasdaq publication policy {name}")
    if any(
        not isinstance(payload[name], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", payload[name]) is None
        for name in ("base_commit", "base_tree")
    ):
        raise ContractError("Nasdaq publication base Git identity is invalid")
    for label in ("snapshot_a", "snapshot_b"):
        snapshot = payload[label]
        if (
            type(snapshot) is not dict
            or set(snapshot)
            != {
                "snapshot_id",
                "raw_sha256",
                "receipt_file_sha256",
                "file_created_at",
                "retrieved_at",
                "record_count",
            }
            or type(snapshot["record_count"]) is not int
            or snapshot["record_count"] < 1
        ):
            raise ContractError(f"Nasdaq publication {label} binding differs")
        for name in ("snapshot_id", "raw_sha256", "receipt_file_sha256"):
            require_sha256(snapshot[name], f"{label}.{name}")
        parse_utc_z(snapshot["file_created_at"], f"{label}.file_created_at")
        parse_utc_z(snapshot["retrieved_at"], f"{label}.retrieved_at")
    preserved = payload["preserved_historical_receipt"]
    if preserved != {
        "path": "config/nasdaq_qualification_receipt.json",
        "file_sha256": "918a8ded82f7d11d0ed967163868bc1f00695df1016fef831ebb73b439ff14c2",
        "receipt_id": "3163ace9f5a71e403f1c030e1d06ef8bc42ea77cb03c71a1b63436882d87feca",
        "role": "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT",
    }:
        raise ContractError("preserved Nasdaq receipt publication role differs")
    destination = payload["destination"]
    if destination != {
        "accepted_root": "data/vault/accepted",
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "role": ROLE,
        "quality_state": QUALITY_STATE,
        "payload_filename": PAYLOAD_FILENAME,
        "work_root": "data/w/nasdaq_bootstrap",
        "publication_count": 1,
    }:
        raise ContractError("Nasdaq bootstrap publication destination differs")
    contract = payload["receipt_contract"]
    if (
        type(contract) is not dict
        or contract.get("schema_version") != 2
        or contract.get("receipt_class") != RECEIPT_CLASS
        or contract.get("status") != PRODUCTION_STATUS
        or contract.get("baseline_record_count") != 13064
        or contract.get("baseline_snapshot_id")
        != payload["snapshot_b"]["snapshot_id"]
        or contract.get("continuity_baseline_eligible") is not True
        or contract.get("source_active") is not False
        or contract.get("provenance")
        != "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        or contract.get("local_integrity_scope") != PUBLICATION_SCOPE
        or contract.get("receipt_fields") != sorted(RECEIPT_FIELDS)
    ):
        raise ContractError("Nasdaq bootstrap receipt contract differs")
    execution = payload["execution_contract"]
    if execution != {
        "default_mode": "PLAN_ONLY_NO_WRITES",
        "execute_flag": "--execute",
        "network_calls": 0,
        "accepted_release_publications": 1,
        "source_config_mutations": 0,
        "activation": False,
        "historical_receipt_relabel": False,
        "model_or_research_authority": False,
        "require_clean_successor_commit": True,
        "require_production_system_utc": True,
        "atomic_content_addressed_publication": True,
        "idempotent_same_release_only": True,
    }:
        raise ContractError("Nasdaq bootstrap execution boundary differs")


def load_nasdaq_bootstrap_publication_policy(
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    path = root / PUBLICATION_POLICY_PATH
    require_contained_path(path, root)
    payload = _json_object(path, label="Nasdaq bootstrap publication policy")
    _validate_publication_policy_shape(payload)
    unsigned = {key: value for key, value in payload.items() if key != "plan_id"}
    if payload["plan_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError("Nasdaq bootstrap publication plan ID differs")
    bootstrap = load_nasdaq_bootstrap_policy(root)
    if bootstrap.policy_id != payload["bootstrap_policy_id"]:
        raise ContractError("Nasdaq bootstrap policy binding differs")
    if (
        sha256_file(root / payload["preserved_historical_receipt"]["path"])
        != payload["preserved_historical_receipt"]["file_sha256"]
    ):
        raise ContractError("preserved Nasdaq receipt bytes changed")
    # The publication policy is preserved evidence for an already reviewed
    # two-capture assessment. Its policy-time registry ID remains hash-bound;
    # loading it must not rewrite that identity when the live registry evolves.
    return payload


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
            "Nasdaq publication requires a valid committed Git closure"
        ) from exc
    return completed.stdout.strip()


def _repository_binding(
    root: Path,
    *,
    policy: Mapping[str, Any],
) -> dict[str, str]:
    if Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("Nasdaq publication Git root differs")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("Nasdaq publication requires a clean committed tree")
    try:
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", policy["base_commit"], "HEAD"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError(
            "Nasdaq publication commit is not descended from its reviewed base"
        ) from exc
    if int(_run_git(root, "rev-list", "--count", f"{policy['base_commit']}..HEAD")) != 1:
        raise IntegrityError(
            "Nasdaq publication requires exactly one implementation commit after its base"
        )
    head = _run_git(root, "rev-parse", "HEAD")
    tree = _run_git(root, "rev-parse", "HEAD^{tree}")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head) is None or re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", tree
    ) is None:
        raise IntegrityError("Nasdaq publication Git identity is malformed")
    return {"head": head, "tree": tree}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    entries = []
    for relative in paths:
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


def _validated_assessment(
    root: Path,
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    source_config = _json_object(
        root / "config" / "sources.json",
        label="source configuration",
    )
    expected_store = root / "data" / "vault" / "qualification" / "as_received"
    store_root = Path(str(source_config.get("snapshot_store_root")))
    nasdaq_source = source_config.get("sources", {}).get("nasdaq_symbol_directory", {})
    if (
        source_config.get("project") != PROJECT
        or store_root != expected_store
        or nasdaq_source.get("enabled_for_active_pipeline") is not False
        or nasdaq_source.get("qualification_receipt")
        != "config/nasdaq_qualification_receipt.json"
    ):
        raise ContractError("Nasdaq source configuration is not preserved and inactive")
    registry = NetworkAcquisitionRegistry.load(
        root / "config" / "network_acquisition_registry.json",
        allowed_root=root / "config",
    )
    store = AsReceivedSnapshotStore(
        store_root,
        allowed_root=root,
        acquisition_registry=registry,
    )
    snapshot_a = store.load(
        store_root / "nasdaqtraded" / policy["snapshot_a"]["snapshot_id"]
    )
    snapshot_b = store.load(
        store_root / "nasdaqtraded" / policy["snapshot_b"]["snapshot_id"]
    )
    bootstrap_policy = load_nasdaq_bootstrap_policy(root)
    assessment = verify_nasdaq_bootstrap_pair(
        snapshot_a,
        snapshot_b,
        policy=bootstrap_policy,
    )
    if (
        assessment["assessment_id"] != policy["assessment_id"]
        or assessment["policy_id"] != policy["bootstrap_policy_id"]
        or assessment["status"] != "PASS_BOOTSTRAP_BASELINE_CANDIDATE_NOT_ACTIVE"
    ):
        raise IntegrityError("Nasdaq bootstrap assessment differs from the reviewed pass")
    for label, snapshot, key in (
        ("snapshot_a", snapshot_a, "snapshot_a"),
        ("snapshot_b", snapshot_b, "snapshot_b"),
    ):
        expected = policy[key]
        observed = assessment[label]
        if (
            observed["snapshot_id"] != expected["snapshot_id"]
            or observed["raw_sha256"] != expected["raw_sha256"]
            or observed["record_count"] != expected["record_count"]
            or observed["retrieved_at"] != expected["retrieved_at"]
            or observed["file_created_at"] != expected["file_created_at"]
            or sha256_file(snapshot.root / "receipt.json")
            != expected["receipt_file_sha256"]
        ):
            raise IntegrityError(f"{label} differs from publication policy")
    return assessment


def _publication_plan_from_context(
    *,
    policy: Mapping[str, Any],
    assessment: Mapping[str, Any],
    repository: Mapping[str, str],
    code_closure: Mapping[str, object],
    config_closure: Mapping[str, object],
    accepted_root: Path,
    work_root: Path,
    synthetic_permit_id: str | None = None,
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": 2,
        "project": PROJECT,
        "mode": (
            "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
            if synthetic_permit_id is not None
            else "PUBLISH_ONE_NON_ACTIVE_BASELINE_RECEIPT"
        ),
        "implementation_plan_id": policy["plan_id"],
        "assessment_code_commit": policy["base_commit"],
        "publisher_code_commit": repository["head"],
        "publisher_tree": repository["tree"],
        "assessment_id": assessment["assessment_id"],
        "bootstrap_policy_id": assessment["policy_id"],
        "snapshot_a": dict(policy["snapshot_a"]),
        "snapshot_b": dict(policy["snapshot_b"]),
        "baseline_record_count": policy["receipt_contract"]["baseline_record_count"],
        "network_registry_id": policy["current_network_registry_id"],
        "environment_id": policy["current_environment_id"],
        "code_closure_sha256": code_closure["closure_sha256"],
        "config_closure_sha256": config_closure["closure_sha256"],
        "accepted_root": str(accepted_root),
        "work_root": str(work_root),
        "dataset": DATASET,
        "source_epoch": SOURCE_EPOCH,
        "payload_filename": PAYLOAD_FILENAME,
        "publication_count": 1,
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "continuity_baseline_publication": synthetic_permit_id is None,
            "source_activation": False,
            "historical_membership": False,
            "model_or_research": False,
            "network_calls": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_nasdaq_bootstrap_publication_plan(
    *,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Build an exact no-write production publication plan."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_nasdaq_bootstrap_publication_policy(root)
    destination = policy["destination"]
    accepted = Path(accepted_root or root / destination["accepted_root"])
    work = Path(work_root or root / destination["work_root"])
    expected_accepted = root / "data" / "vault" / "accepted"
    expected_work = root / "data" / "w" / "nasdaq_bootstrap"
    if accepted != expected_accepted or work != expected_work:
        raise ContractError("Nasdaq publication roots differ from the reviewed policy")
    assessment = _validated_assessment(root, policy=policy)
    repository = _repository_binding(root, policy=policy)
    return _publication_plan_from_context(
        policy=policy,
        assessment=assessment,
        repository=repository,
        code_closure=_closure(root, CODE_CLOSURE_PATHS),
        config_closure=_closure(root, CONFIG_CLOSURE_PATHS),
        accepted_root=accepted,
        work_root=work,
    )


def _authorization_bindings(plan: Mapping[str, Any]) -> dict[str, str]:
    return {
        "accepted_root": str(plan["accepted_root"]),
        "assessment_id": str(plan["assessment_id"]),
        "baseline_record_count": str(plan["baseline_record_count"]),
        "bootstrap_policy_id": str(plan["bootstrap_policy_id"]),
        "dataset": str(plan["dataset"]),
        "payload_filename": str(plan["payload_filename"]),
        "project": PROJECT,
        "publication_count": "1",
        "publication_plan_id": str(plan["publication_plan_id"]),
        "publisher_code_commit": str(plan["publisher_code_commit"]),
        "snapshot_b_id": str(plan["snapshot_b"]["snapshot_id"]),
        "work_root": str(plan["work_root"]),
    }


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != payload:
            raise IntegrityError("Nasdaq publication work evidence differs")
        return
    atomic_write(path, payload)


def _load_or_create_local_record(
    path: Path,
    *,
    plan: Mapping[str, Any],
    clock: TrustedClock,
) -> LocalIntegrityRecord:
    bindings = _authorization_bindings(plan)
    if path.exists():
        try:
            record = LocalIntegrityRecord.from_dict(
                _json_object(path, label="Nasdaq publication local integrity record")
            )
        except EvaluationAuthorizationError as exc:
            raise IntegrityError("Nasdaq publication local record is invalid") from exc
    else:
        record = create_local_integrity_record(
            scope=PUBLICATION_SCOPE,
            subject_id=str(plan["assessment_id"]),
            bindings=bindings,
            clock=clock,
        )
        _write_exact(path, canonical_json_bytes(record.as_dict()))
    try:
        record.validate(
            expected_scope=PUBLICATION_SCOPE,
            expected_subject_id=str(plan["assessment_id"]),
            required_bindings=bindings,
            clock=clock,
        )
    except EvaluationAuthorizationError as exc:
        raise IntegrityError("Nasdaq publication local record differs") from exc
    return record


def _receipt(
    *,
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    local_record: LocalIntegrityRecord,
    synthetic: bool,
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": 2,
        "project": PROJECT,
        "receipt_class": RECEIPT_CLASS,
        "status": SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS,
        "created_at": local_record.recorded_at,
        "publication_plan_id": plan["publication_plan_id"],
        "assessment_id": plan["assessment_id"],
        "bootstrap_policy_id": plan["bootstrap_policy_id"],
        "assessment_code_commit": plan["assessment_code_commit"],
        "publisher_code_commit": plan["publisher_code_commit"],
        "environment_id": plan["environment_id"],
        "network_registry_id": plan["network_registry_id"],
        "snapshot_a": dict(plan["snapshot_a"]),
        "snapshot_b": dict(plan["snapshot_b"]),
        "baseline": {
            "record_count": plan["baseline_record_count"],
            "snapshot_id": plan["snapshot_b"]["snapshot_id"],
            "continuity_baseline_eligible": not synthetic,
            "source_active": False,
        },
        "preserved_historical_comparison": dict(
            policy["preserved_historical_receipt"]
        ),
        "provenance": (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        ),
        "local_integrity_record": local_record.as_dict(),
        "authorities": {
            "continuity_baseline_publication": not synthetic,
            "source_activation": False,
            "historical_membership": False,
            "model_or_research": False,
            "network_calls": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {**unsigned, "receipt_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_receipt(receipt: Mapping[str, Any], *, synthetic: bool) -> None:
    if set(receipt) != RECEIPT_FIELDS:
        raise IntegrityError("Nasdaq bootstrap receipt fields differ")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    require_sha256(receipt["receipt_id"], "Nasdaq bootstrap receipt_id")
    if receipt["receipt_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("Nasdaq bootstrap receipt ID differs")
    for name in (
        "publication_plan_id",
        "assessment_id",
        "bootstrap_policy_id",
        "environment_id",
        "network_registry_id",
    ):
        require_sha256(receipt[name], f"Nasdaq bootstrap receipt {name}")
    if any(
        not isinstance(receipt[name], str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", receipt[name]) is None
        for name in ("assessment_code_commit", "publisher_code_commit")
    ):
        raise IntegrityError("Nasdaq bootstrap receipt Git identity differs")
    snapshot_fields = {
        "snapshot_id",
        "raw_sha256",
        "receipt_file_sha256",
        "file_created_at",
        "retrieved_at",
        "record_count",
    }
    for name in ("snapshot_a", "snapshot_b"):
        snapshot = receipt[name]
        if (
            type(snapshot) is not dict
            or set(snapshot) != snapshot_fields
            or type(snapshot["record_count"]) is not int
            or snapshot["record_count"] < 1
        ):
            raise IntegrityError(f"Nasdaq bootstrap receipt {name} differs")
        for field in ("snapshot_id", "raw_sha256", "receipt_file_sha256"):
            require_sha256(snapshot[field], f"Nasdaq receipt {name}.{field}")
        parse_utc_z(snapshot["file_created_at"], f"Nasdaq receipt {name}.file_created_at")
        parse_utc_z(snapshot["retrieved_at"], f"Nasdaq receipt {name}.retrieved_at")
    baseline = receipt["baseline"]
    if (
        type(baseline) is not dict
        or set(baseline)
        != {
            "record_count",
            "snapshot_id",
            "continuity_baseline_eligible",
            "source_active",
        }
        or type(baseline["record_count"]) is not int
        or baseline["record_count"] != receipt["snapshot_b"]["record_count"]
        or baseline["snapshot_id"] != receipt["snapshot_b"]["snapshot_id"]
    ):
        raise IntegrityError("Nasdaq bootstrap baseline binding differs")
    if (
        receipt["schema_version"] != 2
        or receipt["project"] != PROJECT
        or receipt["receipt_class"] != RECEIPT_CLASS
        or receipt["status"] != (SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS)
        or receipt["baseline"]["source_active"] is not False
        or receipt["baseline"]["continuity_baseline_eligible"] is synthetic
        or receipt["provenance"]
        != (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        )
        or receipt["authorities"]
        != {
            "continuity_baseline_publication": not synthetic,
            "source_activation": False,
            "historical_membership": False,
            "model_or_research": False,
            "network_calls": False,
        }
        or receipt["authorities"]["source_activation"] is not False
        or receipt["authorities"]["network_calls"] is not False
        or receipt["prohibitions"] != list(PROHIBITIONS)
    ):
        raise IntegrityError("Nasdaq bootstrap receipt weakens the non-active boundary")
    parse_utc_z(receipt["created_at"], "Nasdaq receipt created_at")
    preserved = receipt["preserved_historical_comparison"]
    if (
        type(preserved) is not dict
        or set(preserved) != {"path", "file_sha256", "receipt_id", "role"}
        or preserved["role"] != "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT"
    ):
        raise IntegrityError("Nasdaq historical comparison role differs")
    require_sha256(preserved["file_sha256"], "preserved receipt file_sha256")
    require_sha256(preserved["receipt_id"], "preserved receipt_id")
    record = LocalIntegrityRecord.from_dict(receipt["local_integrity_record"])
    record.validate_content()
    if (
        record.scope != PUBLICATION_SCOPE
        or record.subject_id != receipt["assessment_id"]
        or record.bindings.get("publication_plan_id")
        != receipt["publication_plan_id"]
        or record.bindings.get("snapshot_b_id")
        != receipt["snapshot_b"]["snapshot_id"]
        or (record.clock_mode == "PRODUCTION_SYSTEM_UTC") is synthetic
    ):
        raise IntegrityError("Nasdaq publication local integrity binding differs")


@dataclass(frozen=True)
class NasdaqBootstrapPublication:
    publication_plan_id: str
    receipt_id: str
    release_directory: Path
    release_id: str
    local_integrity_record: LocalIntegrityRecord


def _publish_prevalidated(
    *,
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    synthetic: bool,
) -> NasdaqBootstrapPublication:
    trusted_clock = require_trusted_clock(clock)
    if synthetic:
        if trusted_clock.mode != "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            raise PermissionError("synthetic publication requires a synthetic clock")
    elif trusted_clock.mode != "PRODUCTION_SYSTEM_UTC":
        raise PermissionError("Nasdaq publication requires the production UTC clock")
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("Nasdaq publication roots must be absolute")
    build_root = work / str(plan["publication_plan_id"])[:20]
    local_record = _load_or_create_local_record(
        build_root / "local_action_record.json",
        plan=plan,
        clock=trusted_clock,
    )
    if parse_utc_z(
        local_record.recorded_at,
        "Nasdaq publication local record time",
    ) < parse_utc_z(
        str(plan["snapshot_b"]["retrieved_at"]),
        "Nasdaq snapshot B retrieval time",
    ):
        raise IntegrityError(
            "Nasdaq publication cannot predate snapshot B retrieval"
        )
    receipt = _receipt(
        plan=plan,
        policy=policy,
        local_record=local_record,
        synthetic=synthetic,
    )
    _validate_receipt(receipt, synthetic=synthetic)
    stage = build_root / "stage"
    _write_exact(stage / PAYLOAD_FILENAME, canonical_json_bytes(receipt))
    try:
        assert_exact_tree(stage, {PAYLOAD_FILENAME}, set())
    except ContractError as exc:
        raise IntegrityError("Nasdaq publication stage differs") from exc
    schema_fingerprint = sha256_bytes(
        canonical_json_bytes({"receipt_fields": sorted(RECEIPT_FIELDS)})
    )
    manifest = build_manifest(
        stage,
        (PAYLOAD_FILENAME,),
        project=PROJECT,
        dataset=DATASET,
        source_epoch=SOURCE_EPOCH,
        role=ROLE,
        quality_state=QUALITY_STATE,
        created_at=str(receipt["created_at"]),
        row_count=1,
        event_start=str(plan["snapshot_a"]["file_created_at"]),
        event_end=str(plan["snapshot_b"]["file_created_at"]),
        upstream_release_ids=(),
        schema_fingerprint=schema_fingerprint,
        code_hash=str(plan["code_closure_sha256"]),
        config_hash=str(plan["config_closure_sha256"]),
        environment_hash=str(plan["environment_id"]),
    )
    release_directory = AtomicReleasePublisher(accepted).publish(stage, manifest)
    loaded = verify_nasdaq_bootstrap_baseline_release(
        release_directory,
        accepted_root=accepted,
        expected_plan_id=str(plan["publication_plan_id"]),
        synthetic=synthetic,
    )
    return NasdaqBootstrapPublication(
        publication_plan_id=str(plan["publication_plan_id"]),
        receipt_id=str(loaded["receipt_id"]),
        release_directory=release_directory,
        release_id=manifest.release_id,
        local_integrity_record=local_record,
    )


def publish_nasdaq_bootstrap_receipt(
    *,
    approved_plan_id: str,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
    clock: TrustedClock,
    owner_confirmation: str,
) -> NasdaqBootstrapPublication:
    """Publish one non-active receipt under a separately approved exact plan."""

    require_sha256(approved_plan_id, "approved Nasdaq publication plan ID")
    if owner_confirmation != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError("Nasdaq publication owner confirmation differs")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_nasdaq_bootstrap_publication_policy(root)
    plan = build_nasdaq_bootstrap_publication_plan(
        repo_root=root,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if plan["publication_plan_id"] != approved_plan_id:
        raise PermissionError("approved Nasdaq publication plan ID differs")
    return _publish_prevalidated(
        plan=plan,
        policy=policy,
        accepted_root=Path(plan["accepted_root"]),
        work_root=Path(plan["work_root"]),
        clock=clock,
        synthetic=False,
    )


def publish_nasdaq_bootstrap_fixture(
    *,
    plan: Mapping[str, Any],
    policy: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    permit: SyntheticOnlyPermit,
) -> NasdaqBootstrapPublication:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    if (
        plan.get("mode") != "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
        or plan.get("synthetic_permit_id") != verified.permit_id
    ):
        raise ContractError("synthetic Nasdaq publication plan differs from its permit")
    return _publish_prevalidated(
        plan=plan,
        policy=policy,
        accepted_root=accepted_root,
        work_root=work_root,
        clock=clock,
        synthetic=True,
    )


def verify_nasdaq_bootstrap_baseline_release(
    release_directory: Path,
    *,
    accepted_root: Path,
    expected_plan_id: str | None = None,
    synthetic: bool = False,
) -> dict[str, Any]:
    manifest = verify_accepted_release(
        Path(release_directory),
        accepted_root=Path(accepted_root),
    )
    if (
        manifest.dataset != DATASET
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != ROLE
        or manifest.quality_state != QUALITY_STATE
        or manifest.row_count != 1
        or [entry.path for entry in manifest.files] != [PAYLOAD_FILENAME]
    ):
        raise IntegrityError("Nasdaq bootstrap release manifest differs")
    receipt = _json_object(
        Path(release_directory) / PAYLOAD_FILENAME,
        label="Nasdaq bootstrap baseline receipt",
    )
    _validate_receipt(receipt, synthetic=synthetic)
    if expected_plan_id is not None and receipt["publication_plan_id"] != expected_plan_id:
        raise IntegrityError("Nasdaq bootstrap receipt plan differs")
    if (
        manifest.created_at != receipt["created_at"]
        or manifest.event_start != receipt["snapshot_a"]["file_created_at"]
        or manifest.event_end != receipt["snapshot_b"]["file_created_at"]
    ):
        raise IntegrityError("Nasdaq bootstrap release timing differs")
    return receipt
