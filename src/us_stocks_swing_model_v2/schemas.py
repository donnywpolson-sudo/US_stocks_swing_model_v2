from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import Enum
from typing import Mapping

from .common import (
    canonical_json_bytes,
    iso_z,
    parse_timestamp,
    require_aware_utc,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError


class SecurityType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"
    UNKNOWN = "UNKNOWN"


class OutcomeStatus(str, Enum):
    PENDING = "PENDING"
    MATURED = "MATURED"
    HALTED = "HALTED"
    DELISTED = "DELISTED"
    ACTION_UNRESOLVED = "ACTION_UNRESOLVED"
    MISSING_SOURCE = "MISSING_SOURCE"


FORBIDDEN_FEATURE_FIELDS = {
    "label",
    "target",
    "outcome",
    "realized_return",
    "exit_close",
    "future_return",
    "wfa_result",
}
FORBIDDEN_FEATURE_PREFIXES = ("target_", "future_", "label_", "outcome_")

FORBIDDEN_PREDICTION_FIELDS = FORBIDDEN_FEATURE_FIELDS | {
    "option",
    "option_symbol",
    "strike",
    "expiration",
    "premium",
    "implied_volatility",
    "delta",
    "gamma",
    "vega",
    "theta",
    "proposed_trade",
    "trade_size",
}


def _forbidden_keys(payload: Mapping[str, object], forbidden: set[str]) -> set[str]:
    lowered = {key.lower(): key for key in payload}
    exact = {lowered[key] for key in forbidden if key in lowered}
    prefixes = {
        original
        for lowered_key, original in lowered.items()
        if any(lowered_key.startswith(prefix) for prefix in FORBIDDEN_FEATURE_PREFIXES)
    }
    return exact | prefixes


@dataclass(frozen=True)
class FeatureRow:
    asset_id: str
    symbol: str
    security_type: SecurityType
    decision_session: date
    decision_at: datetime
    available_at: datetime
    source_release_id: str
    feature_schema_id: str
    identity_release_id: str
    security_type_evidence_id: str
    calendar_release_id: str
    action_release_id: str
    source_epoch: str
    identity_known_at: datetime
    point_in_time_state: str
    prediction_deadline_at: datetime
    information_barrier_at: datetime
    values: Mapping[str, float]

    def validate(self) -> None:
        required = (
            self.asset_id,
            self.symbol,
            self.source_release_id,
            self.feature_schema_id,
            self.identity_release_id,
            self.security_type_evidence_id,
            self.calendar_release_id,
            self.action_release_id,
            self.source_epoch,
        )
        if not all(required):
            raise ContractError("feature identity and release fields are required")
        for name in (
            "source_release_id",
            "identity_release_id",
            "security_type_evidence_id",
            "calendar_release_id",
            "action_release_id",
        ):
            require_sha256(getattr(self, name), f"feature.{name}")
        decision = require_aware_utc(self.decision_at, "decision_at")
        available = require_aware_utc(self.available_at, "available_at")
        identity_known = require_aware_utc(self.identity_known_at, "identity_known_at")
        prediction_deadline = require_aware_utc(self.prediction_deadline_at, "prediction_deadline_at")
        information_barrier = require_aware_utc(self.information_barrier_at, "information_barrier_at")
        if available > decision:
            raise ContractError("feature was not available by decision time")
        if identity_known > decision:
            raise ContractError("identity evidence was not known by decision time")
        if prediction_deadline < decision or information_barrier <= prediction_deadline:
            raise ContractError("prediction deadline/label unlock chronology is invalid")
        if self.point_in_time_state not in {"PIT_CONFIRMED", "HISTORICAL_PROXY", "PIT_UNRESOLVED"}:
            raise ContractError("point-in-time state is not recognized")
        poisoned = _forbidden_keys(self.values, FORBIDDEN_FEATURE_FIELDS)
        if poisoned:
            raise ContractError(f"feature payload contains future/outcome fields: {sorted(poisoned)}")
        if not self.values:
            raise ContractError("feature values cannot be empty")
        for name, value in self.values.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ContractError(f"invalid feature value: {name}")

    @classmethod
    def from_release_payload(
        cls,
        payload: Mapping[str, object],
        *,
        source_release_id: str,
        source_epoch: str,
    ) -> "FeatureRow":
        expected = (
            set(cls.__dataclass_fields__)
            - {"source_release_id", "source_epoch", "values"}
        ) | {"ordered_values"}
        if set(payload) != expected:
            raise ContractError("feature release row fields differ from the exact contract")
        ordered_values = payload["ordered_values"]
        if not isinstance(ordered_values, list) or not ordered_values:
            raise ContractError("feature ordered_values must be a nonempty list")
        values: dict[str, float] = {}
        for pair in ordered_values:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not pair[0]
                or pair[0] in values
                or isinstance(pair[1], bool)
                or not isinstance(pair[1], (int, float))
            ):
                raise ContractError("feature ordered_values entry is invalid")
            values[pair[0]] = float(pair[1])
        row = cls(
            asset_id=str(payload["asset_id"]),
            symbol=str(payload["symbol"]),
            security_type=SecurityType(str(payload["security_type"])),
            decision_session=date.fromisoformat(str(payload["decision_session"])),
            decision_at=parse_timestamp(str(payload["decision_at"]), "feature.decision_at"),
            available_at=parse_timestamp(str(payload["available_at"]), "feature.available_at"),
            source_release_id=source_release_id,
            feature_schema_id=str(payload["feature_schema_id"]),
            identity_release_id=str(payload["identity_release_id"]),
            security_type_evidence_id=str(payload["security_type_evidence_id"]),
            calendar_release_id=str(payload["calendar_release_id"]),
            action_release_id=str(payload["action_release_id"]),
            source_epoch=source_epoch,
            identity_known_at=parse_timestamp(
                str(payload["identity_known_at"]), "feature.identity_known_at"
            ),
            point_in_time_state=str(payload["point_in_time_state"]),
            prediction_deadline_at=parse_timestamp(
                str(payload["prediction_deadline_at"]), "feature.prediction_deadline_at"
            ),
            information_barrier_at=parse_timestamp(
                str(payload["information_barrier_at"]), "feature.information_barrier_at"
            ),
            values=values,
        )
        row.validate()
        return row

    def receipt_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "security_type": self.security_type.value,
            "decision_session": self.decision_session.isoformat(),
            "decision_at": iso_z(self.decision_at),
            "available_at": iso_z(self.available_at),
            "source_release_id": self.source_release_id,
            "feature_schema_id": self.feature_schema_id,
            "identity_release_id": self.identity_release_id,
            "security_type_evidence_id": self.security_type_evidence_id,
            "calendar_release_id": self.calendar_release_id,
            "action_release_id": self.action_release_id,
            "source_epoch": self.source_epoch,
            "identity_known_at": iso_z(self.identity_known_at),
            "point_in_time_state": self.point_in_time_state,
            "prediction_deadline_at": iso_z(self.prediction_deadline_at),
            "information_barrier_at": iso_z(self.information_barrier_at),
            "ordered_values": [[name, float(value)] for name, value in self.values.items()],
        }

    @property
    def row_hash(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.receipt_dict()))


@dataclass(frozen=True)
class OutcomeRow:
    revision_id: str
    prediction_id: str
    eligibility_census_id: str
    revision_number: int
    prior_revision_id: str | None
    asset_id: str
    decision_session: date
    entry_session: date | None
    exit_session: date | None
    status: OutcomeStatus
    split_normalized_price_return: float | None
    reason: str | None
    calendar_release_id: str
    bar_release_id: str
    action_release_id: str
    source_epoch: str
    action_view_as_of: datetime
    target_semantics: str = "SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN"

    def validate(self) -> None:
        require_aware_utc(self.action_view_as_of, "action_view_as_of")
        required = (
            self.revision_id,
            self.prediction_id,
            self.eligibility_census_id,
            self.asset_id,
            self.calendar_release_id,
            self.bar_release_id,
            self.action_release_id,
            self.source_epoch,
        )
        if not all(required):
            raise ContractError("outcome identity and release IDs are required")
        for name in (
            "revision_id",
            "prediction_id",
            "eligibility_census_id",
            "calendar_release_id",
            "bar_release_id",
            "action_release_id",
        ):
            require_sha256(getattr(self, name), f"outcome.{name}")
        if self.target_semantics != "SIMPLE_SPLIT_NORMALIZED_PRICE_RETURN":
            raise ContractError("outcome target semantics must be simple split-normalized price return")
        if (
            isinstance(self.revision_number, bool)
            or not isinstance(self.revision_number, int)
            or self.revision_number < 1
        ):
            raise ContractError("outcome revision number must be positive")
        if self.revision_number == 1 and self.prior_revision_id is not None:
            raise ContractError("first outcome revision cannot name a predecessor")
        if self.revision_number > 1 and (
            self.prior_revision_id is None
        ):
            raise ContractError("later outcome revision requires the prior revision ID")
        if self.prior_revision_id is not None:
            require_sha256(self.prior_revision_id, "outcome.prior_revision_id")
        if self.revision_id != self.computed_revision_id:
            raise ContractError("outcome revision ID differs from canonical content")
        if self.entry_session is not None and self.entry_session <= self.decision_session:
            raise ContractError("outcome entry must follow the decision session")
        if self.entry_session is not None and self.exit_session is not None and self.exit_session < self.entry_session:
            raise ContractError("outcome exit cannot precede entry")
        if self.status is OutcomeStatus.MATURED:
            if self.entry_session is None or self.exit_session is None:
                raise ContractError("matured outcome requires entry and exit sessions")
            if (
                self.split_normalized_price_return is None
                or isinstance(self.split_normalized_price_return, bool)
                or not isinstance(self.split_normalized_price_return, (int, float))
                or not math.isfinite(self.split_normalized_price_return)
                or self.split_normalized_price_return <= -1
            ):
                raise ContractError("matured outcome requires a finite return")
        elif self.split_normalized_price_return is not None:
            raise ContractError("unresolved outcomes cannot contain a realized return")
        elif not self.reason:
            raise ContractError("unresolved outcomes require a reason")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "prediction_id": self.prediction_id,
            "eligibility_census_id": self.eligibility_census_id,
            "revision_number": self.revision_number,
            "prior_revision_id": self.prior_revision_id,
            "asset_id": self.asset_id,
            "decision_session": self.decision_session.isoformat(),
            "entry_session": self.entry_session.isoformat() if self.entry_session else None,
            "exit_session": self.exit_session.isoformat() if self.exit_session else None,
            "status": self.status.value,
            "split_normalized_price_return": self.split_normalized_price_return,
            "reason": self.reason,
            "calendar_release_id": self.calendar_release_id,
            "bar_release_id": self.bar_release_id,
            "action_release_id": self.action_release_id,
            "source_epoch": self.source_epoch,
            "action_view_as_of": iso_z(self.action_view_as_of),
            "target_semantics": self.target_semantics,
        }

    @property
    def computed_revision_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.unsigned_dict()))

    @classmethod
    def create(cls, **fields: object) -> "OutcomeRow":
        provisional = cls(revision_id="", **fields)
        outcome = replace(provisional, revision_id=provisional.computed_revision_id)
        outcome.validate()
        return outcome

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "OutcomeRow":
        if set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("outcome ledger fields differ from the exact schema")
        fields = dict(payload)
        fields["decision_session"] = date.fromisoformat(str(fields["decision_session"]))
        for name in ("entry_session", "exit_session"):
            fields[name] = date.fromisoformat(str(fields[name])) if fields[name] is not None else None
        fields["status"] = OutcomeStatus(str(fields["status"]))
        fields["action_view_as_of"] = parse_timestamp(str(fields["action_view_as_of"]), "action_view_as_of")
        outcome = cls(**fields)
        outcome.validate()
        return outcome

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {"revision_id": self.revision_id, **self.unsigned_dict()}


@dataclass(frozen=True)
class UnderlyingPrediction:
    prediction_id: str
    asset_id: str
    symbol: str
    security_type: SecurityType
    decision_session: date
    decision_at: datetime
    recorded_at: datetime
    time_authority: str
    synthetic_clock_permit_id: str | None
    eligibility_census_id: str
    bundle_id: str
    feature_release_id: str
    feature_row_hash: str
    identity_release_id: str
    security_type_evidence_id: str
    calendar_release_id: str
    action_release_id: str
    source_epoch: str
    point_in_time_state: str
    prediction_deadline_at: datetime
    information_barrier_at: datetime
    expected_five_session_return: float | None
    p_up: float | None
    p_down: float | None
    p_neutral: float | None
    uncertainty: float | None
    rank: int | None
    abstain: bool
    abstention_reason: str | None

    def validate(self) -> None:
        if not isinstance(self.decision_session, date):
            raise ContractError("decision_session must be a date")
        decision = require_aware_utc(self.decision_at, "decision_at")
        recorded = require_aware_utc(self.recorded_at, "recorded_at")
        if self.time_authority == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_clock_permit_id is not None:
                raise ContractError("production prediction cannot carry a synthetic clock permit")
        elif self.time_authority == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            if self.synthetic_clock_permit_id is None:
                raise ContractError("synthetic prediction requires its mechanics-only clock permit")
            require_sha256(self.synthetic_clock_permit_id, "prediction.synthetic_clock_permit_id")
        else:
            raise ContractError("prediction time authority is invalid")
        if recorded < decision:
            raise ContractError("prediction cannot be recorded before its decision time")
        deadline = require_aware_utc(self.prediction_deadline_at, "prediction_deadline_at")
        unlock = require_aware_utc(self.information_barrier_at, "information_barrier_at")
        if recorded > deadline or recorded >= unlock:
            raise ContractError("prediction was recorded after its entry/label-safe deadline")
        required = (
            self.prediction_id,
            self.asset_id,
            self.symbol,
            self.bundle_id,
            self.feature_release_id,
            self.feature_row_hash,
            self.identity_release_id,
            self.security_type_evidence_id,
            self.calendar_release_id,
            self.action_release_id,
            self.source_epoch,
            self.point_in_time_state,
            self.eligibility_census_id,
        )
        if not all(required):
            raise ContractError("prediction identity fields are required")
        for name in (
            "prediction_id",
            "eligibility_census_id",
            "bundle_id",
            "feature_release_id",
            "feature_row_hash",
            "identity_release_id",
            "security_type_evidence_id",
            "calendar_release_id",
            "action_release_id",
        ):
            require_sha256(getattr(self, name), f"prediction.{name}")
        if self.prediction_id != self.computed_prediction_id:
            raise ContractError("prediction ID/evidence hash does not match canonical prediction content")
        if type(self.abstain) is not bool:
            raise ContractError("prediction abstain must be boolean")
        if self.abstain:
            if not self.abstention_reason:
                raise ContractError("abstention requires a reason")
            if any(value is not None for value in (self.expected_five_session_return, self.p_up, self.p_down, self.p_neutral, self.uncertainty, self.rank)):
                raise ContractError("abstention cannot carry actionable forecast values")
            return
        if self.security_type is SecurityType.UNKNOWN:
            raise ContractError("unknown security type must abstain")
        probabilities = (self.p_up, self.p_down, self.p_neutral)
        if any(
            value is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in probabilities
        ):
            raise ContractError("prediction probabilities must be finite values in [0,1]")
        if abs(sum(float(value) for value in probabilities) - 1.0) > 1e-9:
            raise ContractError("prediction probabilities must sum to one")
        if (
            self.expected_five_session_return is None
            or isinstance(self.expected_five_session_return, bool)
            or not isinstance(self.expected_five_session_return, (int, float))
            or not math.isfinite(self.expected_five_session_return)
        ):
            raise ContractError("prediction requires finite expected return")
        if (
            self.uncertainty is None
            or isinstance(self.uncertainty, bool)
            or not isinstance(self.uncertainty, (int, float))
            or not math.isfinite(self.uncertainty)
            or self.uncertainty <= 0
        ):
            raise ContractError("prediction requires positive finite uncertainty")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ContractError("prediction requires a positive rank")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "security_type": self.security_type.value,
            "decision_session": self.decision_session.isoformat(),
            "decision_at": iso_z(self.decision_at),
            "recorded_at": iso_z(self.recorded_at),
            "time_authority": self.time_authority,
            "synthetic_clock_permit_id": self.synthetic_clock_permit_id,
            "eligibility_census_id": self.eligibility_census_id,
            "bundle_id": self.bundle_id,
            "feature_release_id": self.feature_release_id,
            "feature_row_hash": self.feature_row_hash,
            "identity_release_id": self.identity_release_id,
            "security_type_evidence_id": self.security_type_evidence_id,
            "calendar_release_id": self.calendar_release_id,
            "action_release_id": self.action_release_id,
            "source_epoch": self.source_epoch,
            "point_in_time_state": self.point_in_time_state,
            "prediction_deadline_at": iso_z(self.prediction_deadline_at),
            "information_barrier_at": iso_z(self.information_barrier_at),
            "expected_five_session_return": self.expected_five_session_return,
            "p_up": self.p_up,
            "p_down": self.p_down,
            "p_neutral": self.p_neutral,
            "uncertainty": self.uncertainty,
            "rank": self.rank,
            "abstain": self.abstain,
            "abstention_reason": self.abstention_reason,
        }

    @property
    def computed_prediction_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.unsigned_dict()))

    @classmethod
    def create(cls, **fields: object) -> "UnderlyingPrediction":
        provisional = cls(prediction_id="", **fields)
        prediction = replace(provisional, prediction_id=provisional.computed_prediction_id)
        prediction.validate()
        return prediction

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "UnderlyingPrediction":
        expected = set(cls.__dataclass_fields__)
        if set(payload) != expected:
            raise ContractError("prediction ledger fields differ from the exact schema")
        fields = dict(payload)
        fields["security_type"] = SecurityType(str(fields["security_type"]))
        fields["decision_session"] = date.fromisoformat(str(fields["decision_session"]))
        for name in ("decision_at", "recorded_at", "prediction_deadline_at", "information_barrier_at"):
            fields[name] = parse_timestamp(str(fields[name]), name)
        prediction = cls(**fields)
        prediction.validate()
        return prediction

    def as_dict(self) -> dict[str, object]:
        self.validate()
        payload = {"prediction_id": self.prediction_id, **self.unsigned_dict()}
        poisoned = _forbidden_keys(payload, FORBIDDEN_PREDICTION_FIELDS)
        if poisoned:
            raise ContractError(f"prediction contains forbidden fields: {sorted(poisoned)}")
        return payload


def assert_underlying_only_payload(payload: Mapping[str, object]) -> None:
    poisoned = _forbidden_keys(payload, FORBIDDEN_PREDICTION_FIELDS)
    if poisoned:
        raise ContractError(f"underlying prediction contains forbidden fields: {sorted(poisoned)}")
