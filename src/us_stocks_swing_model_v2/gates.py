from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from .common import canonical_json_bytes, iso_z, parse_utc_z, require_sha256, sha256_bytes
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError


REQUIRED_SLEEVES = ("stock_long", "stock_short", "etf_long", "etf_short")
EVALUATION_SCOPES = {"OUTER_SCREEN", "FINAL_HOLDOUT"}


class GateState(str, Enum):
    INVALID = "INVALID"
    INCONCLUSIVE_PIT_IDENTITY = "INCONCLUSIVE_PIT_IDENTITY"
    FAIL_NO_EDGE = "FAIL_NO_EDGE"
    FAIL_NOT_ECONOMIC = "FAIL_NOT_ECONOMIC"
    FAIL_MULTIPLICITY_OR_CONTROL = "FAIL_MULTIPLICITY_OR_CONTROL"
    INCONCLUSIVE_DATA_OR_POWER = "INCONCLUSIVE_DATA_OR_POWER"
    INCONCLUSIVE_EFFECT = "INCONCLUSIVE_EFFECT"
    INCONCLUSIVE_ROBUSTNESS = "INCONCLUSIVE_ROBUSTNESS"
    PASS_HISTORICAL_DISCOVERY_SCREEN = "PASS_HISTORICAL_DISCOVERY_SCREEN"


GATE_DECISION_ORDER = tuple(GateState)


@dataclass(frozen=True)
class SleeveMetric:
    effective_sessions: int
    after_cost_effect: float
    preregistered_economic_hurdle: float
    multiplicity_adjusted_confidence_lower: float
    multiplicity_adjusted_confidence_upper: float
    rw_adjusted_p: float
    rw_alpha: float
    dsr_probability: float
    minimum_dsr_probability: float
    pbo_applicability: str
    conservative_pbo: float | None
    maximum_conservative_pbo: float
    pbo_failure_threshold: float
    planned_power_pass: bool
    numerical_valid: bool
    lineage_valid: bool
    pit_identity_state: str
    negative_control_state: str
    robustness_state: str
    robustness_evidence_hash: str

    def validate(self) -> None:
        if (
            isinstance(self.effective_sessions, bool)
            or not isinstance(self.effective_sessions, int)
            or self.effective_sessions < 0
        ):
            raise ContractError("effective_sessions must be a nonnegative integer")
        if (
            isinstance(self.after_cost_effect, bool)
            or not isinstance(self.after_cost_effect, (int, float))
            or isinstance(self.preregistered_economic_hurdle, bool)
            or not isinstance(self.preregistered_economic_hurdle, (int, float))
            or isinstance(self.multiplicity_adjusted_confidence_lower, bool)
            or not isinstance(self.multiplicity_adjusted_confidence_lower, (int, float))
            or isinstance(self.multiplicity_adjusted_confidence_upper, bool)
            or not isinstance(self.multiplicity_adjusted_confidence_upper, (int, float))
            or not math.isfinite(self.after_cost_effect)
            or not math.isfinite(self.preregistered_economic_hurdle)
            or not math.isfinite(self.multiplicity_adjusted_confidence_lower)
            or not math.isfinite(self.multiplicity_adjusted_confidence_upper)
        ):
            raise ContractError("gate metrics must be finite")
        if (
            self.multiplicity_adjusted_confidence_lower
            > self.multiplicity_adjusted_confidence_upper
        ):
            raise ContractError("gate confidence interval is inverted")
        if not (
            self.multiplicity_adjusted_confidence_lower
            <= self.after_cost_effect
            <= self.multiplicity_adjusted_confidence_upper
        ):
            raise ContractError("gate confidence interval excludes its point effect")
        probabilities = (
            self.rw_adjusted_p,
            self.rw_alpha,
            self.dsr_probability,
            self.minimum_dsr_probability,
            self.maximum_conservative_pbo,
            self.pbo_failure_threshold,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
            for value in probabilities
        ):
            raise ContractError("gate statistical probabilities/alphas must be finite values in [0,1]")
        if self.pbo_applicability not in {
            "APPLICABLE_MULTIPLE_CONFIGURATIONS",
            "NOT_APPLICABLE_SINGLE_PREDECLARED_CONFIGURATION",
        }:
            raise ContractError("gate PBO applicability is invalid")
        if self.pbo_applicability == "APPLICABLE_MULTIPLE_CONFIGURATIONS":
            if (
                isinstance(self.conservative_pbo, bool)
                or not isinstance(self.conservative_pbo, (int, float))
                or not math.isfinite(self.conservative_pbo)
                or not 0 <= self.conservative_pbo <= 1
            ):
                raise ContractError("applicable PBO must be a finite value in [0,1]")
        elif self.conservative_pbo is not None:
            raise ContractError("single predeclared configuration must not fabricate a PBO value")
        if (
            not isinstance(self.planned_power_pass, bool)
            or not isinstance(self.numerical_valid, bool)
            or not isinstance(self.lineage_valid, bool)
        ):
            raise ContractError("gate power/numerical/lineage states must be explicit booleans")
        if self.negative_control_state not in {"PASS", "FAIL", "INCONCLUSIVE"}:
            raise ContractError("gate negative-control state is invalid")
        if self.pit_identity_state not in {
            "PASS",
            "INCONCLUSIVE_PIT_IDENTITY",
            "INVALID",
        }:
            raise ContractError("gate PIT identity state is invalid")
        if self.robustness_state not in {
            "PASS",
            "FAIL",
            "INCONCLUSIVE_ROBUSTNESS",
            "INVALID",
        }:
            raise ContractError("gate robustness state is invalid")
        require_sha256(self.robustness_evidence_hash, "gate.robustness_evidence_hash")

    def as_dict(self) -> dict[str, int | float | bool | str]:
        return {
            "effective_sessions": self.effective_sessions,
            "after_cost_effect": self.after_cost_effect,
            "preregistered_economic_hurdle": self.preregistered_economic_hurdle,
            "multiplicity_adjusted_confidence_lower": self.multiplicity_adjusted_confidence_lower,
            "multiplicity_adjusted_confidence_upper": self.multiplicity_adjusted_confidence_upper,
            "rw_adjusted_p": self.rw_adjusted_p,
            "rw_alpha": self.rw_alpha,
            "dsr_probability": self.dsr_probability,
            "minimum_dsr_probability": self.minimum_dsr_probability,
            "pbo_applicability": self.pbo_applicability,
            "conservative_pbo": self.conservative_pbo,
            "maximum_conservative_pbo": self.maximum_conservative_pbo,
            "pbo_failure_threshold": self.pbo_failure_threshold,
            "planned_power_pass": self.planned_power_pass,
            "numerical_valid": self.numerical_valid,
            "lineage_valid": self.lineage_valid,
            "pit_identity_state": self.pit_identity_state,
            "negative_control_state": self.negative_control_state,
            "robustness_state": self.robustness_state,
            "robustness_evidence_hash": self.robustness_evidence_hash,
        }


@dataclass(frozen=True)
class IndependentGatePolicy:
    minimum_effective_sessions: int
    sleeve_economic_hurdles: Mapping[str, float]
    minimum_confidence_lower: float
    rw_alpha: float
    minimum_dsr_probability: float
    maximum_conservative_pbo: float
    pbo_failure_threshold: float

    def validate(self) -> None:
        if (
            isinstance(self.minimum_effective_sessions, bool)
            or not isinstance(self.minimum_effective_sessions, int)
            or self.minimum_effective_sessions < 1
        ):
            raise ContractError("minimum effective sessions must be a positive integer")
        if (
            isinstance(self.minimum_confidence_lower, bool)
            or not isinstance(self.minimum_confidence_lower, (int, float))
            or not math.isfinite(self.minimum_confidence_lower)
        ):
            raise ContractError("gate thresholds must be finite")
        if set(self.sleeve_economic_hurdles) != set(REQUIRED_SLEEVES):
            raise ContractError("gate policy must preregister one hurdle for every independent sleeve")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in self.sleeve_economic_hurdles.values()
        ):
            raise ContractError("gate economic hurdles must be explicit finite numerics")
        for value in (
            self.rw_alpha,
            self.minimum_dsr_probability,
            self.maximum_conservative_pbo,
            self.pbo_failure_threshold,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ContractError("gate statistical thresholds must be finite values in [0,1]")
        if not 0 < self.rw_alpha < 1:
            raise ContractError("Romano-Wolf alpha must be strictly between zero and one")
        if not self.maximum_conservative_pbo < self.pbo_failure_threshold:
            raise ContractError("PBO pass ceiling must be below its failure threshold")

    def as_dict(self) -> dict[str, object]:
        self.validate()
        return {
            "minimum_effective_sessions": self.minimum_effective_sessions,
            "sleeve_economic_hurdles": dict(sorted(self.sleeve_economic_hurdles.items())),
            "minimum_confidence_lower": self.minimum_confidence_lower,
            "rw_alpha": self.rw_alpha,
            "minimum_dsr_probability": self.minimum_dsr_probability,
            "maximum_conservative_pbo": self.maximum_conservative_pbo,
            "pbo_failure_threshold": self.pbo_failure_threshold,
        }

    def evaluate(self, metrics: Mapping[str, SleeveMetric]) -> dict[str, GateState]:
        self.validate()
        if set(metrics) - set(REQUIRED_SLEEVES):
            raise ContractError("unknown sleeve in gate input")
        results: dict[str, GateState] = {}
        for sleeve in REQUIRED_SLEEVES:
            metric = metrics.get(sleeve)
            if metric is None:
                results[sleeve] = GateState.INCONCLUSIVE_DATA_OR_POWER
                continue
            metric.validate()
            expected_hurdle = self.sleeve_economic_hurdles[sleeve]
            if (
                metric.preregistered_economic_hurdle != expected_hurdle
                or metric.rw_alpha != self.rw_alpha
                or metric.minimum_dsr_probability != self.minimum_dsr_probability
                or metric.maximum_conservative_pbo != self.maximum_conservative_pbo
                or metric.pbo_failure_threshold != self.pbo_failure_threshold
            ):
                raise ContractError("gate metric thresholds differ from the preregistered sleeve policy")
            if (
                not metric.numerical_valid
                or not metric.lineage_valid
                or metric.pit_identity_state == "INVALID"
                or metric.robustness_state == "INVALID"
            ):
                results[sleeve] = GateState.INVALID
            elif metric.pit_identity_state == "INCONCLUSIVE_PIT_IDENTITY":
                results[sleeve] = GateState.INCONCLUSIVE_PIT_IDENTITY
            elif metric.multiplicity_adjusted_confidence_upper <= 0.0:
                results[sleeve] = GateState.FAIL_NO_EDGE
            elif metric.multiplicity_adjusted_confidence_upper <= expected_hurdle:
                results[sleeve] = GateState.FAIL_NOT_ECONOMIC
            elif (
                metric.rw_adjusted_p > self.rw_alpha
                or metric.dsr_probability < self.minimum_dsr_probability
                or (
                    metric.pbo_applicability
                    == "APPLICABLE_MULTIPLE_CONFIGURATIONS"
                    and metric.conservative_pbo is not None
                    and metric.conservative_pbo >= self.pbo_failure_threshold
                )
                or metric.negative_control_state == "FAIL"
                or metric.robustness_state == "FAIL"
            ):
                results[sleeve] = GateState.FAIL_MULTIPLICITY_OR_CONTROL
            elif (
                metric.effective_sessions < self.minimum_effective_sessions
                or not metric.planned_power_pass
                or metric.negative_control_state == "INCONCLUSIVE"
                or (
                    metric.pbo_applicability == "APPLICABLE_MULTIPLE_CONFIGURATIONS"
                    and metric.conservative_pbo is not None
                    and metric.conservative_pbo >= self.maximum_conservative_pbo
                    and metric.conservative_pbo < self.pbo_failure_threshold
                )
            ):
                results[sleeve] = GateState.INCONCLUSIVE_DATA_OR_POWER
            elif (
                metric.multiplicity_adjusted_confidence_lower <= expected_hurdle
                or metric.multiplicity_adjusted_confidence_lower
                < self.minimum_confidence_lower
            ):
                results[sleeve] = GateState.INCONCLUSIVE_EFFECT
            elif metric.robustness_state == "INCONCLUSIVE_ROBUSTNESS":
                results[sleeve] = GateState.INCONCLUSIVE_ROBUSTNESS
            else:
                results[sleeve] = GateState.PASS_HISTORICAL_DISCOVERY_SCREEN
        return results

    def aggregate(self, metrics: Mapping[str, SleeveMetric]) -> GateState:
        results = self.evaluate(metrics)
        return min(results.values(), key=GATE_DECISION_ORDER.index)


@dataclass(frozen=True)
class GateReceipt:
    schema_version: int
    trial_registry_binding_id: str
    trial_id: str
    evaluation_permit_id: str
    permit_payload_hash: str
    registration_hash: str
    evaluation_scope: str
    evaluation_input_hash: str
    evaluator_code_hash: str
    evaluator_closure_hash: str
    census_anchor_id: str
    trial_family_anchor_id: str
    governance_contract_hash: str
    release_bindings_hash: str
    holdout_receipt_id: str
    authorization_receipt_id: str
    permit_issued_at: str
    primary_gate_id: str
    policy_hash: str
    robustness_policy_hash: str
    robustness_evidence_hash: str
    metrics_hash: str
    state: str
    evaluated_at: str
    time_authority: str
    synthetic_clock_permit_id: str | None
    receipt_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "trial_registry_binding_id": self.trial_registry_binding_id,
            "trial_id": self.trial_id,
            "evaluation_permit_id": self.evaluation_permit_id,
            "permit_payload_hash": self.permit_payload_hash,
            "registration_hash": self.registration_hash,
            "evaluation_scope": self.evaluation_scope,
            "evaluation_input_hash": self.evaluation_input_hash,
            "evaluator_code_hash": self.evaluator_code_hash,
            "evaluator_closure_hash": self.evaluator_closure_hash,
            "census_anchor_id": self.census_anchor_id,
            "trial_family_anchor_id": self.trial_family_anchor_id,
            "governance_contract_hash": self.governance_contract_hash,
            "release_bindings_hash": self.release_bindings_hash,
            "holdout_receipt_id": self.holdout_receipt_id,
            "authorization_receipt_id": self.authorization_receipt_id,
            "permit_issued_at": self.permit_issued_at,
            "primary_gate_id": self.primary_gate_id,
            "policy_hash": self.policy_hash,
            "robustness_policy_hash": self.robustness_policy_hash,
            "robustness_evidence_hash": self.robustness_evidence_hash,
            "metrics_hash": self.metrics_hash,
            "state": self.state,
            "evaluated_at": self.evaluated_at,
            "time_authority": self.time_authority,
            "synthetic_clock_permit_id": self.synthetic_clock_permit_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "receipt_id": self.receipt_id}

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 3
            or self.state not in {item.value for item in GateState}
            or type(self.evaluation_scope) is not str
            or self.evaluation_scope not in EVALUATION_SCOPES
        ):
            raise ContractError("gate receipt schema/state/scope is invalid")
        evaluated = parse_utc_z(self.evaluated_at, "gate.evaluated_at")
        issued = parse_utc_z(self.permit_issued_at, "gate.permit_issued_at")
        if evaluated <= issued:
            raise ContractError("gate evaluation must follow the issued permit")
        if self.time_authority == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_clock_permit_id is not None:
                raise ContractError("production gate cannot carry a synthetic clock permit")
        elif self.time_authority == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            if self.synthetic_clock_permit_id is None:
                raise ContractError("synthetic gate requires its mechanics-only clock permit")
            require_sha256(self.synthetic_clock_permit_id, "gate.synthetic_clock_permit_id")
        else:
            raise ContractError("gate time authority is invalid")
        for name in (
            "trial_registry_binding_id",
            "trial_id",
            "evaluation_permit_id",
            "permit_payload_hash",
            "registration_hash",
            "evaluation_input_hash",
            "evaluator_code_hash",
            "evaluator_closure_hash",
            "census_anchor_id",
            "trial_family_anchor_id",
            "governance_contract_hash",
            "release_bindings_hash",
            "holdout_receipt_id",
            "authorization_receipt_id",
            "primary_gate_id",
            "policy_hash",
            "robustness_policy_hash",
            "robustness_evidence_hash",
            "metrics_hash",
            "receipt_id",
        ):
            require_sha256(getattr(self, name), f"gate.{name}")
        if self.policy_hash != self.primary_gate_id:
            raise ContractError("gate policy does not match the permit primary gate")
        if self.receipt_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("gate receipt ID differs from its content")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GateReceipt":
        if set(payload) != set(cls.__dataclass_fields__):
            raise ContractError("gate receipt fields differ from the exact contract")
        receipt = cls(**payload)
        receipt.validate()
        return receipt


def _build_gate_receipt_from_issued_permit(
    *,
    permit: Any,
    issued_permit: Mapping[str, Any],
    policy: IndependentGatePolicy,
    metrics: Mapping[str, SleeveMetric],
    clock: TrustedClock,
) -> GateReceipt:
    permit.validate()
    if dict(issued_permit) != permit.as_dict():
        raise ContractError("gate requires the exact registry-issued permit payload")
    trusted_clock = require_trusted_clock(clock)
    state = policy.aggregate(metrics)
    unsigned = {
        "schema_version": 3,
        "trial_registry_binding_id": permit.trial_registry_binding_id,
        "trial_id": permit.trial_id,
        "evaluation_permit_id": permit.permit_id,
        "permit_payload_hash": sha256_bytes(canonical_json_bytes(permit.as_dict())),
        "registration_hash": permit.registration_hash,
        "evaluation_scope": permit.evaluation_scope,
        "evaluation_input_hash": permit.evaluation_input_hash,
        "evaluator_code_hash": permit.evaluator_code_hash,
        "evaluator_closure_hash": permit.evaluator_closure_hash,
        "census_anchor_id": permit.census_anchor_id,
        "trial_family_anchor_id": permit.trial_family_anchor_id,
        "governance_contract_hash": permit.governance_contract_hash,
        "release_bindings_hash": permit.release_bindings_hash,
        "holdout_receipt_id": permit.holdout_receipt_id,
        "authorization_receipt_id": permit.authorization_receipt_id,
        "permit_issued_at": permit.issued_at,
        "primary_gate_id": permit.primary_gate_id,
        "policy_hash": sha256_bytes(canonical_json_bytes(policy.as_dict())),
        "robustness_policy_hash": permit.robustness_policy_id,
        "robustness_evidence_hash": sha256_bytes(
            canonical_json_bytes(
                {
                    name: {
                        "state": metric.robustness_state,
                        "evidence_hash": metric.robustness_evidence_hash,
                    }
                    for name, metric in sorted(metrics.items())
                }
            )
        ),
        "metrics_hash": sha256_bytes(
            canonical_json_bytes({name: metric.as_dict() for name, metric in sorted(metrics.items())})
        ),
        "state": state.value,
        "evaluated_at": iso_z(trusted_clock.now()),
        "time_authority": trusted_clock.mode,
        "synthetic_clock_permit_id": trusted_clock.synthetic_permit_id,
    }
    if unsigned["policy_hash"] != permit.primary_gate_id:
        raise ContractError("gate policy differs from the predeclared trial policy")
    if unsigned["robustness_policy_hash"] != permit.robustness_policy_id:
        raise ContractError("gate robustness policy differs from the predeclared trial policy")
    receipt = GateReceipt(
        **unsigned,
        receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    receipt.validate()
    return receipt


def build_gate_receipt(**_: object) -> GateReceipt:
    """Reject unregistered gate construction; use TrialRegistry.build_gate_receipt."""

    raise ContractError("gate receipts require an exact registry-issued permit")
