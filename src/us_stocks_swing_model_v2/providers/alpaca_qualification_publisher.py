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
from ..environment import validate_environment_lock
from ..errors import ContractError, EvaluationAuthorizationError, IntegrityError
from ..governance import LocalIntegrityRecord, create_local_integrity_record
from ..releases import AtomicReleasePublisher, build_manifest, verify_accepted_release
from .alpaca_qualification_readiness import (
    PROJECT,
    build_alpaca_feed_cutover_design,
    load_alpaca_feed_qualification_policy,
    load_validated_alpaca_feed_qualification_assessment,
)


PUBLICATION_CONFIRMATION_TOKEN = "ALPACA_QUALIFICATION_PUBLICATION_APPROVED"
PUBLICATION_CONFIRMATION_VALUE = "YES"
PUBLICATION_SCOPE = "ALPACA_FEED_QUALIFICATION_RECEIPT_PUBLICATION"
FIXTURE_SCOPE = "ALPACA_FEED_QUALIFICATION_PUBLICATION_FIXTURE"
DATASET = "alpaca_feed_qualification"
FIXTURE_DATASET = "alpaca_feed_qualification_fixture"
SOURCE_EPOCH = "alpaca_basic_sip_20260723_20260729"
PAYLOAD_FILENAME = "alpaca_feed_qualification_receipt.json"
RECEIPT_CLASS = "ALPACA_SIP_IEX_FEED_QUALIFICATION"
PRODUCTION_STATUS = "PASS_SELECTED_SIP_NOT_ACTIVE"
SYNTHETIC_STATUS = "SYNTHETIC_ALPACA_QUALIFICATION_PUBLICATION_MECHANICS_ONLY"
ROLE = "qualification_evidence_only"
QUALITY_STATE = "QUALIFICATION_EVIDENCE"
FIXTURE_ROLE = "qualification_evidence_only"
FIXTURE_QUALITY_STATE = "QUALIFICATION_EVIDENCE"
RECEIPT_FIELDS = {
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
}
PROHIBITIONS = (
    "provider_call",
    "credential_access",
    "source_activation",
    "canonical_bars",
    "research_execution",
    "audit_execution",
)
CODE_CLOSURE_PATHS = (
    "pyproject.toml",
    "src/us_stocks_swing_model_v2/cli/publish_alpaca_qualification.py",
    "src/us_stocks_swing_model_v2/providers/alpaca.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_qualification_publisher.py",
    "src/us_stocks_swing_model_v2/providers/alpaca_qualification_readiness.py",
    "src/us_stocks_swing_model_v2/providers/snapshots.py",
    "src/us_stocks_swing_model_v2/governance.py",
    "src/us_stocks_swing_model_v2/releases.py",
)
CONFIG_CLOSURE_PATHS = (
    "config/alpaca_feed_qualification_policy.json",
    "config/environment.lock.json",
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
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"{label} is unreadable") from exc
    if type(value) is not dict:
        raise IntegrityError(f"{label} must be one JSON object")
    return value


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
            "Alpaca qualification publication requires a valid committed Git closure"
        ) from exc
    return completed.stdout.strip()


def _repository_binding(root: Path) -> dict[str, str]:
    if Path(_run_git(root, "rev-parse", "--show-toplevel")).resolve(strict=True) != root:
        raise IntegrityError("Alpaca qualification publication Git root differs")
    if _run_git(root, "branch", "--show-current") != "main":
        raise IntegrityError("Alpaca qualification publication requires main")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise IntegrityError(
            "Alpaca qualification publication requires a clean committed tree"
        )
    head = _run_git(root, "rev-parse", "HEAD")
    tree = _run_git(root, "rev-parse", "HEAD^{tree}")
    if any(
        re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        for value in (head, tree)
    ):
        raise IntegrityError("Alpaca qualification publication Git identity is malformed")
    return {"head": head, "tree": tree}


def _closure(root: Path, paths: tuple[str, ...]) -> dict[str, object]:
    entries: list[dict[str, str]] = []
    for relative in paths:
        path = require_contained_path(root / relative, root)
        reject_link(path)
        if not path.is_file() or path.stat().st_nlink != 1:
            raise IntegrityError(
                f"Alpaca qualification publication closure file is absent: {relative}"
            )
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return {
        "files": entries,
        "closure_sha256": sha256_bytes(canonical_json_bytes(entries)),
    }


def _publication_plan_from_context(
    *,
    policy: Mapping[str, Any],
    assessment: Mapping[str, Any],
    repository: Mapping[str, str],
    code_closure: Mapping[str, object],
    config_closure: Mapping[str, object],
    environment_id: str,
    accepted_root: Path,
    work_root: Path,
    synthetic_permit_id: str | None = None,
) -> dict[str, Any]:
    require_sha256(environment_id, "Alpaca qualification environment_id")
    if (
        assessment.get("assessment_id") != policy["assessment"]["assessment_id"]
        or assessment.get("selected_feed_candidate") != "sip"
        or assessment.get("selection_reason") != "both_pass_prefer_sip"
        or assessment.get("activation_authorized") is not False
    ):
        raise IntegrityError("Alpaca qualification assessment differs from policy")
    if (
        type(assessment.get("snapshots")) is not dict
        or set(assessment["snapshots"]) != {"sip", "iex"}
        or type(assessment.get("qualifications")) is not dict
        or set(assessment["qualifications"]) != {"sip", "iex"}
    ):
        raise IntegrityError("Alpaca qualification assessment census differs")
    for feed, binding in policy["snapshots"].items():
        snapshot = assessment["snapshots"][feed]
        result = assessment["qualifications"][feed]
        production = synthetic_permit_id is None
        if (
            snapshot.get("snapshot_id") != binding["snapshot_id"]
            or snapshot.get("raw_sha256") != binding["raw_sha256"]
            or result.get("feed") != feed
            or result.get("state") != "PASS"
            or result.get("reasons") != []
            or result.get("snapshot_ids") != [binding["snapshot_id"]]
            or result.get("bar_count") != binding["bar_count"]
            or result.get("calendar_release_id") != policy["calendar"]["release_id"]
            or result.get("trust_eligible") is not production
            or result.get("evidence_state")
            != (
                "NETWORK_AS_RECEIVED"
                if production
                else "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            )
        ):
            raise IntegrityError(f"Alpaca {feed} qualification differs from policy")
    publication = policy["receipt_publication"]
    unsigned: dict[str, Any] = {
        "schema_version": 1,
        "project": PROJECT,
        "mode": (
            "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
            if synthetic_permit_id is not None
            else "PUBLISH_ONE_NON_ACTIVE_ALPACA_QUALIFICATION_RECEIPT"
        ),
        "publisher_code_commit": repository["head"],
        "publisher_tree": repository["tree"],
        "policy_id": policy["policy_id"],
        "assessment_id": assessment["assessment_id"],
        "selected_feed": "sip",
        "selection_reason": "both_pass_prefer_sip",
        "window": dict(policy["window"]),
        "request_contract": dict(policy["request_contract"]),
        "network_registry_id": policy["network_registry"]["registry_id"],
        "calendar_release_id": policy["calendar"]["release_id"],
        "snapshots": {
            feed: dict(binding)
            for feed, binding in sorted(policy["snapshots"].items())
        },
        "qualifications": {
            feed: dict(result)
            for feed, result in sorted(assessment["qualifications"].items())
        },
        "assessment_snapshots": {
            feed: dict(snapshot)
            for feed, snapshot in sorted(assessment["snapshots"].items())
        },
        "code_closure": dict(code_closure),
        "config_closure": dict(config_closure),
        "environment_id": environment_id,
        "accepted_root": str(accepted_root),
        "work_root": str(work_root),
        "dataset": FIXTURE_DATASET if synthetic_permit_id else publication["dataset"],
        "source_epoch": publication["source_epoch"],
        "payload_filename": publication["payload_filename"],
        "publication_count": 1,
        "synthetic_permit_id": synthetic_permit_id,
        "authorities": {
            "qualification_receipt_publication": synthetic_permit_id is None,
            "source_activation": False,
            "config_sources_mutation": False,
            "canonical_bars": False,
            "network_calls": False,
            "credential_access": False,
            "model_or_research": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {
        **unsigned,
        "publication_plan_id": sha256_bytes(canonical_json_bytes(unsigned)),
    }


def build_alpaca_qualification_publication_plan(
    *,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
) -> dict[str, Any]:
    """Build the exact production publication plan without writing."""

    root = Path(repo_root or _repo_root()).resolve(strict=True)
    repository = _repository_binding(root)
    policy = load_alpaca_feed_qualification_policy(root)
    publication = policy["receipt_publication"]
    accepted = Path(accepted_root or root / publication["accepted_root"])
    work = Path(work_root or root / publication["work_root"])
    if (
        accepted != root / publication["accepted_root"]
        or work != root / publication["work_root"]
    ):
        raise ContractError("Alpaca qualification publication roots differ")
    design = build_alpaca_feed_cutover_design(root)
    if design["repository"] != repository:
        raise IntegrityError("Alpaca cutover design repository binding differs")
    assessment = load_validated_alpaca_feed_qualification_assessment(root)
    return _publication_plan_from_context(
        policy=policy,
        assessment=assessment,
        repository=repository,
        code_closure=_closure(root, CODE_CLOSURE_PATHS),
        config_closure=_closure(root, CONFIG_CLOSURE_PATHS),
        environment_id=validate_environment_lock(
            root / "config" / "environment.lock.json"
        ),
        accepted_root=accepted,
        work_root=work,
    )


def _authorization_bindings(plan: Mapping[str, Any]) -> dict[str, str]:
    return {
        "accepted_root": str(plan["accepted_root"]),
        "assessment_id": str(plan["assessment_id"]),
        "calendar_release_id": str(plan["calendar_release_id"]),
        "dataset": str(plan["dataset"]),
        "network_registry_id": str(plan["network_registry_id"]),
        "policy_id": str(plan["policy_id"]),
        "publication_count": "1",
        "publication_plan_id": str(plan["publication_plan_id"]),
        "publisher_code_commit": str(plan["publisher_code_commit"]),
        "selected_feed": str(plan["selected_feed"]),
        "work_root": str(plan["work_root"]),
    }


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        reject_link(path)
        if (
            not path.is_file()
            or path.stat().st_nlink != 1
            or path.read_bytes() != payload
        ):
            raise IntegrityError("Alpaca qualification publication work evidence differs")
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
                _json_object(
                    path,
                    label="Alpaca qualification publication local integrity record",
                )
            )
        except EvaluationAuthorizationError as exc:
            raise IntegrityError(
                "Alpaca qualification publication local record is invalid"
            ) from exc
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
        raise IntegrityError(
            "Alpaca qualification publication local record differs"
        ) from exc
    return record


def _receipt(
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
        "publication_plan_id": plan["publication_plan_id"],
        "policy_id": plan["policy_id"],
        "assessment_id": plan["assessment_id"],
        "selected_feed": plan["selected_feed"],
        "selection_reason": plan["selection_reason"],
        "window": plan["window"],
        "request_contract": plan["request_contract"],
        "network_registry_id": plan["network_registry_id"],
        "calendar_release_id": plan["calendar_release_id"],
        "snapshots": plan["snapshots"],
        "qualifications": plan["qualifications"],
        "code_closure": plan["code_closure"],
        "config_closure": plan["config_closure"],
        "environment_id": plan["environment_id"],
        "provenance": (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        ),
        "authorities": {
            "qualification_receipt_publication": not synthetic,
            "source_activation": False,
            "config_sources_mutation": False,
            "canonical_bars": False,
            "network_calls": False,
            "credential_access": False,
            "model_or_research": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    return {**unsigned, "receipt_id": sha256_bytes(canonical_json_bytes(unsigned))}


def _validate_closure(value: object, *, label: str) -> None:
    if type(value) is not dict or set(value) != {"files", "closure_sha256"}:
        raise IntegrityError(f"{label} fields differ")
    files = value["files"]
    if (
        type(files) is not list
        or not files
        or any(
            type(item) is not dict
            or set(item) != {"path", "sha256"}
            or type(item["path"]) is not str
            for item in files
        )
    ):
        raise IntegrityError(f"{label} files differ")
    for item in files:
        require_sha256(item["sha256"], f"{label} file hash")
    require_sha256(value["closure_sha256"], f"{label} ID")
    if value["closure_sha256"] != sha256_bytes(canonical_json_bytes(files)):
        raise IntegrityError(f"{label} ID differs")


def _validate_receipt(receipt: Mapping[str, Any], *, synthetic: bool) -> None:
    if set(receipt) != RECEIPT_FIELDS:
        raise IntegrityError("Alpaca qualification receipt fields differ")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_id"}
    require_sha256(receipt["receipt_id"], "Alpaca qualification receipt_id")
    if receipt["receipt_id"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise IntegrityError("Alpaca qualification receipt ID differs")
    for name in (
        "publication_plan_id",
        "policy_id",
        "assessment_id",
        "network_registry_id",
        "calendar_release_id",
        "environment_id",
    ):
        require_sha256(receipt[name], f"Alpaca qualification receipt {name}")
    _validate_closure(receipt["code_closure"], label="Alpaca code closure")
    _validate_closure(receipt["config_closure"], label="Alpaca config closure")
    if (
        receipt["schema_version"] != 1
        or receipt["project"] != PROJECT
        or receipt["receipt_class"] != RECEIPT_CLASS
        or receipt["status"] != (SYNTHETIC_STATUS if synthetic else PRODUCTION_STATUS)
        or receipt["selected_feed"] != "sip"
        or receipt["selection_reason"] != "both_pass_prefer_sip"
        or receipt["authorities"]
        != {
            "qualification_receipt_publication": not synthetic,
            "source_activation": False,
            "config_sources_mutation": False,
            "canonical_bars": False,
            "network_calls": False,
            "credential_access": False,
            "model_or_research": False,
        }
        or receipt["prohibitions"] != list(PROHIBITIONS)
        or receipt["provenance"]
        != (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        )
    ):
        raise IntegrityError("Alpaca qualification receipt weakens its boundary")
    parse_utc_z(receipt["created_at"], "Alpaca qualification receipt created_at")
    if type(receipt["snapshots"]) is not dict or set(receipt["snapshots"]) != {
        "sip",
        "iex",
    }:
        raise IntegrityError("Alpaca qualification receipt snapshots differ")
    for feed, snapshot in receipt["snapshots"].items():
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
            or snapshot["bar_count"] != 10
            or f"alpaca_{feed}_qualification" not in snapshot["relative_directory"]
        ):
            raise IntegrityError(f"Alpaca qualification {feed} snapshot differs")
        for field in ("snapshot_id", "raw_sha256", "receipt_file_sha256"):
            require_sha256(snapshot[field], f"Alpaca qualification {feed}.{field}")
    qualifications = receipt["qualifications"]
    if type(qualifications) is not dict or set(qualifications) != {"sip", "iex"}:
        raise IntegrityError("Alpaca qualification receipt results differ")
    for feed, result in qualifications.items():
        if (
            type(result) is not dict
            or result.get("feed") != feed
            or result.get("state") != "PASS"
            or result.get("reasons") != []
            or result.get("snapshot_ids")
            != [receipt["snapshots"][feed]["snapshot_id"]]
            or result.get("bar_count") != 10
            or result.get("calendar_release_id") != receipt["calendar_release_id"]
            or result.get("trust_eligible") is synthetic
            or result.get("evidence_state")
            != (
                "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
                if synthetic
                else "NETWORK_AS_RECEIVED"
            )
        ):
            raise IntegrityError(f"Alpaca qualification {feed} result differs")


@dataclass(frozen=True)
class AlpacaQualificationPublication:
    publication_plan_id: str
    receipt_id: str
    release_id: str
    release_directory: Path
    local_integrity_record: LocalIntegrityRecord


def _publish_prevalidated(
    *,
    plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    synthetic: bool,
) -> AlpacaQualificationPublication:
    trusted_clock = require_trusted_clock(clock)
    if synthetic:
        if trusted_clock.mode != "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            raise PermissionError(
                "synthetic Alpaca qualification publication requires a synthetic clock"
            )
    elif trusted_clock.mode != "PRODUCTION_SYSTEM_UTC":
        raise PermissionError(
            "Alpaca qualification publication requires production system UTC"
        )
    accepted = Path(accepted_root)
    work = Path(work_root)
    if not accepted.is_absolute() or not work.is_absolute():
        raise ContractError("Alpaca qualification publication roots must be absolute")
    build_root = work / str(plan["publication_plan_id"])[:20]
    record = _load_or_create_local_record(
        build_root / "local_action_record.json",
        plan=plan,
        clock=trusted_clock,
    )
    latest_retrieval = max(
        parse_utc_z(
            str(result["retrieved_at"]),
            f"Alpaca {feed} snapshot retrieval time",
        )
        for feed, result in plan["assessment_snapshots"].items()
    )
    if parse_utc_z(record.recorded_at, "Alpaca publication time") < latest_retrieval:
        raise IntegrityError("Alpaca qualification publication predates its snapshots")
    receipt = _receipt(plan=plan, record=record, synthetic=synthetic)
    _validate_receipt(receipt, synthetic=synthetic)
    stage = build_root / "stage"
    _write_exact(stage / PAYLOAD_FILENAME, canonical_json_bytes(receipt))
    try:
        assert_exact_tree(stage, {PAYLOAD_FILENAME}, set())
    except ContractError as exc:
        raise IntegrityError("Alpaca qualification publication stage differs") from exc
    manifest = build_manifest(
        stage,
        (PAYLOAD_FILENAME,),
        project=PROJECT,
        dataset=FIXTURE_DATASET if synthetic else DATASET,
        source_epoch=SOURCE_EPOCH,
        role=FIXTURE_ROLE if synthetic else ROLE,
        quality_state=FIXTURE_QUALITY_STATE if synthetic else QUALITY_STATE,
        created_at=record.recorded_at,
        row_count=20,
        event_start=str(plan["window"]["start"]),
        event_end=str(plan["window"]["end"]),
        upstream_release_ids=(str(plan["calendar_release_id"]),),
        schema_fingerprint=sha256_bytes(
            canonical_json_bytes({"receipt_fields": sorted(RECEIPT_FIELDS)})
        ),
        code_hash=str(plan["code_closure"]["closure_sha256"]),
        config_hash=str(plan["config_closure"]["closure_sha256"]),
        environment_hash=str(plan["environment_id"]),
    )
    release_directory = AtomicReleasePublisher(accepted).publish(stage, manifest)
    loaded = verify_alpaca_qualification_release(
        release_directory,
        accepted_root=accepted,
        expected_plan_id=str(plan["publication_plan_id"]),
        synthetic=synthetic,
    )
    return AlpacaQualificationPublication(
        publication_plan_id=str(plan["publication_plan_id"]),
        receipt_id=str(loaded["receipt_id"]),
        release_id=manifest.release_id,
        release_directory=release_directory,
        local_integrity_record=record,
    )


def publish_alpaca_qualification_receipt(
    *,
    approved_plan_id: str,
    clock: TrustedClock,
    owner_confirmation: str,
    repo_root: Path | None = None,
    accepted_root: Path | None = None,
    work_root: Path | None = None,
) -> AlpacaQualificationPublication:
    """Publish one non-active receipt under a separately approved exact plan."""

    require_sha256(approved_plan_id, "approved Alpaca publication plan ID")
    if owner_confirmation != PUBLICATION_CONFIRMATION_VALUE:
        raise PermissionError("Alpaca publication owner confirmation differs")
    plan = build_alpaca_qualification_publication_plan(
        repo_root=repo_root,
        accepted_root=accepted_root,
        work_root=work_root,
    )
    if plan["publication_plan_id"] != approved_plan_id:
        raise PermissionError("approved Alpaca publication plan ID differs")
    return _publish_prevalidated(
        plan=plan,
        accepted_root=Path(plan["accepted_root"]),
        work_root=Path(plan["work_root"]),
        clock=clock,
        synthetic=False,
    )


def build_alpaca_qualification_publication_fixture_plan(
    *,
    policy: Mapping[str, Any],
    assessment: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    permit: SyntheticOnlyPermit,
) -> dict[str, Any]:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    return _publication_plan_from_context(
        policy=policy,
        assessment=assessment,
        repository={"head": "a" * 40, "tree": "b" * 40},
        code_closure={
            "files": [{"path": "synthetic/code.py", "sha256": "c" * 64}],
            "closure_sha256": sha256_bytes(
                canonical_json_bytes(
                    [{"path": "synthetic/code.py", "sha256": "c" * 64}]
                )
            ),
        },
        config_closure={
            "files": [{"path": "synthetic/config.json", "sha256": "d" * 64}],
            "closure_sha256": sha256_bytes(
                canonical_json_bytes(
                    [{"path": "synthetic/config.json", "sha256": "d" * 64}]
                )
            ),
        },
        environment_id="e" * 64,
        accepted_root=Path(accepted_root),
        work_root=Path(work_root),
        synthetic_permit_id=verified.permit_id,
    )


def publish_alpaca_qualification_fixture(
    *,
    plan: Mapping[str, Any],
    accepted_root: Path,
    work_root: Path,
    clock: TrustedClock,
    permit: SyntheticOnlyPermit,
) -> AlpacaQualificationPublication:
    verified = require_synthetic_permit(permit, scope=FIXTURE_SCOPE)
    if (
        plan.get("mode") != "SYNTHETIC_FIXTURE_NOT_TRUST_ELIGIBLE"
        or plan.get("synthetic_permit_id") != verified.permit_id
    ):
        raise ContractError(
            "synthetic Alpaca qualification publication plan differs from its permit"
        )
    if Path(accepted_root).parent.resolve() != Path(work_root).parent.resolve():
        raise ContractError(
            "synthetic Alpaca qualification outputs require one fixture root"
        )
    return _publish_prevalidated(
        plan=plan,
        accepted_root=accepted_root,
        work_root=work_root,
        clock=clock,
        synthetic=True,
    )


def verify_alpaca_qualification_release(
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
        or manifest.row_count != 20
        or [entry.path for entry in manifest.files] != [PAYLOAD_FILENAME]
    ):
        raise IntegrityError("Alpaca qualification release manifest differs")
    receipt = _json_object(
        Path(release_directory) / PAYLOAD_FILENAME,
        label="Alpaca feed qualification receipt",
    )
    _validate_receipt(receipt, synthetic=synthetic)
    if expected_plan_id is not None and receipt["publication_plan_id"] != expected_plan_id:
        raise IntegrityError("Alpaca qualification receipt plan differs")
    if (
        manifest.created_at != receipt["created_at"]
        or manifest.event_start != receipt["window"]["start"]
        or manifest.event_end != receipt["window"]["end"]
        or manifest.upstream_release_ids != (receipt["calendar_release_id"],)
        or manifest.code_hash != receipt["code_closure"]["closure_sha256"]
        or manifest.config_hash != receipt["config_closure"]["closure_sha256"]
        or manifest.environment_hash != receipt["environment_id"]
    ):
        raise IntegrityError("Alpaca qualification release binding differs")
    return receipt
