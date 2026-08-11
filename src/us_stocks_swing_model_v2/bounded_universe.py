from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from .alpaca_free_bounded import EvidenceClass, PROFILE_ID
from .common import canonical_json_bytes, iso_z, require_aware_utc, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError


PRIMARY_PROFILE = PROFILE_ID
SENSITIVITY_PROFILE = "ALPACA_FREE_BOUNDED_V1_TOP_1000_SENSITIVITY"
ALLOWED_EXCHANGES = {"NASDAQ", "NYSE", "NYSE_AMERICAN"}
AFFIRMATIVE_COMMON_STOCK = "US_PRIMARY_LISTED_COMMON_STOCK"


@dataclass(frozen=True)
class IdentityEvidence:
    stable_asset_id: str
    provider_asset_id: str
    original_requested_ticker: str
    returned_ticker: str
    source_ticker: str
    requested_as_of: date
    ticker_effective_from: date
    ticker_effective_through: date | None
    listing_from: date | None
    delisting_through: date | None
    exchange: str
    effective_at: datetime
    known_at: datetime
    mapping_evidence_id: str
    mapping_status: str
    evidence_class: EvidenceClass

    def validate(self) -> None:
        for name in (
            "stable_asset_id",
            "provider_asset_id",
            "original_requested_ticker",
            "returned_ticker",
            "source_ticker",
            "exchange",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ContractError(f"identity {name} must be nonempty canonical text")
        for name in ("original_requested_ticker", "returned_ticker", "source_ticker"):
            if getattr(self, name) != getattr(self, name).upper():
                raise ContractError("identity tickers must be uppercase")
        if self.ticker_effective_through is not None and self.ticker_effective_through < self.ticker_effective_from:
            raise ContractError("ticker effective interval is invalid")
        if self.delisting_through is not None and self.listing_from is not None and self.delisting_through < self.listing_from:
            raise ContractError("listing interval is invalid")
        require_aware_utc(self.effective_at, "identity.effective_at")
        require_aware_utc(self.known_at, "identity.known_at")
        require_sha256(self.mapping_evidence_id, "identity.mapping_evidence_id")
        if self.mapping_status not in {"CONFIRMED_CONTINUITY", "CONFIRMED_DISTINCT", "UNRESOLVED"}:
            raise ContractError("identity mapping status is invalid")


class ProfileIdentityLedger:
    """Bitemporal profile view layered on the repository stable asset IDs."""

    def __init__(self, rows: Iterable[IdentityEvidence] = ()) -> None:
        self._rows: list[IdentityEvidence] = []
        for row in rows:
            self.append(row)

    def append(self, row: IdentityEvidence) -> None:
        row.validate()
        if any(existing == row for existing in self._rows):
            return
        for existing in self._rows:
            overlap = not (
                (existing.ticker_effective_through is not None and existing.ticker_effective_through < row.ticker_effective_from)
                or (row.ticker_effective_through is not None and row.ticker_effective_through < existing.ticker_effective_from)
            )
            if (
                overlap
                and existing.source_ticker == row.source_ticker
                and existing.stable_asset_id != row.stable_asset_id
            ):
                raise IntegrityError("overlapping ticker reuse maps to contradictory stable identities")
            if (
                existing.provider_asset_id == row.provider_asset_id
                and existing.stable_asset_id != row.stable_asset_id
            ):
                raise IntegrityError("one provider asset ID cannot map to multiple stable identities")
        self._rows.append(row)
        self._rows.sort(key=lambda value: (value.effective_at, value.known_at, value.stable_asset_id))

    def visible_as_of(self, *, effective_as_of: datetime, known_as_of: datetime) -> tuple[IdentityEvidence, ...]:
        effective = require_aware_utc(effective_as_of, "effective_as_of")
        known = require_aware_utc(known_as_of, "known_as_of")
        latest: dict[str, IdentityEvidence] = {}
        for row in self._rows:
            if row.effective_at <= effective and row.known_at <= known:
                prior = latest.get(row.stable_asset_id)
                if prior is None or (row.effective_at, row.known_at) > (prior.effective_at, prior.known_at):
                    latest[row.stable_asset_id] = row
        return tuple(sorted(latest.values(), key=lambda value: value.stable_asset_id))


@dataclass(frozen=True)
class LiquidityObservation:
    session: date
    close: float
    volume: float
    available_at: datetime
    source_hash: str

    def validate(self) -> None:
        require_aware_utc(self.available_at, "liquidity.available_at")
        require_sha256(self.source_hash, "liquidity.source_hash")
        if (
            isinstance(self.close, bool)
            or isinstance(self.volume, bool)
            or not isinstance(self.close, (int, float))
            or not isinstance(self.volume, (int, float))
            or not math.isfinite(float(self.close))
            or not math.isfinite(float(self.volume))
            or self.close <= 0
            or self.volume < 0
        ):
            raise ContractError("liquidity observation price/volume is invalid")


@dataclass(frozen=True)
class UniverseCandidate:
    identity: IdentityEvidence
    ticker: str
    security_classification: str
    exchange: str
    source_memberships: tuple[str, ...]
    source_receipt_times: tuple[datetime, ...]
    observations: tuple[LiquidityObservation, ...]
    evidence_hashes: tuple[str, ...]
    is_etf_or_etp: bool = False
    is_adr: bool = False
    is_preferred_warrant_right_or_unit: bool = False
    is_closed_end_or_mutual_fund: bool = False
    is_structured_product: bool = False
    is_leveraged_or_inverse: bool = False
    is_test_issue: bool = False
    is_otc: bool = False

    def validate(self) -> None:
        self.identity.validate()
        if self.ticker != self.ticker.upper() or self.ticker != self.identity.source_ticker:
            raise ContractError("candidate ticker differs from its effective identity evidence")
        for value in self.source_receipt_times:
            require_aware_utc(value, "candidate.source_receipt_time")
        if not self.source_memberships or len(self.source_receipt_times) != len(self.source_memberships):
            raise ContractError("candidate source membership evidence is incomplete")
        if not self.evidence_hashes:
            raise ContractError("candidate requires source evidence hashes")
        for value in self.evidence_hashes:
            require_sha256(value, "candidate.evidence_hash")
        sessions: set[date] = set()
        for observation in self.observations:
            observation.validate()
            if observation.session in sessions:
                raise ContractError("candidate has duplicate asset/session bars")
            sessions.add(observation.session)


@dataclass(frozen=True)
class UniverseDecision:
    stable_asset_id: str
    ticker: str
    source_memberships: tuple[str, ...]
    source_receipt_times: tuple[str, ...]
    security_classification: str
    exchange: str
    previous_close: float | None
    valid_prior_session_count: int
    trailing_60_median_dollar_volume: float | None
    liquidity_rank: int | None
    included: bool
    exclusion_reasons: tuple[str, ...]
    evidence_class: EvidenceClass
    evidence_hashes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "stable_asset_id": self.stable_asset_id,
            "ticker": self.ticker,
            "source_memberships": list(self.source_memberships),
            "source_receipt_times": list(self.source_receipt_times),
            "security_classification": self.security_classification,
            "exchange": self.exchange,
            "previous_close": self.previous_close,
            "valid_prior_session_count": self.valid_prior_session_count,
            "trailing_60_median_dollar_volume": self.trailing_60_median_dollar_volume,
            "liquidity_rank": self.liquidity_rank,
            "included": self.included,
            "exclusion_reasons": list(self.exclusion_reasons),
            "evidence_class": self.evidence_class.value,
            "evidence_hashes": list(self.evidence_hashes),
        }


@dataclass(frozen=True)
class UniverseSnapshot:
    snapshot_id: str
    profile_id: str
    signal_session: date
    information_cutoff_session: date
    decision_at: datetime
    target_size: int
    rows: tuple[UniverseDecision, ...]

    @property
    def selected(self) -> tuple[UniverseDecision, ...]:
        return tuple(row for row in self.rows if row.included)

    def as_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "profile_id": self.profile_id,
            "signal_session": self.signal_session.isoformat(),
            "information_cutoff_session": self.information_cutoff_session.isoformat(),
            "decision_at": iso_z(self.decision_at),
            "target_size": self.target_size,
            "candidate_count": len(self.rows),
            "selected_count": len(self.selected),
            "rows": [row.as_dict() for row in self.rows],
        }


def build_universe_snapshot(
    *,
    profile_id: str,
    signal_session: date,
    information_cutoff_session: date,
    decision_at: datetime,
    candidates: Iterable[UniverseCandidate],
) -> UniverseSnapshot:
    target_size = 500 if profile_id == PRIMARY_PROFILE else 1000 if profile_id == SENSITIVITY_PROFILE else None
    if target_size is None:
        raise ContractError("universe profile must be primary top-500 or isolated top-1,000 sensitivity")
    decision_at = require_aware_utc(decision_at, "decision_at")
    if information_cutoff_session >= signal_session:
        raise ContractError("universe information cutoff must be through T-1")
    candidate_list = tuple(candidates)
    if not candidate_list:
        raise ContractError("universe candidate snapshot cannot be empty")
    if len({candidate.identity.stable_asset_id for candidate in candidate_list}) != len(candidate_list):
        raise IntegrityError("universe candidates contain duplicate stable identities")
    prelim: list[dict[str, object]] = []
    for candidate in candidate_list:
        candidate.validate()
        reasons: list[str] = []
        if "CURRENT_ACTIVE_LIST_ONLY" in candidate.source_memberships:
            reasons.append("SURVIVORSHIP_SHORTCUT_REJECTED")
        if candidate.identity.mapping_status == "UNRESOLVED":
            reasons.append("UNRESOLVED_STABLE_IDENTITY")
        if (
            candidate.identity.listing_from is not None
            and candidate.identity.listing_from > signal_session
        ) or (
            candidate.identity.delisting_through is not None
            and candidate.identity.delisting_through < signal_session
        ):
            reasons.append("NOT_LISTED_AT_SIGNAL")
        if candidate.security_classification != AFFIRMATIVE_COMMON_STOCK:
            reasons.append("UNKNOWN_SECURITY_TYPE" if candidate.security_classification == "UNKNOWN" else "INELIGIBLE_SECURITY_TYPE")
        if candidate.exchange not in ALLOWED_EXCHANGES:
            reasons.append("OTC_EXCLUDED" if candidate.is_otc or candidate.exchange == "OTC" else "PRIMARY_EXCHANGE_EXCLUDED")
        flags = (
            (candidate.is_etf_or_etp, "ETF_OR_ETP_EXCLUDED"),
            (candidate.is_adr, "ADR_EXCLUDED"),
            (candidate.is_preferred_warrant_right_or_unit, "PREFERRED_WARRANT_RIGHT_UNIT_EXCLUDED"),
            (candidate.is_closed_end_or_mutual_fund, "FUND_EXCLUDED"),
            (candidate.is_structured_product, "STRUCTURED_PRODUCT_EXCLUDED"),
            (candidate.is_leveraged_or_inverse, "LEVERAGED_OR_INVERSE_EXCLUDED"),
            (candidate.is_test_issue, "TEST_ISSUE_EXCLUDED"),
            (candidate.is_otc, "OTC_EXCLUDED"),
        )
        reasons.extend(reason for enabled, reason in flags if enabled)
        if any(received > decision_at for received in candidate.source_receipt_times):
            reasons.append("SOURCE_NOT_KNOWN_BY_DECISION")
        eligible_observations = sorted(
            (
                row
                for row in candidate.observations
                if row.session <= information_cutoff_session and row.available_at <= decision_at
            ),
            key=lambda row: row.session,
        )
        trailing = eligible_observations[-60:]
        previous_close = eligible_observations[-1].close if eligible_observations else None
        median_dollar_volume = (
            statistics.median(float(row.close) * float(row.volume) for row in trailing)
            if len(trailing) == 60
            else None
        )
        if len(trailing) < 60:
            reasons.append("INSUFFICIENT_60_SESSION_LOOKBACK")
        if previous_close is None or previous_close < 5.0:
            reasons.append("PREVIOUS_CLOSE_BELOW_5")
        prelim.append(
            {
                "candidate": candidate,
                "reasons": sorted(set(reasons)),
                "previous_close": previous_close,
                "valid_count": len(eligible_observations),
                "median": median_dollar_volume,
            }
        )
    ranked = sorted(
        (item for item in prelim if not item["reasons"]),
        key=lambda item: (-float(item["median"]), item["candidate"].identity.stable_asset_id),
    )
    rank_by_id = {
        item["candidate"].identity.stable_asset_id: index + 1
        for index, item in enumerate(ranked)
    }
    rows: list[UniverseDecision] = []
    for item in prelim:
        candidate = item["candidate"]
        rank = rank_by_id.get(candidate.identity.stable_asset_id)
        reasons = list(item["reasons"])
        included = rank is not None and rank <= target_size
        if rank is not None and not included:
            reasons.append("OUTSIDE_LIQUIDITY_CUTOFF")
        rows.append(
            UniverseDecision(
                stable_asset_id=candidate.identity.stable_asset_id,
                ticker=candidate.ticker,
                source_memberships=candidate.source_memberships,
                source_receipt_times=tuple(iso_z(value) for value in candidate.source_receipt_times),
                security_classification=candidate.security_classification,
                exchange=candidate.exchange,
                previous_close=item["previous_close"],
                valid_prior_session_count=int(item["valid_count"]),
                trailing_60_median_dollar_volume=item["median"],
                liquidity_rank=rank,
                included=included,
                exclusion_reasons=tuple(sorted(set(reasons))),
                evidence_class=candidate.identity.evidence_class,
                evidence_hashes=candidate.evidence_hashes,
            )
        )
    rows.sort(key=lambda row: row.stable_asset_id)
    unsigned = {
        "profile_id": profile_id,
        "signal_session": signal_session.isoformat(),
        "information_cutoff_session": information_cutoff_session.isoformat(),
        "decision_at": iso_z(decision_at),
        "target_size": target_size,
        "rows": [row.as_dict() for row in rows],
    }
    return UniverseSnapshot(
        snapshot_id=sha256_bytes(canonical_json_bytes(unsigned)),
        profile_id=profile_id,
        signal_session=signal_session,
        information_cutoff_session=information_cutoff_session,
        decision_at=decision_at,
        target_size=target_size,
        rows=tuple(rows),
    )
