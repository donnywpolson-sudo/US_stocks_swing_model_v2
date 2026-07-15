from __future__ import annotations

import math
import json
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Iterable

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import parse_timestamp, require_aware_utc, require_sha256
from .errors import ContractError, IntegrityError
from .releases import verify_accepted_release


class ActionType(str, Enum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"
    CONVERSION = "CONVERSION"
    DELISTING = "DELISTING"
    OTHER = "OTHER"


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
        if not self.action_id or not self.asset_id:
            raise ContractError("action_id and asset_id are required")
        if not self.source_epoch:
            raise ContractError("corporate action lacks content-addressed source provenance")
        require_sha256(self.source_snapshot_id, "corporate_action.source_snapshot_id")
        require_sha256(self.source_release_id, "corporate_action.source_release_id")
        require_sha256(self.raw_row_sha256, "corporate_action.raw_row_sha256")
        require_aware_utc(self.received_at, "received_at")
        if self.announced_at is not None:
            require_aware_utc(self.announced_at, "announced_at")
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


def _load_action_release_payload(
    release_directory: Path,
    *,
    release_id: str,
    source_epoch: str,
    expected_row_count: int,
) -> tuple[CorporateAction, ...]:
    path = Path(release_directory) / "corporate_actions.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("corporate-action release payload is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "actions"}:
        raise IntegrityError("corporate-action release payload fields differ")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise IntegrityError("corporate-action release payload schema is invalid")
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
        ratio = row["ratio_new_for_old"]
        if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float))):
            raise IntegrityError("corporate-action split ratio must be numeric or null")
        action = CorporateAction(
            action_id=str(row["action_id"]),
            asset_id=str(row["asset_id"]),
            action_type=ActionType(str(row["action_type"])),
            effective_session=date.fromisoformat(str(row["effective_session"])),
            announced_at=(
                parse_timestamp(str(row["announced_at"]), "action.announced_at")
                if row["announced_at"] is not None
                else None
            ),
            received_at=parse_timestamp(str(row["received_at"]), "action.received_at"),
            revision=row["revision"],
            source_snapshot_id=str(row["source_snapshot_id"]),
            source_release_id=release_id,
            source_epoch=source_epoch,
            raw_row_sha256=str(row["raw_row_sha256"]),
            ratio_new_for_old=(float(ratio) if ratio is not None else None),
            voided=row["voided"],
        )
        action.validate()
        actions.append(action)
    return tuple(actions)


class BitemporalActionLedger:
    """Append-only revisions visible only after their provider receipt time."""

    def __init__(
        self,
        actions: Iterable[CorporateAction] = (),
        *,
        verified_release_directory: Path | None = None,
        accepted_release_root: Path | None = None,
        synthetic_permit: SyntheticOnlyPermit | None = None,
    ):
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
            self.trust_eligible = True
            self.verification_receipt_id = manifest.release_id
            loaded_actions = _load_action_release_payload(
                release_directory,
                release_id=manifest.release_id,
                source_epoch=manifest.source_epoch,
                expected_row_count=manifest.row_count,
            )
        else:
            if accepted_release_root is not None:
                raise ContractError("synthetic corporate actions cannot name an accepted release root")
            permit = require_synthetic_permit(
                synthetic_permit,
                scope="SYNTHETIC_CORPORATE_ACTION_LEDGER",
            )
            self.release_id = permit.permit_id
            self.source_epoch = "SYNTHETIC_ONLY"
            self.trust_eligible = False
            self.verification_receipt_id = permit.permit_id
            loaded_actions = tuple(actions)
        self._actions: list[CorporateAction] = []
        if verified_release_directory is not None and tuple(actions):
            raise ContractError("verified corporate-action rows must come only from release payload")
        for action in loaded_actions:
            self._append(action, from_verified_payload=verified_release_directory is not None)

    def append(self, action: CorporateAction) -> None:
        if self.trust_eligible:
            raise ContractError("verified corporate-action ledgers are immutable payload views")
        self._append(action, from_verified_payload=False)

    def _append(self, action: CorporateAction, *, from_verified_payload: bool) -> None:
        action.validate()
        if self.trust_eligible and not from_verified_payload:
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
