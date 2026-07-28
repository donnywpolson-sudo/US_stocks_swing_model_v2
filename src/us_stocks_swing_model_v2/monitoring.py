"""Prospective monitoring with pure decisions and append-only governed state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from pathlib import Path
from typing import Any, Mapping

from .capabilities import SyntheticOnlyPermit
from .clock import TrustedClock, require_trusted_clock
from .common import (
    canonical_json_bytes,
    parse_utc_z,
    require_sha256,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError, IntegrityError
from .governance import LocalIntegrityRecord
from .ledger import HashChainLedger, LedgerAnchorStore
from .monitoring_policy import (
    FROZEN_MONITORING_POLICY,
    MONITORING_POLICY_VERSION,
    MONITORING_STATE_PRECEDENCE,
    frozen_monitoring_policy_hash,
)


class MonitoringContractError(ValueError):
    pass


class MonitoringState(str, Enum):
    MONITORING_PENDING = "MONITORING_PENDING"
    MONITORING_OK = "MONITORING_OK"
    MONITORING_WARNING = "MONITORING_WARNING"
    MONITORING_PAUSED = "MONITORING_PAUSED"
    MONITORING_INVALID = "MONITORING_INVALID"


@dataclass(frozen=True)
class MonitoringPolicy:
    minimum_distinct_dates: int = 30
    minimum_predictions: int = 500
    psi_warning: float = 0.10
    psi_pause: float = 0.25
    missingness_warning_delta: float = 0.05
    missingness_pause_delta: float = 0.10
    coverage_warning_ratio: float = 0.95
    coverage_pause_ratio: float = 0.90
    matured_score_pause_degradation: float = 0.10

    def as_dict(self) -> dict[str, object]:
        self.validate()
        payload = {
            "policy_version": MONITORING_POLICY_VERSION,
            "minimum_distinct_dates": self.minimum_distinct_dates,
            "minimum_predictions": self.minimum_predictions,
            "psi_warning": self.psi_warning,
            "psi_pause": self.psi_pause,
            "missingness_warning_delta": self.missingness_warning_delta,
            "missingness_pause_delta": self.missingness_pause_delta,
            "coverage_warning_ratio": self.coverage_warning_ratio,
            "coverage_pause_ratio": self.coverage_pause_ratio,
            "matured_score_pause_degradation": self.matured_score_pause_degradation,
            "state_precedence": list(MONITORING_STATE_PRECEDENCE),
        }
        if payload != FROZEN_MONITORING_POLICY:
            raise MonitoringContractError(
                "monitoring policy differs from the frozen canonical payload"
            )
        return payload

    @property
    def policy_hash(self) -> str:
        self.as_dict()
        return frozen_monitoring_policy_hash()

    def validate(self) -> None:
        if self.minimum_distinct_dates != 30 or self.minimum_predictions != 500:
            raise MonitoringContractError("global monitoring window must remain 30 dates and 500 predictions")
        expected = (0.10, 0.25, 0.05, 0.10, 0.95, 0.90, 0.10)
        actual = (
            self.psi_warning, self.psi_pause, self.missingness_warning_delta,
            self.missingness_pause_delta, self.coverage_warning_ratio,
            self.coverage_pause_ratio, self.matured_score_pause_degradation,
        )
        if any(type(value) is not float or not math.isfinite(value) for value in actual):
            raise MonitoringContractError("monitoring thresholds must be finite explicit floats")
        if actual != expected:
            raise MonitoringContractError("monitoring thresholds differ from the frozen balanced policy")


@dataclass(frozen=True)
class MonitoringObservation:
    distinct_dates: int
    eligible_predictions: int
    maximum_psi: float
    maximum_missingness_delta: float
    coverage: float
    sealed_coverage_floor: float
    matured_score_relative_degradation: float | None
    source_or_bundle_stale: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "distinct_dates": self.distinct_dates,
            "eligible_predictions": self.eligible_predictions,
            "maximum_psi": self.maximum_psi,
            "maximum_missingness_delta": self.maximum_missingness_delta,
            "coverage": self.coverage,
            "sealed_coverage_floor": self.sealed_coverage_floor,
            "matured_score_relative_degradation": self.matured_score_relative_degradation,
            "source_or_bundle_stale": self.source_or_bundle_stale,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MonitoringObservation":
        if set(payload) != set(cls.__dataclass_fields__):
            raise MonitoringContractError(
                "monitoring observation fields differ from the exact contract"
            )
        return cls(**payload)

    @property
    def observation_hash(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))


@dataclass(frozen=True)
class MonitoringDecision:
    state: MonitoringState
    reasons: tuple[str, ...]
    mechanics_only: bool = True
    automatic_actions: tuple[str, ...] = ()

    @property
    def requires_abstention(self) -> bool:
        return self.state in {
            MonitoringState.MONITORING_PENDING,
            MonitoringState.MONITORING_PAUSED,
            MonitoringState.MONITORING_INVALID,
        }


def assess_monitoring(
    observation: MonitoringObservation, policy: MonitoringPolicy = MonitoringPolicy()
) -> MonitoringDecision:
    policy.validate()
    if type(observation.distinct_dates) is not int or observation.distinct_dates < 0:
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_DATE_COUNT",))
    if type(observation.eligible_predictions) is not int or observation.eligible_predictions < 0:
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_PREDICTION_COUNT",))
    numeric = (observation.maximum_psi, observation.maximum_missingness_delta, observation.coverage, observation.sealed_coverage_floor)
    if any(type(value) is not float or not math.isfinite(value) for value in numeric):
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_NUMERIC_EVIDENCE",))
    invalid_drift_metrics: list[str] = []
    if observation.maximum_psi < 0.0:
        invalid_drift_metrics.append("INVALID_PSI")
    if observation.maximum_missingness_delta < 0.0:
        invalid_drift_metrics.append("INVALID_MISSINGNESS_DELTA")
    if invalid_drift_metrics:
        return MonitoringDecision(
            MonitoringState.MONITORING_INVALID,
            tuple(invalid_drift_metrics),
        )
    if observation.matured_score_relative_degradation is not None and (
        type(observation.matured_score_relative_degradation) is not float
        or not math.isfinite(observation.matured_score_relative_degradation)
    ):
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_MATURED_SCORE",))
    if not (0.0 <= observation.coverage <= 1.0 and 0.0 < observation.sealed_coverage_floor <= 1.0):
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_COVERAGE",))
    if type(observation.source_or_bundle_stale) is not bool:
        return MonitoringDecision(MonitoringState.MONITORING_INVALID, ("INVALID_STALENESS_FLAG",))
    pause: list[str] = []
    warning: list[str] = []
    coverage_ratio = observation.coverage / observation.sealed_coverage_floor
    if observation.source_or_bundle_stale:
        pause.append("SOURCE_OR_BUNDLE_STALE")
    if observation.maximum_psi >= policy.psi_pause:
        pause.append("PSI_PAUSE")
    elif observation.maximum_psi >= policy.psi_warning:
        warning.append("PSI_WARNING")
    if observation.maximum_missingness_delta >= policy.missingness_pause_delta:
        pause.append("MISSINGNESS_PAUSE")
    elif observation.maximum_missingness_delta >= policy.missingness_warning_delta:
        warning.append("MISSINGNESS_WARNING")
    if coverage_ratio < policy.coverage_pause_ratio:
        pause.append("COVERAGE_PAUSE")
    elif coverage_ratio < policy.coverage_warning_ratio:
        warning.append("COVERAGE_WARNING")
    if observation.matured_score_relative_degradation is not None and observation.matured_score_relative_degradation >= policy.matured_score_pause_degradation:
        pause.append("MATURED_SCORE_PAUSE")
    if pause:
        return MonitoringDecision(MonitoringState.MONITORING_PAUSED, tuple(pause))
    if (
        observation.distinct_dates < policy.minimum_distinct_dates
        or observation.eligible_predictions < policy.minimum_predictions
    ):
        return MonitoringDecision(
            MonitoringState.MONITORING_PENDING,
            ("MINIMUM_WINDOW_PENDING",),
        )
    if warning:
        return MonitoringDecision(MonitoringState.MONITORING_WARNING, tuple(warning))
    return MonitoringDecision(MonitoringState.MONITORING_OK, ())


@dataclass(frozen=True)
class MonitoringRecord:
    schema_version: int
    bundle_id: str
    monitoring_policy_hash: str
    monitoring_reference_hash: str
    observation: Mapping[str, Any]
    observation_hash: str
    assessed_state: str
    effective_state: str
    reasons: tuple[str, ...]
    abstention_required: bool
    previous_record_id: str | None
    recovery_authorization: Mapping[str, Any] | None
    automatic_actions: tuple[str, ...]
    record_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "monitoring_policy_hash": self.monitoring_policy_hash,
            "monitoring_reference_hash": self.monitoring_reference_hash,
            "observation": dict(self.observation),
            "observation_hash": self.observation_hash,
            "assessed_state": self.assessed_state,
            "effective_state": self.effective_state,
            "reasons": list(self.reasons),
            "abstention_required": self.abstention_required,
            "previous_record_id": self.previous_record_id,
            "recovery_authorization": (
                None
                if self.recovery_authorization is None
                else dict(self.recovery_authorization)
            ),
            "automatic_actions": list(self.automatic_actions),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "record_id": self.record_id}

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise MonitoringContractError("monitoring record schema is invalid")
        for name in (
            "bundle_id",
            "monitoring_policy_hash",
            "monitoring_reference_hash",
            "observation_hash",
            "record_id",
        ):
            require_sha256(getattr(self, name), f"monitoring.{name}")
        if self.previous_record_id is not None:
            require_sha256(
                self.previous_record_id,
                "monitoring.previous_record_id",
            )
        observation = MonitoringObservation.from_dict(self.observation)
        if self.monitoring_policy_hash != MonitoringPolicy().policy_hash:
            raise MonitoringContractError(
                "monitoring policy hash differs from the frozen evaluated policy"
            )
        if observation.observation_hash != self.observation_hash:
            raise MonitoringContractError(
                "monitoring observation hash differs from its evidence"
            )
        assessed = assess_monitoring(observation)
        states = {item.value for item in MonitoringState}
        if self.assessed_state not in states or self.effective_state not in states:
            raise MonitoringContractError("monitoring record state is invalid")
        if (
            type(self.reasons) is not tuple
            or any(
                type(reason) is not str or not reason.isascii() or not reason
                for reason in self.reasons
            )
            or len(set(self.reasons)) != len(self.reasons)
        ):
            raise MonitoringContractError("monitoring reasons are invalid")
        if (
            self.assessed_state != assessed.state.value
            or not set(assessed.reasons).issubset(self.reasons)
            or set(self.reasons) - set(assessed.reasons)
            not in (set(), {"RECOVERY_REVIEW_REQUIRED"})
        ):
            raise MonitoringContractError(
                "monitoring record differs from the frozen assessment"
            )
        if self.effective_state != self.assessed_state and (
            self.effective_state != MonitoringState.MONITORING_PAUSED.value
            or "RECOVERY_REVIEW_REQUIRED" not in self.reasons
        ):
            raise MonitoringContractError(
                "monitoring effective state is not a fail-closed recovery hold"
            )
        expected_abstention = self.effective_state in {
            MonitoringState.MONITORING_PENDING.value,
            MonitoringState.MONITORING_PAUSED.value,
            MonitoringState.MONITORING_INVALID.value,
        }
        if (
            type(self.abstention_required) is not bool
            or self.abstention_required is not expected_abstention
        ):
            raise MonitoringContractError(
                "monitoring abstention differs from the effective state"
            )
        if self.automatic_actions != ():
            raise MonitoringContractError(
                "monitoring cannot retrain, retune, substitute sources, resume, or promote"
            )
        if self.recovery_authorization is not None:
            LocalIntegrityRecord.from_dict(
                self.recovery_authorization
            ).validate_content()
        if self.record_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise MonitoringContractError("monitoring record ID differs from its content")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "MonitoringRecord":
        if set(payload) != set(cls.__dataclass_fields__):
            raise MonitoringContractError(
                "monitoring record fields differ from the exact contract"
            )
        record = cls(
            schema_version=payload["schema_version"],
            bundle_id=payload["bundle_id"],
            monitoring_policy_hash=payload["monitoring_policy_hash"],
            monitoring_reference_hash=payload["monitoring_reference_hash"],
            observation=payload["observation"],
            observation_hash=payload["observation_hash"],
            assessed_state=payload["assessed_state"],
            effective_state=payload["effective_state"],
            reasons=tuple(payload["reasons"]),
            abstention_required=payload["abstention_required"],
            previous_record_id=payload["previous_record_id"],
            recovery_authorization=payload["recovery_authorization"],
            automatic_actions=tuple(payload["automatic_actions"]),
            record_id=payload["record_id"],
        )
        record.validate()
        return record


class ProspectiveMonitoringLedger:
    """Append monitoring decisions with explicit reviewed recovery from pauses."""

    def __init__(
        self,
        path: Path,
        anchor_root: Path,
        *,
        bundle_id: str,
        monitoring_policy_hash: str,
        monitoring_reference_hash: str,
        clock: TrustedClock,
        synthetic_history_clock_permit_ids: tuple[str, ...] = (),
        synthetic_history_permit: SyntheticOnlyPermit | None = None,
    ):
        for name, value in (
            ("bundle_id", bundle_id),
            ("monitoring_policy_hash", monitoring_policy_hash),
            ("monitoring_reference_hash", monitoring_reference_hash),
        ):
            require_sha256(value, f"monitoring.{name}")
        if monitoring_policy_hash != MonitoringPolicy().policy_hash:
            raise ContractError(
                "monitoring policy hash differs from the frozen evaluated policy"
            )
        self.bundle_id = bundle_id
        self.monitoring_policy_hash = monitoring_policy_hash
        self.monitoring_reference_hash = monitoring_reference_hash
        self._clock = require_trusted_clock(clock)
        self._ledger = HashChainLedger(
            Path(path),
            "prospective_monitoring_v1",
            clock=self._clock,
            unique_key="record_id",
            payload_validator=lambda payload: MonitoringRecord.from_dict(payload),
        )
        if synthetic_history_clock_permit_ids or synthetic_history_permit is not None:
            if (
                not synthetic_history_clock_permit_ids
                or synthetic_history_permit is None
            ):
                raise ContractError(
                    "synthetic monitoring history requires both its exact permit "
                    "census and fixture permit"
                )
            self._ledger.authorize_synthetic_history(
                synthetic_history_clock_permit_ids,
                permit=synthetic_history_permit,
            )
        self._anchors = LedgerAnchorStore(
            Path(anchor_root),
            self._ledger,
            clock=self._clock,
        )

    def _recovery_bindings(
        self,
        *,
        previous_record_id: str,
        observation_hash: str,
    ) -> dict[str, str]:
        return {
            "bundle_id": self.bundle_id,
            "monitoring_policy_hash": self.monitoring_policy_hash,
            "monitoring_reference_hash": self.monitoring_reference_hash,
            "previous_monitoring_record_id": previous_record_id,
            "observation_hash": observation_hash,
        }

    def append(
        self,
        observation: MonitoringObservation,
        *,
        previous_anchor: Path | None,
        recovery_authorization: LocalIntegrityRecord | None = None,
        policy: MonitoringPolicy = MonitoringPolicy(),
    ) -> Mapping[str, object]:
        if policy.policy_hash != self.monitoring_policy_hash:
            raise MonitoringContractError(
                "evaluated monitoring policy differs from the ledger binding"
            )
        decision = assess_monitoring(observation, policy)
        before = self._verified_history(previous_anchor)
        previous = (
            None
            if not before
            else MonitoringRecord.from_dict(before[-1]["payload"])
        )
        assessed_state = decision.state
        effective_state = decision.state
        reasons = list(decision.reasons)
        recovery_payload: Mapping[str, Any] | None = None
        recovery_required = (
            previous is not None
            and previous.effective_state
            in {
                MonitoringState.MONITORING_PAUSED.value,
                MonitoringState.MONITORING_INVALID.value,
            }
            and decision.state
            not in {
                MonitoringState.MONITORING_PAUSED,
                MonitoringState.MONITORING_INVALID,
            }
        )
        if recovery_required:
            if recovery_authorization is None:
                effective_state = MonitoringState.MONITORING_PAUSED
                reasons.append("RECOVERY_REVIEW_REQUIRED")
            else:
                recovery_authorization.validate(
                    expected_scope="AUTHORIZE_MONITORING_RECOVERY",
                    expected_subject_id=self.bundle_id,
                    required_bindings=self._recovery_bindings(
                        previous_record_id=previous.record_id,
                        observation_hash=observation.observation_hash,
                    ),
                    clock=self._clock,
                )
                recovery_payload = recovery_authorization.as_dict()
        elif recovery_authorization is not None:
            raise MonitoringContractError(
                "recovery authorization is accepted only for a paused or invalid predecessor"
            )
        unsigned = {
            "schema_version": 1,
            "bundle_id": self.bundle_id,
            "monitoring_policy_hash": self.monitoring_policy_hash,
            "monitoring_reference_hash": self.monitoring_reference_hash,
            "observation": observation.as_dict(),
            "observation_hash": observation.observation_hash,
            "assessed_state": assessed_state.value,
            "effective_state": effective_state.value,
            "reasons": list(dict.fromkeys(reasons)),
            "abstention_required": effective_state
            in {
                MonitoringState.MONITORING_PENDING,
                MonitoringState.MONITORING_PAUSED,
                MonitoringState.MONITORING_INVALID,
            },
            "previous_record_id": None if previous is None else previous.record_id,
            "recovery_authorization": recovery_payload,
            "automatic_actions": [],
        }
        record = MonitoringRecord(
            schema_version=1,
            bundle_id=self.bundle_id,
            monitoring_policy_hash=self.monitoring_policy_hash,
            monitoring_reference_hash=self.monitoring_reference_hash,
            observation=observation.as_dict(),
            observation_hash=observation.observation_hash,
            assessed_state=assessed_state.value,
            effective_state=effective_state.value,
            reasons=tuple(unsigned["reasons"]),
            abstention_required=unsigned["abstention_required"],
            previous_record_id=unsigned["previous_record_id"],
            recovery_authorization=recovery_payload,
            automatic_actions=(),
            record_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        record.validate()
        expected_head = before[-1]["record_hash"] if before else "0" * 64
        envelope = self._ledger.append(
            record.as_dict(),
            expected_record_count=len(before),
            expected_head_hash=expected_head,
        )
        history = self._ledger.read_verified()
        anchor = self._anchors.create(
            history,
            previous_anchor=previous_anchor,
            prior_record_count=len(before),
        )
        self._verify_transition_history(history)
        return {
            "record": record.as_dict(),
            "envelope": envelope,
            "anchor_path": str(anchor),
        }

    def verify(self, anchor: Path) -> tuple[MonitoringRecord, ...]:
        history = self._ledger.read_verified()
        self._anchors.verify(anchor, history)
        return self._verify_transition_history(history)

    def _verified_history(
        self,
        previous_anchor: Path | None,
    ) -> list[dict[str, Any]]:
        history = self._ledger.read_verified()
        if history:
            if previous_anchor is None:
                raise IntegrityError(
                    "monitoring append requires the retained prior anchor"
                )
            self._anchors.verify(previous_anchor, history)
        elif previous_anchor is not None:
            raise IntegrityError(
                "empty monitoring ledger cannot use a prior anchor"
            )
        self._verify_transition_history(history)
        return history

    def _verify_transition_history(
        self,
        history: list[dict[str, Any]],
    ) -> tuple[MonitoringRecord, ...]:
        records: list[MonitoringRecord] = []
        previous: MonitoringRecord | None = None
        for envelope in history:
            record = MonitoringRecord.from_dict(envelope["payload"])
            if (
                record.bundle_id != self.bundle_id
                or record.monitoring_policy_hash != self.monitoring_policy_hash
                or record.monitoring_reference_hash
                != self.monitoring_reference_hash
                or record.previous_record_id
                != (None if previous is None else previous.record_id)
            ):
                raise IntegrityError(
                    "monitoring record differs from its bundle, policy, reference, or predecessor"
                )
            resumed = (
                previous is not None
                and previous.effective_state
                in {
                    MonitoringState.MONITORING_PAUSED.value,
                    MonitoringState.MONITORING_INVALID.value,
                }
                and record.effective_state
                not in {
                    MonitoringState.MONITORING_PAUSED.value,
                    MonitoringState.MONITORING_INVALID.value,
                }
            )
            if resumed:
                if record.recovery_authorization is None:
                    raise IntegrityError(
                        "monitoring resumed without reviewed recovery"
                    )
                authorization = LocalIntegrityRecord.from_dict(
                    record.recovery_authorization
                )
                authorization.validate_at(
                    expected_scope="AUTHORIZE_MONITORING_RECOVERY",
                    expected_subject_id=self.bundle_id,
                    required_bindings=self._recovery_bindings(
                        previous_record_id=previous.record_id,
                        observation_hash=record.observation_hash,
                    ),
                    observed_at=parse_utc_z(
                        envelope["recorded_at"],
                        "monitoring.recorded_at",
                    ),
                    expected_clock_mode=envelope["time_authority"],
                    expected_synthetic_permit_id=(
                        envelope["synthetic_clock_permit_id"]
                    ),
                )
            elif record.recovery_authorization is not None:
                raise IntegrityError(
                    "monitoring record carries an inapplicable recovery authorization"
                )
            records.append(record)
            previous = record
        return tuple(records)
