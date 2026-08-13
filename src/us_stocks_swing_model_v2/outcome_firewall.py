"""Default-deny exploratory access boundary for outcomes and holdouts."""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .common import (
    canonical_json_bytes,
    iso_z,
    require_aware_utc,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError, IntegrityError


ALLOWED_FOUNDATION_NAMESPACES = (
    "corporate_actions",
    "features",
    "identity",
    "observations",
    "sessions",
    "synthetic",
    "universe",
)
DENIED_DATASET_NAMES = {
    "alpaca_discovery_joined_trial_input",
    "alpaca_discovery_joined_trial_inputs",
    "alpaca_discovery_proxy_outcomes",
    "outcomes",
    "real_outcomes",
}
DENIED_PATH_TOKENS = {
    "alpha",
    "backtest",
    "evaluation",
    "future",
    "holdout",
    "label",
    "outcome",
    "performance",
    "pnl",
    "profit",
    "realized",
    "sharpe",
    "strategy",
    "target",
}
DENIED_IMPORTS = {
    "outcomes",
    "research.builder",
    "research.evaluator",
    "research.executor",
    "trials",
}
FORBIDDEN_PAYLOAD_FIELDS = {
    "alpha",
    "backtest",
    "cagr",
    "drawdown",
    "final_holdout",
    "forward_return",
    "future_price_path",
    "future_return",
    "hit_rate",
    "label",
    "outcome",
    "performance",
    "pnl",
    "realized_return",
    "sharpe",
    "strategy_return",
    "target",
}
FORBIDDEN_PAYLOAD_PREFIXES = (
    "forward_",
    "future_",
    "label_",
    "outcome_",
    "pnl_",
    "realized_",
    "target_",
)
SYNTHETIC_OUTCOME_SCOPE = "SYNTHETIC_FOUNDATION_REHEARSAL"


@dataclass(frozen=True)
class FoundationPhasePolicy:
    schema_version: int
    phase: str
    allowed_namespaces: tuple[str, ...]
    real_outcome_access: bool
    real_label_access: bool
    holdout_access: bool
    training_on_real_outcomes: bool
    evaluation_on_real_outcomes: bool
    backtesting: bool
    broker_connectivity: bool
    trading: bool
    policy_id: str

    @classmethod
    def default(cls) -> "FoundationPhasePolicy":
        unsigned = {
            "schema_version": 1,
            "phase": "HISTORICAL_RESEARCH_FOUNDATION_OUTCOME_FIREWALL",
            "allowed_namespaces": list(ALLOWED_FOUNDATION_NAMESPACES),
            "real_outcome_access": False,
            "real_label_access": False,
            "holdout_access": False,
            "training_on_real_outcomes": False,
            "evaluation_on_real_outcomes": False,
            "backtesting": False,
            "broker_connectivity": False,
            "trading": False,
        }
        value = cls(
            schema_version=1,
            phase="HISTORICAL_RESEARCH_FOUNDATION_OUTCOME_FIREWALL",
            allowed_namespaces=ALLOWED_FOUNDATION_NAMESPACES,
            real_outcome_access=False,
            real_label_access=False,
            holdout_access=False,
            training_on_real_outcomes=False,
            evaluation_on_real_outcomes=False,
            backtesting=False,
            broker_connectivity=False,
            trading=False,
            policy_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "allowed_namespaces": list(self.allowed_namespaces),
            "real_outcome_access": self.real_outcome_access,
            "real_label_access": self.real_label_access,
            "holdout_access": self.holdout_access,
            "training_on_real_outcomes": self.training_on_real_outcomes,
            "evaluation_on_real_outcomes": self.evaluation_on_real_outcomes,
            "backtesting": self.backtesting,
            "broker_connectivity": self.broker_connectivity,
            "trading": self.trading,
        }

    def validate(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.phase
            != "HISTORICAL_RESEARCH_FOUNDATION_OUTCOME_FIREWALL"
            or self.allowed_namespaces != ALLOWED_FOUNDATION_NAMESPACES
        ):
            raise ContractError("foundation outcome-firewall policy identity differs")
        denied_flags = (
            self.real_outcome_access,
            self.real_label_access,
            self.holdout_access,
            self.training_on_real_outcomes,
            self.evaluation_on_real_outcomes,
            self.backtesting,
            self.broker_connectivity,
            self.trading,
        )
        if any(type(value) is not bool or value for value in denied_flags):
            raise ContractError("foundation policy cannot authorize prohibited activity")
        require_sha256(self.policy_id, "foundation_policy.policy_id")
        if self.policy_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("foundation policy ID differs from its content")


def load_foundation_phase_policy(path: Path) -> FoundationPhasePolicy:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("foundation outcome-firewall policy is missing or invalid") from exc
    expected = {
        "schema_version",
        "phase",
        "allowed_namespaces",
        "real_outcome_access",
        "real_label_access",
        "holdout_access",
        "training_on_real_outcomes",
        "evaluation_on_real_outcomes",
        "backtesting",
        "broker_connectivity",
        "trading",
        "policy_id",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise ContractError("foundation policy fields differ from the exact contract")
    if type(payload["allowed_namespaces"]) is not list:
        raise ContractError("foundation policy namespaces must be a list")
    policy = FoundationPhasePolicy(
        schema_version=payload["schema_version"],
        phase=payload["phase"],
        allowed_namespaces=tuple(payload["allowed_namespaces"]),
        real_outcome_access=payload["real_outcome_access"],
        real_label_access=payload["real_label_access"],
        holdout_access=payload["holdout_access"],
        training_on_real_outcomes=payload["training_on_real_outcomes"],
        evaluation_on_real_outcomes=payload["evaluation_on_real_outcomes"],
        backtesting=payload["backtesting"],
        broker_connectivity=payload["broker_connectivity"],
        trading=payload["trading"],
        policy_id=payload["policy_id"],
    )
    policy.validate()
    return policy


@dataclass(frozen=True)
class FoundationAccessAuditEvent:
    policy_id: str
    requested_path: str
    purpose: str
    requested_at: datetime
    decision: str
    reason: str
    synthetic_permit_id: str | None
    event_id: str

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        requested_path: str,
        purpose: str,
        requested_at: datetime,
        decision: str,
        reason: str,
        synthetic_permit_id: str | None,
    ) -> "FoundationAccessAuditEvent":
        unsigned = {
            "policy_id": policy_id,
            "requested_path": requested_path,
            "purpose": purpose,
            "requested_at": iso_z(requested_at),
            "decision": decision,
            "reason": reason,
            "synthetic_permit_id": synthetic_permit_id,
        }
        value = cls(
            policy_id=policy_id,
            requested_path=requested_path,
            purpose=purpose,
            requested_at=requested_at,
            decision=decision,
            reason=reason,
            synthetic_permit_id=synthetic_permit_id,
            event_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        value.validate()
        return value

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "requested_path": self.requested_path,
            "purpose": self.purpose,
            "requested_at": iso_z(self.requested_at),
            "decision": self.decision,
            "reason": self.reason,
            "synthetic_permit_id": self.synthetic_permit_id,
        }

    def validate(self) -> None:
        require_sha256(self.policy_id, "foundation audit policy_id")
        for name in ("requested_path", "purpose", "reason"):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ContractError(f"foundation audit {name} must be canonical text")
        require_aware_utc(self.requested_at, "foundation audit requested_at")
        if self.decision not in {"ALLOW_FOUNDATION_INPUT", "ALLOW_SYNTHETIC_OUTCOME", "DENY"}:
            raise ContractError("foundation audit decision is invalid")
        if self.synthetic_permit_id is not None:
            require_sha256(
                self.synthetic_permit_id,
                "foundation audit synthetic_permit_id",
            )
        if self.decision == "ALLOW_SYNTHETIC_OUTCOME" and self.synthetic_permit_id is None:
            raise ContractError("synthetic outcome audit requires a permit ID")
        require_sha256(self.event_id, "foundation audit event_id")
        if self.event_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise IntegrityError("foundation audit event ID differs from its content")


class OutcomeAccessDenied(ContractError):
    def __init__(self, event: FoundationAccessAuditEvent) -> None:
        self.event = event
        super().__init__(f"outcome firewall denied access: {event.reason}; event={event.event_id}")


def _path_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", value.casefold())
        if token
    }


def _denied_foundation_path(relative: str) -> str | None:
    path = safe_relative_path(relative)
    for part in path.parts:
        lowered = part.casefold()
        stem = lowered.rsplit(".", 1)[0]
        if lowered in DENIED_DATASET_NAMES or stem in DENIED_DATASET_NAMES:
            return f"denied dataset component {part}"
        tokens = _path_tokens(stem)
        matched = sorted(tokens & DENIED_PATH_TOKENS)
        if matched:
            return f"denied outcome/evaluation token {matched[0]}"
    return None


def validate_foundation_payload(payload: Mapping[str, object]) -> None:
    if not isinstance(payload, Mapping):
        raise ContractError("foundation payload must be a mapping")

    def visit(value: object, path: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if type(key) is not str or not key:
                    raise ContractError("foundation payload keys must be nonempty strings")
                lowered = key.casefold()
                if lowered in FORBIDDEN_PAYLOAD_FIELDS or any(
                    lowered.startswith(prefix)
                    for prefix in FORBIDDEN_PAYLOAD_PREFIXES
                ):
                    dotted = ".".join((*path, key))
                    raise ContractError(
                        f"foundation payload contains prohibited outcome field: {dotted}"
                    )
                visit(child, (*path, key))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, (*path, str(index)))
        elif value is not None and type(value) not in {str, int, float, bool}:
            raise ContractError("foundation payload contains a non-JSON value")

    visit(payload, ())


def exploratory_import_violations(source: str) -> tuple[str, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise ContractError("exploratory source is not valid Python") from exc
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        for name in names:
            normalized = name.removeprefix("us_stocks_swing_model_v2.")
            if any(
                normalized == denied or normalized.startswith(f"{denied}.")
                for denied in DENIED_IMPORTS
            ):
                found.add(normalized)
    return tuple(sorted(found))


class FoundationDataGateway:
    """Resolve only approved input paths and retain content-addressed audit events."""

    def __init__(
        self,
        root: Path,
        *,
        policy: FoundationPhasePolicy | None = None,
    ) -> None:
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ContractError("foundation gateway root must be absolute")
        require_contained_path(self.root, self.root)
        self.policy = policy or FoundationPhasePolicy.default()
        self.policy.validate()
        self._events: list[FoundationAccessAuditEvent] = []

    @property
    def audit_events(self) -> tuple[FoundationAccessAuditEvent, ...]:
        return tuple(self._events)

    def _record(
        self,
        *,
        relative: str,
        purpose: str,
        requested_at: datetime,
        decision: str,
        reason: str,
        synthetic_permit_id: str | None = None,
    ) -> FoundationAccessAuditEvent:
        event = FoundationAccessAuditEvent.create(
            policy_id=self.policy.policy_id,
            requested_path=relative,
            purpose=purpose,
            requested_at=requested_at,
            decision=decision,
            reason=reason,
            synthetic_permit_id=synthetic_permit_id,
        )
        self._events.append(event)
        return event

    def resolve_foundation_input(
        self,
        relative: str,
        *,
        purpose: str,
        requested_at: datetime,
    ) -> Path:
        requested = require_aware_utc(requested_at, "requested_at")
        reason = _denied_foundation_path(relative)
        if reason is not None:
            raise OutcomeAccessDenied(
                self._record(
                    relative=relative,
                    purpose=purpose,
                    requested_at=requested,
                    decision="DENY",
                    reason=reason,
                )
            )
        safe = safe_relative_path(relative)
        if safe.parts[0] not in self.policy.allowed_namespaces:
            raise OutcomeAccessDenied(
                self._record(
                    relative=relative,
                    purpose=purpose,
                    requested_at=requested,
                    decision="DENY",
                    reason="namespace is outside the foundation allowlist",
                )
            )
        candidate = self.root.joinpath(*safe.parts)
        resolved = require_contained_path(candidate, self.root)
        self._record(
            relative=relative,
            purpose=purpose,
            requested_at=requested,
            decision="ALLOW_FOUNDATION_INPUT",
            reason="path is contained in an approved outcome-free namespace",
        )
        return resolved

    def resolve_synthetic_outcome_fixture(
        self,
        relative: str,
        *,
        permit: SyntheticOnlyPermit,
        requested_at: datetime,
    ) -> Path:
        requested = require_aware_utc(requested_at, "requested_at")
        verified = require_synthetic_permit(permit, scope=SYNTHETIC_OUTCOME_SCOPE)
        safe = safe_relative_path(relative)
        if safe.parts[:2] != ("synthetic", "outcomes"):
            raise OutcomeAccessDenied(
                self._record(
                    relative=relative,
                    purpose="SYNTHETIC_OUTCOME_FIXTURE",
                    requested_at=requested,
                    decision="DENY",
                    reason="synthetic outcome path must use synthetic/outcomes",
                    synthetic_permit_id=verified.permit_id,
                )
            )
        candidate = self.root.joinpath(*safe.parts)
        resolved = require_contained_path(candidate, self.root)
        self._record(
            relative=relative,
            purpose="SYNTHETIC_OUTCOME_FIXTURE",
            requested_at=requested,
            decision="ALLOW_SYNTHETIC_OUTCOME",
            reason="exact synthetic-only permit and namespace verified",
            synthetic_permit_id=verified.permit_id,
        )
        return resolved

    def deny_real_outcome_operation(
        self,
        operation: str,
        *,
        requested_at: datetime,
    ) -> None:
        requested = require_aware_utc(requested_at, "requested_at")
        event = self._record(
            relative="REAL_OUTCOME_API",
            purpose=operation,
            requested_at=requested,
            decision="DENY",
            reason="this phase has no real-outcome authorization mechanism",
        )
        raise OutcomeAccessDenied(event)
