"""Dependency-neutral identity for the frozen prospective-monitoring policy."""

from __future__ import annotations

from .common import canonical_json_bytes, sha256_bytes


MONITORING_POLICY_VERSION = "1.1.0-pending-before-warning-non-authorizing"
MONITORING_STATE_PRECEDENCE = (
    "MONITORING_INVALID",
    "MONITORING_PAUSED",
    "MONITORING_PENDING",
    "MONITORING_WARNING",
    "MONITORING_OK",
)


FROZEN_MONITORING_POLICY: dict[str, object] = {
    "policy_version": MONITORING_POLICY_VERSION,
    "minimum_distinct_dates": 30,
    "minimum_predictions": 500,
    "psi_warning": 0.10,
    "psi_pause": 0.25,
    "missingness_warning_delta": 0.05,
    "missingness_pause_delta": 0.10,
    "coverage_warning_ratio": 0.95,
    "coverage_pause_ratio": 0.90,
    "matured_score_pause_degradation": 0.10,
    "state_precedence": list(MONITORING_STATE_PRECEDENCE),
}


def frozen_monitoring_policy_hash() -> str:
    return sha256_bytes(canonical_json_bytes(FROZEN_MONITORING_POLICY))
