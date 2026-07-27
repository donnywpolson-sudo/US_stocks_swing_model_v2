from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .clock import TrustedClock
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, IntegrityError
from .governance import (
    AuthorizationAuthority,
    SignedAuthorizationReceipt,
)
from .releases import verify_accepted_release


class ActionType(str, Enum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    CONVERSION = "CONVERSION"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


EFFECTIVE_EVENT_COMPLETENESS = "EFFECTIVE_EVENT_COMPLETENESS"
AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS = (
    "AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS"
)


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    asset_id: str
    action_type: ActionType
    effective_session: date
    announced_at: datetime | None
    received_at: datetime
    revision: int
    source_snapshot_id: str
    source_release_id: str
    source_epoch: str
    raw_row_sha256: str
    ratio_new_for_old: float | None = None
    voided: bool = False

    def validate(self) -> None:
        if any(
            type(value) is not str or not value
            for value in (
                self.action_id,
                self.asset_id,
                self.source_snapshot_id,
                self.source_release_id,
                self.source_epoch,
                self.raw_row_sha256,
            )
        ):
            raise ContractError(
                "corporate-action identity/provenance fields must be exact text"
            )
        if type(self.action_type) is not ActionType:
            raise ContractError("corporate action must use the exact action type enum")
        if type(self.effective_session) is not date:
            raise ContractError(
                "corporate action effective_session must be an exact date"
            )
        require_sha256(self.source_snapshot_id, "corporate_action.source_snapshot_id")
        require_sha256(self.source_release_id, "corporate_action.source_release_id")
        require_sha256(self.raw_row_sha256, "corporate_action.raw_row_sha256")
        require_aware_utc(self.received_at, "received_at")
        if self.announced_at is not None:
            require_aware_utc(self.announced_at, "announced_at")
        if type(self.voided) is not bool:
            raise ContractError("corporate-action voided must be boolean")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ContractError("corporate-action revision must be positive")
        if self.action_type is ActionType.SPLIT:
            if (
                self.ratio_new_for_old is None
                or isinstance(self.ratio_new_for_old, bool)
                or not isinstance(self.ratio_new_for_old, (int, float))
                or not math.isfinite(self.ratio_new_for_old)
            ):
                raise ContractError("split action requires a finite ratio")
            if self.ratio_new_for_old <= 0:
                raise ContractError("split ratio must be positive")
        elif self.ratio_new_for_old is not None:
            raise ContractError(
                "only split actions may carry ratio_new_for_old"
            )


@dataclass(frozen=True)
class CorporateActionCoverage:
    coverage_id: str
    coverage_content_id: str
    coverage_semantics: str
    effective_start_session: date
    effective_end_session: date
    asset_scope: str
    asset_ids: tuple[str, ...]
    received_at: datetime
    source_snapshot_ids: tuple[str, ...]
    provider_coverage_id: str
    source_release_id: str
    source_epoch: str

    def content_dict(self) -> dict[str, object]:
        return {
            "coverage_semantics": self.coverage_semantics,
            "effective_start_session": self.effective_start_session.isoformat(),
            "effective_end_session": self.effective_end_session.isoformat(),
            "asset_scope": self.asset_scope,
            "asset_ids": list(self.asset_ids),
            "received_at": iso_z(self.received_at),
            "source_snapshot_ids": list(self.source_snapshot_ids),
            "provider_coverage_id": self.provider_coverage_id,
        }

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "coverage_content_id": self.coverage_content_id,
            "source_release_id": self.source_release_id,
            "source_epoch": self.source_epoch,
        }

    def payload_dict(self) -> dict[str, object]:
        return {
            **self.content_dict(),
            "coverage_content_id": self.coverage_content_id,
        }

    def validate(self) -> None:
        if (
            type(self.coverage_semantics) is not str
            or self.coverage_semantics != EFFECTIVE_EVENT_COMPLETENESS
        ):
            raise ContractError(
                "outcome corporate-action coverage must prove effective-event completeness"
            )
        if (
            type(self.effective_start_session) is not date
            or type(self.effective_end_session) is not date
            or self.effective_start_session > self.effective_end_session
        ):
            raise ContractError(
                "corporate-action effective-event coverage interval is invalid"
            )
        if (
            type(self.asset_scope) is not str
            or self.asset_scope not in {"ALL_ASSETS", "EXACT_ASSET_IDS"}
        ):
            raise ContractError("corporate-action coverage asset scope is invalid")
        if (
            type(self.asset_ids) is not tuple
            or self.asset_ids != tuple(sorted(set(self.asset_ids)))
            or any(
                type(value) is not str or not value
                for value in self.asset_ids
            )
            or (self.asset_scope == "ALL_ASSETS" and self.asset_ids)
            or (self.asset_scope == "EXACT_ASSET_IDS" and not self.asset_ids)
        ):
            raise ContractError("corporate-action coverage asset census is invalid")
        require_aware_utc(self.received_at, "corporate_action_coverage.received_at")
        if (
            type(self.source_snapshot_ids) is not tuple
            or not self.source_snapshot_ids
            or self.source_snapshot_ids
            != tuple(sorted(set(self.source_snapshot_ids)))
        ):
            raise ContractError("corporate-action coverage snapshot census is invalid")
        for index, snapshot_id in enumerate(self.source_snapshot_ids):
            require_sha256(snapshot_id, f"corporate_action_coverage.source_snapshot_ids[{index}]")
        require_sha256(
            self.provider_coverage_id,
            "corporate_action_coverage.provider_coverage_id",
        )
        require_sha256(self.source_release_id, "corporate_action_coverage.source_release_id")
        if type(self.source_epoch) is not str or not self.source_epoch:
            raise ContractError(
                "corporate-action coverage source epoch must be exact text"
            )
        require_sha256(
            self.coverage_content_id,
            "corporate_action_coverage.coverage_content_id",
        )
        if self.coverage_content_id != sha256_bytes(
            canonical_json_bytes(self.content_dict())
        ):
            raise ContractError(
                "corporate-action coverage content ID differs from its content"
            )
        require_sha256(self.coverage_id, "corporate_action_coverage.coverage_id")
        if self.coverage_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError(
                "corporate-action coverage ID differs from its release binding"
            )

    @classmethod
    def create(
        cls,
        *,
        effective_start_session: date,
        effective_end_session: date,
        asset_scope: str,
        asset_ids: tuple[str, ...],
        received_at: datetime,
        source_snapshot_ids: tuple[str, ...],
        provider_coverage_id: str,
        source_release_id: str,
        source_epoch: str,
    ) -> "CorporateActionCoverage":
        content = {
            "coverage_semantics": EFFECTIVE_EVENT_COMPLETENESS,
            "effective_start_session": effective_start_session.isoformat(),
            "effective_end_session": effective_end_session.isoformat(),
            "asset_scope": asset_scope,
            "asset_ids": list(asset_ids),
            "received_at": iso_z(received_at),
            "source_snapshot_ids": list(source_snapshot_ids),
            "provider_coverage_id": provider_coverage_id,
        }
        content_id = sha256_bytes(canonical_json_bytes(content))
        binding = {
            "coverage_content_id": content_id,
            "source_release_id": source_release_id,
            "source_epoch": source_epoch,
        }
        coverage = cls(
            coverage_id=sha256_bytes(canonical_json_bytes(binding)),
            coverage_content_id=content_id,
            coverage_semantics=EFFECTIVE_EVENT_COMPLETENESS,
            effective_start_session=effective_start_session,
            effective_end_session=effective_end_session,
            asset_scope=asset_scope,
            asset_ids=asset_ids,
            received_at=received_at,
            source_snapshot_ids=source_snapshot_ids,
            provider_coverage_id=provider_coverage_id,
            source_release_id=source_release_id,
            source_epoch=source_epoch,
        )
        coverage.validate()
        return coverage


def _coverage_contains_action(
    coverage: CorporateActionCoverage,
    action: CorporateAction,
) -> bool:
    return (
        action.source_epoch == coverage.source_epoch
        and action.source_snapshot_id in coverage.source_snapshot_ids
        and coverage.effective_start_session
        <= action.effective_session
        <= coverage.effective_end_session
        and (
            coverage.asset_scope == "ALL_ASSETS"
            or action.asset_id in coverage.asset_ids
        )
    )


@dataclass(frozen=True)
class PreparedEffectiveEventCoverage:
    coverage: CorporateActionCoverage
    provider_coverage: Any
    provider_contract_id: str
    late_arrival_policy_id: str

    def validate(self) -> None:
        from .providers.corporate_actions import (
            CorporateActionCoverageEvidence,
            PROCESS_DATE_ACQUISITION_COVERAGE,
        )

        if type(self.coverage) is not CorporateActionCoverage:
            raise ContractError(
                "prepared effective-event coverage object is invalid"
            )
        self.coverage.validate()
        if type(self.provider_coverage) is not CorporateActionCoverageEvidence:
            raise ContractError(
                "effective-event promotion requires exact provider coverage evidence"
            )
        self.provider_coverage.validate()
        if (
            self.provider_coverage.coverage_semantics
            != PROCESS_DATE_ACQUISITION_COVERAGE
        ):
            raise ContractError(
                "effective-event promotion source must remain process-date acquisition evidence"
            )
        for name, value in (
            ("provider_contract_id", self.provider_contract_id),
            ("late_arrival_policy_id", self.late_arrival_policy_id),
        ):
            require_sha256(value, f"effective_event_coverage.{name}")
        if (
            self.coverage.provider_coverage_id
            != self.provider_coverage.coverage_id
            or self.coverage.source_snapshot_ids
            != tuple(sorted(self.provider_coverage.snapshot_ids))
            or self.coverage.source_epoch != self.provider_coverage.source_epoch
            or self.coverage.received_at < self.provider_coverage.completed_at
        ):
            raise ContractError(
                "effective-event coverage differs from its provider evidence"
            )

    def authorization_bindings(self) -> dict[str, str]:
        self.validate()
        provider = self.provider_coverage
        coverage = self.coverage
        return {
            "coverage_content_id": coverage.coverage_content_id,
            "provider_coverage_id": provider.coverage_id,
            "provider_process_date_start": provider.process_date_start.isoformat(),
            "provider_process_date_end": provider.process_date_end.isoformat(),
            "provider_snapshot_census_hash": sha256_bytes(
                canonical_json_bytes(list(provider.snapshot_ids))
            ),
            "provider_requested_symbol_census_hash": sha256_bytes(
                canonical_json_bytes(list(provider.requested_symbols))
            ),
            "provider_acquisition_mode": provider.acquisition_mode,
            "provider_contract_id": self.provider_contract_id,
            "late_arrival_policy_id": self.late_arrival_policy_id,
            "effective_start_session": (
                coverage.effective_start_session.isoformat()
            ),
            "effective_end_session": coverage.effective_end_session.isoformat(),
            "asset_scope": coverage.asset_scope,
            "asset_id_census_hash": sha256_bytes(
                canonical_json_bytes(list(coverage.asset_ids))
            ),
            "coverage_received_at": iso_z(coverage.received_at),
            "source_epoch": coverage.source_epoch,
        }


@dataclass(frozen=True)
class GovernedEffectiveEventCoverage:
    prepared: PreparedEffectiveEventCoverage
    authorization: SignedAuthorizationReceipt

    @property
    def coverage(self) -> CorporateActionCoverage:
        return self.prepared.coverage

    def validate(
        self,
        *,
        authority: AuthorizationAuthority,
        clock: TrustedClock,
    ) -> None:
        self.prepared.validate()
        provider_mode = self.prepared.provider_coverage.acquisition_mode
        if (
            authority.authorization_class == "EXTERNAL_USER_AUTHORITY"
            and provider_mode != "NETWORK_AS_RECEIVED"
        ) or (
            authority.authorization_class == "SYNTHETIC_ONLY_NOT_AUTHORITY"
            and provider_mode != "SYNTHETIC_DIRECT_NOT_AS_RECEIVED"
        ):
            raise ContractError(
                "effective-event authority class differs from provider evidence"
            )
        self.authorization.validate(
            authority=authority,
            expected_scope=AUTHORIZE_EFFECTIVE_EVENT_COMPLETENESS,
            expected_subject_id=self.coverage.coverage_content_id,
            required_bindings=self.prepared.authorization_bindings(),
            clock=clock,
        )

    def payload_dict(self) -> dict[str, object]:
        return {
            "coverage": self.coverage.payload_dict(),
            "provider_coverage": self.prepared.provider_coverage.as_dict(),
            "provider_contract_id": self.prepared.provider_contract_id,
            "late_arrival_policy_id": self.prepared.late_arrival_policy_id,
            "authorization": self.authorization.as_dict(),
        }


def prepare_effective_event_coverage(
    provider_coverage: Any,
    *,
    effective_start_session: date,
    effective_end_session: date,
    asset_scope: str,
    asset_ids: tuple[str, ...],
    reviewed_at: datetime,
    source_release_id: str,
    provider_contract_id: str,
    late_arrival_policy_id: str,
) -> PreparedEffectiveEventCoverage:
    from .providers.corporate_actions import CorporateActionCoverageEvidence

    if type(provider_coverage) is not CorporateActionCoverageEvidence:
        raise ContractError(
            "effective-event promotion requires exact provider coverage evidence"
        )
    provider_coverage.validate()
    coverage = CorporateActionCoverage.create(
        effective_start_session=effective_start_session,
        effective_end_session=effective_end_session,
        asset_scope=asset_scope,
        asset_ids=asset_ids,
        received_at=reviewed_at,
        source_snapshot_ids=tuple(sorted(provider_coverage.snapshot_ids)),
        provider_coverage_id=provider_coverage.coverage_id,
        source_release_id=source_release_id,
        source_epoch=provider_coverage.source_epoch,
    )
    prepared = PreparedEffectiveEventCoverage(
        coverage=coverage,
        provider_coverage=provider_coverage,
        provider_contract_id=provider_contract_id,
        late_arrival_policy_id=late_arrival_policy_id,
    )
    prepared.validate()
    return prepared


def authorize_effective_event_coverage(
    prepared: PreparedEffectiveEventCoverage,
    *,
    authorization: SignedAuthorizationReceipt,
    authority: AuthorizationAuthority,
    clock: TrustedClock,
) -> GovernedEffectiveEventCoverage:
    governed = GovernedEffectiveEventCoverage(
        prepared=prepared,
        authorization=authorization,
    )
    governed.validate(authority=authority, clock=clock)
    return governed


def build_governed_corporate_action_release_payload(
    *,
    actions: Iterable[CorporateAction],
    governed_coverage: Iterable[GovernedEffectiveEventCoverage],
    authority: AuthorizationAuthority,
    clock: TrustedClock,
) -> bytes:
    action_rows: list[dict[str, object]] = []
    seen_action_revisions: set[tuple[str, int]] = set()
    action_items = tuple(actions)
    governed_items = tuple(governed_coverage)
    if not governed_items:
        raise ContractError(
            "schema-v5 corporate-action publication requires governed coverage"
        )
    coverage_rows: list[dict[str, object]] = []
    seen_coverage_content_ids: set[str] = set()
    authorized_coverage: list[CorporateActionCoverage] = []
    source_epochs: set[str] = set()
    for governed in governed_items:
        if type(governed) is not GovernedEffectiveEventCoverage:
            raise ContractError(
                "schema-v5 publication contains invalid governed coverage"
            )
        governed.validate(authority=authority, clock=clock)
        content_id = governed.coverage.coverage_content_id
        if content_id in seen_coverage_content_ids:
            raise ContractError(
                "schema-v5 publication repeats governed coverage content"
            )
        seen_coverage_content_ids.add(content_id)
        authorized_coverage.append(governed.coverage)
        source_epochs.add(governed.coverage.source_epoch)
        coverage_rows.append(governed.payload_dict())
    if len(source_epochs) != 1:
        raise ContractError(
            "schema-v5 governed coverage mixes source epochs"
        )
    expected_epoch = next(iter(source_epochs))
    for action in action_items:
        if type(action) is not CorporateAction:
            raise ContractError(
                "schema-v5 publication contains an invalid corporate action"
            )
        action.validate()
        key = (action.action_id, action.revision)
        if key in seen_action_revisions:
            raise ContractError(
                "schema-v5 publication repeats a corporate-action revision"
            )
        seen_action_revisions.add(key)
        if action.source_epoch != expected_epoch:
            raise ContractError(
                "schema-v5 action provenance differs from governed provider coverage"
            )
        if not any(
            _coverage_contains_action(coverage, action)
            for coverage in authorized_coverage
        ):
            raise ContractError(
                "schema-v5 action is outside governed asset/session coverage"
            )
        action_rows.append(
            {
                "action_id": action.action_id,
                "asset_id": action.asset_id,
                "action_type": action.action_type.value,
                "effective_session": action.effective_session.isoformat(),
                "announced_at": (
                    iso_z(action.announced_at)
                    if action.announced_at is not None
                    else None
                ),
                "received_at": iso_z(action.received_at),
                "revision": action.revision,
                "source_snapshot_id": action.source_snapshot_id,
                "raw_row_sha256": action.raw_row_sha256,
                "ratio_new_for_old": action.ratio_new_for_old,
                "voided": action.voided,
            }
        )
    action_rows.sort(key=lambda row: (row["action_id"], row["revision"]))
    coverage_rows.sort(
        key=lambda row: row["coverage"]["coverage_content_id"]
    )
    return canonical_json_bytes(
        {
            "schema_version": 5,
            "actions": action_rows,
            "coverage": coverage_rows,
        }
    )


def _load_action_release_payload(
    release_directory: Path,
    *,
    release_id: str,
    source_epoch: str,
    expected_row_count: int,
    coverage_authorization_authority: AuthorizationAuthority | None,
    clock: TrustedClock | None,
) -> tuple[
    tuple[CorporateAction, ...],
    tuple[CorporateActionCoverage, ...],
    int,
]:
    path = Path(release_directory) / "corporate_actions.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("corporate-action release payload is missing or invalid") from exc
    if not isinstance(payload, dict):
        raise IntegrityError("corporate-action release payload fields differ")
    if type(payload.get("schema_version")) is not int:
        raise IntegrityError("corporate-action release payload schema is invalid")
    if payload["schema_version"] in {2, 3, 4}:
        raise IntegrityError(
            "corporate-action schema lacks governed effective-event coverage authorization"
        )
    if payload["schema_version"] not in {1, 5}:
        raise IntegrityError("corporate-action release payload schema is invalid")
    expected_fields = (
        {"schema_version", "actions"}
        if payload["schema_version"] == 1
        else {"schema_version", "actions", "coverage"}
    )
    if set(payload) != expected_fields:
        raise IntegrityError("corporate-action release payload fields differ")
    if not isinstance(payload["actions"], list) or len(payload["actions"]) != expected_row_count:
        raise IntegrityError("corporate-action release row_count differs from payload")
    payload_fields = set(CorporateAction.__dataclass_fields__) - {
        "source_release_id",
        "source_epoch",
    }
    actions: list[CorporateAction] = []
    for row in payload["actions"]:
        if not isinstance(row, dict) or set(row) != payload_fields:
            raise IntegrityError("corporate-action payload row fields differ")
        if type(row["revision"]) is not int or type(row["voided"]) is not bool:
            raise IntegrityError("corporate-action revision/voided types are invalid")
        exact_text_fields = (
            "action_id",
            "asset_id",
            "action_type",
            "effective_session",
            "received_at",
            "source_snapshot_id",
            "raw_row_sha256",
        )
        if any(type(row[name]) is not str for name in exact_text_fields):
            raise IntegrityError(
                "corporate-action identity/provenance fields must be exact text"
            )
        if row["announced_at"] is not None and type(row["announced_at"]) is not str:
            raise IntegrityError(
                "corporate-action announced_at must be exact text or null"
            )
        ratio = row["ratio_new_for_old"]
        if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float))):
            raise IntegrityError("corporate-action split ratio must be numeric or null")
        try:
            action_type = ActionType(row["action_type"])
            effective_session = date.fromisoformat(row["effective_session"])
        except (TypeError, ValueError) as exc:
            raise IntegrityError(
                "corporate-action action type or effective session is invalid"
            ) from exc
        action = CorporateAction(
            action_id=row["action_id"],
            asset_id=row["asset_id"],
            action_type=action_type,
            effective_session=effective_session,
            announced_at=(
                parse_utc_z(row["announced_at"], "action.announced_at")
                if row["announced_at"] is not None
                else None
            ),
            received_at=parse_utc_z(row["received_at"], "action.received_at"),
            revision=row["revision"],
            source_snapshot_id=row["source_snapshot_id"],
            source_release_id=release_id,
            source_epoch=source_epoch,
            raw_row_sha256=row["raw_row_sha256"],
            ratio_new_for_old=(float(ratio) if ratio is not None else None),
            voided=row["voided"],
        )
        action.validate()
        actions.append(action)
    coverage_rows = payload.get("coverage", [])
    if not isinstance(coverage_rows, list):
        raise IntegrityError("corporate-action release coverage must be an exact JSON array")
    if payload["schema_version"] == 1:
        return tuple(actions), (), 1
    if (
        type(coverage_authorization_authority) is not AuthorizationAuthority
        or type(clock) is not TrustedClock
    ):
        raise ContractError(
            "governed corporate-action coverage requires an authority and trusted clock"
        )
    from .providers.corporate_actions import CorporateActionCoverageEvidence

    coverage_fields = set(CorporateActionCoverage.__dataclass_fields__) - {
        "coverage_id",
        "source_release_id",
        "source_epoch",
    }
    coverage: list[CorporateActionCoverage] = []
    governed_fields = {
        "coverage",
        "provider_coverage",
        "provider_contract_id",
        "late_arrival_policy_id",
        "authorization",
    }
    for governed_row in coverage_rows:
        if type(governed_row) is not dict or set(governed_row) != governed_fields:
            raise IntegrityError(
                "governed corporate-action coverage row fields differ"
            )
        if (
            type(governed_row["coverage"]) is not dict
            or type(governed_row["provider_coverage"]) is not dict
            or type(governed_row["authorization"]) is not dict
            or type(governed_row["provider_contract_id"]) is not str
            or type(governed_row["late_arrival_policy_id"]) is not str
        ):
            raise IntegrityError(
                "governed corporate-action coverage evidence types differ"
            )
        row = governed_row["coverage"]
        if set(row) != coverage_fields:
            raise IntegrityError("corporate-action coverage row fields differ")
        if type(row["asset_ids"]) is not list or type(row["source_snapshot_ids"]) is not list:
            raise IntegrityError("corporate-action coverage censuses require JSON arrays")
        for name in (
            "coverage_content_id",
            "coverage_semantics",
            "effective_start_session",
            "effective_end_session",
            "asset_scope",
            "received_at",
            "provider_coverage_id",
        ):
            if type(row[name]) is not str:
                raise IntegrityError(
                    f"corporate-action coverage {name} must be exact text"
                )
        if any(
            type(value) is not str
            for value in (*row["asset_ids"], *row["source_snapshot_ids"])
        ):
            raise IntegrityError(
                "corporate-action coverage censuses require exact text"
            )
        binding = {
            "coverage_content_id": row["coverage_content_id"],
            "source_release_id": release_id,
            "source_epoch": source_epoch,
        }
        try:
            effective_start_session = date.fromisoformat(
                row["effective_start_session"]
            )
            effective_end_session = date.fromisoformat(
                row["effective_end_session"]
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityError(
                "corporate-action coverage effective interval is invalid"
            ) from exc
        item = CorporateActionCoverage(
            coverage_id=sha256_bytes(canonical_json_bytes(binding)),
            coverage_content_id=row["coverage_content_id"],
            coverage_semantics=row["coverage_semantics"],
            effective_start_session=effective_start_session,
            effective_end_session=effective_end_session,
            asset_scope=row["asset_scope"],
            asset_ids=tuple(row["asset_ids"]),
            received_at=parse_utc_z(row["received_at"], "coverage.received_at"),
            source_snapshot_ids=tuple(row["source_snapshot_ids"]),
            provider_coverage_id=row["provider_coverage_id"],
            source_release_id=release_id,
            source_epoch=source_epoch,
        )
        item.validate()
        try:
            provider_coverage = CorporateActionCoverageEvidence.from_dict(
                governed_row["provider_coverage"]
            )
        except ContractError as exc:
            raise IntegrityError(
                "provider corporate-action coverage evidence is invalid"
            ) from exc
        if provider_coverage.source_epoch != source_epoch:
            raise IntegrityError(
                "provider corporate-action coverage epoch differs from release"
            )
        prepared = prepare_effective_event_coverage(
            provider_coverage,
            effective_start_session=item.effective_start_session,
            effective_end_session=item.effective_end_session,
            asset_scope=item.asset_scope,
            asset_ids=item.asset_ids,
            reviewed_at=item.received_at,
            source_release_id=release_id,
            provider_contract_id=governed_row["provider_contract_id"],
            late_arrival_policy_id=governed_row["late_arrival_policy_id"],
        )
        if prepared.coverage != item:
            raise IntegrityError(
                "governed effective-event coverage differs from serialized evidence"
            )
        authorization = SignedAuthorizationReceipt.from_dict(
            governed_row["authorization"]
        )
        governed = authorize_effective_event_coverage(
            prepared,
            authorization=authorization,
            authority=coverage_authorization_authority,
            clock=clock,
        )
        coverage.append(governed.coverage)
    if not coverage:
        raise IntegrityError(
            "corporate-action schema v5 requires governed coverage"
        )
    for action in actions:
        if not any(
            _coverage_contains_action(item, action)
            for item in coverage
        ):
            raise IntegrityError(
                "corporate-action row is outside governed asset/session coverage"
            )
    return tuple(actions), tuple(coverage), 5


class BitemporalActionLedger:
    """Append-only revisions visible only after their provider receipt time."""

    def __init__(
        self,
        actions: Iterable[CorporateAction] = (),
        *,
        verified_release_directory: Path | None = None,
        accepted_release_root: Path | None = None,
        synthetic_permit: SyntheticOnlyPermit | None = None,
        coverage: Iterable[CorporateActionCoverage] = (),
        coverage_authorization_authority: AuthorizationAuthority | None = None,
        clock: TrustedClock | None = None,
    ):
        supplied_actions = tuple(actions)
        supplied_coverage = tuple(coverage)
        self._verified_release_view = verified_release_directory is not None
        if (verified_release_directory is None) == (synthetic_permit is None):
            raise ContractError(
                "corporate-action ledger requires exactly one verified release or synthetic-only permit"
        )
        if verified_release_directory is not None:
            if accepted_release_root is None:
                raise ContractError("verified corporate actions require their accepted release root")
            release_directory = Path(verified_release_directory)
            manifest = verify_accepted_release(
                release_directory,
                accepted_root=Path(accepted_release_root),
            )
            if (
                manifest.project != "US_stocks_swing_model_v2"
                or manifest.dataset != "corporate_actions"
                or manifest.role != "prospective_as_received"
                or manifest.quality_state != "PASS"
            ):
                raise ContractError("corporate-action release is not trust eligible")
            self.release_id = manifest.release_id
            self.source_epoch = manifest.source_epoch
            self.verification_receipt_id = manifest.release_id
            loaded_actions, loaded_coverage, payload_schema_version = (
                _load_action_release_payload(
                    release_directory,
                    release_id=manifest.release_id,
                    source_epoch=manifest.source_epoch,
                    expected_row_count=manifest.row_count,
                    coverage_authorization_authority=coverage_authorization_authority,
                    clock=clock,
                )
            )
            self.trust_eligible = payload_schema_version == 5
        else:
            if accepted_release_root is not None:
                raise ContractError("synthetic corporate actions cannot name an accepted release root")
            if coverage_authorization_authority is not None or clock is not None:
                raise ContractError(
                    "synthetic corporate-action ledgers cannot name release coverage authority"
                )
            permit = require_synthetic_permit(
                synthetic_permit,
                scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
            )
            self.release_id = permit.permit_id
            self.source_epoch = "SYNTHETIC_ONLY"
            self.trust_eligible = False
            self.verification_receipt_id = permit.permit_id
            loaded_actions = supplied_actions
            loaded_coverage = supplied_coverage
        self._actions: list[CorporateAction] = []
        self._coverage: list[CorporateActionCoverage] = []
        if verified_release_directory is not None and (
            supplied_actions or supplied_coverage
        ):
            raise ContractError(
                "verified corporate-action rows/coverage must come only from release payload"
            )
        for action in loaded_actions:
            self._append(action, from_verified_payload=verified_release_directory is not None)
        for item in loaded_coverage:
            self._append_coverage(
                item,
                from_verified_payload=verified_release_directory is not None,
            )

    def append(self, action: CorporateAction) -> None:
        if self._verified_release_view:
            raise ContractError("verified corporate-action ledgers are immutable payload views")
        self._append(action, from_verified_payload=False)

    def _append(self, action: CorporateAction, *, from_verified_payload: bool) -> None:
        action.validate()
        if self._verified_release_view and not from_verified_payload:
            raise ContractError("verified corporate actions must originate in the release payload")
        if (
            action.source_release_id != self.release_id
            or action.source_epoch != self.source_epoch
        ):
            raise ContractError("corporate action provenance differs from its verified ledger release")
        prior = [existing for existing in self._actions if existing.action_id == action.action_id]
        for existing in self._actions:
            if existing.action_id == action.action_id and existing.revision == action.revision:
                if existing != action:
                    raise IntegrityError("conflicting corporate-action revision")
                return
        if prior:
            latest = max(prior, key=lambda existing: existing.revision)
            if action.revision <= latest.revision:
                raise IntegrityError("corporate-action revisions must append monotonically")
            if action.received_at < latest.received_at:
                raise IntegrityError("corporate-action receipt time cannot move backward")
        self._actions.append(action)

    def _append_coverage(
        self,
        coverage: CorporateActionCoverage,
        *,
        from_verified_payload: bool,
    ) -> None:
        coverage.validate()
        if self._verified_release_view and not from_verified_payload:
            raise ContractError("verified corporate-action coverage must originate in release payload")
        if (
            coverage.source_release_id != self.release_id
            or coverage.source_epoch != self.source_epoch
        ):
            raise ContractError("corporate-action coverage provenance differs from its ledger")
        if any(existing.coverage_id == coverage.coverage_id for existing in self._coverage):
            if coverage not in self._coverage:
                raise IntegrityError("conflicting corporate-action coverage receipt")
            return
        self._coverage.append(coverage)

    def covers_effective_interval(
        self,
        asset_id: str,
        start_session: date,
        end_session: date,
        as_of: datetime,
    ) -> bool:
        cutoff = require_aware_utc(as_of, "as_of")
        return any(
            coverage.coverage_semantics == EFFECTIVE_EVENT_COMPLETENESS
            and coverage.effective_start_session <= start_session
            and coverage.effective_end_session >= end_session
            and coverage.received_at <= cutoff
            and (
                coverage.asset_scope == "ALL_ASSETS"
                or asset_id in coverage.asset_ids
            )
            for coverage in self._coverage
        )

    def covers_interval(
        self,
        asset_id: str,
        start_session: date,
        end_session: date,
        as_of: datetime,
    ) -> bool:
        """Compatibility alias for explicitly effective-event coverage."""

        return self.covers_effective_interval(
            asset_id,
            start_session,
            end_session,
            as_of,
        )

    def visible_as_of(self, asset_id: str, as_of: datetime) -> tuple[CorporateAction, ...]:
        cutoff = require_aware_utc(as_of, "as_of")
        latest: dict[str, CorporateAction] = {}
        for action in self._actions:
            if action.asset_id != asset_id or require_aware_utc(action.received_at, "received_at") > cutoff:
                continue
            current = latest.get(action.action_id)
            if current is None or action.revision > current.revision:
                latest[action.action_id] = action
        return tuple(sorted((a for a in latest.values() if not a.voided), key=lambda a: (a.effective_session, a.action_id)))

    def effective_between(
        self,
        asset_id: str,
        start_exclusive: date,
        end_inclusive: date,
        as_of: datetime,
    ) -> tuple[CorporateAction, ...]:
        return tuple(
            action
            for action in self.visible_as_of(asset_id, as_of)
            if start_exclusive < action.effective_session <= end_inclusive
        )
