"""Outcome-free point-in-time input and stock-date foundation contracts.

The types in this module compose verified identity, universe, calendar, bar,
and corporate-action evidence.  They cannot represent labels, future paths,
realized returns, strategy results, or holdout data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Mapping

from .bounded_universe import UniverseSnapshot
from .common import (
    canonical_json_bytes,
    iso_z,
    parse_timestamp,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .corporate_actions import BitemporalActionLedger
from .errors import ContractError, IntegrityError
from .identity import BitemporalIdentityLedger, IdentityVersion


FOUNDATION_EVIDENCE_STATES = {
    "PIT_CONFIRMED",
    "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE",
}
FORBIDDEN_INPUT_FIELDS = {
    "alpha",
    "backtest",
    "cagr",
    "drawdown",
    "exit_price",
    "final_holdout",
    "forward_return",
    "future_price_path",
    "future_return",
    "hit_rate",
    "holding_period_return",
    "label",
    "outcome",
    "performance",
    "pnl",
    "realized_return",
    "sharpe",
    "strategy_return",
    "target",
    "trade_return",
}
FORBIDDEN_INPUT_PREFIXES = (
    "forward_",
    "future_",
    "label_",
    "outcome_",
    "pnl_",
    "realized_",
    "target_",
)
BAR_ADJUSTMENT_STATES = {"RAW_OBSERVED", "CAUSAL_ACTION_ADJUSTED"}
BAR_QUALITY_FLAGS = {
    "ACTION_ASSOCIATED_EXTREME_GAP",
    "EXTREME_GAP_REQUIRES_REVIEW",
    "STALE_ZERO_VOLUME_BAR",
    "TRADING_HALT_SUSPECTED",
    "ZERO_VOLUME",
}


class CausalInputKind(str, Enum):
    PRICE = "PRICE"
    VOLUME = "VOLUME"
    IDENTITY = "IDENTITY"
    MEMBERSHIP = "MEMBERSHIP"
    CORPORATE_ACTION = "CORPORATE_ACTION"
    DELISTING = "DELISTING"
    SECTOR_CLASSIFICATION = "SECTOR_CLASSIFICATION"
    INDUSTRY_CLASSIFICATION = "INDUSTRY_CLASSIFICATION"
    FUNDAMENTAL = "FUNDAMENTAL"
    EARNINGS_EVENT = "EARNINGS_EVENT"
    ANALYST_ESTIMATE = "ANALYST_ESTIMATE"
    SESSION = "SESSION"


def _canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError(f"{field} must be nonempty canonical text")
    return value


def _validate_payload_items(
    payload_items: tuple[tuple[str, object], ...],
) -> None:
    if type(payload_items) is not tuple or not payload_items:
        raise ContractError("causal input payload must be a nonempty tuple")
    if any(type(item) is not tuple or len(item) != 2 for item in payload_items):
        raise ContractError("causal input payload entries must be exact pairs")
    names = tuple(item[0] for item in payload_items)
    if names != tuple(sorted(set(names))):
        raise ContractError("causal input payload fields must be sorted and unique")
    for item in payload_items:
        name, value = item
        _canonical_text(name, "causal input payload field")
        lowered = name.casefold()
        if lowered in FORBIDDEN_INPUT_FIELDS or any(
            lowered.startswith(prefix) for prefix in FORBIDDEN_INPUT_PREFIXES
        ):
            raise ContractError(f"causal input payload field is prohibited: {name}")
        if value is not None and type(value) not in {str, int, float, bool}:
            raise ContractError(
                "causal input payload values must be flat canonical JSON scalars"
            )
        if type(value) is str and value != value.strip():
            raise ContractError("causal input payload text must be canonical")
        if type(value) is float and not math.isfinite(value):
            raise ContractError("causal input payload numbers must be finite")


@dataclass(frozen=True)
class AvailabilityStamp:
    effective_time: datetime
    published_time: datetime | None
    received_time: datetime
    usable_time: datetime
    source_revision: str
    source_identifier: str
    source_snapshot_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "effective_time": iso_z(self.effective_time),
            "published_time": (
                iso_z(self.published_time) if self.published_time is not None else None
            ),
            "received_time": iso_z(self.received_time),
            "usable_time": iso_z(self.usable_time),
            "source_revision": self.source_revision,
            "source_identifier": self.source_identifier,
            "source_snapshot_id": self.source_snapshot_id,
        }

    def validate(self) -> None:
        require_aware_utc(self.effective_time, "availability.effective_time")
        received = require_aware_utc(
            self.received_time, "availability.received_time"
        )
        usable = require_aware_utc(self.usable_time, "availability.usable_time")
        published = (
            require_aware_utc(
                self.published_time, "availability.published_time"
            )
            if self.published_time is not None
            else None
        )
        if published is not None and published > received:
            raise ContractError("source cannot be received before publication")
        if usable < received or (published is not None and usable < published):
            raise ContractError(
                "usable time cannot precede publication or repository receipt"
            )
        _canonical_text(self.source_revision, "availability.source_revision")
        _canonical_text(self.source_identifier, "availability.source_identifier")
        require_sha256(
            self.source_snapshot_id, "availability.source_snapshot_id"
        )

    def require_usable_at(self, signal_cutoff: datetime) -> None:
        self.validate()
        cutoff = require_aware_utc(signal_cutoff, "signal_cutoff")
        if require_aware_utc(self.usable_time, "availability.usable_time") > cutoff:
            raise ContractError("causal input is unavailable at the signal cutoff")


@dataclass(frozen=True)
class CausalInputVersion:
    logical_key: str
    stable_security_id: str | None
    input_kind: CausalInputKind
    availability: AvailabilityStamp
    revision_number: int
    predecessor_record_id: str | None
    payload_items: tuple[tuple[str, object], ...]
    evidence_state: str
    record_id: str

    @classmethod
    def create(
        cls,
        *,
        logical_key: str,
        stable_security_id: str | None,
        input_kind: CausalInputKind,
        availability: AvailabilityStamp,
        revision_number: int,
        predecessor_record_id: str | None,
        payload: Mapping[str, object],
        evidence_state: str,
    ) -> "CausalInputVersion":
        payload_items = tuple(sorted(payload.items()))
        provisional = cls(
            logical_key=logical_key,
            stable_security_id=stable_security_id,
            input_kind=input_kind,
            availability=availability,
            revision_number=revision_number,
            predecessor_record_id=predecessor_record_id,
            payload_items=payload_items,
            evidence_state=evidence_state,
            record_id="",
        )
        value = cls(
            **{
                **provisional.__dict__,
                "record_id": sha256_bytes(
                    canonical_json_bytes(provisional.unsigned_dict())
                ),
            }
        )
        value.validate()
        return value

    def payload_dict(self) -> dict[str, object]:
        return dict(self.payload_items)

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "logical_key": self.logical_key,
            "stable_security_id": self.stable_security_id,
            "input_kind": self.input_kind.value,
            "availability": self.availability.as_dict(),
            "revision_number": self.revision_number,
            "predecessor_record_id": self.predecessor_record_id,
            "payload": self.payload_dict(),
            "evidence_state": self.evidence_state,
        }

    def validate(self) -> None:
        _canonical_text(self.logical_key, "causal input logical_key")
        if self.stable_security_id is not None:
            _canonical_text(
                self.stable_security_id, "causal input stable_security_id"
            )
        if type(self.input_kind) is not CausalInputKind:
            raise ContractError("causal input kind must use the exact enum")
        if self.input_kind is not CausalInputKind.SESSION and self.stable_security_id is None:
            raise ContractError("security-level causal input requires a stable identity")
        self.availability.validate()
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise ContractError("causal input revision number must be positive")
        if self.revision_number == 1:
            if self.predecessor_record_id is not None:
                raise ContractError("first causal input revision cannot name a predecessor")
        else:
            require_sha256(
                self.predecessor_record_id,
                "causal input predecessor_record_id",
            )
        _validate_payload_items(self.payload_items)
        if self.evidence_state not in FOUNDATION_EVIDENCE_STATES:
            raise ContractError("causal input evidence state is not eligible")
        require_sha256(self.record_id, "causal input record_id")
        if self.record_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("causal input record ID differs from its content")


class CausalInputStore:
    """Append-only bitemporal input vintages with cutoff-safe views."""

    def __init__(self, rows: Iterable[CausalInputVersion] = ()) -> None:
        self._rows: list[CausalInputVersion] = []
        for row in rows:
            self.append(row)

    @property
    def rows(self) -> tuple[CausalInputVersion, ...]:
        return tuple(self._rows)

    def append(self, row: CausalInputVersion) -> None:
        if type(row) is not CausalInputVersion:
            raise ContractError("causal input store requires exact version rows")
        row.validate()
        if any(existing.record_id == row.record_id for existing in self._rows):
            return
        prior = [
            existing
            for existing in self._rows
            if existing.logical_key == row.logical_key
        ]
        if any(existing.revision_number == row.revision_number for existing in prior):
            raise IntegrityError("conflicting causal input revision")
        if not prior:
            if row.revision_number != 1:
                raise IntegrityError("causal input revision chain must begin at one")
        else:
            latest = max(prior, key=lambda value: value.revision_number)
            if row.revision_number != latest.revision_number + 1:
                raise IntegrityError("causal input revisions must append consecutively")
            if row.predecessor_record_id != latest.record_id:
                raise IntegrityError("causal input predecessor binding differs")
            if row.availability.usable_time < latest.availability.usable_time:
                raise IntegrityError("causal input usable time cannot move backward")
            if (
                row.input_kind is not latest.input_kind
                or row.stable_security_id != latest.stable_security_id
            ):
                raise IntegrityError("causal input revision changes its identity or kind")
        self._rows.append(row)
        self._rows.sort(
            key=lambda value: (
                value.availability.usable_time,
                value.logical_key,
                value.revision_number,
                value.record_id,
            )
        )

    def visible_as_of(
        self,
        signal_cutoff: datetime,
        *,
        kinds: frozenset[CausalInputKind] | None = None,
    ) -> tuple[CausalInputVersion, ...]:
        cutoff = require_aware_utc(signal_cutoff, "signal_cutoff")
        latest: dict[str, CausalInputVersion] = {}
        for row in self._rows:
            if row.availability.usable_time > cutoff:
                continue
            if kinds is not None and row.input_kind not in kinds:
                continue
            current = latest.get(row.logical_key)
            if current is None or row.revision_number > current.revision_number:
                latest[row.logical_key] = row
        return tuple(sorted(latest.values(), key=lambda value: value.logical_key))

    def snapshot_id(
        self,
        signal_cutoff: datetime,
        *,
        kinds: frozenset[CausalInputKind] | None = None,
    ) -> str:
        visible = self.visible_as_of(signal_cutoff, kinds=kinds)
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "signal_cutoff": iso_z(signal_cutoff),
                    "kinds": (
                        sorted(value.value for value in kinds)
                        if kinds is not None
                        else None
                    ),
                    "record_ids": [value.record_id for value in visible],
                }
            )
        )


def require_inputs_usable_at(
    rows: Iterable[CausalInputVersion],
    signal_cutoff: datetime,
) -> tuple[CausalInputVersion, ...]:
    cutoff = require_aware_utc(signal_cutoff, "signal_cutoff")
    materialized = tuple(rows)
    for row in materialized:
        if type(row) is not CausalInputVersion:
            raise ContractError("causal consumer requires exact input versions")
        row.validate()
        row.availability.require_usable_at(cutoff)
    return materialized


@dataclass(frozen=True)
class SessionBoundary:
    session: date
    open_at: datetime
    close_at: datetime
    early_close: bool
    calendar_release_id: str
    timezone: str
    market_phase: str
    session_id: str

    @classmethod
    def create(
        cls,
        *,
        session: date,
        open_at: datetime,
        close_at: datetime,
        early_close: bool,
        calendar_release_id: str,
    ) -> "SessionBoundary":
        unsigned = {
            "session": session.isoformat() if type(session) is date else session,
            "open_at": iso_z(open_at),
            "close_at": iso_z(close_at),
            "early_close": early_close,
            "calendar_release_id": calendar_release_id,
            "timezone": "America/New_York",
            "market_phase": "REGULAR",
        }
        value = cls(
            session=session,
            open_at=open_at,
            close_at=close_at,
            early_close=early_close,
            calendar_release_id=calendar_release_id,
            timezone="America/New_York",
            market_phase="REGULAR",
            session_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "open_at": iso_z(self.open_at),
            "close_at": iso_z(self.close_at),
            "early_close": self.early_close,
            "calendar_release_id": self.calendar_release_id,
            "timezone": self.timezone,
            "market_phase": self.market_phase,
        }

    def validate(self) -> None:
        if type(self.session) is not date:
            raise ContractError("session boundary requires an exact date")
        opened = require_aware_utc(self.open_at, "session.open_at")
        closed = require_aware_utc(self.close_at, "session.close_at")
        if opened >= closed:
            raise ContractError("regular session open must precede close")
        duration_minutes = int((closed - opened).total_seconds() // 60)
        if duration_minutes <= 0 or duration_minutes > 390:
            raise ContractError("regular session duration is invalid")
        if type(self.early_close) is not bool or self.early_close != (
            duration_minutes < 390
        ):
            raise ContractError("session early-close flag differs from its boundaries")
        require_sha256(self.calendar_release_id, "session.calendar_release_id")
        if self.timezone != "America/New_York" or self.market_phase != "REGULAR":
            raise ContractError("session boundary must use regular America/New_York semantics")
        require_sha256(self.session_id, "session.session_id")
        if self.session_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("session ID differs from its boundaries")


@dataclass(frozen=True)
class CausalDailyBar:
    stable_security_id: str
    session: date
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int | None
    vwap: float | None
    availability: AvailabilityStamp
    source_release_id: str
    identity_snapshot_id: str
    adjustment_state: str
    raw_source_bar_id: str | None
    corporate_action_ids: tuple[str, ...]
    quality_flags: tuple[str, ...]
    evidence_state: str
    bar_id: str

    @classmethod
    def create(cls, **fields: object) -> "CausalDailyBar":
        provisional = cls(**fields, bar_id="")
        value = cls(
            **{
                **provisional.__dict__,
                "bar_id": sha256_bytes(
                    canonical_json_bytes(provisional.unsigned_dict())
                ),
            }
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "session": self.session.isoformat(),
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": self.volume,
            "trade_count": self.trade_count,
            "vwap": float(self.vwap) if self.vwap is not None else None,
            "availability": self.availability.as_dict(),
            "source_release_id": self.source_release_id,
            "identity_snapshot_id": self.identity_snapshot_id,
            "adjustment_state": self.adjustment_state,
            "raw_source_bar_id": self.raw_source_bar_id,
            "corporate_action_ids": list(self.corporate_action_ids),
            "quality_flags": list(self.quality_flags),
            "evidence_state": self.evidence_state,
        }

    def validate(self) -> None:
        _canonical_text(self.stable_security_id, "bar.stable_security_id")
        if type(self.session) is not date:
            raise ContractError("bar session must be an exact date")
        values = (self.open, self.high, self.low, self.close)
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            for value in values
        ):
            raise ContractError("bar OHLC values must be positive finite numbers")
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ) or self.low > self.high:
            raise ContractError("bar violates OHLC relationships")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int) or self.volume < 0:
            raise ContractError("bar volume must be a nonnegative exact integer")
        if self.trade_count is not None and (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count < 0
        ):
            raise ContractError("bar trade_count must be a nonnegative integer or null")
        if self.vwap is not None and (
            isinstance(self.vwap, bool)
            or not isinstance(self.vwap, (int, float))
            or not math.isfinite(float(self.vwap))
            or float(self.vwap) <= 0
        ):
            raise ContractError("bar VWAP must be positive finite or null")
        self.availability.validate()
        require_sha256(self.source_release_id, "bar.source_release_id")
        require_sha256(self.identity_snapshot_id, "bar.identity_snapshot_id")
        if self.adjustment_state not in BAR_ADJUSTMENT_STATES:
            raise ContractError("bar adjustment state is invalid")
        if self.corporate_action_ids != tuple(sorted(set(self.corporate_action_ids))):
            raise ContractError("bar corporate-action IDs must be sorted and unique")
        for value in self.corporate_action_ids:
            _canonical_text(value, "bar.corporate_action_id")
        if self.adjustment_state == "RAW_OBSERVED":
            if self.corporate_action_ids or self.raw_source_bar_id is not None:
                raise ContractError(
                    "raw observed bar cannot claim adjustments or another raw source"
                )
        else:
            require_sha256(self.raw_source_bar_id, "bar.raw_source_bar_id")
            if not self.corporate_action_ids:
                raise ContractError("causally adjusted bar requires exact action lineage")
        if self.quality_flags != tuple(sorted(set(self.quality_flags))) or any(
            value not in BAR_QUALITY_FLAGS for value in self.quality_flags
        ):
            raise ContractError("bar quality flags are invalid")
        if (self.volume == 0) != ("ZERO_VOLUME" in self.quality_flags):
            raise ContractError("bar zero-volume flag differs from volume")
        if self.evidence_state not in FOUNDATION_EVIDENCE_STATES:
            raise ContractError("bar evidence state is not eligible")
        require_sha256(self.bar_id, "bar.bar_id")
        if self.bar_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("bar ID differs from its content")


@dataclass(frozen=True)
class BarIntegrityReport:
    stable_security_id: str
    expected_sessions: tuple[date, ...]
    observed_sessions: tuple[date, ...]
    missing_sessions: tuple[date, ...]
    unexpected_sessions: tuple[date, ...]
    zero_volume_sessions: tuple[date, ...]
    stale_price_sessions: tuple[date, ...]
    stale_zero_volume_sessions: tuple[date, ...]
    halt_suspected_sessions: tuple[date, ...]
    action_associated_extreme_gap_sessions: tuple[date, ...]
    unexplained_extreme_gap_sessions: tuple[date, ...]
    extreme_gap_threshold: float | None
    state: str
    report_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "expected_sessions": [value.isoformat() for value in self.expected_sessions],
            "observed_sessions": [value.isoformat() for value in self.observed_sessions],
            "missing_sessions": [value.isoformat() for value in self.missing_sessions],
            "unexpected_sessions": [
                value.isoformat() for value in self.unexpected_sessions
            ],
            "zero_volume_sessions": [value.isoformat() for value in self.zero_volume_sessions],
            "stale_price_sessions": [
                value.isoformat() for value in self.stale_price_sessions
            ],
            "stale_zero_volume_sessions": [
                value.isoformat() for value in self.stale_zero_volume_sessions
            ],
            "halt_suspected_sessions": [
                value.isoformat() for value in self.halt_suspected_sessions
            ],
            "action_associated_extreme_gap_sessions": [
                value.isoformat()
                for value in self.action_associated_extreme_gap_sessions
            ],
            "unexplained_extreme_gap_sessions": [
                value.isoformat() for value in self.unexplained_extreme_gap_sessions
            ],
            "extreme_gap_threshold": self.extreme_gap_threshold,
            "state": self.state,
        }

    def validate(self) -> None:
        _canonical_text(self.stable_security_id, "bar report stable_security_id")
        session_fields = (
            self.expected_sessions,
            self.observed_sessions,
            self.missing_sessions,
            self.unexpected_sessions,
            self.zero_volume_sessions,
            self.stale_price_sessions,
            self.stale_zero_volume_sessions,
            self.halt_suspected_sessions,
            self.action_associated_extreme_gap_sessions,
            self.unexplained_extreme_gap_sessions,
        )
        if any(
            values != tuple(sorted(set(values)))
            or any(type(value) is not date for value in values)
            for values in session_fields
        ):
            raise ContractError("bar integrity session censuses must be sorted and unique")
        if not self.expected_sessions or not self.observed_sessions:
            raise ContractError("bar integrity report requires expected and observed sessions")
        expected_missing = tuple(
            value for value in self.expected_sessions if value not in self.observed_sessions
        )
        expected_unexpected = tuple(
            value for value in self.observed_sessions if value not in self.expected_sessions
        )
        if (
            self.missing_sessions != expected_missing
            or self.unexpected_sessions != expected_unexpected
        ):
            raise IntegrityError("bar integrity session differences are inconsistent")
        if not set(self.stale_zero_volume_sessions).issubset(
            set(self.stale_price_sessions) & set(self.zero_volume_sessions)
        ):
            raise IntegrityError("stale zero-volume sessions differ from their evidence")
        if set(self.action_associated_extreme_gap_sessions) & set(
            self.unexplained_extreme_gap_sessions
        ):
            raise IntegrityError("extreme-gap classifications overlap")
        if self.extreme_gap_threshold is not None and (
            isinstance(self.extreme_gap_threshold, bool)
            or not isinstance(self.extreme_gap_threshold, (int, float))
            or not math.isfinite(float(self.extreme_gap_threshold))
            or not 0 < float(self.extreme_gap_threshold) < 10
        ):
            raise ContractError("bar integrity extreme-gap threshold is invalid")
        if self.state not in {
            "PASS",
            "FAIL_INTEGRITY",
            "BLOCKED_EXTREME_GAP_POLICY_UNRESOLVED",
        }:
            raise ContractError("bar integrity report state is invalid")
        hard_failures = any(
            (
                self.missing_sessions,
                self.unexpected_sessions,
                self.zero_volume_sessions,
                self.stale_price_sessions,
                self.halt_suspected_sessions,
                self.unexplained_extreme_gap_sessions,
            )
        )
        expected_state = (
            "BLOCKED_EXTREME_GAP_POLICY_UNRESOLVED"
            if self.extreme_gap_threshold is None
            else "FAIL_INTEGRITY"
            if hard_failures
            else "PASS"
        )
        if self.state != expected_state:
            raise IntegrityError("bar integrity state differs from its evidence")
        require_sha256(self.report_id, "bar integrity report_id")
        if self.report_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("bar integrity report ID differs from its content")


def assess_daily_bar_integrity(
    bars: Iterable[CausalDailyBar],
    *,
    expected_sessions: tuple[date, ...],
    action_sessions: frozenset[date],
    extreme_gap_threshold: float | None,
) -> BarIntegrityReport:
    materialized = tuple(bars)
    if not materialized:
        raise ContractError("bar integrity assessment requires observations")
    if expected_sessions != tuple(sorted(set(expected_sessions))) or not expected_sessions:
        raise ContractError("expected sessions must be sorted, unique, and nonempty")
    if any(type(value) is not date for value in action_sessions):
        raise ContractError("action sessions must contain exact dates")
    if extreme_gap_threshold is not None and (
        isinstance(extreme_gap_threshold, bool)
        or not isinstance(extreme_gap_threshold, (int, float))
        or not math.isfinite(float(extreme_gap_threshold))
        or not 0 < float(extreme_gap_threshold) < 10
    ):
        raise ContractError("extreme gap threshold must be finite and between zero and ten")
    for bar in materialized:
        bar.validate()
    asset_ids = {bar.stable_security_id for bar in materialized}
    if len(asset_ids) != 1:
        raise ContractError("bar integrity assessment requires one stable security")
    by_session: dict[date, CausalDailyBar] = {}
    for bar in materialized:
        if bar.session in by_session:
            raise IntegrityError("duplicate asset/session bars are prohibited")
        by_session[bar.session] = bar
    observed = tuple(sorted(by_session))
    missing = tuple(value for value in expected_sessions if value not in by_session)
    unexpected = tuple(value for value in observed if value not in expected_sessions)
    zero = tuple(value for value in observed if by_session[value].volume == 0)
    stale_price: list[date] = []
    stale_zero: list[date] = []
    halt_suspected = tuple(
        value
        for value in observed
        if "TRADING_HALT_SUSPECTED" in by_session[value].quality_flags
    )
    associated: list[date] = []
    unexplained: list[date] = []
    prior: CausalDailyBar | None = None
    for session in observed:
        current = by_session[session]
        if prior is not None:
            unchanged = (current.open, current.high, current.low, current.close) == (
                prior.open,
                prior.high,
                prior.low,
                prior.close,
            )
            if unchanged:
                stale_price.append(session)
                if current.volume == 0:
                    stale_zero.append(session)
            if extreme_gap_threshold is not None:
                gap = abs(float(current.open) / float(prior.close) - 1.0)
                if gap > float(extreme_gap_threshold):
                    if session in action_sessions:
                        associated.append(session)
                    else:
                        unexplained.append(session)
        prior = current
    if extreme_gap_threshold is None:
        state = "BLOCKED_EXTREME_GAP_POLICY_UNRESOLVED"
    elif missing or unexpected or zero or stale_price or halt_suspected or unexplained:
        state = "FAIL_INTEGRITY"
    else:
        state = "PASS"
    unsigned = {
        "stable_security_id": next(iter(asset_ids)),
        "expected_sessions": [value.isoformat() for value in expected_sessions],
        "observed_sessions": [value.isoformat() for value in observed],
        "missing_sessions": [value.isoformat() for value in missing],
        "unexpected_sessions": [value.isoformat() for value in unexpected],
        "zero_volume_sessions": [value.isoformat() for value in zero],
        "stale_price_sessions": [value.isoformat() for value in stale_price],
        "stale_zero_volume_sessions": [value.isoformat() for value in stale_zero],
        "halt_suspected_sessions": [value.isoformat() for value in halt_suspected],
        "action_associated_extreme_gap_sessions": [
            value.isoformat() for value in associated
        ],
        "unexplained_extreme_gap_sessions": [
            value.isoformat() for value in unexplained
        ],
        "extreme_gap_threshold": (
            float(extreme_gap_threshold) if extreme_gap_threshold is not None else None
        ),
        "state": state,
    }
    report = BarIntegrityReport(
        stable_security_id=next(iter(asset_ids)),
        expected_sessions=expected_sessions,
        observed_sessions=observed,
        missing_sessions=missing,
        unexpected_sessions=unexpected,
        zero_volume_sessions=zero,
        stale_price_sessions=tuple(stale_price),
        stale_zero_volume_sessions=tuple(stale_zero),
        halt_suspected_sessions=halt_suspected,
        action_associated_extreme_gap_sessions=tuple(associated),
        unexplained_extreme_gap_sessions=tuple(unexplained),
        extreme_gap_threshold=(
            float(extreme_gap_threshold) if extreme_gap_threshold is not None else None
        ),
        state=state,
        report_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    report.validate()
    return report


@dataclass(frozen=True)
class CausalStockDateRow:
    stable_security_id: str
    symbol: str
    exchange: str
    security_type: str
    session: date
    session_id: str
    identity_snapshot_id: str
    universe_snapshot_id: str
    raw_bar_id: str | None
    adjusted_bar_id: str | None
    eligible: bool
    universe_reason_codes: tuple[str, ...]
    corporate_action_ids: tuple[str, ...]
    corporate_action_coverage_complete: bool
    earliest_execution_session: date
    usable_at: datetime
    lineage_ids: tuple[str, ...]
    source_identifiers: tuple[str, ...]
    causal_ready: bool
    blocker_codes: tuple[str, ...]
    evidence_state: str
    row_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "stable_security_id": self.stable_security_id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "security_type": self.security_type,
            "session": self.session.isoformat(),
            "session_id": self.session_id,
            "identity_snapshot_id": self.identity_snapshot_id,
            "universe_snapshot_id": self.universe_snapshot_id,
            "raw_bar_id": self.raw_bar_id,
            "adjusted_bar_id": self.adjusted_bar_id,
            "eligible": self.eligible,
            "universe_reason_codes": list(self.universe_reason_codes),
            "corporate_action_ids": list(self.corporate_action_ids),
            "corporate_action_coverage_complete": self.corporate_action_coverage_complete,
            "earliest_execution_session": self.earliest_execution_session.isoformat(),
            "usable_at": iso_z(self.usable_at),
            "lineage_ids": list(self.lineage_ids),
            "source_identifiers": list(self.source_identifiers),
            "causal_ready": self.causal_ready,
            "blocker_codes": list(self.blocker_codes),
            "evidence_state": self.evidence_state,
        }

    def validate(self) -> None:
        for name in ("stable_security_id", "symbol", "exchange", "security_type"):
            _canonical_text(getattr(self, name), f"panel row {name}")
        if self.symbol != self.symbol.upper() or self.exchange != self.exchange.upper():
            raise ContractError("panel symbol and exchange must be uppercase")
        if type(self.session) is not date or type(self.earliest_execution_session) is not date:
            raise ContractError("panel sessions must be exact dates")
        if self.earliest_execution_session <= self.session:
            raise ContractError("earliest execution must follow the completed session")
        for name in (
            "session_id",
            "identity_snapshot_id",
            "universe_snapshot_id",
            "row_id",
        ):
            require_sha256(getattr(self, name), f"panel row {name}")
        for name in ("raw_bar_id", "adjusted_bar_id"):
            value = getattr(self, name)
            if value is not None:
                require_sha256(value, f"panel row {name}")
        if self.corporate_action_ids != tuple(sorted(set(self.corporate_action_ids))):
            raise ContractError(
                "panel row corporate_action_ids must be sorted and unique"
            )
        for value in self.corporate_action_ids:
            _canonical_text(value, "panel row corporate_action_id")
        if self.lineage_ids != tuple(sorted(set(self.lineage_ids))):
            raise ContractError("panel row lineage_ids must be sorted and unique")
        for value in self.lineage_ids:
            require_sha256(value, "panel row lineage_id")
        if self.source_identifiers != tuple(sorted(set(self.source_identifiers))) or any(
            type(value) is not str or not value for value in self.source_identifiers
        ):
            raise ContractError("panel source identifiers must be sorted and unique")
        for values, field in (
            (self.universe_reason_codes, "universe_reason_codes"),
            (self.blocker_codes, "blocker_codes"),
        ):
            if values != tuple(sorted(set(values))):
                raise ContractError(f"panel row {field} must be sorted and unique")
        require_aware_utc(self.usable_at, "panel row usable_at")
        if self.evidence_state not in FOUNDATION_EVIDENCE_STATES:
            raise ContractError("panel row evidence state is invalid")
        if self.causal_ready and (
            not self.eligible
            or self.raw_bar_id is None
            or not self.corporate_action_coverage_complete
            or self.blocker_codes
        ):
            raise ContractError("causal-ready panel row retains a blocker")
        if not self.causal_ready and not self.blocker_codes:
            raise ContractError("non-ready panel row requires explicit blockers")
        if self.row_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("panel row ID differs from its content")


@dataclass(frozen=True)
class CausalStockDatePanel:
    session: date
    signal_cutoff: datetime
    earliest_execution_session: date
    rows: tuple[CausalStockDateRow, ...]
    evidence_state: str
    panel_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "session": self.session.isoformat(),
            "signal_cutoff": iso_z(self.signal_cutoff),
            "earliest_execution_session": self.earliest_execution_session.isoformat(),
            "row_ids": [value.row_id for value in self.rows],
            "evidence_state": self.evidence_state,
        }

    def validate(self) -> None:
        if type(self.session) is not date or type(self.earliest_execution_session) is not date:
            raise ContractError("panel sessions must be exact dates")
        require_aware_utc(self.signal_cutoff, "panel.signal_cutoff")
        if not self.rows or self.rows != tuple(
            sorted(self.rows, key=lambda value: value.stable_security_id)
        ):
            raise ContractError("panel rows must be nonempty and stable-ID ordered")
        if len({value.stable_security_id for value in self.rows}) != len(self.rows):
            raise ContractError("panel contains duplicate stable securities")
        for row in self.rows:
            row.validate()
            if (
                row.session != self.session
                or row.earliest_execution_session != self.earliest_execution_session
                or row.evidence_state != self.evidence_state
            ):
                raise ContractError("panel row differs from its panel contract")
            if require_aware_utc(row.usable_at, "panel row usable_at") > require_aware_utc(
                self.signal_cutoff, "panel.signal_cutoff"
            ):
                raise ContractError("panel row was unavailable at the signal cutoff")
        if self.evidence_state not in FOUNDATION_EVIDENCE_STATES:
            raise ContractError("panel evidence state is invalid")
        require_sha256(self.panel_id, "panel.panel_id")
        if self.panel_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("panel ID differs from its content")


def _require_visible_identity(
    rows: Mapping[str, IdentityVersion],
    stable_security_id: str,
) -> IdentityVersion:
    try:
        return rows[stable_security_id]
    except KeyError as exc:
        raise ContractError(
            "universe decision lacks point-in-time stable identity evidence"
        ) from exc


def build_causal_stock_date_panel(
    *,
    session: SessionBoundary,
    next_session: SessionBoundary,
    signal_cutoff: datetime,
    identity_ledger: BitemporalIdentityLedger,
    universe_snapshot: UniverseSnapshot,
    bars: Iterable[CausalDailyBar],
    action_ledger: BitemporalActionLedger,
    evidence_state: str,
) -> CausalStockDatePanel:
    """Compose one D0 panel without loading or representing any future outcome."""

    session.validate()
    next_session.validate()
    cutoff = require_aware_utc(signal_cutoff, "signal_cutoff")
    if (
        next_session.session <= session.session
        or session.close_at > cutoff
        or cutoff >= next_session.open_at
    ):
        raise ContractError(
            "post-close signal cutoff must follow D0 close and precede next-session open"
        )
    if evidence_state not in FOUNDATION_EVIDENCE_STATES:
        raise ContractError("panel evidence state is invalid")
    if evidence_state == "PIT_CONFIRMED" and (
        not identity_ledger.trust_eligible or not action_ledger.trust_eligible
    ):
        raise ContractError(
            "PIT-confirmed panel requires trust-eligible identity and action ledgers"
        )
    if evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE" and (
        identity_ledger.trust_eligible or action_ledger.trust_eligible
    ):
        raise ContractError(
            "synthetic panel cannot relabel verified ledgers as synthetic fixtures"
        )
    if universe_snapshot.signal_session != session.session:
        raise ContractError("universe signal session differs from the panel session")
    if universe_snapshot.information_cutoff_session >= session.session:
        raise ContractError("universe does not preserve its prior-session cutoff")
    if require_aware_utc(universe_snapshot.decision_at, "universe.decision_at") > cutoff:
        raise ContractError("universe decision was unavailable at signal cutoff")
    visible_identities = {
        row.asset_id: row
        for row in identity_ledger.visible_as_of(
            effective_as_of=session.close_at,
            known_as_of=cutoff,
        )
    }
    bar_map: dict[str, dict[str, CausalDailyBar]] = {}
    for bar in bars:
        if type(bar) is not CausalDailyBar:
            raise ContractError("panel bars must use exact CausalDailyBar rows")
        bar.validate()
        asset_bars = bar_map.setdefault(bar.stable_security_id, {})
        if bar.adjustment_state in asset_bars:
            raise IntegrityError(
                "panel contains duplicate stable-security adjustment-state bars"
            )
        if bar.session != session.session:
            raise ContractError("panel bar belongs to another session")
        if require_aware_utc(bar.availability.effective_time, "bar.effective_time") != require_aware_utc(
            session.close_at, "session.close_at"
        ):
            raise ContractError(
                "daily bar effective time must equal the pinned regular-session close"
            )
        bar.availability.require_usable_at(cutoff)
        if bar.evidence_state != evidence_state:
            raise ContractError("bar and panel evidence states differ")
        asset_bars[bar.adjustment_state] = bar
    rows: list[CausalStockDateRow] = []
    for decision in universe_snapshot.rows:
        identity = _require_visible_identity(
            visible_identities, decision.stable_asset_id
        )
        expected_identity_state = (
            "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            if evidence_state == "SYNTHETIC_ONLY_NOT_TRUST_ELIGIBLE"
            else "NETWORK_AS_RECEIVED"
        )
        if identity.evidence_state != expected_identity_state:
            raise ContractError("identity and panel evidence states differ")
        if identity.symbol != decision.ticker:
            raise IntegrityError(
                "point-in-time ticker differs between identity and universe evidence"
            )
        if require_aware_utc(identity.known_at, "identity.known_at") > cutoff:
            raise ContractError("identity was not known by signal cutoff")
        asset_bars = bar_map.get(decision.stable_asset_id, {})
        raw_bar = asset_bars.get("RAW_OBSERVED")
        adjusted_bar = asset_bars.get("CAUSAL_ACTION_ADJUSTED")
        for bar in asset_bars.values():
            if bar.identity_snapshot_id != identity.identity_snapshot_id:
                raise IntegrityError("bar identity binding differs from visible identity")
        if adjusted_bar is not None and raw_bar is not None and (
            adjusted_bar.raw_source_bar_id != raw_bar.bar_id
        ):
            raise IntegrityError("adjusted bar differs from its raw bar lineage")
        action_rows = tuple(
            action
            for action in action_ledger.visible_as_of(decision.stable_asset_id, cutoff)
            if action.effective_session == session.session
        )
        action_ids = tuple(sorted(action.action_id for action in action_rows))
        coverage_rows = action_ledger.covering_effective_interval(
            decision.stable_asset_id,
            session.session,
            session.session,
            cutoff,
        )
        coverage_complete = bool(coverage_rows)
        blockers = set(decision.exclusion_reasons)
        if not decision.included:
            blockers.add("UNIVERSE_EXCLUDED")
        if not identity.membership_present:
            blockers.add("POINT_IN_TIME_MEMBERSHIP_ABSENT")
        if not identity.eligible:
            blockers.add("IDENTITY_INELIGIBLE")
        if raw_bar is None and adjusted_bar is None:
            blockers.add("MISSING_CAUSAL_DAILY_BAR")
        if not coverage_complete:
            blockers.add("INCOMPLETE_EFFECTIVE_EVENT_OR_DELISTING_COVERAGE")
        if action_ids and adjusted_bar is None:
            blockers.add("ACTION_EVENT_REQUIRES_CAUSAL_RECONCILIATION")
        if adjusted_bar is not None and adjusted_bar.corporate_action_ids != action_ids:
            raise IntegrityError(
                "adjusted bar action lineage differs from visible effective actions"
            )
        receipt_times = [
            parse_timestamp(text, "universe.source_receipt_time")
            for text in decision.source_receipt_times
        ]
        usable_candidates = [
            require_aware_utc(identity.known_at, "identity.known_at"),
            require_aware_utc(universe_snapshot.decision_at, "universe.decision_at"),
            *receipt_times,
            *(require_aware_utc(action.received_at, "action.received_at") for action in action_rows),
            *(
                require_aware_utc(coverage.received_at, "coverage.received_at")
                for coverage in coverage_rows
            ),
        ]
        for bar in asset_bars.values():
            usable_candidates.append(
                require_aware_utc(bar.availability.usable_time, "bar.usable_time")
            )
        usable_at = max(usable_candidates)
        if usable_at > cutoff:
            raise ContractError("panel lineage was unavailable at signal cutoff")
        raw_bar_id = (
            raw_bar.bar_id
            if raw_bar is not None
            else adjusted_bar.raw_source_bar_id
            if adjusted_bar is not None
            else None
        )
        adjusted_bar_id = adjusted_bar.bar_id if adjusted_bar is not None else None
        lineage = {
            session.session_id,
            next_session.session_id,
            identity.identity_snapshot_id,
            universe_snapshot.snapshot_id,
            *decision.evidence_hashes,
            *(action.raw_row_sha256 for action in action_rows),
            *(action.source_snapshot_id for action in action_rows),
            *(coverage.coverage_id for coverage in coverage_rows),
            *(coverage.coverage_content_id for coverage in coverage_rows),
            *(coverage.provider_coverage_id for coverage in coverage_rows),
            *(coverage.source_release_id for coverage in coverage_rows),
            *(
                snapshot_id
                for coverage in coverage_rows
                for snapshot_id in coverage.source_snapshot_ids
            ),
        }
        for bar in asset_bars.values():
            lineage.add(bar.bar_id)
        source_identifiers = {
            identity_ledger.source_epoch,
            action_ledger.source_epoch,
            *decision.source_memberships,
        }
        provisional = CausalStockDateRow(
            stable_security_id=decision.stable_asset_id,
            symbol=identity.symbol,
            exchange=identity.listing_exchange,
            security_type=identity.security_type.value,
            session=session.session,
            session_id=session.session_id,
            identity_snapshot_id=identity.identity_snapshot_id,
            universe_snapshot_id=universe_snapshot.snapshot_id,
            raw_bar_id=raw_bar_id,
            adjusted_bar_id=adjusted_bar_id,
            eligible=decision.included,
            universe_reason_codes=decision.exclusion_reasons,
            corporate_action_ids=action_ids,
            corporate_action_coverage_complete=coverage_complete,
            earliest_execution_session=next_session.session,
            usable_at=usable_at,
            lineage_ids=tuple(sorted(lineage)),
            source_identifiers=tuple(sorted(source_identifiers)),
            causal_ready=not blockers,
            blocker_codes=tuple(sorted(blockers)),
            evidence_state=evidence_state,
            row_id="",
        )
        row = CausalStockDateRow(
            **{
                **provisional.__dict__,
                "row_id": sha256_bytes(
                    canonical_json_bytes(provisional.unsigned_dict())
                ),
            }
        )
        row.validate()
        rows.append(row)
    rows.sort(key=lambda value: value.stable_security_id)
    unsigned = {
        "session": session.session.isoformat(),
        "signal_cutoff": iso_z(cutoff),
        "earliest_execution_session": next_session.session.isoformat(),
        "row_ids": [value.row_id for value in rows],
        "evidence_state": evidence_state,
    }
    panel = CausalStockDatePanel(
        session=session.session,
        signal_cutoff=cutoff,
        earliest_execution_session=next_session.session,
        rows=tuple(rows),
        evidence_state=evidence_state,
        panel_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    panel.validate()
    return panel
