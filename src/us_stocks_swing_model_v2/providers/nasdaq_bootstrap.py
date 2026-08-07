from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from ..errors import ContractError
from .nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
    _parse_nasdaq_traded_absolute,
)
from .snapshots import LandedSnapshot


PROJECT = "US_stocks_swing_model_v2"
BOOTSTRAP_POLICY_PATH = Path("config/nasdaq_bootstrap_policy.json")
PRESERVED_RECEIPT_PATH = Path("config/nasdaq_qualification_receipt.json")
PRODUCTION_STATUS = "IMPLEMENTED_NOT_EXECUTED_STOP_BEFORE_SNAPSHOT_B_CAPTURE"
PRODUCTION_CLASS = "TWO_FRESH_LOCALLY_INTEGRITY_VERIFIED_CAPTURES"
PASS_STATUS = "PASS_BOOTSTRAP_BASELINE_CANDIDATE_NOT_ACTIVE"
SYNTHETIC_PASS_STATUS = "PASS_SYNTHETIC_MECHANICS_ONLY_NOT_TRUST_ELIGIBLE"
PROHIBITIONS = (
    "network_capture",
    "receipt_publication",
    "source_activation",
    "historical_receipt_relabel",
    "model_fit",
    "research_execution",
)


@dataclass(frozen=True)
class NasdaqBootstrapPolicy:
    policy_id: str
    status: str
    snapshot_a_id: str
    snapshot_a_raw_sha256: str
    snapshot_a_retrieved_at: datetime
    network_registry_id: str | None
    preserved_receipt_file_sha256: str | None
    preserved_receipt_id: str | None
    preserved_record_count: int | None
    completeness: NasdaqCompletenessPolicy

    @property
    def synthetic_only(self) -> bool:
        return self.completeness.synthetic_permit is not None

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "project": PROJECT,
            "status": self.status,
            "bootstrap_class": PRODUCTION_CLASS,
            "source": "nasdaqtraded",
            "url": NASDAQ_TRADED_URL,
            "required_capture_count": 2,
            "snapshot_a": {
                "snapshot_id": self.snapshot_a_id,
                "raw_sha256": self.snapshot_a_raw_sha256,
                "retrieved_at": iso_z(self.snapshot_a_retrieved_at),
            },
            "snapshot_b_requirements": {
                "distinct_snapshot_id": True,
                "distinct_raw_sha256": True,
                "retrieved_after_snapshot_a": True,
                "file_created_after_snapshot_a": True,
            },
            "completeness_policy": {
                "minimum_bytes": self.completeness.minimum_bytes,
                "maximum_bytes": self.completeness.maximum_bytes,
                "minimum_records": self.completeness.minimum_records,
                "maximum_records": self.completeness.maximum_records,
                "maximum_drop_fraction": self.completeness.maximum_drop_fraction,
                "maximum_count_change_fraction": (
                    self.completeness.maximum_count_change_fraction
                ),
            },
            "network_registry_id": self.network_registry_id,
            "preserved_historical_receipt": {
                "path": PRESERVED_RECEIPT_PATH.as_posix(),
                "file_sha256": self.preserved_receipt_file_sha256,
                "receipt_id": self.preserved_receipt_id,
                "record_count": self.preserved_record_count,
                "role": "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT",
            },
            "authorities": {
                "network_calls": False,
                "receipt_publication": False,
                "source_activation": False,
                "historical_relabel": False,
            },
            "prohibitions": list(PROHIBITIONS),
        }

    def validate(self) -> None:
        require_sha256(self.policy_id, "Nasdaq bootstrap policy_id")
        require_sha256(self.snapshot_a_id, "Nasdaq bootstrap snapshot_a_id")
        require_sha256(
            self.snapshot_a_raw_sha256,
            "Nasdaq bootstrap snapshot_a_raw_sha256",
        )
        parse_utc_z(
            iso_z(self.snapshot_a_retrieved_at),
            "Nasdaq bootstrap snapshot_a_retrieved_at",
        )
        self.completeness.validate()
        if self.synthetic_only:
            if (
                self.status != "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
                or self.network_registry_id is not None
                or self.preserved_receipt_file_sha256 is not None
                or self.preserved_receipt_id is not None
                or self.preserved_record_count is not None
            ):
                raise ContractError("synthetic Nasdaq bootstrap policy claims production evidence")
        else:
            if self.status != PRODUCTION_STATUS:
                raise ContractError("production Nasdaq bootstrap status differs")
            require_sha256(
                self.network_registry_id,
                "Nasdaq bootstrap network_registry_id",
            )
            require_sha256(
                self.preserved_receipt_file_sha256,
                "Nasdaq bootstrap preserved receipt file_sha256",
            )
            require_sha256(
                self.preserved_receipt_id,
                "Nasdaq bootstrap preserved receipt_id",
            )
            if (
                isinstance(self.preserved_record_count, bool)
                or not isinstance(self.preserved_record_count, int)
                or self.preserved_record_count < 1
            ):
                raise ContractError(
                    "Nasdaq bootstrap preserved comparison count must be positive"
                )
        if self.policy_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("Nasdaq bootstrap policy_id differs from policy content")

    @classmethod
    def synthetic_fixture(
        cls,
        *,
        snapshot_a: LandedSnapshot,
        completeness: NasdaqCompletenessPolicy,
    ) -> "NasdaqBootstrapPolicy":
        if completeness.synthetic_permit is None:
            raise ContractError("synthetic bootstrap requires a synthetic completeness policy")
        fields = {
            "policy_id": "",
            "status": "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
            "snapshot_a_id": snapshot_a.snapshot_id,
            "snapshot_a_raw_sha256": snapshot_a.raw_sha256,
            "snapshot_a_retrieved_at": snapshot_a.retrieved_at,
            "network_registry_id": None,
            "preserved_receipt_file_sha256": None,
            "preserved_receipt_id": None,
            "preserved_record_count": None,
            "completeness": completeness,
        }
        provisional = cls(**fields)
        policy = cls(
            **{
                **fields,
                "policy_id": sha256_bytes(
                    canonical_json_bytes(provisional.unsigned_dict())
                ),
            }
        )
        policy.validate()
        return policy


def load_nasdaq_bootstrap_policy(
    repo_root: Path,
    *,
    policy_path: Path | None = None,
) -> NasdaqBootstrapPolicy:
    root = Path(repo_root).resolve(strict=True)
    candidate = root / (policy_path or BOOTSTRAP_POLICY_PATH)
    require_contained_path(candidate, root)
    path = candidate.resolve(strict=True)
    reject_link(path)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ContractError("Nasdaq bootstrap policy must be an independent plain file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("Nasdaq bootstrap policy is unreadable") from exc
    expected_fields = {
        "schema_version",
        "project",
        "status",
        "bootstrap_class",
        "source",
        "url",
        "required_capture_count",
        "snapshot_a",
        "snapshot_b_requirements",
        "completeness_policy",
        "network_registry_id",
        "preserved_historical_receipt",
        "authorities",
        "prohibitions",
        "policy_id",
    }
    if not isinstance(payload, dict) or set(payload) != expected_fields:
        raise ContractError("Nasdaq bootstrap policy fields differ")
    policy_id = payload.pop("policy_id")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 2
        or payload["project"] != PROJECT
        or payload["status"] != PRODUCTION_STATUS
        or payload["bootstrap_class"] != PRODUCTION_CLASS
        or payload["source"] != "nasdaqtraded"
        or payload["url"] != NASDAQ_TRADED_URL
        or type(payload["required_capture_count"]) is not int
        or payload["required_capture_count"] != 2
        or payload["snapshot_b_requirements"]
        != {
            "distinct_snapshot_id": True,
            "distinct_raw_sha256": True,
            "retrieved_after_snapshot_a": True,
            "file_created_after_snapshot_a": True,
        }
        or payload["authorities"]
        != {
            "network_calls": False,
            "receipt_publication": False,
            "source_activation": False,
            "historical_relabel": False,
        }
        or payload["prohibitions"] != list(PROHIBITIONS)
    ):
        raise ContractError("Nasdaq bootstrap policy weakens the frozen contract")
    if policy_id != sha256_bytes(canonical_json_bytes(payload)):
        raise ContractError("Nasdaq bootstrap policy_id differs from checked-in content")
    snapshot_a = payload["snapshot_a"]
    completeness_payload = payload["completeness_policy"]
    preserved = payload["preserved_historical_receipt"]
    if (
        not isinstance(snapshot_a, dict)
        or set(snapshot_a) != {"snapshot_id", "raw_sha256", "retrieved_at"}
        or not isinstance(completeness_payload, dict)
        or set(completeness_payload)
        != {
            "minimum_bytes",
            "maximum_bytes",
            "minimum_records",
            "maximum_records",
            "maximum_drop_fraction",
            "maximum_count_change_fraction",
        }
        or not isinstance(preserved, dict)
        or set(preserved)
        != {"path", "file_sha256", "receipt_id", "record_count", "role"}
        or preserved["path"] != PRESERVED_RECEIPT_PATH.as_posix()
        or preserved["role"] != "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT"
    ):
        raise ContractError("Nasdaq bootstrap evidence bindings differ")
    completeness = NasdaqCompletenessPolicy(**completeness_payload)
    preserved_path = root / PRESERVED_RECEIPT_PATH
    require_contained_path(preserved_path, root)
    reject_link(preserved_path)
    if sha256_file(preserved_path) != preserved["file_sha256"]:
        raise ContractError("preserved Nasdaq receipt bytes changed")
    try:
        preserved_payload = json.loads(preserved_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError("preserved Nasdaq receipt is unreadable") from exc
    if (
        preserved_payload.get("receipt_id") != preserved["receipt_id"]
        or preserved_payload.get("record_count") != preserved["record_count"]
    ):
        raise ContractError("preserved Nasdaq comparison metadata differs")
    # This is the immutable registry identity used by the historical two-capture
    # evidence. Actual production snapshots must still revalidate their exact
    # acquisition capability against that identity in
    # ``verify_nasdaq_bootstrap_pair``; the mutable current registry is not a
    # reason to relabel the historical policy.
    policy = NasdaqBootstrapPolicy(
        policy_id=policy_id,
        status=payload["status"],
        snapshot_a_id=require_sha256(
            snapshot_a["snapshot_id"],
            "Nasdaq bootstrap snapshot_a.snapshot_id",
        ),
        snapshot_a_raw_sha256=require_sha256(
            snapshot_a["raw_sha256"],
            "Nasdaq bootstrap snapshot_a.raw_sha256",
        ),
        snapshot_a_retrieved_at=parse_utc_z(
            snapshot_a["retrieved_at"],
            "Nasdaq bootstrap snapshot_a.retrieved_at",
        ),
        network_registry_id=payload["network_registry_id"],
        preserved_receipt_file_sha256=preserved["file_sha256"],
        preserved_receipt_id=preserved["receipt_id"],
        preserved_record_count=preserved["record_count"],
        completeness=completeness,
    )
    policy.validate()
    return policy


def _snapshot_summary(
    snapshot: LandedSnapshot,
    *,
    record_count: int,
    file_created_at: datetime,
) -> dict[str, object]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "raw_sha256": snapshot.raw_sha256,
        "record_count": record_count,
        "retrieved_at": iso_z(snapshot.retrieved_at),
        "file_created_at": iso_z(file_created_at),
    }


def verify_nasdaq_bootstrap_pair(
    snapshot_a: LandedSnapshot,
    snapshot_b: LandedSnapshot,
    *,
    policy: NasdaqBootstrapPolicy,
) -> dict[str, object]:
    """Verify two captures without writing, publishing, activating, or networking."""

    policy.validate()
    if (
        snapshot_a.snapshot_id != policy.snapshot_a_id
        or snapshot_a.raw_sha256 != policy.snapshot_a_raw_sha256
        or snapshot_a.retrieved_at != policy.snapshot_a_retrieved_at
    ):
        raise ContractError("snapshot A differs from the checked bootstrap binding")
    if snapshot_b.snapshot_id == snapshot_a.snapshot_id:
        raise ContractError("snapshot B must have a distinct snapshot ID")
    if snapshot_b.raw_sha256 == snapshot_a.raw_sha256:
        raise ContractError("snapshot B must have distinct raw bytes")
    if snapshot_b.retrieved_at <= snapshot_a.retrieved_at:
        raise ContractError("snapshot B retrieval must be later than snapshot A")
    if not policy.synthetic_only:
        for label, snapshot in (("A", snapshot_a), ("B", snapshot_b)):
            if (
                not snapshot.local_integrity_verified
                or snapshot.acquisition_registry is None
                or snapshot.acquisition_registry.registry_id
                != policy.network_registry_id
            ):
                raise ContractError(
                    f"snapshot {label} is not locally verified under the bound registry"
                )
    records_a = _parse_nasdaq_traded_absolute(
        snapshot_a,
        policy=policy.completeness,
    )
    records_b = _parse_nasdaq_traded_absolute(
        snapshot_b,
        policy=policy.completeness,
    )
    file_created_a = {record.file_created_at for record in records_a}
    file_created_b = {record.file_created_at for record in records_b}
    if len(file_created_a) != 1 or len(file_created_b) != 1:
        raise ContractError("Nasdaq capture does not have one file-creation time")
    created_a = next(iter(file_created_a))
    created_b = next(iter(file_created_b))
    if created_b <= created_a:
        raise ContractError(
            "snapshot B file-creation time must be later than snapshot A"
        )
    count_delta = (len(records_b) - len(records_a)) / len(records_a)
    if count_delta < -policy.completeness.maximum_drop_fraction:
        raise ContractError("Nasdaq bootstrap count drop exceeds the accepted policy")
    if abs(count_delta) > policy.completeness.maximum_count_change_fraction:
        raise ContractError("Nasdaq bootstrap count change exceeds the accepted policy")
    synthetic = policy.synthetic_only
    assessment: dict[str, object] = {
        "schema_version": 2,
        "project": PROJECT,
        "assessment_class": PRODUCTION_CLASS,
        "policy_id": policy.policy_id,
        "status": SYNTHETIC_PASS_STATUS if synthetic else PASS_STATUS,
        "provenance": (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if synthetic
            else "OWNER_OPERATED_LOCAL_INTEGRITY_NOT_INDEPENDENT_PROVENANCE"
        ),
        "snapshot_a": _snapshot_summary(
            snapshot_a,
            record_count=len(records_a),
            file_created_at=created_a,
        ),
        "snapshot_b": _snapshot_summary(
            snapshot_b,
            record_count=len(records_b),
            file_created_at=created_b,
        ),
        "baseline_candidate": {
            "snapshot_id": snapshot_b.snapshot_id,
            "record_count": len(records_b),
            "active": False,
            "publication_authorized": False,
        },
        "preserved_historical_comparison": {
            "receipt_id": policy.preserved_receipt_id,
            "record_count": policy.preserved_record_count,
            "role": "COMPARISON_ONLY_NOT_TRUSTED_NOT_GATE_INPUT",
        },
        "authorities": {
            "network_calls": False,
            "receipt_publication": False,
            "source_activation": False,
            "historical_relabel": False,
        },
        "prohibitions": list(PROHIBITIONS),
    }
    assessment["assessment_id"] = sha256_bytes(canonical_json_bytes(assessment))
    return assessment
