"""Outcome-free feature registry for causal infrastructure work."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .common import canonical_json_bytes, require_sha256, sha256_bytes
from .errors import ContractError, IntegrityError
from .prospective_price_features import FEATURE_NAMES


REGISTRY_PHASE = "HISTORICAL_RESEARCH_FOUNDATION_OUTCOME_FIREWALL"
FEATURE_STATUSES = {
    "IMPLEMENTED_CAUSAL_INFRASTRUCTURE_ONLY",
    "REGISTERED_NOT_IMPLEMENTED",
}
DIAGNOSTIC_PERMISSIONS = (
    "COMPUTATIONAL_REPRODUCIBILITY",
    "COVERAGE",
    "CROSS_FEATURE_CORRELATION",
    "CROSS_SECTIONAL_CONCENTRATION",
    "DISTRIBUTION_SHAPE",
    "ESTIMATED_FEATURE_TURNOVER",
    "MISSINGNESS",
    "OUTLIER_INCIDENCE",
    "TIME_STABILITY",
)
FORBIDDEN_FIELD_TOKENS = {
    "alpha",
    "forward_return",
    "future_return",
    "label",
    "outcome",
    "pnl",
    "realized_return",
    "sharpe",
    "strategy_return",
    "target",
}
FORBIDDEN_FIELD_PREFIXES = (
    "forward_",
    "future_",
    "label_",
    "outcome_",
    "pnl_",
    "realized_",
    "target_",
)
FEATURE_KEYS = {
    "feature_id",
    "name",
    "family",
    "hypothesis",
    "required_source_fields",
    "lookback_sessions",
    "minimum_history_sessions",
    "signal_cutoff",
    "required_lag_sessions",
    "usable_time_rule",
    "missing_data_policy",
    "outlier_policy",
    "cross_sectional_normalization",
    "universe_dependency",
    "corporate_action_treatment",
    "update_frequency",
    "known_leakage_risks",
    "implementation_ref",
    "version",
    "status",
}


def _canonical_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ContractError(f"{field} must be nonempty canonical text")
    return value


def _text_tuple(value: object, field: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if type(value) is not list or (nonempty and not value):
        raise ContractError(f"{field} must be a canonical JSON list")
    result = tuple(_canonical_text(item, field) for item in value)
    if result != tuple(sorted(set(result))):
        raise ContractError(f"{field} must be sorted and unique")
    return result


@dataclass(frozen=True)
class FeatureDefinition:
    feature_id: str
    name: str
    family: str
    hypothesis: str
    required_source_fields: tuple[str, ...]
    lookback_sessions: int
    minimum_history_sessions: int
    signal_cutoff: str
    required_lag_sessions: int
    usable_time_rule: str
    missing_data_policy: str
    outlier_policy: str
    cross_sectional_normalization: str
    universe_dependency: str
    corporate_action_treatment: str
    update_frequency: str
    known_leakage_risks: tuple[str, ...]
    implementation_ref: str
    version: str
    status: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FeatureDefinition":
        if not isinstance(payload, Mapping) or set(payload) != FEATURE_KEYS:
            raise ContractError("feature definition fields differ from the exact registry contract")
        value = cls(
            feature_id=_canonical_text(payload["feature_id"], "feature_id"),
            name=_canonical_text(payload["name"], "feature name"),
            family=_canonical_text(payload["family"], "feature family"),
            hypothesis=_canonical_text(payload["hypothesis"], "feature hypothesis"),
            required_source_fields=_text_tuple(
                payload["required_source_fields"], "required_source_fields"
            ),
            lookback_sessions=payload["lookback_sessions"],
            minimum_history_sessions=payload["minimum_history_sessions"],
            signal_cutoff=_canonical_text(payload["signal_cutoff"], "signal_cutoff"),
            required_lag_sessions=payload["required_lag_sessions"],
            usable_time_rule=_canonical_text(
                payload["usable_time_rule"], "usable_time_rule"
            ),
            missing_data_policy=_canonical_text(
                payload["missing_data_policy"], "missing_data_policy"
            ),
            outlier_policy=_canonical_text(payload["outlier_policy"], "outlier_policy"),
            cross_sectional_normalization=_canonical_text(
                payload["cross_sectional_normalization"],
                "cross_sectional_normalization",
            ),
            universe_dependency=_canonical_text(
                payload["universe_dependency"], "universe_dependency"
            ),
            corporate_action_treatment=_canonical_text(
                payload["corporate_action_treatment"],
                "corporate_action_treatment",
            ),
            update_frequency=_canonical_text(
                payload["update_frequency"], "update_frequency"
            ),
            known_leakage_risks=_text_tuple(
                payload["known_leakage_risks"], "known_leakage_risks"
            ),
            implementation_ref=_canonical_text(
                payload["implementation_ref"], "implementation_ref"
            ),
            version=_canonical_text(payload["version"], "feature version"),
            status=_canonical_text(payload["status"], "feature status"),
        )
        value.validate()
        return value

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_id": self.feature_id,
            "name": self.name,
            "family": self.family,
            "hypothesis": self.hypothesis,
            "required_source_fields": list(self.required_source_fields),
            "lookback_sessions": self.lookback_sessions,
            "minimum_history_sessions": self.minimum_history_sessions,
            "signal_cutoff": self.signal_cutoff,
            "required_lag_sessions": self.required_lag_sessions,
            "usable_time_rule": self.usable_time_rule,
            "missing_data_policy": self.missing_data_policy,
            "outlier_policy": self.outlier_policy,
            "cross_sectional_normalization": self.cross_sectional_normalization,
            "universe_dependency": self.universe_dependency,
            "corporate_action_treatment": self.corporate_action_treatment,
            "update_frequency": self.update_frequency,
            "known_leakage_risks": list(self.known_leakage_risks),
            "implementation_ref": self.implementation_ref,
            "version": self.version,
            "status": self.status,
        }

    def validate(self) -> None:
        for field in (
            "feature_id",
            "name",
            "family",
            "hypothesis",
            "signal_cutoff",
            "usable_time_rule",
            "missing_data_policy",
            "outlier_policy",
            "cross_sectional_normalization",
            "universe_dependency",
            "corporate_action_treatment",
            "update_frequency",
            "implementation_ref",
            "version",
            "status",
        ):
            _canonical_text(getattr(self, field), field)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in (
                self.lookback_sessions,
                self.minimum_history_sessions,
                self.required_lag_sessions,
            )
        ):
            raise ContractError("feature session counts must be nonnegative exact integers")
        if self.minimum_history_sessions < self.lookback_sessions:
            raise ContractError("minimum history cannot be shorter than the lookback")
        if not self.required_source_fields or not self.known_leakage_risks:
            raise ContractError("feature source fields and leakage risks must be explicit")
        if self.required_source_fields != tuple(sorted(set(self.required_source_fields))):
            raise ContractError("feature source fields must be sorted and unique")
        if self.known_leakage_risks != tuple(sorted(set(self.known_leakage_risks))):
            raise ContractError("feature leakage risks must be sorted and unique")
        tokens = {value.casefold() for value in self.required_source_fields}
        if tokens & FORBIDDEN_FIELD_TOKENS or any(
            value.startswith(FORBIDDEN_FIELD_PREFIXES) for value in tokens
        ):
            raise ContractError("feature registry cannot require outcomes or labels")
        if self.status not in FEATURE_STATUSES:
            raise ContractError("feature registry status is invalid")


@dataclass(frozen=True)
class FeatureRegistry:
    schema_version: int
    project: str
    phase: str
    diagnostic_permissions: tuple[str, ...]
    real_outcome_access: bool
    performance_based_ranking: bool
    features: tuple[FeatureDefinition, ...]
    registry_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "phase": self.phase,
            "diagnostic_permissions": list(self.diagnostic_permissions),
            "real_outcome_access": self.real_outcome_access,
            "performance_based_ranking": self.performance_based_ranking,
            "features": [feature.as_dict() for feature in self.features],
        }

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.project != "US_stocks_swing_model_v2"
            or self.phase != REGISTRY_PHASE
        ):
            raise ContractError("feature registry identity differs")
        if self.diagnostic_permissions != DIAGNOSTIC_PERMISSIONS:
            raise ContractError("feature diagnostic permissions differ")
        if self.real_outcome_access is not False or self.performance_based_ranking is not False:
            raise ContractError("feature registry cannot enable outcome access or ranking")
        if not self.features:
            raise ContractError("feature registry must contain definitions")
        for feature in self.features:
            feature.validate()
        if tuple(feature.name for feature in self.features) != FEATURE_NAMES:
            raise ContractError("implemented registry names differ from the causal feature module")
        if len({feature.feature_id for feature in self.features}) != len(self.features):
            raise ContractError("feature IDs must be unique")
        require_sha256(self.registry_id, "feature_registry.registry_id")
        if self.registry_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("feature registry ID differs from its content")


def load_feature_registry(path: Path) -> FeatureRegistry:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("feature registry is missing or invalid JSON") from exc
    expected = {
        "schema_version",
        "project",
        "phase",
        "diagnostic_permissions",
        "real_outcome_access",
        "performance_based_ranking",
        "features",
        "registry_id",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise ContractError("feature registry fields differ from the exact contract")
    if type(payload["features"]) is not list:
        raise ContractError("feature registry definitions must be a list")
    value = FeatureRegistry(
        schema_version=payload["schema_version"],
        project=payload["project"],
        phase=payload["phase"],
        diagnostic_permissions=_text_tuple(
            payload["diagnostic_permissions"], "diagnostic_permissions"
        ),
        real_outcome_access=payload["real_outcome_access"],
        performance_based_ranking=payload["performance_based_ranking"],
        features=tuple(FeatureDefinition.from_dict(item) for item in payload["features"]),
        registry_id=payload["registry_id"],
    )
    value.validate()
    return value
