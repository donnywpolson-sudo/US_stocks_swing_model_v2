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
    iso_z,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError, EvaluationAuthorizationError, IntegrityError
from ..governance import LocalIntegrityRecord, create_local_integrity_record
from ..identity import (
    BitemporalIdentityLedger,
    IdentitySnapshot,
    _load_identity_release_payload,
)
from ..releases import (
    AtomicReleasePublisher,
    build_manifest,
    verify_accepted_release,
)
from .identity_readiness import (
    PROJECT,
    IdentityInputAssessment,
    assess_identity_inputs,
    load_identity_readiness_policy,
)


PUBLICATION_CONFIRMATION_TOKEN = "NASDAQ_IDENTITY_RELEASE_PUBLICATION_APPROVED"
PUBLICATION_CONFIRMATION_VALUE = "YES"
PUBLICATION_SCOPE = "NASDAQ_ALPACA_IDENTITY_RELEASE_PUBLICATION"
FIXTURE_SCOPE = "IDENTITY_RELEASE_PUBLICATION_FIXTURE"
DATASET = "identity"
FIXTURE_DATASET = "identity_publication_fixture"
SOURCE_EPOCH = "nasdaq_alpaca_active_us_equity_v1"
ROLE = "prospective_as_received"
FIXTURE_ROLE = "qualification_evidence_only"
QUALITY_STATE = "PASS"
FIXTURE_QUALITY_STATE = "QUALIFICATION_EVIDENCE"
PAYLOAD_FILENAME = "identity_snapshots.json"
RECEIPT_FILENAME = "identity_publication_receipt.json"
RECEIPT_CLASS = "NASDAQ_ALPACA_IDENTITY_RELEASE"
PRODUCTION_STATUS = "PASS_IDENTITY_RELEASE_PUBLISHED_NOT_ACTIVE"
SYNTHETIC_STATUS = "SYNTHETIC_IDENTITY_PUBLICATION_MECHANICS_ONLY"
PROHIBITIONS = (
    "source_activation",
    "config_sources_mutation",
    "historical_membership_backfill",
    "model_fit",
    "research_execution",
    "network_capture",
)
RECEIPT_FIELDS = {
    "schema_version",
    "project",
    "receipt_class",
    "status",
    "created_at",
    "implementation_plan_id",
    "publication_plan_id",
    "publisher_code_commit",
    "baseline",
    "input_assessment_id",
    "alpaca_snapshot_id",
    "alpaca_projection_contract_id",
    "alpaca_projection_assessment_id",
    "alpaca_raw_record_count",
    "alpaca_selected_record_count",
    "alpaca_selected_rows_sha256",
    "alpaca_excluded_counts",
    "nasdaq_snapshot_id",
    "identity_snapshot_id",
    "identity_row_count",
    "environment_id",
    "network_registry_id",
    "local_integrity_record",
    "authorities",
    "prohibitions",
    "provenance",
    "receipt_id",
}
CODE_CLOSURE_PATHS = (
    "src/us_stocks_swing_model_v2/cli/publish_identity_release.py",
    "src/us_stocks_swing_model_v2/cli/qualify_identity_sources.py",
    "src/us_stocks_swing_model_v2/providers/identity_publisher.py",
    "src/us_stocks_swing_model_v2/providers/identity_readiness.py",
    "src/us_stocks_swing_model_v2/providers/nasdaq.py",
    "src/us_stocks_swing_model_v2/providers/nasdaq_bootstrap_publisher.py",
    "src/us_stocks_swing_model_v2/providers/network_execution.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/identity.py",
    "src/us_stocks_swing_model_v2/governance.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_asset_projection_policy.json",
    "config/environment.lock.json",
    "config/nasdaq_identity_readiness_policy.json",
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
            "identity publication requires a valid committed Git closure"
        ) from exc
    return completed.stdout.strip()


def _repository_binding(
    root: Path,
    *,
    policy: Mapping[str, Any],
) -> dict[str, str]:
    if Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("identity publication Git root differs")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError("identity publication requires a clean committed tree")
    base = policy["authorization_plan"]["base_commit"]
    try:
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", base, "HEAD"],
            check=True,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise IntegrityError(
            "identity publication commit is not descended from its reviewed base"
        ) from exc
    if int(_run_git(root, "rev-list", "--count", f"{base}..HEAD")) != 1:
        raise IntegrityError(
            "identity publication requires exactly one implementation commit after its base"
        )
    head = _run_git(root, "rev-parse", "HEAD")
    tree = _run_git(root, "rev-parse", "HEAD^{tree}")
    if any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        for value in (head, tree)
    ):
        raise IntegrityError("identity publication Git identity is malformed")
    return {"head": head, "tree": tree}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for relative in paths:
        path = root / relative
        require_contained_path(path, root)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(f"identity publication closure file is absent: {relative}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def _plan_from_context(
    *,
    policy: Mapping[str, Any],
    assessment: IdentityInputAssessment,
    repository: Mapping[str, str],
    code_closure_sha256: str,
    config_closure_sha256: str,
    accepted_root: Path,
    work_root: Path,
    synthetic_permit_id: str | None = None,
) -> dict[str, Any]:
    summary = assessment.summary()
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": (
            "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
            if synthetic_permit_id is not None
            else "PUBLISH_ONE_NON_ACTIVE_IDENTITY_RELEASE"
        ),
        "implementation_plan_id": policy["authorization_plan_id"],
        "publisher_code_commit": repository["head"],
        "publisher_tree": repository["tree"],
        "baseline_release_id": assessment.baseline.release_id,
        "baseline_receipt_id": assessment.baseline.receipt_id,
        "baseline_snapshot_id": assessment.baseline.snapshot_id,
        "baseline_record_count": assessment.baseline.record_count,
        "input_assessment_id": assessment.assessment_id,
        "alpaca_snapshot_id": assessment.alpaca_snapshot_id,
        "alpaca_raw_sha256": assessment.alpaca_raw_sha256,
        "alpaca_receipt_sha256": assessment.alpaca_receipt_sha256,
        "alpaca_record_count": assessment.alpaca_record_count,
        "alpaca_raw_record_count": assessment.alpaca_raw_record_count,
        "alpaca_projection_contract_id": assessment.alpaca_projection_contract_id,
        "alpaca_projection_assessment_id": (
            assessment.alpaca_projection_assessment_id
        ),
        "alpaca_selected_rows_sha256": assessment.alpaca_selected_rows_sha256,
        "alpaca_excluded_counts": dict(assessment.alpaca_excluded_counts),
        "nasdaq_snapshot_id": assessment.nasdaq_snapshot_id,
        "nasdaq_raw_sha256": assessment.nasdaq_raw_sha256,
        "nasdaq_receipt_sha256": assessment.nasdaq_receipt_sha256,
        "nasdaq_record_count": assessment.nasdaq_record_count,
        "identity_snapshot_id": assessment.identity_snapshot.snapshot_id,
        "identity_row_count": len(assessment.identity_snapshot.rows),
        "identity_effective_at": summary["effective_at"],
        "identity_known_at": summary["known_at"],
        "environment_id": policy["environment_id"],
        "network_registry_id": policy["network_registry_id"],
        "code_closure_sha256": code_closure_sha256,
        "config_closure_sha256": config_closure_sha256,
        "accepted_root": str(accepted_root),
        "work_root": str(work_root),
        "dataset": FIXTURE_DATASET if synthetic_permit_id else DATASET,
        "source_epoch": SOURCE_EPOCH,
        "payload_filename": PAYLOAD_FILENAME,
        "receipt_filename": RECEIPT_FILENAME,
        "upstream_release_ids": (
            [] if synthetic_permit_id else [assessment.baseline.release_id]
        ),
        "publication_count": 1,
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "identity_release_publication": synthetic_permit_id is None,
            "source_activation": False,
            "config_sources_mutation": False,
            "network_calls": False,
            "model_or_research": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_identity_release_publication_plan(
    *,
    alpaca_snapshot_directory: Path,
    nasdaq_snapshot_directory: Path,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Build a content-addressed production publication plan without writes."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    policy = load_identity_readiness_policy(root)
    release = policy["identity_release_contract"]
    accepted = Path(accepted_root or root / release["accepted_root"])
    work = Path(work_root or root / release["work_root"])
    if (
        accepted != root / "data" / "vault" / "accepted"
        or work != root / "data" / "w" / "nasdaq_identity"
    ):
        raise ContractError("identity publication roots differ from policy")
    assessment = assess_identity_inputs(
        alpaca_snapshot_directory=alpaca_snapshot_directory,
        nasdaq_snapshot_directory=nasdaq_snapshot_directory,
        repo_root=root,
        accepted_root=accepted,
    )
    repository = _repository_binding(root, policy=policy)
    return _plan_from_context(
        policy=policy,
        assessment=assessment,
        repository=repository,
        code_closure_sha256=str(
            _closure(root, CODE_CLOSURE_PATHS)["closure_sha256"]
        ),
        config_closure_sha256=str(
            _closure(root, CONFIG_CLOSURE_PATHS)["closure_sha256"]
        ),
        accepted_root=accepted,
        work_root=work,
    )


def _authorization_bindings(plan: Mapping[str, Any]) -> dict[str, str]:
    return {
        "accepted_root": str(plan["accepted_root"]),
        "alpaca_projection_assessment_id": str(
            plan["alpaca_projection_assessment_id"]
        ),
        "alpaca_projection_contract_id": str(plan["alpaca_projection_contract_id"]),
        "alpaca_snapshot_id": str(plan["alpaca_snapshot_id"]),
        "baseline_release_id": str(plan["baseline_release_id"]),
        "dataset": str(plan["dataset"]),
        "identity_snapshot_id": str(plan["identity_snapshot_id"]),
        "input_assessment_id": str(plan["input_assessment_id"]),
        "nasdaq_snapshot_id": str(plan["nasdaq_snapshot_id"]),
        "project": PROJECT,
        "publication_count": "1",
        "publication_plan_id": str(plan["publication_plan_id"]),
        "publisher_code_commit": str(plan["publisher_code_commit"]),
        "work_root": str(plan["work_root"]),
    }


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1 or path.read_bytes() != payload:
            raise IntegrityError("identity publication work evidence differs")
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
                _json_object(path, label="identity publication local record")
            )
        except EvaluationAuthorizationError as exc:
            raise IntegrityError("identity publication local record is invalid") from exc
    else:
        record = create_local_integrity_record(
            scope=PUBLICATION_SCOPE,
            subject_id=str(plan["input_assessment_id"]),
            bindings=bindings,
            clock=clock,
        )
        _write_exact(path, canonical_json_bytes(record.as_dict()))
    try:
        record.validate(
            expected_scope=PUBLICATION_SCOPE,
            expected_subject_id=str(plan["input_assessment_id"]),
            required_bindings=bindings,
            clock=clock,
        )
    except EvaluationAuthorizationError as exc:
        raise IntegrityError("identity publication local record differs") from exc
    return record


def _publication_receipt(
    *,
    plan: Mapping[str, Any],
    record: LocalIntegrityRecord,
    synthetic: bool,
) -> dict[str, Any]:
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "project": PROJECT,
        "receipt_class": RECEIPT_CLASS,
        "status": SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS,
        "created_at": record.recorded_at,
        "implementation_plan_id": plan["implementation_plan_id"],
        "publication_plan_id": plan["publication_plan_id"],
        "publisher_code_commit": plan["publisher_code_commit"],
        "baseline": {
            "release_id": plan["baseline_release_id"],
            "receipt_id": plan["baseline_receipt_id"],
            "snapshot_id": plan["baseline_snapshot_id"],
            "record_count": plan["baseline_record_count"],
        },
        "input_assessment_id": plan["input_assessment_id"],
        "alpaca_snapshot_id": plan["alpaca_snapshot_id"],
        "alpaca_projection_contract_id": plan["alpaca_projection_contract_id"],
        "alpaca_projection_assessment_id": plan[
            "alpaca_projection_assessment_id"
        ],
        "alpaca_raw_record_count": plan["alpaca_raw_record_count"],
        "alpaca_selected_record_count": plan["alpaca_record_count"],
        "alpaca_selected_rows_sha256": plan["alpaca_selected_rows_sha256"],
        "alpaca_excluded_counts": plan["alpaca_excluded_counts"],
        "nasdaq_snapshot_id": plan["nasdaq_snapshot_id"],
        "identity_snapshot_id": plan["identity_snapshot_id"],
        "identity_row_count": plan["identity_row_count"],
        "environment_id": plan["environment_id"],
        "network_registry_id": plan["network_registry_id"],
        "local_integrity_record": record.as_dict(),
        "authorities": {
            "identity_release_publication": not synthetic,
            "source_activation": False,
            "config_sources_mutation": False,
            "network_calls": False,
            "model_or_research": False,
        },
        "prohibitions": list(PROHIBITIONS),
        "provenance": (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        ),
    }
    return {**unsigned, "receipt_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_publication_receipt(
    receipt: Mapping[str, Any],
    *,
    synthetic: bool,
) -> None:
    if set(receipt) != RECEIPT_FIELDS:
        raise IntegrityError("identity publication receipt fields differ")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    require_sha256(receipt["receipt_id"], "identity publication receipt_id")
    if receipt["receipt_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("identity publication receipt ID differs")
    for name in (
        "implementation_plan_id",
        "publication_plan_id",
        "input_assessment_id",
        "alpaca_snapshot_id",
        "alpaca_projection_contract_id",
        "alpaca_projection_assessment_id",
        "alpaca_selected_rows_sha256",
        "nasdaq_snapshot_id",
        "identity_snapshot_id",
        "environment_id",
        "network_registry_id",
    ):
        require_sha256(receipt[name], f"identity receipt {name}")
    if (
        type(receipt["identity_row_count"]) is not int
        or receipt["identity_row_count"] < 1
        or type(receipt["alpaca_raw_record_count"]) is not int
        or receipt["alpaca_raw_record_count"] < 1
        or type(receipt["alpaca_selected_record_count"]) is not int
        or receipt["alpaca_selected_record_count"] < 1
        or type(receipt["alpaca_excluded_counts"]) is not dict
        or any(
            type(key) is not str or type(value) is not int or value < 1
            for key, value in receipt["alpaca_excluded_counts"].items()
        )
        or receipt["alpaca_selected_record_count"]
        + sum(receipt["alpaca_excluded_counts"].values())
        != receipt["alpaca_raw_record_count"]
        or receipt["schema_version"] != 1
        or receipt["project"] != PROJECT
        or receipt["receipt_class"] != RECEIPT_CLASS
        or receipt["status"] != (SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS)
        or receipt["prohibitions"] != list(PROHIBITIONS)
        or receipt["authorities"]
        != {
            "identity_release_publication": not synthetic,
            "source_activation": False,
            "config_sources_mutation": False,
            "network_calls": False,
            "model_or_research": False,
        }
    ):
        raise IntegrityError("identity publication receipt weakens its boundary")
    baseline = receipt["baseline"]
    if (
        type(baseline) is not dict
        or set(baseline)
        != {"release_id", "receipt_id", "snapshot_id", "record_count"}
        or type(baseline["record_count"]) is not int
        or baseline["record_count"] < 1
    ):
        raise IntegrityError("identity publication baseline receipt differs")
    for name in ("release_id", "receipt_id", "snapshot_id"):
        require_sha256(baseline[name], f"identity receipt baseline.{name}")
    parse_utc_z(receipt["created_at"], "identity receipt created_at")
    record = LocalIntegrityRecord.from_dict(receipt["local_integrity_record"])
    record.validate_content()
    if (
        record.scope != PUBLICATION_SCOPE
        or record.subject_id != receipt["input_assessment_id"]
        or record.bindings.get("publication_plan_id")
        != receipt["publication_plan_id"]
        or record.bindings.get("alpaca_projection_contract_id")
        != receipt["alpaca_projection_contract_id"]
        or record.bindings.get("alpaca_projection_assessment_id")
        != receipt["alpaca_projection_assessment_id"]
        or (record.clock_mode == "PRODUCTION_SYSTEM_UTC") is synthetic
    ):
        raise IntegrityError("identity publication local record differs")


@dataclass(frozen=True)
class IdentityPublication:
    publication_plan_id: str
    receipt_id: str
    release_id: str
    release_directory: Path
    local_integrity_record: LocalIntegrityRecord


def _publish_prevalidated(
    *,
    plan: Mapping[str, Any],
    snapshot: IdentitySnapshot,
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    synthetic: bool,
) -> IdentityPublication:
    trusted_clock = require_trusted_clock(clock)
    if synthetic:
        if trusted_clock.mode != "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            raise PermissionError("synthetic identity publication requires a synthetic clock")
        if snapshot.trust_eligible:
            raise ContractError("synthetic publication cannot contain trusted identity")
    else:
        if trusted_clock.mode != "PRODUCTION_SYSTEM_UTC":
            raise PermissionError("identity publication requires production system UTC")
        if not snapshot.trust_eligible:
            raise ContractError("production publication requires trusted identity")
    if (
        snapshot.snapshot_id != plan["identity_snapshot_id"]
        or len(snapshot.rows) != plan["identity_row_count"]
    ):
        raise IntegrityError("identity snapshot differs from its publication plan")
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("identity publication roots must be absolute")
    build_root = work / str(plan["publication_plan_id"])[:20]
    record = _load_or_create_local_record(
        build_root / "local_action_record.json",
        plan=plan,
        clock=trusted_clock,
    )
    if parse_utc_z(record.recorded_at, "identity publication time") < snapshot.known_at:
        raise IntegrityError("identity publication cannot predate input knowledge time")
    receipt = _publication_receipt(plan=plan, record=record, synthetic=synthetic)
    _validate_publication_receipt(receipt, synthetic=synthetic)
    stage = build_root / "stage"
    payload = {
        "schema_version": 1,
        "snapshots": [snapshot.receipt_dict()],
    }
    _write_exact(stage / PAYLOAD_FILENAME, canonical_json_bytes(payload))
    _write_exact(stage / RECEIPT_FILENAME, canonical_json_bytes(receipt))
    try:
        assert_exact_tree(stage, {PAYLOAD_FILENAME, RECEIPT_FILENAME}, set())
    except ContractError as exc:
        raise IntegrityError("identity publication stage differs") from exc
    row_fields = sorted(next(iter(snapshot.rows)).receipt_dict())
    snapshot_fields = sorted(snapshot.receipt_dict())
    schema_fingerprint = sha256_bytes(
        canonical_json_bytes(
            {
                "payload_schema_version": 1,
                "snapshot_fields": snapshot_fields,
                "row_fields": row_fields,
                "publication_receipt_fields": sorted(RECEIPT_FIELDS),
            }
        )
    )
    manifest = build_manifest(
        stage,
        (PAYLOAD_FILENAME, RECEIPT_FILENAME),
        project=PROJECT,
        dataset=FIXTURE_DATASET if synthetic else DATASET,
        source_epoch=SOURCE_EPOCH,
        role=FIXTURE_ROLE if synthetic else ROLE,
        quality_state=FIXTURE_QUALITY_STATE if synthetic else QUALITY_STATE,
        created_at=record.recorded_at,
        row_count=len(snapshot.rows),
        event_start=iso_z(snapshot.nasdaq_file_created_at),
        event_end=iso_z(snapshot.effective_at),
        upstream_release_ids=plan["upstream_release_ids"],
        schema_fingerprint=schema_fingerprint,
        code_hash=str(plan["code_closure_sha256"]),
        config_hash=str(plan["config_closure_sha256"]),
        environment_hash=str(plan["environment_id"]),
    )
    release_directory = AtomicReleasePublisher(accepted).publish(stage, manifest)
    loaded_receipt = verify_identity_release(
        release_directory,
        accepted_root=accepted,
        expected_plan_id=str(plan["publication_plan_id"]),
        synthetic=synthetic,
    )
    return IdentityPublication(
        publication_plan_id=str(plan["publication_plan_id"]),
        receipt_id=str(loaded_receipt["receipt_id"]),
        release_id=manifest.release_id,
        release_directory=release_directory,
        local_integrity_record=record,
    )


def publish_identity_release(
    *,
    approved_plan_id: str,
    alpaca_snapshot_directory: Path,
    nasdaq_snapshot_directory: Path,
    clock: TrustedClock,
    owner_confirmation: str,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
) -> IdentityPublication:
    """Publish one non-active identity release under a separately approved plan."""

    require_sha256(approved_plan_id, "approved identity publication plan ID")
    if owner_confirmation != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError("identity publication owner confirmation differs")
    root = Path(repo_root or _repo_root()).resolve(strict=True)
    plan = build_identity_release_publication_plan(
        alpaca_snapshot_directory=alpaca_snapshot_directory,
        nasdaq_snapshot_directory=nasdaq_snapshot_directory,
        repo_root=root,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if plan["publication_plan_id"] != approved_plan_id:
        raise PermissionError("approved identity publication plan ID differs")
    assessment = assess_identity_inputs(
        alpaca_snapshot_directory=alpaca_snapshot_directory,
        nasdaq_snapshot_directory=nasdaq_snapshot_directory,
        repo_root=root,
        accepted_root=Path(plan["accepted_root"]),
    )
    if assessment.assessment_id != plan["input_assessment_id"]:
        raise IntegrityError("identity assessment changed after plan creation")
    return _publish_prevalidated(
        plan=plan,
        snapshot=assessment.identity_snapshot,
        accepted_root=Path(plan["accepted_root"]),
        work_root=Path(plan["work_root"]),
        clock=clock,
        synthetic=False,
    )


def build_identity_publication_fixture_plan(
    *,
    assessment: IdentityInputAssessment,
    accepted_root: Path,
    work_root: Path,
    permit: SyntheticOnlyPermit,
) -> dict[str, Any]:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    policy = {
        "authorization_plan_id": "a" * 64,
        "environment_id": "b" * 64,
        "network_registry_id": "c" * 64,
    }
    return _plan_from_context(
        policy=policy,
        assessment=assessment,
        repository={"head": "d" * 40, "tree": "e" * 40},
        code_closure_sha256="f" * 64,
        config_closure_sha256="1" * 64,
        accepted_root=Path(accepted_root),
        work_root=Path(work_root),
        synthetic_permit_id=verified.permit_id,
    )


def publish_identity_release_fixture(
    *,
    plan: Mapping[str, Any],
    snapshot: IdentitySnapshot,
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    permit: SyntheticOnlyPermit,
) -> IdentityPublication:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    if (
        plan.get("mode") != "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
        or plan.get("synthetic_permit_id") != verified.permit_id
    ):
        raise ContractError("synthetic identity publication plan differs from its permit")
    return _publish_prevalidated(
        plan=plan,
        snapshot=snapshot,
        accepted_root=accepted_root,
        work_root=work_root,
        clock=clock,
        synthetic=True,
    )


def verify_identity_release(
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
        manifest.dataset != (FIXTURE_DATASET if synthetic else DATASET)
        or manifest.source_epoch != SOURCE_EPOCH
        or manifest.role != (FIXTURE_ROLE if synthetic else ROLE)
        or manifest.quality_state
        != (FIXTURE_QUALITY_STATE if synthetic else QUALITY_STATE)
        or [entry.path for entry in manifest.files]
        != sorted((PAYLOAD_FILENAME, RECEIPT_FILENAME))
    ):
        raise IntegrityError("identity release manifest differs")
    receipt = _json_object(
        Path(release_directory) / RECEIPT_FILENAME,
        label="identity publication receipt",
    )
    _validate_publication_receipt(receipt, synthetic=synthetic)
    if expected_plan_id is not None and receipt["publication_plan_id"] != expected_plan_id:
        raise IntegrityError("identity publication plan differs")
    snapshots = _load_identity_release_payload(
        Path(release_directory),
        manifest.row_count,
    )
    if (
        len(snapshots) != 1
        or snapshots[0].snapshot_id != receipt["identity_snapshot_id"]
        or len(snapshots[0].rows) != receipt["identity_row_count"]
        or snapshots[0].trust_eligible is synthetic
        or manifest.created_at != receipt["created_at"]
    ):
        raise IntegrityError("identity release payload differs from its receipt")
    if not synthetic:
        ledger = BitemporalIdentityLedger(
            verified_release_directory=Path(release_directory),
            accepted_release_root=Path(accepted_root),
        )
        if ledger.release_id != manifest.release_id or not ledger.trust_eligible:
            raise IntegrityError("identity release ledger verification differs")
    return receipt
