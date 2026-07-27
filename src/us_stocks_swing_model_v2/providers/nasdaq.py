from __future__ import annotations

import re
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..common import require_aware_utc
from ..capabilities import SyntheticOnlyPermit, require_synthetic_permit
from ..errors import ContractError
from ..schemas import SecurityType
from .snapshots import LandedSnapshot


NASDAQ_TRADED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt"
EXPECTED_HEADER = (
    "Nasdaq Traded",
    "Symbol",
    "Security Name",
    "Listing Exchange",
    "Market Category",
    "ETF",
    "Round Lot Size",
    "Test Issue",
    "Financial Status",
    "CQS Symbol",
    "NASDAQ Symbol",
    "NextShares",
)
TRAILER = re.compile(r"^File Creation Time: (?P<date>\d{8})(?P<hour>\d{2}):(?P<minute>\d{2})$")

NONSTANDARD_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bWARRANTS?\b",
        r"\bWTS\b",
        r"\bUNITS?\b",
        r"\bRIGHTS?\b",
        r"\bPREFERRED\b",
        r"\bDEPOSITARY\b",
        r"\bNOTES?\b",
        r"\bBONDS?\b",
        r"\bDEBENTURES?\b",
        r"\bNEXTSHARES\b",
    )
)
STOCK_TOKENS = ("COMMON STOCK", "COMMON SHARES", "ORDINARY SHARES", "ORDINARY SHARE")
MAX_NASDAQ_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class NasdaqCompletenessPolicy:
    minimum_bytes: int = 100_000
    maximum_bytes: int = MAX_NASDAQ_BYTES
    minimum_records: int = 1_000
    maximum_records: int = 50_000
    maximum_drop_fraction: float = 0.10
    maximum_count_change_fraction: float = 0.25
    synthetic_permit: SyntheticOnlyPermit | None = None

    @classmethod
    def synthetic_fixture(
        cls,
        *,
        permit: SyntheticOnlyPermit,
    ) -> "NasdaqCompletenessPolicy":
        verified = require_synthetic_permit(permit, scope="NASDAQ_COMPLETENESS_FIXTURE")
        return cls(
            minimum_bytes=1,
            maximum_bytes=1_000_000,
            minimum_records=1,
            maximum_records=100,
            maximum_drop_fraction=1.0,
            maximum_count_change_fraction=1.0,
            synthetic_permit=verified,
        )

    def validate(self) -> None:
        if self.synthetic_permit is not None:
            require_synthetic_permit(
                self.synthetic_permit,
                scope="NASDAQ_COMPLETENESS_FIXTURE",
            )
        elif (
            self.minimum_bytes < 100_000
            or self.minimum_records < 1_000
            or self.maximum_bytes > MAX_NASDAQ_BYTES
            or self.maximum_records > 50_000
            or self.maximum_drop_fraction > 0.10
            or self.maximum_count_change_fraction > 0.25
        ):
            raise ContractError("production Nasdaq completeness policy cannot be weakened")
        integer_values = (
            self.minimum_bytes,
            self.maximum_bytes,
            self.minimum_records,
            self.maximum_records,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
            raise ContractError("Nasdaq completeness counts must be explicit integers")
        if not (0 < self.minimum_bytes <= self.maximum_bytes and 0 < self.minimum_records <= self.maximum_records):
            raise ContractError("Nasdaq completeness bounds are invalid")
        for value in (self.maximum_drop_fraction, self.maximum_count_change_fraction):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ContractError("Nasdaq completeness deltas must be finite fractions")


@dataclass(frozen=True)
class IdentityRecord:
    symbol: str
    security_name: str
    listing_exchange: str
    nasdaq_traded: bool
    security_type: SecurityType
    test_issue: bool
    financial_status: str
    snapshot_id: str
    snapshot_retrieved_at: datetime
    file_created_at: datetime
    evidence_state: str
    synthetic_permit_ids: tuple[str, ...]

    @property
    def eligible_type(self) -> bool:
        return (
            self.nasdaq_traded
            and
            self.security_type in {SecurityType.STOCK, SecurityType.ETF}
            and not self.test_issue
            and self.financial_status in {"", "N"}
        )


def _classify(
    name: str,
    etf: str,
    test_issue: str,
    financial_status: str,
    nasdaq_traded: str,
) -> SecurityType:
    upper = name.upper()
    if (
        nasdaq_traded != "Y"
        or test_issue != "N"
        or financial_status not in {"", "N"}
    ):
        return SecurityType.UNKNOWN
    if any(pattern.search(upper) for pattern in NONSTANDARD_PATTERNS):
        return SecurityType.UNKNOWN
    if etf == "Y":
        return SecurityType.ETF
    if any(token in upper for token in STOCK_TOKENS):
        return SecurityType.STOCK
    return SecurityType.UNKNOWN


def _unambiguous_eastern_wall_time(value: str) -> datetime:
    try:
        naive = datetime.strptime(value, "%m%d%Y%H:%M")
    except ValueError as exc:
        raise ContractError(
            "nasdaqtraded.txt creation timestamp is not a real date/time"
        ) from exc
    eastern = ZoneInfo("America/New_York")
    candidates: list[datetime] = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=eastern, fold=fold)
        utc = local.astimezone(timezone.utc)
        round_trip = utc.astimezone(eastern)
        if (
            round_trip.replace(tzinfo=None) == naive
            and round_trip.fold == fold
        ):
            candidates.append(utc)
    distinct = {candidate for candidate in candidates}
    if not distinct:
        raise ContractError(
            "nasdaqtraded.txt creation timestamp is a nonexistent local time"
        )
    if len(distinct) != 1:
        raise ContractError(
            "nasdaqtraded.txt creation timestamp is an ambiguous local time"
        )
    return distinct.pop()


def parse_nasdaq_traded(
    snapshot: LandedSnapshot,
    *,
    policy: NasdaqCompletenessPolicy | None = None,
    prior_accepted_record_count: int | None = None,
) -> tuple[IdentityRecord, ...]:
    """Parse only an atomically landed comprehensive Nasdaq Trader snapshot."""
    if snapshot.source != "nasdaqtraded" or snapshot.url != NASDAQ_TRADED_URL:
        raise ContractError("snapshot is not the contracted comprehensive Nasdaq file")
    if snapshot.http_status != 200 or snapshot.raw_sha256 == "" or not snapshot.headers:
        raise ContractError("as-received bytes and HTTP headers must be preserved before parse")
    completeness = policy or NasdaqCompletenessPolicy()
    completeness.validate()
    if completeness.synthetic_permit is None:
        if not snapshot.trust_eligible:
            raise ContractError("production Nasdaq parse requires a network as-received snapshot")
        evidence_state = "NETWORK_AS_RECEIVED"
        synthetic_permit_ids: tuple[str, ...] = ()
    else:
        evidence_state = "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
        synthetic_permit_ids = tuple(
            sorted(
                {
                    completeness.synthetic_permit.permit_id,
                    *(value for value in (snapshot.synthetic_permit_id,) if value is not None),
                }
            )
        )
    if completeness.synthetic_permit is None and prior_accepted_record_count is None:
        raise ContractError(
            "production Nasdaq parse requires a trusted prior accepted record count"
        )
    raw = snapshot.read_verified_bytes()
    if not completeness.minimum_bytes <= len(raw) <= completeness.maximum_bytes:
        raise ContractError("nasdaqtraded.txt byte count fails the completeness policy")
    content_type = snapshot.headers.get("content-type")
    if content_type is not None and not content_type.lower().startswith(("text/plain", "text/csv")):
        raise ContractError("nasdaqtraded.txt content-type is inconsistent with text evidence")
    content_length = snapshot.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise ContractError("nasdaqtraded.txt content-length is malformed") from exc
        if declared_length != len(raw):
            raise ContractError("nasdaqtraded.txt content-length differs from landed bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContractError("nasdaqtraded.txt is not UTF-8 text") from exc
    lines = text.splitlines()
    if len(lines) < 3 or tuple(lines[0].split("|")) != EXPECTED_HEADER:
        raise ContractError("nasdaqtraded.txt header differs from the frozen contract")
    trailer_fields = lines[-1].split("|")
    # Nasdaq currently emits a shortened timestamp trailer, while older
    # retained fixtures padded it to the twelve-column body width. The trailer
    # is not a body row: require only the timestamp plus at least one and no
    # more than the body-width empty placeholders. Any payload is fatal.
    if not 2 <= len(trailer_fields) <= len(EXPECTED_HEADER) or any(
        trailer_fields[1:]
    ):
        raise ContractError("nasdaqtraded.txt trailer shape is malformed")
    match = TRAILER.fullmatch(trailer_fields[0])
    if not match:
        raise ContractError("nasdaqtraded.txt creation timestamp is malformed")
    file_creation_time = match.group(0).removeprefix("File Creation Time: ")
    # Nasdaq supplies no fold indicator, so repeated fall-back wall times are
    # rejected instead of guessing which physical instant the trailer means.
    file_created_at = _unambiguous_eastern_wall_time(file_creation_time)
    retrieved_at = require_aware_utc(snapshot.retrieved_at, "snapshot.retrieved_at")
    if file_created_at > retrieved_at:
        raise ContractError("nasdaqtraded.txt creation timestamp is later than retrieval")
    records: list[IdentityRecord] = []
    seen: set[str] = set()
    for line_number, line in enumerate(lines[1:-1], start=2):
        fields = line.split("|")
        if len(fields) != len(EXPECTED_HEADER):
            raise ContractError(f"malformed nasdaqtraded row at line {line_number}")
        row = dict(zip(EXPECTED_HEADER, fields, strict=True))
        if row["Nasdaq Traded"] not in {"Y", "N"}:
            raise ContractError(
                f"invalid Nasdaq-traded flag at line {line_number}"
            )
        if row["ETF"] not in {"Y", "N"} or row["Test Issue"] not in {"Y", "N"}:
            raise ContractError(f"invalid ETF/test flag at line {line_number}")
        raw_symbol = row["Symbol"]
        if not raw_symbol or raw_symbol != raw_symbol.strip().upper():
            raise ContractError(
                f"Nasdaq symbol must be exact canonical wire text at line {line_number}"
            )
        symbol = raw_symbol
        if not symbol or symbol in seen:
            raise ContractError(f"missing or duplicate symbol at line {line_number}: {symbol}")
        seen.add(symbol)
        records.append(
            IdentityRecord(
                symbol=symbol,
                security_name=row["Security Name"].strip(),
                listing_exchange=row["Listing Exchange"].strip(),
                nasdaq_traded=row["Nasdaq Traded"] == "Y",
                security_type=_classify(
                    row["Security Name"],
                    row["ETF"],
                    row["Test Issue"],
                    row["Financial Status"],
                    row["Nasdaq Traded"],
                ),
                test_issue=row["Test Issue"] == "Y",
                financial_status=row["Financial Status"].strip(),
                snapshot_id=snapshot.snapshot_id,
                snapshot_retrieved_at=snapshot.retrieved_at,
                file_created_at=file_created_at,
                evidence_state=evidence_state,
                synthetic_permit_ids=synthetic_permit_ids,
            )
        )
    if not completeness.minimum_records <= len(records) <= completeness.maximum_records:
        raise ContractError("nasdaqtraded.txt record count fails the completeness policy")
    if prior_accepted_record_count is not None:
        if (
            isinstance(prior_accepted_record_count, bool)
            or not isinstance(prior_accepted_record_count, int)
            or prior_accepted_record_count < 1
        ):
            raise ContractError("prior Nasdaq accepted count must be a positive integer")
        delta = (len(records) - prior_accepted_record_count) / prior_accepted_record_count
        if delta < -completeness.maximum_drop_fraction:
            raise ContractError("Nasdaq membership count drop exceeds the accepted policy")
        if abs(delta) > completeness.maximum_count_change_fraction:
            raise ContractError("Nasdaq membership count change exceeds the accepted policy")
    return tuple(records)
