from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_timestamp,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, IntegrityError
from .providers.nasdaq import IdentityRecord as NasdaqIdentityRecord
from .providers.snapshots import LandedSnapshot
from .releases import verify_accepted_release
from .schemas import SecurityType


@dataclass(frozen=True)
class AlpacaAssetRecord:
    asset_id: str
    symbol: str
    asset_class: str
    exchange: str
    status: str
    tradable: bool
    snapshot_id: str
    known_at: datetime
    evidence_state: str
    synthetic_permit_ids: tuple[str, ...]


@dataclass(frozen=True)
class IdentityVersion:
    asset_id: str
    symbol: str
    security_type: SecurityType
    listing_exchange: str
    active: bool
    eligible: bool
    membership_present: bool
    abstention_reason: str | None
    effective_at: datetime
    known_at: datetime
    identity_snapshot_id: str
    alpaca_snapshot_id: str
    nasdaq_snapshot_id: str | None
    nasdaq_file_created_at: datetime | None
    evidence_state: str
    synthetic_permit_ids: tuple[str, ...]

    def receipt_dict(self, *, include_snapshot_id: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "security_type": self.security_type.value,
            "listing_exchange": self.listing_exchange,
            "active": self.active,
            "eligible": self.eligible,
            "membership_present": self.membership_present,
            "abstention_reason": self.abstention_reason,
            "effective_at": iso_z(self.effective_at),
            "known_at": iso_z(self.known_at),
            "alpaca_snapshot_id": self.alpaca_snapshot_id,
            "nasdaq_snapshot_id": self.nasdaq_snapshot_id,
            "nasdaq_file_created_at": (
                iso_z(self.nasdaq_file_created_at) if self.nasdaq_file_created_at else None
            ),
            "evidence_state": self.evidence_state,
            "synthetic_permit_ids": list(self.synthetic_permit_ids),
        }
        if include_snapshot_id:
            payload["identity_snapshot_id"] = self.identity_snapshot_id
        return payload


@dataclass(frozen=True)
class IdentitySnapshot:
    snapshot_id: str
    effective_at: datetime
    known_at: datetime
    complete_membership: bool
    alpaca_snapshot_id: str
    nasdaq_snapshot_id: str
    nasdaq_file_created_at: datetime
    evidence_state: str
    synthetic_permit_ids: tuple[str, ...]
    rows: tuple[IdentityVersion, ...]

    @property
    def trust_eligible(self) -> bool:
        return self.evidence_state == "NETWORK_AS_RECEIVED" and not self.synthetic_permit_ids

    def receipt_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "effective_at": iso_z(self.effective_at),
            "known_at": iso_z(self.known_at),
            "complete_membership": self.complete_membership,
            "alpaca_snapshot_id": self.alpaca_snapshot_id,
            "nasdaq_snapshot_id": self.nasdaq_snapshot_id,
            "nasdaq_file_created_at": iso_z(self.nasdaq_file_created_at),
            "evidence_state": self.evidence_state,
            "synthetic_permit_ids": list(self.synthetic_permit_ids),
            "rows": [row.receipt_dict() for row in self.rows],
        }

    def validate(self) -> None:
        effective = require_aware_utc(self.effective_at, "effective_at")
        known = require_aware_utc(self.known_at, "known_at")
        file_created = require_aware_utc(self.nasdaq_file_created_at, "nasdaq_file_created_at")
        if not self.complete_membership or not self.rows:
            raise ContractError("identity snapshot must be an explicit complete nonempty membership snapshot")
        if not any(row.membership_present for row in self.rows):
            raise ContractError("complete identity snapshot contains no Nasdaq-census members")
        if effective > known or file_created > known:
            raise ContractError("identity effective/file time cannot follow knowledge time")
        if not self.alpaca_snapshot_id or not self.nasdaq_snapshot_id:
            raise ContractError("identity source snapshot IDs are required")
        require_sha256(self.snapshot_id, "identity.snapshot_id")
        require_sha256(self.alpaca_snapshot_id, "identity.alpaca_snapshot_id")
        require_sha256(self.nasdaq_snapshot_id, "identity.nasdaq_snapshot_id")
        if self.evidence_state == "NETWORK_AS_RECEIVED":
            if self.synthetic_permit_ids:
                raise ContractError("trust-eligible identity cannot carry synthetic permits")
        elif self.evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE":
            if not self.synthetic_permit_ids:
                raise ContractError("synthetic identity must bind its mechanics permits")
            for index, permit_id in enumerate(self.synthetic_permit_ids):
                require_sha256(permit_id, f"identity.synthetic_permit_ids[{index}]")
        else:
            raise ContractError("identity evidence state is invalid")
        asset_ids = [row.asset_id for row in self.rows]
        symbols = [row.symbol for row in self.rows]
        if len(asset_ids) != len(set(asset_ids)) or len(symbols) != len(set(symbols)):
            raise ContractError("complete identity snapshot asset IDs and symbols must be unique")
        for row in self.rows:
            if (
                row.identity_snapshot_id != self.snapshot_id
                or require_aware_utc(row.effective_at, "row.effective_at") != effective
                or require_aware_utc(row.known_at, "row.known_at") != known
                or row.alpaca_snapshot_id != self.alpaca_snapshot_id
                or row.nasdaq_snapshot_id not in {None, self.nasdaq_snapshot_id}
                or row.nasdaq_file_created_at not in {None, self.nasdaq_file_created_at}
                or row.evidence_state != self.evidence_state
                or row.synthetic_permit_ids != self.synthetic_permit_ids
            ):
                raise ContractError("identity row does not bind the complete snapshot receipt")
            if not row.membership_present and (
                row.eligible
                or row.nasdaq_snapshot_id is not None
                or row.nasdaq_file_created_at is not None
            ):
                raise ContractError("nonmember identity row cannot claim Nasdaq membership evidence")
        unsigned = self.receipt_dict()
        unsigned.pop("snapshot_id")
        for row in unsigned["rows"]:
            row.pop("identity_snapshot_id")
        if self.snapshot_id != sha256_bytes(canonical_json_bytes(unsigned)):
            raise IntegrityError("identity snapshot ID differs from its receipt")


def parse_alpaca_assets(snapshot: LandedSnapshot) -> tuple[AlpacaAssetRecord, ...]:
    if snapshot.source != "alpaca_assets":
        raise ContractError("snapshot is not an Alpaca asset snapshot")
    if snapshot.http_status != 200:
        raise ContractError("Alpaca asset snapshot HTTP status is not successful")
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except json.JSONDecodeError as exc:
        raise ContractError("Alpaca asset snapshot is not JSON") from exc
    rows = payload.get("assets") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ContractError("Alpaca asset snapshot root must be a list or assets list")
    records: list[AlpacaAssetRecord] = []
    seen_ids: set[str] = set()
    seen_symbols: set[str] = set()
    required = {"id", "symbol", "class", "exchange", "status", "tradable"}
    for row in rows:
        if not isinstance(row, dict) or not required <= row.keys():
            raise ContractError("Alpaca asset row lacks frozen fields")
        asset_id = str(row["id"])
        symbol = str(row["symbol"]).strip().upper()
        if not asset_id or not symbol or asset_id in seen_ids or symbol in seen_symbols:
            raise ContractError("Alpaca asset IDs and symbols must be nonempty and unique per snapshot")
        seen_ids.add(asset_id)
        seen_symbols.add(symbol)
        if row["class"] != "us_equity" or not isinstance(row["tradable"], bool):
            raise ContractError("Alpaca asset row is not a well-formed US equity")
        records.append(
            AlpacaAssetRecord(
                asset_id=asset_id,
                symbol=symbol,
                asset_class=row["class"],
                exchange=str(row["exchange"]),
                status=str(row["status"]),
                tradable=row["tradable"],
                snapshot_id=snapshot.snapshot_id,
                known_at=snapshot.retrieved_at,
                evidence_state=(
                    "NETWORK_AS_RECEIVED"
                    if snapshot.trust_eligible
                    else "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
                ),
                synthetic_permit_ids=(
                    ()
                    if snapshot.synthetic_permit_id is None
                    else (snapshot.synthetic_permit_id,)
                ),
            )
        )
    return tuple(records)


def merge_identity_snapshot(
    alpaca_assets: Iterable[AlpacaAssetRecord],
    nasdaq_records: Iterable[NasdaqIdentityRecord],
) -> IdentitySnapshot:
    assets = tuple(alpaca_assets)
    listings = tuple(nasdaq_records)
    if not assets or not listings:
        raise ContractError("complete identity merge requires both nonempty source snapshots")
    alpaca_snapshot_ids = {record.snapshot_id for record in assets}
    alpaca_known = {require_aware_utc(record.known_at, "alpaca.known_at") for record in assets}
    nasdaq_snapshot_ids = {record.snapshot_id for record in listings}
    nasdaq_known = {require_aware_utc(record.snapshot_retrieved_at, "nasdaq.known_at") for record in listings}
    nasdaq_created = {require_aware_utc(record.file_created_at, "nasdaq.file_created_at") for record in listings}
    if any(len(values) != 1 for values in (alpaca_snapshot_ids, alpaca_known, nasdaq_snapshot_ids, nasdaq_known, nasdaq_created)):
        raise ContractError("identity merge cannot mix source snapshots or receipt times")
    if len({record.symbol for record in listings}) != len(listings):
        raise ContractError("Nasdaq identity snapshot symbols must be unique")
    if len({record.symbol for record in assets}) != len(assets) or len({record.asset_id for record in assets}) != len(assets):
        raise ContractError("Alpaca identity snapshot symbols and asset IDs must be unique")
    alpaca_snapshot_id = next(iter(alpaca_snapshot_ids))
    nasdaq_snapshot_id = next(iter(nasdaq_snapshot_ids))
    nasdaq_file_created_at = next(iter(nasdaq_created))
    evidence_states = {
        *(record.evidence_state for record in assets),
        *(record.evidence_state for record in listings),
    }
    evidence_state = (
        "NETWORK_AS_RECEIVED"
        if evidence_states == {"NETWORK_AS_RECEIVED"}
        else "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
    )
    synthetic_permit_ids = tuple(
        sorted(
            {
                *(permit for record in assets for permit in record.synthetic_permit_ids),
                *(permit for record in listings for permit in record.synthetic_permit_ids),
            }
        )
    )
    if evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE" and not synthetic_permit_ids:
        raise ContractError("non-network identity merge lacks explicit synthetic provenance")
    known_at = max(next(iter(alpaca_known)), next(iter(nasdaq_known)))
    # Nasdaq's embedded file-creation timestamp is the source-effective time;
    # retrieval is the later knowledge time. Alpaca has no historical effective
    # timestamp, so its membership evidence is never visible before knowledge_at.
    effective_at = nasdaq_file_created_at
    nasdaq = {record.symbol: record for record in listings}
    alpaca = {record.symbol: record for record in assets}
    output: list[IdentityVersion] = []
    # Nasdaq is the primary comprehensive membership census. Alpaca supplies a
    # stable asset identity/tradability join. The union is retained so missing
    # joins abstain visibly instead of disappearing from the census.
    for symbol in sorted(set(nasdaq) | set(alpaca)):
        asset = alpaca.get(symbol)
        listing = nasdaq.get(symbol)
        security_type = listing.security_type if listing and listing.eligible_type else SecurityType.UNKNOWN
        active = asset is not None and asset.status == "active" and asset.tradable
        eligible = active and listing is not None and security_type in {SecurityType.STOCK, SecurityType.ETF}
        if asset is None:
            reason = "missing_alpaca_asset_identity"
        elif not active:
            reason = "inactive_or_not_tradable"
        elif listing is None:
            reason = "missing_nasdaq_identity"
        elif security_type is SecurityType.UNKNOWN:
            reason = "unknown_or_nonstandard_security_type"
        else:
            reason = None
        asset_id = (
            asset.asset_id
            if asset is not None
            else "NASDAQ_UNRESOLVED_"
            + sha256_bytes(canonical_json_bytes({"snapshot_id": nasdaq_snapshot_id, "symbol": symbol}))
        )
        output.append(
            IdentityVersion(
                asset_id=asset_id,
                symbol=symbol,
                security_type=security_type,
                listing_exchange=listing.listing_exchange if listing else asset.exchange,
                active=active,
                eligible=eligible,
                # Membership means presence in the comprehensive Nasdaq census,
                # not merely presence in Alpaca's tradable-assets response.
                membership_present=listing is not None,
                abstention_reason=reason,
                effective_at=effective_at,
                known_at=known_at,
                identity_snapshot_id="",
                alpaca_snapshot_id=alpaca_snapshot_id,
                nasdaq_snapshot_id=listing.snapshot_id if listing else None,
                nasdaq_file_created_at=listing.file_created_at if listing else None,
                evidence_state=evidence_state,
                synthetic_permit_ids=synthetic_permit_ids,
            )
        )
    rows_without_id = tuple(sorted(output, key=lambda row: row.asset_id))
    unsigned = {
        "schema_version": 1,
        "effective_at": iso_z(effective_at),
        "known_at": iso_z(known_at),
        "complete_membership": True,
        "alpaca_snapshot_id": alpaca_snapshot_id,
        "nasdaq_snapshot_id": nasdaq_snapshot_id,
        "nasdaq_file_created_at": iso_z(nasdaq_file_created_at),
        "evidence_state": evidence_state,
        "synthetic_permit_ids": list(synthetic_permit_ids),
        "rows": [row.receipt_dict(include_snapshot_id=False) for row in rows_without_id],
    }
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    snapshot = IdentitySnapshot(
        snapshot_id=snapshot_id,
        effective_at=effective_at,
        known_at=known_at,
        complete_membership=True,
        alpaca_snapshot_id=alpaca_snapshot_id,
        nasdaq_snapshot_id=nasdaq_snapshot_id,
        nasdaq_file_created_at=nasdaq_file_created_at,
        evidence_state=evidence_state,
        synthetic_permit_ids=synthetic_permit_ids,
        rows=tuple(replace(row, identity_snapshot_id=snapshot_id) for row in rows_without_id),
    )
    snapshot.validate()
    return snapshot


def _load_identity_release_payload(
    release_directory: Path,
    expected_row_count: int,
) -> tuple[IdentitySnapshot, ...]:
    payload_path = Path(release_directory) / "identity_snapshots.json"
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("identity release payload is missing or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "snapshots"}:
        raise IntegrityError("identity release payload fields differ from the exact contract")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise IntegrityError("identity release payload schema is invalid")
    if not isinstance(payload["snapshots"], list) or not payload["snapshots"]:
        raise IntegrityError("identity release requires at least one complete snapshot")
    snapshots: list[IdentitySnapshot] = []
    snapshot_fields = set(IdentitySnapshot.__dataclass_fields__) | {"schema_version"}
    row_fields = set(IdentityVersion.__dataclass_fields__)
    for raw_snapshot in payload["snapshots"]:
        if not isinstance(raw_snapshot, dict) or set(raw_snapshot) != snapshot_fields:
            raise IntegrityError("identity snapshot payload fields differ")
        if type(raw_snapshot["schema_version"]) is not int or raw_snapshot["schema_version"] != 1:
            raise IntegrityError("identity snapshot payload schema is invalid")
        raw_rows = raw_snapshot["rows"]
        if not isinstance(raw_rows, list) or not raw_rows:
            raise IntegrityError("identity snapshot rows must be a nonempty list")
        rows: list[IdentityVersion] = []
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict) or set(raw_row) != row_fields:
                raise IntegrityError("identity row payload fields differ")
            for boolean_name in ("active", "eligible", "membership_present"):
                if type(raw_row[boolean_name]) is not bool:
                    raise IntegrityError(f"identity row {boolean_name} must be boolean")
            row = IdentityVersion(
                asset_id=str(raw_row["asset_id"]),
                symbol=str(raw_row["symbol"]),
                security_type=SecurityType(str(raw_row["security_type"])),
                listing_exchange=str(raw_row["listing_exchange"]),
                active=raw_row["active"],
                eligible=raw_row["eligible"],
                membership_present=raw_row["membership_present"],
                abstention_reason=(
                    str(raw_row["abstention_reason"])
                    if raw_row["abstention_reason"] is not None
                    else None
                ),
                effective_at=parse_timestamp(str(raw_row["effective_at"]), "identity.effective_at"),
                known_at=parse_timestamp(str(raw_row["known_at"]), "identity.known_at"),
                identity_snapshot_id=str(raw_row["identity_snapshot_id"]),
                alpaca_snapshot_id=str(raw_row["alpaca_snapshot_id"]),
                nasdaq_snapshot_id=(
                    str(raw_row["nasdaq_snapshot_id"])
                    if raw_row["nasdaq_snapshot_id"] is not None
                    else None
                ),
                nasdaq_file_created_at=(
                    parse_timestamp(
                        str(raw_row["nasdaq_file_created_at"]),
                        "identity.nasdaq_file_created_at",
                    )
                    if raw_row["nasdaq_file_created_at"] is not None
                    else None
                ),
                evidence_state=str(raw_row["evidence_state"]),
                synthetic_permit_ids=tuple(raw_row["synthetic_permit_ids"]),
            )
            rows.append(row)
        if type(raw_snapshot["complete_membership"]) is not bool:
            raise IntegrityError("identity complete_membership must be boolean")
        snapshot = IdentitySnapshot(
            snapshot_id=str(raw_snapshot["snapshot_id"]),
            effective_at=parse_timestamp(str(raw_snapshot["effective_at"]), "identity.effective_at"),
            known_at=parse_timestamp(str(raw_snapshot["known_at"]), "identity.known_at"),
            complete_membership=raw_snapshot["complete_membership"],
            alpaca_snapshot_id=str(raw_snapshot["alpaca_snapshot_id"]),
            nasdaq_snapshot_id=str(raw_snapshot["nasdaq_snapshot_id"]),
            nasdaq_file_created_at=parse_timestamp(
                str(raw_snapshot["nasdaq_file_created_at"]),
                "identity.nasdaq_file_created_at",
            ),
            evidence_state=str(raw_snapshot["evidence_state"]),
            synthetic_permit_ids=tuple(raw_snapshot["synthetic_permit_ids"]),
            rows=tuple(rows),
        )
        snapshot.validate()
        snapshots.append(snapshot)
    if sum(len(snapshot.rows) for snapshot in snapshots) != expected_row_count:
        raise IntegrityError("identity release row_count differs from its payload")
    return tuple(snapshots)


class BitemporalIdentityLedger:
    def __init__(
        self,
        *,
        verified_release_directory: Path | None = None,
        accepted_release_root: Path | None = None,
        synthetic_permit: SyntheticOnlyPermit | None = None,
    ) -> None:
        if (verified_release_directory is None) == (synthetic_permit is None):
            raise ContractError(
                "identity ledger requires exactly one verified release or synthetic-only permit"
        )
        if verified_release_directory is not None:
            if accepted_release_root is None:
                raise ContractError("verified identity requires its accepted release root")
            manifest = verify_accepted_release(
                Path(verified_release_directory),
                accepted_root=Path(accepted_release_root),
            )
            if (
                manifest.project != "US_stocks_swing_model_v2"
                or manifest.dataset != "identity"
                or manifest.role != "prospective_as_received"
                or manifest.quality_state != "PASS"
            ):
                raise ContractError("identity release is not trust eligible")
            self.release_id = manifest.release_id
            self.source_epoch = manifest.source_epoch
            self.trust_eligible = True
            self.synthetic_permit_id = None
            self._rows = []
            self._snapshots = []
            snapshots = _load_identity_release_payload(Path(verified_release_directory), manifest.row_count)
            for snapshot in snapshots:
                self._append_snapshot(snapshot, from_verified_payload=True)
        else:
            if accepted_release_root is not None:
                raise ContractError("synthetic identity cannot name an accepted release root")
            permit = require_synthetic_permit(
                synthetic_permit,
                scope="SYNTHETIC_IDENTITY_LEDGER",
            )
            self.release_id = permit.permit_id
            self.source_epoch = "SYNTHETIC_ONLY"
            self.trust_eligible = False
            self.synthetic_permit_id = permit.permit_id
            self._rows: list[IdentityVersion] = []
            self._snapshots: list[IdentitySnapshot] = []

    def append_snapshot(self, snapshot: IdentitySnapshot) -> None:
        if self.trust_eligible:
            raise ContractError("verified identity ledgers are immutable payload views")
        self._append_snapshot(snapshot, from_verified_payload=False)

    def _append_snapshot(
        self,
        snapshot: IdentitySnapshot,
        *,
        from_verified_payload: bool,
    ) -> None:
        snapshot.validate()
        if self.trust_eligible != snapshot.trust_eligible:
            raise ContractError("identity snapshot and ledger trust modes differ")
        if self.trust_eligible and not from_verified_payload:
            raise ContractError("verified identity rows must originate in the release payload")
        if any(prior.snapshot_id == snapshot.snapshot_id for prior in self._snapshots):
            return
        if self._snapshots and snapshot.known_at <= self._snapshots[-1].known_at:
            raise IntegrityError("complete identity snapshots must append with increasing known_at")
        if self._snapshots and snapshot.effective_at < self._snapshots[-1].effective_at:
            raise IntegrityError("identity effective time cannot move backwards; same-time revisions are allowed")
        previous_members = {
            row.asset_id: row
            for row in self.visible_as_of(
                effective_as_of=snapshot.effective_at,
                known_as_of=self._snapshots[-1].known_at,
            )
            if row.membership_present
        } if self._snapshots else {}
        represented_ids = {row.asset_id for row in snapshot.rows}
        tombstones = [
            IdentityVersion(
                asset_id=prior.asset_id,
                symbol=prior.symbol,
                security_type=SecurityType.UNKNOWN,
                listing_exchange=prior.listing_exchange,
                active=False,
                eligible=False,
                membership_present=False,
                abstention_reason="absent_from_complete_snapshot",
                effective_at=snapshot.effective_at,
                known_at=snapshot.known_at,
                identity_snapshot_id=snapshot.snapshot_id,
                alpaca_snapshot_id=snapshot.alpaca_snapshot_id,
                nasdaq_snapshot_id=snapshot.nasdaq_snapshot_id,
                nasdaq_file_created_at=snapshot.nasdaq_file_created_at,
                evidence_state=snapshot.evidence_state,
                synthetic_permit_ids=snapshot.synthetic_permit_ids,
            )
            for asset_id, prior in previous_members.items()
            if asset_id not in represented_ids
        ]
        self._rows.extend((*snapshot.rows, *tombstones))
        self._snapshots.append(snapshot)

    def visible_as_of(
        self,
        *,
        effective_as_of: datetime,
        known_as_of: datetime,
    ) -> tuple[IdentityVersion, ...]:
        effective_cutoff = require_aware_utc(effective_as_of, "effective_as_of")
        known_cutoff = require_aware_utc(known_as_of, "known_as_of")
        latest: dict[str, IdentityVersion] = {}
        for row in self._rows:
            if row.effective_at <= effective_cutoff and row.known_at <= known_cutoff:
                current = latest.get(row.asset_id)
                if current is None or (row.effective_at, row.known_at) > (current.effective_at, current.known_at):
                    latest[row.asset_id] = row
        return tuple(sorted(latest.values(), key=lambda row: row.asset_id))
