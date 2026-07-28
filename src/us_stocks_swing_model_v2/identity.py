from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

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


UNRESOLVED_NASDAQ_ASSET_PREFIX = "NASDAQ_UNRESOLVED_"


def _unresolved_nasdaq_asset_id(symbol: str) -> str:
    return UNRESOLVED_NASDAQ_ASSET_PREFIX + sha256_bytes(
        canonical_json_bytes(
            {
                "namespace": "NASDAQ_UNRESOLVED_SYMBOL_V1",
                "symbol": symbol,
            }
        )
    )


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
class AlpacaAssetProjection:
    projection_contract_id: str
    projection_assessment_id: str
    snapshot_id: str
    raw_sha256: str
    raw_record_count: int
    selected_rows_sha256: str
    excluded_counts: tuple[tuple[str, int], ...]
    records: tuple[AlpacaAssetRecord, ...]

    @property
    def selected_record_count(self) -> int:
        return len(self.records)

    @property
    def evidence_state(self) -> str:
        states = {record.evidence_state for record in self.records}
        if len(states) != 1:
            raise IntegrityError("Alpaca projection mixes evidence states")
        return next(iter(states))

    @property
    def trust_eligible(self) -> bool:
        return self.evidence_state == "NETWORK_AS_RECEIVED" and all(
            not record.synthetic_permit_ids for record in self.records
        )

    def summary(self) -> dict[str, object]:
        return {
            "projection_contract_id": self.projection_contract_id,
            "projection_assessment_id": self.projection_assessment_id,
            "snapshot_id": self.snapshot_id,
            "raw_sha256": self.raw_sha256,
            "raw_record_count": self.raw_record_count,
            "selected_record_count": self.selected_record_count,
            "selected_rows_sha256": self.selected_rows_sha256,
            "excluded_counts": dict(self.excluded_counts),
            "selected_duplicate_id_keys": 0,
            "selected_duplicate_symbol_keys": 0,
            "evidence_state": self.evidence_state,
            "trust_eligible": self.trust_eligible,
        }


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


def _validate_identity_version(row: IdentityVersion) -> None:
    if type(row) is not IdentityVersion:
        raise ContractError("identity snapshot row must use the exact identity type")
    for name in ("asset_id", "symbol", "listing_exchange"):
        value = getattr(row, name)
        if type(value) is not str or not value or value != value.strip():
            raise ContractError(f"identity row {name} must be nonempty canonical text")
    if row.symbol != row.symbol.upper():
        raise ContractError("identity row symbol must be uppercase canonical text")
    if row.listing_exchange != row.listing_exchange.upper():
        raise ContractError(
            "identity row listing_exchange must be uppercase canonical text"
        )
    if row.asset_id.startswith(UNRESOLVED_NASDAQ_ASSET_PREFIX) and (
        row.asset_id != _unresolved_nasdaq_asset_id(row.symbol)
        or row.active
        or row.eligible
    ):
        raise ContractError(
            "unresolved Nasdaq identity must use its stable ineligible symbol binding"
        )
    if type(row.security_type) is not SecurityType:
        raise ContractError("identity row security type is invalid")
    if (
        type(row.active) is not bool
        or type(row.eligible) is not bool
        or type(row.membership_present) is not bool
    ):
        raise ContractError("identity row state flags must be exact booleans")
    if row.eligible and (
        not row.active
        or not row.membership_present
        or row.security_type not in {SecurityType.STOCK, SecurityType.ETF}
    ):
        raise ContractError(
            "eligible identity row must be active, present, and STOCK or ETF"
        )
    if row.membership_present:
        if (
            row.nasdaq_snapshot_id is None
            or row.nasdaq_file_created_at is None
        ):
            raise ContractError(
                "member identity row requires exact Nasdaq membership evidence"
            )
    elif (
        row.nasdaq_snapshot_id is not None
        or row.nasdaq_file_created_at is not None
    ):
        raise ContractError(
            "nonmember identity row cannot claim Nasdaq membership evidence"
        )
    if row.eligible:
        if row.abstention_reason is not None:
            raise ContractError("eligible identity row cannot carry an abstention reason")
    elif (
        type(row.abstention_reason) is not str
        or not row.abstention_reason
        or row.abstention_reason != row.abstention_reason.strip()
    ):
        raise ContractError("ineligible identity row requires a canonical abstention reason")


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
            _validate_identity_version(row)
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
        if any(
            type(row[name]) is not str
            for name in ("id", "symbol", "class", "exchange", "status")
        ):
            raise ContractError("Alpaca asset identity fields must be exact text")
        asset_id = row["id"]
        symbol = row["symbol"].strip().upper()
        if (
            not asset_id
            or asset_id.startswith(UNRESOLVED_NASDAQ_ASSET_PREFIX)
            or not symbol
            or asset_id in seen_ids
            or symbol in seen_symbols
        ):
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
                exchange=row["exchange"],
                status=row["status"],
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


def project_active_us_equity_assets(
    snapshot: LandedSnapshot,
    *,
    projection_contract: Mapping[str, object],
    projection_contract_id: str,
) -> AlpacaAssetProjection:
    """Project current US-equity identity without mutating as-received bytes.

    The legacy strict parser above remains unchanged. This projection validates
    every provider row, audits excluded classes/statuses, and never chooses
    between duplicate selected identities.
    """

    expected_contract = {
        "schema_version": 1,
        "name": "ALPACA_ACTIVE_US_EQUITY_PROJECTION",
        "input_root": "exact_json_list",
        "required_fields": [
            "id",
            "symbol",
            "class",
            "exchange",
            "status",
            "tradable",
        ],
        "all_row_validation": [
            "required_fields_present",
            "required_field_types_exact",
            "asset_id_nonempty_not_reserved",
            "asset_id_globally_unique",
        ],
        "selection": {"class": "us_equity", "status": "active"},
        "selected_row_validation": [
            "symbol_nonempty_exact_trimmed_uppercase",
            "exchange_nonempty_exact_trimmed_uppercase",
            "tradable_exact_boolean",
            "asset_id_unique",
            "symbol_unique",
        ],
        "excluded_rows": "audited_by_class_and_status_never_emitted",
        "duplicate_policy": (
            "never_deduplicate_fail_selected_duplicate_id_or_symbol"
        ),
        "output_order": "asset_id_ascending",
    }
    if dict(projection_contract) != expected_contract:
        raise ContractError("Alpaca asset projection contract differs")
    require_sha256(
        projection_contract_id,
        "alpaca_asset_projection.projection_contract_id",
    )
    if projection_contract_id != sha256_bytes(
        canonical_json_bytes(expected_contract)
    ):
        raise ContractError("Alpaca asset projection contract ID differs")
    if snapshot.source != "alpaca_assets" or snapshot.http_status != 200:
        raise ContractError("snapshot is not a successful Alpaca asset snapshot")
    try:
        payload = json.loads(snapshot.read_verified_bytes())
    except json.JSONDecodeError as exc:
        raise ContractError("Alpaca asset snapshot is not JSON") from exc
    if not isinstance(payload, list):
        raise ContractError("Alpaca asset projection requires an exact JSON list")
    required = {"id", "symbol", "class", "exchange", "status", "tradable"}
    seen_all_ids: set[str] = set()
    seen_selected_ids: set[str] = set()
    seen_selected_symbols: set[str] = set()
    excluded_counts: dict[str, int] = {}
    selected_wire_rows: list[dict[str, object]] = []
    records: list[AlpacaAssetRecord] = []
    for row in payload:
        if not isinstance(row, dict) or not required <= row.keys():
            raise ContractError("Alpaca asset row lacks frozen fields")
        if any(
            type(row[name]) is not str
            for name in ("id", "symbol", "class", "exchange", "status")
        ) or type(row["tradable"]) is not bool:
            raise ContractError("Alpaca asset row has invalid frozen field types")
        asset_id = row["id"]
        if (
            not asset_id
            or asset_id.startswith(UNRESOLVED_NASDAQ_ASSET_PREFIX)
            or asset_id in seen_all_ids
        ):
            raise ContractError(
                "Alpaca asset IDs must be nonempty, nonreserved, and globally unique"
            )
        seen_all_ids.add(asset_id)
        selected = row["class"] == "us_equity" and row["status"] == "active"
        if not selected:
            audit_key = f"{row['class']}_{row['status']}"
            excluded_counts[audit_key] = excluded_counts.get(audit_key, 0) + 1
            continue
        symbol = row["symbol"]
        exchange = row["exchange"]
        if (
            not symbol
            or symbol != symbol.strip().upper()
            or not exchange
            or exchange != exchange.strip().upper()
        ):
            raise ContractError(
                "selected Alpaca symbols and exchanges must be exact uppercase text"
            )
        if asset_id in seen_selected_ids or symbol in seen_selected_symbols:
            raise ContractError(
                "selected Alpaca asset IDs and symbols must be unique; "
                "deduplication is prohibited"
            )
        seen_selected_ids.add(asset_id)
        seen_selected_symbols.add(symbol)
        selected_wire_rows.append(
            {
                name: row[name]
                for name in (
                    "id",
                    "symbol",
                    "class",
                    "exchange",
                    "status",
                    "tradable",
                )
            }
        )
        records.append(
            AlpacaAssetRecord(
                asset_id=asset_id,
                symbol=symbol,
                asset_class=row["class"],
                exchange=exchange,
                status=row["status"],
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
    if not records:
        raise ContractError("Alpaca asset projection selected no active US equities")
    selected_wire_rows.sort(key=lambda row: str(row["id"]))
    records.sort(key=lambda record: record.asset_id)
    selected_rows_sha256 = sha256_bytes(
        canonical_json_bytes(selected_wire_rows)
    )
    unsigned_assessment = {
        "schema_version": 1,
        "snapshot_id": snapshot.snapshot_id,
        "raw_sha256": snapshot.raw_sha256,
        "projection_contract_id": projection_contract_id,
        "raw_record_count": len(payload),
        "selected_record_count": len(records),
        "selected_rows_sha256": selected_rows_sha256,
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "selected_duplicate_id_keys": 0,
        "selected_duplicate_symbol_keys": 0,
    }
    projection = AlpacaAssetProjection(
        projection_contract_id=projection_contract_id,
        projection_assessment_id=sha256_bytes(
            canonical_json_bytes(unsigned_assessment)
        ),
        snapshot_id=snapshot.snapshot_id,
        raw_sha256=snapshot.raw_sha256,
        raw_record_count=len(payload),
        selected_rows_sha256=selected_rows_sha256,
        excluded_counts=tuple(sorted(excluded_counts.items())),
        records=tuple(records),
    )
    projection.summary()
    return projection


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
    # Nasdaq's embedded file-creation timestamp is source-effective only for
    # its census fields. Alpaca supplies current status/tradability without an
    # independent historical effective timestamp, so those fields may become
    # effective no earlier than the exact time they were observed. Using the
    # later provenance boundary keeps the merged row invisible to earlier
    # effective-time queries instead of backdating current Alpaca state.
    effective_at = max(
        nasdaq_file_created_at,
        next(iter(alpaca_known)),
    )
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
            else _unresolved_nasdaq_asset_id(symbol)
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
            for text_name in (
                "asset_id",
                "symbol",
                "security_type",
                "listing_exchange",
                "effective_at",
                "known_at",
                "identity_snapshot_id",
                "alpaca_snapshot_id",
                "evidence_state",
            ):
                if type(raw_row[text_name]) is not str:
                    raise IntegrityError(
                        f"identity row {text_name} must be exact text"
                    )
            for nullable_text_name in (
                "abstention_reason",
                "nasdaq_snapshot_id",
                "nasdaq_file_created_at",
            ):
                if (
                    raw_row[nullable_text_name] is not None
                    and type(raw_row[nullable_text_name]) is not str
                ):
                    raise IntegrityError(
                        f"identity row {nullable_text_name} must be exact text or null"
                    )
            if (
                type(raw_row["synthetic_permit_ids"]) is not list
                or any(
                    type(value) is not str
                    for value in raw_row["synthetic_permit_ids"]
                )
            ):
                raise IntegrityError(
                    "identity row synthetic_permit_ids must be an exact text list"
                )
            for boolean_name in ("active", "eligible", "membership_present"):
                if type(raw_row[boolean_name]) is not bool:
                    raise IntegrityError(f"identity row {boolean_name} must be boolean")
            try:
                security_type = SecurityType(raw_row["security_type"])
            except ValueError as exc:
                raise IntegrityError("identity row security_type is invalid") from exc
            row = IdentityVersion(
                asset_id=raw_row["asset_id"],
                symbol=raw_row["symbol"],
                security_type=security_type,
                listing_exchange=raw_row["listing_exchange"],
                active=raw_row["active"],
                eligible=raw_row["eligible"],
                membership_present=raw_row["membership_present"],
                abstention_reason=raw_row["abstention_reason"],
                effective_at=parse_timestamp(raw_row["effective_at"], "identity.effective_at"),
                known_at=parse_timestamp(raw_row["known_at"], "identity.known_at"),
                identity_snapshot_id=raw_row["identity_snapshot_id"],
                alpaca_snapshot_id=raw_row["alpaca_snapshot_id"],
                nasdaq_snapshot_id=raw_row["nasdaq_snapshot_id"],
                nasdaq_file_created_at=(
                    parse_timestamp(
                        raw_row["nasdaq_file_created_at"],
                        "identity.nasdaq_file_created_at",
                    )
                    if raw_row["nasdaq_file_created_at"] is not None
                    else None
                ),
                evidence_state=raw_row["evidence_state"],
                synthetic_permit_ids=tuple(raw_row["synthetic_permit_ids"]),
            )
            rows.append(row)
        if type(raw_snapshot["complete_membership"]) is not bool:
            raise IntegrityError("identity complete_membership must be boolean")
        for text_name in (
            "snapshot_id",
            "effective_at",
            "known_at",
            "alpaca_snapshot_id",
            "nasdaq_snapshot_id",
            "nasdaq_file_created_at",
            "evidence_state",
        ):
            if type(raw_snapshot[text_name]) is not str:
                raise IntegrityError(
                    f"identity snapshot {text_name} must be exact text"
                )
        if (
            type(raw_snapshot["synthetic_permit_ids"]) is not list
            or any(
                type(value) is not str
                for value in raw_snapshot["synthetic_permit_ids"]
            )
        ):
            raise IntegrityError(
                "identity snapshot synthetic_permit_ids must be an exact text list"
            )
        snapshot = IdentitySnapshot(
            snapshot_id=raw_snapshot["snapshot_id"],
            effective_at=parse_timestamp(raw_snapshot["effective_at"], "identity.effective_at"),
            known_at=parse_timestamp(raw_snapshot["known_at"], "identity.known_at"),
            complete_membership=raw_snapshot["complete_membership"],
            alpaca_snapshot_id=raw_snapshot["alpaca_snapshot_id"],
            nasdaq_snapshot_id=raw_snapshot["nasdaq_snapshot_id"],
            nasdaq_file_created_at=parse_timestamp(
                raw_snapshot["nasdaq_file_created_at"],
                "identity.nasdaq_file_created_at",
            ),
            evidence_state=raw_snapshot["evidence_state"],
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
                nasdaq_snapshot_id=None,
                nasdaq_file_created_at=None,
                evidence_state=snapshot.evidence_state,
                synthetic_permit_ids=snapshot.synthetic_permit_ids,
            )
            for asset_id, prior in previous_members.items()
            if asset_id not in represented_ids
        ]
        for tombstone in tombstones:
            _validate_identity_version(tombstone)
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
        resolved_symbols = {
            row.symbol
            for row in latest.values()
            if row.membership_present
            and not row.asset_id.startswith(UNRESOLVED_NASDAQ_ASSET_PREFIX)
        }
        visible = (
            row
            for row in latest.values()
            if not (
                row.asset_id.startswith(UNRESOLVED_NASDAQ_ASSET_PREFIX)
                and row.symbol in resolved_symbols
            )
        )
        return tuple(sorted(visible, key=lambda row: row.asset_id))
