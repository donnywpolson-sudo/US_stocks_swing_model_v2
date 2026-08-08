from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .common import (
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    require_contained_path,
    require_sha256,
    sha256_bytes,
    sha256_file,
)
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError, EvaluationAuthorizationError, IntegrityError
from .governance import (
    LocalIntegrityRecord,
    ReleaseBinding,
    release_bindings_hash,
    verify_release_bindings,
)
from .ledger import HashChainLedger


REAL_EVIDENCE_CLASSES = {"REGISTERED_HISTORICAL_DISCOVERY", "PROSPECTIVE_FINAL"}
EVALUATION_SCOPES = {"OUTER_SCREEN", "FINAL_HOLDOUT"}
HOLDOUT_STATES = {"LOCKED", "UNLOCKED_ONCE", "CLOSED"}
EVALUATION_STATES = {
    "INVALID",
    "INCONCLUSIVE_PIT_IDENTITY",
    "FAIL_NO_EDGE",
    "FAIL_NOT_ECONOMIC",
    "FAIL_MULTIPLICITY_OR_CONTROL",
    "INCONCLUSIVE_DATA_OR_POWER",
    "INCONCLUSIVE_EFFECT",
    "INCONCLUSIVE_ROBUSTNESS",
    "PASS_HISTORICAL_DISCOVERY_SCREEN",
}


_TRIAL_ENVIRONMENT_PATHS = (
    "pyproject.toml",
    "requirements.lock",
    "requirements.sha256.lock",
    "config/environment.lock.json",
)
_TRIAL_IDENTITY_FIELDS = (
    "feature_schema_id",
    "outcome_schema_id",
    "split_plan_id",
    "model_family",
    "primary_metric",
    "primary_gate_id",
    "robustness_policy_id",
    "cost_policy_id",
    "evaluator_closure_hash",
    "governance_contract_hash",
    "code_hash",
    "config_hash",
    "environment_hash",
)
_REPOSITORY_TRIAL_IDENTITY_FIELDS = tuple(
    name for name in _TRIAL_IDENTITY_FIELDS if name != "primary_gate_id"
)


def _closure_hash(root: Path, paths: Iterable[Path], *, name: str) -> str:
    entries: list[dict[str, object]] = []
    for candidate in sorted((Path(item) for item in paths), key=lambda item: item.as_posix()):
        contained = require_contained_path(candidate, root)
        if contained.is_symlink() or not contained.is_file():
            raise ContractError(f"{name} closure contains a non-regular file")
        entries.append(
            {
                "path": contained.relative_to(root).as_posix(),
                "size": contained.stat().st_size,
                "sha256": sha256_file(contained),
            }
        )
    if not entries:
        raise ContractError(f"{name} closure cannot be empty")
    return sha256_bytes(canonical_json_bytes({"name": name, "files": entries}))


@dataclass(frozen=True)
class RepositoryTrialIdentity:
    """Live, fixed-census repository identity for a real-evidence trial."""

    feature_schema_id: str
    outcome_schema_id: str
    split_plan_id: str
    model_family: str
    primary_metric: str
    robustness_policy_id: str
    cost_policy_id: str
    evaluator_closure_hash: str
    governance_contract_hash: str
    code_hash: str
    config_hash: str
    environment_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            name: str(getattr(self, name))
            for name in _REPOSITORY_TRIAL_IDENTITY_FIELDS
        }

    @property
    def identity_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))

    def validate(self) -> None:
        if (
            self.feature_schema_id != "prospective_price_only_v1"
            or self.outcome_schema_id
            != "d1_open_to_d5_close_split_normalized_v1"
            or self.model_family != "linear_distribution_v1"
            or self.primary_metric != "multiclass_log_loss"
        ):
            raise ContractError("repository trial semantics differ from the fixed project contract")
        for name in _REPOSITORY_TRIAL_IDENTITY_FIELDS:
            if name not in {
                "feature_schema_id",
                "outcome_schema_id",
                "model_family",
                "primary_metric",
            }:
                require_sha256(getattr(self, name), f"repository_trial_identity.{name}")

    def require_spec(self, spec: "TrialSpec") -> None:
        spec.validate()
        actual = {
            name: getattr(spec, name)
            for name in _REPOSITORY_TRIAL_IDENTITY_FIELDS
        }
        if actual != self.as_dict():
            raise ContractError(
                "trial specification differs from the live repository execution identity"
            )


def repository_trial_identity(repository_root: Path) -> RepositoryTrialIdentity:
    """Derive the non-caller-selectable code, config, environment, and policy identity."""

    root = Path(repository_root).resolve(strict=True)
    executing_root = Path(__file__).resolve(strict=True).parents[2]
    if root != executing_root:
        raise ContractError(
            "repository trial identity root differs from the executing package checkout"
        )
    package = require_contained_path(
        root / "src" / "us_stocks_swing_model_v2",
        root,
    )
    config_root = require_contained_path(root / "config", root)
    readiness_path = require_contained_path(
        config_root / "research_readiness_contract.json",
        root,
    )
    if not (root / "AGENTS.md").is_file() or not package.is_dir() or not config_root.is_dir():
        raise ContractError("repository trial identity requires the exact project checkout layout")
    try:
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError("research readiness contract is unreadable") from exc
    if (
        type(readiness) is not dict
        or readiness.get("project") != "US_stocks_swing_model_v2"
        or type(readiness.get("nested_wfa")) is not dict
        or type(readiness.get("binding_gate")) is not dict
        or type(readiness.get("robustness")) is not dict
        or type(readiness.get("economic_translation")) is not dict
    ):
        raise ContractError("research readiness contract lacks the fixed trial policies")
    code_paths = tuple(package.rglob("*.py"))
    config_paths = tuple(config_root.rglob("*.json"))
    research_paths = tuple((package / "research").rglob("*.py"))
    evaluator_paths = (*research_paths, package / "gates.py", package / "trials.py")
    environment_paths = tuple(root / item for item in _TRIAL_ENVIRONMENT_PATHS)
    result = RepositoryTrialIdentity(
        feature_schema_id="prospective_price_only_v1",
        outcome_schema_id="d1_open_to_d5_close_split_normalized_v1",
        split_plan_id=sha256_bytes(canonical_json_bytes(readiness["nested_wfa"])),
        model_family="linear_distribution_v1",
        primary_metric="multiclass_log_loss",
        robustness_policy_id=sha256_bytes(canonical_json_bytes(readiness["robustness"])),
        cost_policy_id=sha256_bytes(canonical_json_bytes(readiness["economic_translation"])),
        evaluator_closure_hash=_closure_hash(root, evaluator_paths, name="trial_evaluator"),
        governance_contract_hash=sha256_bytes(canonical_json_bytes(readiness)),
        code_hash=_closure_hash(root, code_paths, name="trial_code"),
        config_hash=_closure_hash(root, config_paths, name="trial_config"),
        environment_hash=_closure_hash(root, environment_paths, name="trial_environment"),
    )
    result.validate()
    return result


def require_trial_gate_policy(
    spec: "TrialSpec",
    gate_policy: Any,
    *,
    repository_root: Path,
) -> None:
    """Bind a trial-specific executable gate to fixed repository thresholds."""

    from .gates import IndependentGatePolicy

    spec.validate()
    if type(gate_policy) is not IndependentGatePolicy:
        raise ContractError("trial specification requires the exact independent gate policy")
    gate_policy.validate()
    root = Path(repository_root).resolve(strict=True)
    try:
        readiness = json.loads(
            (root / "config" / "research_readiness_contract.json").read_text(
                encoding="utf-8"
            )
        )
        fixed_gate_values = {
            "rw_alpha": readiness["multiple_testing"]["romano_wolf"][
                "maximum_adjusted_one_sided_p"
            ],
            "minimum_dsr_probability": readiness["multiple_testing"]["dsr"][
                "minimum_probability"
            ],
            "maximum_conservative_pbo": readiness["multiple_testing"]["pbo"][
                "pass_maximum"
            ],
            "pbo_failure_threshold": readiness["multiple_testing"]["pbo"][
                "inconclusive_maximum"
            ],
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ContractError("readiness contract lacks executable gate thresholds") from exc
    if any(
        getattr(gate_policy, name) != value
        for name, value in fixed_gate_values.items()
    ):
        raise ContractError(
            "independent gate policy differs from the fixed readiness thresholds"
        )
    policy_hash = sha256_bytes(canonical_json_bytes(gate_policy.as_dict()))
    if spec.primary_gate_id != policy_hash:
        raise ContractError(
            "trial primary gate differs from its exact executable gate policy"
        )


def _validate_prepermit_input_artifacts(
    artifacts: Mapping[str, Mapping[str, Any]],
    *,
    evaluation_scope: str,
) -> None:
    """Require one exact immutable plan commitment before outcome access."""

    if type(artifacts) is not dict or set(artifacts) != {"evaluation_plan"}:
        raise ContractError(
            "pre-permit evaluation input cannot contain gate or result artifacts; "
            "only the exact plan commitment is allowed"
        )
    plan = artifacts["evaluation_plan"]
    fields = {
        "schema_version",
        "evaluation_scope",
        "purpose",
        "commitment_hash",
    }
    if type(plan) is not dict or set(plan) != fields:
        raise ContractError(
            "pre-permit evaluation input cannot contain gate or result artifacts; "
            "plan fields differ from the exact commitment contract"
        )
    unsigned = {
        "schema_version": 1,
        "evaluation_scope": evaluation_scope,
        "purpose": "SYNTHETIC_MECHANICS_INPUT_ONLY",
    }
    if any(plan.get(name) != value for name, value in unsigned.items()):
        raise ContractError(
            "pre-permit evaluation plan differs from its requested scope"
        )
    try:
        require_sha256(plan.get("commitment_hash"), "evaluation_plan.commitment_hash")
    except ContractError as exc:
        raise ContractError(
            "pre-permit evaluation plan commitment hash is invalid"
        ) from exc
    if plan["commitment_hash"] != sha256_bytes(canonical_json_bytes(unsigned)):
        raise ContractError(
            "pre-permit evaluation plan commitment differs from its exact content"
        )


@dataclass(frozen=True)
class EvaluationExecutionEvidence:
    """Exact synthetic evaluation payload bound to the live evaluator closure."""

    schema_version: int
    trial_id: str
    evaluation_scope: str
    evaluation_input_bytes: bytes
    repository_trial_identity_id: str
    evaluator_code_hash: str
    evidence_id: str

    @classmethod
    def create(
        cls,
        *,
        spec: "TrialSpec",
        evaluation_scope: str,
        artifacts: Mapping[str, Mapping[str, Any]],
        repository_root: Path,
    ) -> "EvaluationExecutionEvidence":
        spec.validate()
        identity = repository_trial_identity(repository_root)
        identity.require_spec(spec)
        if evaluation_scope not in EVALUATION_SCOPES:
            raise ContractError("evaluation execution evidence scope is invalid")
        _validate_prepermit_input_artifacts(
            artifacts,
            evaluation_scope=evaluation_scope,
        )
        evaluation_input: dict[str, Any] = {
            "schema_version": 1,
            "trial_id": spec.trial_id,
            "evaluation_scope": evaluation_scope,
            "data_release_ids": list(spec.data_release_ids),
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "repository_trial_identity_id": identity.identity_id,
            "evaluator_code_hash": identity.evaluator_closure_hash,
            **{name: getattr(spec, name) for name in _TRIAL_IDENTITY_FIELDS},
            "artifacts": {
                name: dict(value) for name, value in sorted(artifacts.items())
            },
        }
        unsigned = {
            "schema_version": 1,
            "trial_id": spec.trial_id,
            "evaluation_scope": evaluation_scope,
            "evaluation_input": evaluation_input,
            "repository_trial_identity_id": identity.identity_id,
            "evaluator_code_hash": identity.evaluator_closure_hash,
        }
        result = cls(
            schema_version=1,
            trial_id=spec.trial_id,
            evaluation_scope=evaluation_scope,
            evaluation_input_bytes=canonical_json_bytes(evaluation_input),
            repository_trial_identity_id=identity.identity_id,
            evaluator_code_hash=identity.evaluator_closure_hash,
            evidence_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        result.validate(spec, repository_identity=identity)
        return result

    @property
    def evaluation_input_hash(self) -> str:
        return sha256_bytes(self.evaluation_input_bytes)

    def evaluation_input(self) -> dict[str, Any]:
        if type(self.evaluation_input_bytes) is not bytes:
            raise ContractError("evaluation input must be retained as exact canonical bytes")
        try:
            payload = json.loads(self.evaluation_input_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("evaluation input is not canonical JSON") from exc
        if type(payload) is not dict or canonical_json_bytes(payload) != self.evaluation_input_bytes:
            raise ContractError("evaluation input differs from its exact canonical bytes")
        return payload

    def validate(
        self,
        spec: "TrialSpec",
        *,
        repository_identity: RepositoryTrialIdentity,
    ) -> None:
        spec.validate()
        repository_identity.validate()
        repository_identity.require_spec(spec)
        evaluation_input = self.evaluation_input()
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.trial_id != spec.trial_id
            or self.evaluation_scope not in EVALUATION_SCOPES
            or self.repository_trial_identity_id != repository_identity.identity_id
            or self.evaluator_code_hash
            != repository_identity.evaluator_closure_hash
        ):
            raise ContractError("evaluation execution evidence identity is invalid")
        expected_fields = {
            "schema_version",
            "trial_id",
            "evaluation_scope",
            "data_release_ids",
            "release_bindings_hash",
            "repository_trial_identity_id",
            "evaluator_code_hash",
            *_TRIAL_IDENTITY_FIELDS,
            "artifacts",
        }
        if set(evaluation_input) != expected_fields:
            raise ContractError("evaluation input fields differ from the exact evidence contract")
        expected = {
            "schema_version": 1,
            "trial_id": spec.trial_id,
            "evaluation_scope": self.evaluation_scope,
            "data_release_ids": list(spec.data_release_ids),
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "repository_trial_identity_id": repository_identity.identity_id,
            "evaluator_code_hash": repository_identity.evaluator_closure_hash,
            **{name: getattr(spec, name) for name in _TRIAL_IDENTITY_FIELDS},
        }
        if any(evaluation_input.get(name) != value for name, value in expected.items()):
            raise ContractError("evaluation input differs from the preregistered trial")
        artifacts = evaluation_input.get("artifacts")
        try:
            _validate_prepermit_input_artifacts(
                artifacts,
                evaluation_scope=self.evaluation_scope,
            )
        except ContractError as exc:
            raise ContractError("evaluation input artifact payloads are invalid") from exc
        unsigned = {
            "schema_version": 1,
            "trial_id": self.trial_id,
            "evaluation_scope": self.evaluation_scope,
            "evaluation_input": evaluation_input,
            "repository_trial_identity_id": self.repository_trial_identity_id,
            "evaluator_code_hash": self.evaluator_code_hash,
        }
        require_sha256(self.evidence_id, "evaluation_execution_evidence.evidence_id")
        if self.evidence_id != sha256_bytes(canonical_json_bytes(unsigned)):
            raise ContractError("evaluation execution evidence ID differs from its content")


@dataclass(frozen=True)
class TrialSpec:
    hypothesis_id: str
    evidence_class: str
    data_release_ids: tuple[str, ...]
    release_bindings: tuple[ReleaseBinding, ...]
    feature_schema_id: str
    outcome_schema_id: str
    split_plan_id: str
    model_family: str
    primary_metric: str
    primary_gate_id: str
    robustness_policy_id: str
    cost_policy_id: str
    trial_family_id: str
    census_anchor_id: str
    trial_family_anchor_id: str
    evaluator_closure_hash: str
    governance_contract_hash: str
    code_hash: str
    config_hash: str
    environment_hash: str

    def unsigned_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["data_release_ids"] = list(self.data_release_ids)
        payload["release_bindings"] = [binding.as_dict() for binding in self.release_bindings]
        return payload

    @property
    def trial_id(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.unsigned_dict()))

    def validate(self) -> None:
        required = (
            self.hypothesis_id,
            self.evidence_class,
            self.feature_schema_id,
            self.outcome_schema_id,
            self.split_plan_id,
            self.model_family,
            self.primary_metric,
            self.primary_gate_id,
            self.robustness_policy_id,
            self.cost_policy_id,
            self.trial_family_id,
        )
        if not all(required):
            raise ContractError("trial specification fields cannot be empty")
        if self.evidence_class not in REAL_EVIDENCE_CLASSES:
            raise ContractError("only counted real-evidence classes belong in the trial registry")
        if list(self.data_release_ids) != sorted(set(self.data_release_ids)) or not self.data_release_ids:
            raise ContractError("trial data releases must be nonempty, sorted, and unique")
        binding_ids = tuple(binding.release_id for binding in self.release_bindings)
        for binding in self.release_bindings:
            binding.validate()
        if binding_ids != self.data_release_ids:
            raise ContractError("trial release bindings must exactly match sorted data release IDs")
        for field_name in (
            "census_anchor_id",
            "trial_family_anchor_id",
            "evaluator_closure_hash",
            "governance_contract_hash",
            "primary_gate_id",
            "robustness_policy_id",
            "code_hash",
            "config_hash",
            "environment_hash",
        ):
            require_sha256(getattr(self, field_name), field_name)

    @classmethod
    def from_registered_payload(cls, payload: Mapping[str, Any]) -> "TrialSpec":
        expected = set(cls.__dataclass_fields__) | {
            "trial_id",
            "registered_at",
            "trial_registry_binding_id",
        }
        allowed = (expected, expected | {"repository_trial_identity_id"})
        if set(payload) not in allowed:
            raise ContractError("registered trial payload fields differ from the frozen contract")
        parse_utc_z(str(payload["registered_at"]), "registered_at")
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {
                "trial_id",
                "registered_at",
                "trial_registry_binding_id",
                "repository_trial_identity_id",
            }
        }
        fields["data_release_ids"] = tuple(fields["data_release_ids"])
        fields["release_bindings"] = tuple(ReleaseBinding(**entry) for entry in fields["release_bindings"])
        return cls(**fields)


@dataclass(frozen=True)
class HoldoutStateReceipt:
    schema_version: int
    trial_id: str
    state: str
    unlock_count: int
    previous_receipt_id: str | None
    created_at: str
    time_authority: str
    synthetic_clock_permit_id: str | None
    receipt_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "trial_id": self.trial_id,
            "state": self.state,
            "unlock_count": self.unlock_count,
            "previous_receipt_id": self.previous_receipt_id,
            "created_at": self.created_at,
            "time_authority": self.time_authority,
            "synthetic_clock_permit_id": self.synthetic_clock_permit_id,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "receipt_id": self.receipt_id}

    def validate(self) -> None:
        parse_utc_z(self.created_at, "holdout.created_at")
        if self.time_authority == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_clock_permit_id is not None:
                raise EvaluationAuthorizationError("production holdout cannot carry synthetic time")
        elif self.time_authority == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            if self.synthetic_clock_permit_id is None:
                raise EvaluationAuthorizationError("synthetic holdout lacks its clock permit")
            try:
                require_sha256(self.synthetic_clock_permit_id, "holdout.synthetic_clock_permit_id")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        else:
            raise EvaluationAuthorizationError("holdout time authority is invalid")
        if type(self.schema_version) is not int or self.schema_version != 1 or self.state not in HOLDOUT_STATES:
            raise EvaluationAuthorizationError("holdout receipt schema/trial/state is invalid")
        try:
            require_sha256(self.trial_id, "holdout.trial_id")
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        valid_shape = type(self.unlock_count) is int and (
            (self.state == "LOCKED" and self.unlock_count == 0 and self.previous_receipt_id is None)
            or (
                self.state in {"UNLOCKED_ONCE", "CLOSED"}
                and self.unlock_count == 1
                and self.previous_receipt_id is not None
                and isinstance(self.previous_receipt_id, str)
            )
        )
        if not valid_shape:
            raise EvaluationAuthorizationError("holdout state transition shape is invalid")
        if self.previous_receipt_id is not None:
            try:
                require_sha256(self.previous_receipt_id, "holdout.previous_receipt_id")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        try:
            require_sha256(self.receipt_id, "holdout.receipt_id")
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        if self.receipt_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise EvaluationAuthorizationError("holdout receipt ID differs from its content")


def build_holdout_receipt(
    *,
    trial_id: str,
    state: str,
    clock: TrustedClock,
    previous: HoldoutStateReceipt | None = None,
) -> HoldoutStateReceipt:
    trusted_clock = require_trusted_clock(clock)
    if state == "LOCKED":
        if previous is not None:
            raise EvaluationAuthorizationError("initial locked holdout cannot name a predecessor")
        unlock_count = 0
        previous_id = None
    else:
        if previous is None:
            raise EvaluationAuthorizationError("holdout transition requires its predecessor")
        previous.validate()
        if previous.trial_id != trial_id:
            raise EvaluationAuthorizationError("holdout transition belongs to another trial")
        if state == "UNLOCKED_ONCE" and previous.state != "LOCKED":
            raise EvaluationAuthorizationError("holdout may be unlocked only once from LOCKED")
        if state == "CLOSED" and previous.state != "UNLOCKED_ONCE":
            raise EvaluationAuthorizationError("holdout may close only after its one unlock")
        unlock_count = 1
        previous_id = previous.receipt_id
    unsigned = {
        "schema_version": 1,
        "trial_id": trial_id,
        "state": state,
        "unlock_count": unlock_count,
        "previous_receipt_id": previous_id,
        "created_at": iso_z(trusted_clock.now()),
        "time_authority": trusted_clock.mode,
        "synthetic_clock_permit_id": trusted_clock.synthetic_permit_id,
    }
    receipt = HoldoutStateReceipt(**unsigned, receipt_id=sha256_bytes(canonical_json_bytes(unsigned)))
    receipt.validate()
    return receipt


@dataclass(frozen=True)
class GovernedHoldoutAccessReceipt:
    """Append-only production-capable holdout transition evidence."""

    schema_version: int
    trial_registry_binding_id: str
    trial_id: str
    state: str
    unlock_count: int
    holdout_state_receipt_id: str
    holdout_state_previous_receipt_id: str | None
    previous_receipt_id: str | None
    pre_unlock_trial_ledger_head: str | None
    authorization_record_id: str | None
    created_at: str
    time_authority: str
    synthetic_clock_permit_id: str | None
    receipt_id: str

    def unsigned_dict(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name != "receipt_id"
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "receipt_id": self.receipt_id}

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise EvaluationAuthorizationError("governed holdout receipt schema differs")
        for name in (
            "trial_registry_binding_id",
            "trial_id",
            "holdout_state_receipt_id",
            "receipt_id",
        ):
            try:
                require_sha256(getattr(self, name), f"governed_holdout.{name}")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        parse_utc_z(self.created_at, "governed_holdout.created_at")
        if self.time_authority == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_clock_permit_id is not None:
                raise EvaluationAuthorizationError("production holdout carries synthetic time")
        elif self.time_authority == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            try:
                require_sha256(
                    self.synthetic_clock_permit_id or "",
                    "governed_holdout.synthetic_clock_permit_id",
                )
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        else:
            raise EvaluationAuthorizationError("governed holdout time authority is invalid")
        locked = (
            self.state == "LOCKED"
            and self.unlock_count == 0
            and self.holdout_state_previous_receipt_id is None
            and self.previous_receipt_id is None
            and self.pre_unlock_trial_ledger_head is None
            and self.authorization_record_id is None
        )
        transitioned = (
            self.state in {"UNLOCKED_ONCE", "CLOSED"}
            and self.unlock_count == 1
            and self.holdout_state_previous_receipt_id is not None
            and self.previous_receipt_id is not None
            and self.pre_unlock_trial_ledger_head is not None
            and self.authorization_record_id is not None
        )
        if type(self.unlock_count) is not int or not (locked or transitioned):
            raise EvaluationAuthorizationError("governed holdout transition shape is invalid")
        if transitioned:
            for name in (
                "previous_receipt_id",
                "holdout_state_previous_receipt_id",
                "pre_unlock_trial_ledger_head",
                "authorization_record_id",
            ):
                try:
                    require_sha256(getattr(self, name), f"governed_holdout.{name}")
                except ContractError as exc:
                    raise EvaluationAuthorizationError(str(exc)) from exc
        if self.receipt_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise EvaluationAuthorizationError("governed holdout receipt ID differs")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GovernedHoldoutAccessReceipt":
        if type(payload) is not dict or set(payload) != set(cls.__dataclass_fields__):
            raise EvaluationAuthorizationError("governed holdout receipt fields differ")
        receipt = cls(**payload)
        receipt.validate()
        return receipt


def _validate_governed_holdout_payload(payload: Mapping[str, Any]) -> None:
    GovernedHoldoutAccessReceipt.from_dict(payload)


class GovernedHoldoutAccessStore:
    """Serialize one irreversible holdout-access chain per registered trial."""

    def __init__(
        self,
        path: Path,
        *,
        governance_root: Path,
        clock: TrustedClock,
    ):
        root = Path(governance_root).resolve(strict=True)
        candidate = require_contained_path(Path(path), root, must_exist=False)
        self._clock = require_trusted_clock(clock)
        self._ledger = HashChainLedger(
            candidate,
            "governed_holdout_access_v1",
            clock=self._clock,
            payload_validator=_validate_governed_holdout_payload,
        )

    def with_clock(self, clock: TrustedClock) -> "GovernedHoldoutAccessStore":
        rebound = object.__new__(GovernedHoldoutAccessStore)
        rebound._clock = require_trusted_clock(clock)
        rebound._ledger = self._ledger.with_clock(clock)
        return rebound

    def _history(self) -> tuple[list[dict[str, Any]], list[GovernedHoldoutAccessReceipt]]:
        history = self._ledger.read_verified()
        receipts = [
            GovernedHoldoutAccessReceipt.from_dict(entry["payload"])
            for entry in history
        ]
        return history, receipts

    def latest(self, trial_id: str) -> GovernedHoldoutAccessReceipt:
        require_sha256(trial_id, "governed_holdout.trial_id")
        _, receipts = self._history()
        matches = [receipt for receipt in receipts if receipt.trial_id == trial_id]
        if not matches:
            raise EvaluationAuthorizationError("governed holdout trial has no receipt")
        return matches[-1]

    def initialize(
        self,
        *,
        trial_registry_binding_id: str,
        holdout_receipt: HoldoutStateReceipt,
    ) -> GovernedHoldoutAccessReceipt:
        require_sha256(trial_registry_binding_id, "governed_holdout.trial_registry_binding_id")
        if type(holdout_receipt) is not HoldoutStateReceipt:
            raise EvaluationAuthorizationError(
                "governed holdout initialization requires the exact holdout receipt"
            )
        holdout_receipt.validate()
        if holdout_receipt.state != "LOCKED":
            raise EvaluationAuthorizationError(
                "governed holdout initialization requires the initial lock"
            )
        trial_id = holdout_receipt.trial_id
        history, receipts = self._history()
        if any(receipt.trial_id == trial_id for receipt in receipts):
            raise EvaluationAuthorizationError("governed holdout trial is already initialized")
        return self._append(
            history,
            trial_registry_binding_id=trial_registry_binding_id,
            trial_id=trial_id,
            state="LOCKED",
            holdout_state_receipt_id=holdout_receipt.receipt_id,
            holdout_state_previous_receipt_id=None,
            previous_receipt_id=None,
            pre_unlock_trial_ledger_head=None,
            authorization_record_id=None,
        )

    def unlock_once(
        self,
        *,
        locked_receipt: GovernedHoldoutAccessReceipt,
        unlocked_holdout_receipt: HoldoutStateReceipt,
        pre_unlock_trial_ledger_head: str,
        authorization: LocalIntegrityRecord,
    ) -> GovernedHoldoutAccessReceipt:
        locked_receipt.validate()
        if type(unlocked_holdout_receipt) is not HoldoutStateReceipt:
            raise EvaluationAuthorizationError(
                "governed holdout unlock requires the exact holdout receipt"
            )
        unlocked_holdout_receipt.validate()
        if (
            unlocked_holdout_receipt.trial_id != locked_receipt.trial_id
            or unlocked_holdout_receipt.state != "UNLOCKED_ONCE"
            or unlocked_holdout_receipt.previous_receipt_id
            != locked_receipt.holdout_state_receipt_id
        ):
            raise EvaluationAuthorizationError(
                "governed holdout unlock differs from the initial holdout lock"
            )
        require_sha256(pre_unlock_trial_ledger_head, "governed_holdout.pre_unlock_trial_ledger_head")
        history, receipts = self._history()
        self._require_latest(receipts, locked_receipt, expected_state="LOCKED")
        required = {
            "trial_registry_binding_id": locked_receipt.trial_registry_binding_id,
            "locked_governed_receipt_id": locked_receipt.receipt_id,
            "locked_holdout_state_receipt_id": locked_receipt.holdout_state_receipt_id,
            "unlocked_holdout_state_receipt_id": unlocked_holdout_receipt.receipt_id,
            "pre_unlock_trial_ledger_head": pre_unlock_trial_ledger_head,
        }
        authorization.validate(
            expected_scope="AUTHORIZE_FINAL_HOLDOUT_ACCESS",
            expected_subject_id=locked_receipt.trial_id,
            required_bindings=required,
            clock=self._clock,
        )
        return self._append(
            history,
            trial_registry_binding_id=locked_receipt.trial_registry_binding_id,
            trial_id=locked_receipt.trial_id,
            state="UNLOCKED_ONCE",
            holdout_state_receipt_id=unlocked_holdout_receipt.receipt_id,
            holdout_state_previous_receipt_id=unlocked_holdout_receipt.previous_receipt_id,
            previous_receipt_id=locked_receipt.receipt_id,
            pre_unlock_trial_ledger_head=pre_unlock_trial_ledger_head,
            authorization_record_id=authorization.record_id,
        )

    def close(
        self,
        *,
        unlocked_receipt: GovernedHoldoutAccessReceipt,
        closed_holdout_receipt: HoldoutStateReceipt,
        authorization: LocalIntegrityRecord,
    ) -> GovernedHoldoutAccessReceipt:
        unlocked_receipt.validate()
        if type(closed_holdout_receipt) is not HoldoutStateReceipt:
            raise EvaluationAuthorizationError(
                "governed holdout closure requires the exact holdout receipt"
            )
        closed_holdout_receipt.validate()
        if (
            closed_holdout_receipt.trial_id != unlocked_receipt.trial_id
            or closed_holdout_receipt.state != "CLOSED"
            or closed_holdout_receipt.previous_receipt_id
            != unlocked_receipt.holdout_state_receipt_id
        ):
            raise EvaluationAuthorizationError(
                "governed holdout closure differs from the one authorized unlock"
            )
        history, receipts = self._history()
        self._require_latest(receipts, unlocked_receipt, expected_state="UNLOCKED_ONCE")
        required = {
            "trial_registry_binding_id": unlocked_receipt.trial_registry_binding_id,
            "unlocked_governed_receipt_id": unlocked_receipt.receipt_id,
            "unlocked_holdout_state_receipt_id": unlocked_receipt.holdout_state_receipt_id,
            "closed_holdout_state_receipt_id": closed_holdout_receipt.receipt_id,
            "pre_unlock_trial_ledger_head": unlocked_receipt.pre_unlock_trial_ledger_head or "",
        }
        authorization.validate(
            expected_scope="CLOSE_FINAL_HOLDOUT_ACCESS",
            expected_subject_id=unlocked_receipt.trial_id,
            required_bindings=required,
            clock=self._clock,
        )
        return self._append(
            history,
            trial_registry_binding_id=unlocked_receipt.trial_registry_binding_id,
            trial_id=unlocked_receipt.trial_id,
            state="CLOSED",
            holdout_state_receipt_id=closed_holdout_receipt.receipt_id,
            holdout_state_previous_receipt_id=closed_holdout_receipt.previous_receipt_id,
            previous_receipt_id=unlocked_receipt.receipt_id,
            pre_unlock_trial_ledger_head=unlocked_receipt.pre_unlock_trial_ledger_head,
            authorization_record_id=authorization.record_id,
        )

    @staticmethod
    def _require_latest(
        receipts: list[GovernedHoldoutAccessReceipt],
        supplied: GovernedHoldoutAccessReceipt,
        *,
        expected_state: str,
    ) -> None:
        matches = [receipt for receipt in receipts if receipt.trial_id == supplied.trial_id]
        if (
            not matches
            or matches[-1].as_dict() != supplied.as_dict()
            or supplied.state != expected_state
        ):
            raise EvaluationAuthorizationError("governed holdout transition is stale or replayed")

    def _append(
        self,
        history: list[dict[str, Any]],
        *,
        trial_registry_binding_id: str,
        trial_id: str,
        state: str,
        holdout_state_receipt_id: str,
        holdout_state_previous_receipt_id: str | None,
        previous_receipt_id: str | None,
        pre_unlock_trial_ledger_head: str | None,
        authorization_record_id: str | None,
    ) -> GovernedHoldoutAccessReceipt:
        unsigned = {
            "schema_version": 1,
            "trial_registry_binding_id": trial_registry_binding_id,
            "trial_id": trial_id,
            "state": state,
            "unlock_count": 0 if state == "LOCKED" else 1,
            "holdout_state_receipt_id": holdout_state_receipt_id,
            "holdout_state_previous_receipt_id": holdout_state_previous_receipt_id,
            "previous_receipt_id": previous_receipt_id,
            "pre_unlock_trial_ledger_head": pre_unlock_trial_ledger_head,
            "authorization_record_id": authorization_record_id,
            "created_at": iso_z(self._clock.now()),
            "time_authority": self._clock.mode,
            "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
        }
        receipt = GovernedHoldoutAccessReceipt(
            **unsigned,
            receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
        )
        receipt.validate()
        self._ledger.append(
            receipt.as_dict(),
            expected_record_count=len(history),
            expected_head_hash=history[-1]["record_hash"] if history else "0" * 64,
        )
        return receipt


@dataclass(frozen=True)
class TrialPermit:
    trial_registry_binding_id: str
    trial_id: str
    registration_hash: str
    evaluation_scope: str
    evaluation_input_hash: str
    evaluator_code_hash: str
    evaluator_closure_hash: str
    census_anchor_id: str
    trial_family_anchor_id: str
    governance_contract_hash: str
    primary_gate_id: str
    robustness_policy_id: str
    release_bindings_hash: str
    holdout_receipt_id: str
    authorization_receipt_id: str
    issued_at: str
    time_authority: str
    synthetic_clock_permit_id: str | None
    permit_id: str

    def unsigned_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "permit_id"}

    def as_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "permit_id": self.permit_id}

    def validate(self) -> None:
        parse_utc_z(self.issued_at, "issued_at")
        if self.evaluation_scope not in EVALUATION_SCOPES:
            raise EvaluationAuthorizationError("evaluation scope is invalid")
        for name in self.__dataclass_fields__:
            if name in {
                "issued_at",
                "evaluation_scope",
                "time_authority",
                "synthetic_clock_permit_id",
            }:
                continue
            try:
                require_sha256(getattr(self, name), f"permit.{name}")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        expected = sha256_bytes(canonical_json_bytes(self.unsigned_dict()))
        if self.permit_id != expected:
            raise EvaluationAuthorizationError("trial permit hash does not match its content")
        if self.time_authority == "PRODUCTION_SYSTEM_UTC":
            if self.synthetic_clock_permit_id is not None:
                raise EvaluationAuthorizationError("production permit cannot carry synthetic time")
        elif self.time_authority == "SYNTHETIC_FIXED_TIME_NOT_TRUST_ELIGIBLE":
            if self.synthetic_clock_permit_id is None:
                raise EvaluationAuthorizationError("synthetic permit lacks its clock capability")
            try:
                require_sha256(self.synthetic_clock_permit_id, "permit.synthetic_clock_permit_id")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        else:
            raise EvaluationAuthorizationError("trial permit time authority is invalid")


def validate_trial_evidence_roles(spec: TrialSpec) -> None:
    """Apply the same release-role ceiling at every trial-registry boundary."""

    pass_roles = {
        "active_historical",
        "prospective_as_received",
        "derived_causal",
        "feature_only",
        "outcome_only",
    }
    for binding in spec.release_bindings:
        if binding.role == "qualification_evidence_only":
            raise ContractError("qualification evidence can never enter a trial")
        if (
            binding.role == "legacy_discovery_only"
            or binding.quality_state == "LEGACY_CAVEATED"
        ):
            raise ContractError("legacy releases cannot enter trial registration")
        if spec.evidence_class == "PROSPECTIVE_FINAL":
            if binding.role not in pass_roles or binding.quality_state != "PASS":
                raise ContractError(
                    "prospective final evidence requires PASS trust-eligible releases"
                )
        elif binding.role not in pass_roles or binding.quality_state != "PASS":
            raise ContractError(
                "historical discovery release role/quality is not eligible"
            )


def _validate_gate_receipt_payload(payload: Mapping[str, Any]) -> None:
    from .gates import GateReceipt

    GateReceipt.from_dict(payload)


class TrialRegistry:
    def __init__(
        self,
        registry_path: Path,
        evaluations_path: Path,
        *,
        accepted_release_root: Path,
        governance_root: Path,
        synthetic_permit: SyntheticOnlyPermit,
        expected_project: str = "US_stocks_swing_model_v2",
        clock: TrustedClock | None = None,
    ):
        self._clock = require_trusted_clock(clock)
        if self._clock.trust_eligible:
            raise EvaluationAuthorizationError(
                "production trial registry requires an external immutable registry loader"
            )
        self._synthetic_permit = require_synthetic_permit(
            synthetic_permit,
            scope="SYNTHETIC_TRIAL_REGISTRY",
        )
        self.accepted_release_root = Path(accepted_release_root)
        self.governance_root = Path(governance_root)
        if not self.accepted_release_root.is_absolute() or not self.governance_root.is_absolute():
            raise ContractError("trial registry roots must be absolute")
        require_contained_path(self.accepted_release_root, self.accepted_release_root)
        require_contained_path(self.governance_root, self.governance_root)
        registry_path = Path(registry_path)
        evaluations_path = Path(evaluations_path)
        require_contained_path(registry_path, self.governance_root, must_exist=False)
        require_contained_path(evaluations_path, self.governance_root, must_exist=False)
        self.registry_binding_id = sha256_bytes(
            canonical_json_bytes(
                {
                    "project": expected_project,
                    "registry_path": str(registry_path),
                    "evaluations_path": str(evaluations_path),
                    "accepted_release_root": str(self.accepted_release_root),
                    "governance_root": str(self.governance_root),
                    "time_authority": self._clock.mode,
                    "synthetic_permit_id": self._synthetic_permit.permit_id,
                }
            )
        )
        self.registry = HashChainLedger(
            registry_path,
            "registered_trial_v1",
            clock=self._clock,
            unique_key="trial_id",
        )
        self.evaluations = HashChainLedger(
            evaluations_path,
            "evaluation_result_v1",
            clock=self._clock,
            unique_key="permit_id",
        )
        self.permits = HashChainLedger(
            evaluations_path.with_name(f"{evaluations_path.stem}.permits.jsonl"),
            "trial_permit_v1",
            clock=self._clock,
            unique_key="permit_id",
        )
        self._gate_receipts = HashChainLedger(
            evaluations_path.with_name(
                f"{evaluations_path.stem}.gate_receipts.jsonl"
            ),
            "trial_gate_receipt_v1",
            clock=self._clock,
            unique_key="evaluation_permit_id",
            payload_validator=_validate_gate_receipt_payload,
        )
        self.expected_project = expected_project

    def with_clock(self, clock: TrustedClock) -> "TrialRegistry":
        rebound = TrialRegistry(
            self.registry.path,
            self.evaluations.path,
            accepted_release_root=self.accepted_release_root,
            governance_root=self.governance_root,
            synthetic_permit=self._synthetic_permit,
            expected_project=self.expected_project,
            clock=clock,
        )
        rebound.registry = self.registry.with_clock(clock)
        rebound.evaluations = self.evaluations.with_clock(clock)
        rebound.permits = self.permits.with_clock(clock)
        rebound._gate_receipts = self._gate_receipts.with_clock(clock)
        return rebound

    def register(
        self,
        spec: TrialSpec,
        *,
        verified_release_directories: Iterable[Path],
        repository_root: Path,
        gate_policy: Any,
    ) -> str:
        spec.validate()
        live_identity = repository_trial_identity(repository_root)
        live_identity.require_spec(spec)
        require_trial_gate_policy(
            spec,
            gate_policy,
            repository_root=repository_root,
        )
        verified = verify_release_bindings(
            verified_release_directories,
            accepted_release_root=self.accepted_release_root,
            expected_project=self.expected_project,
        )
        if verified != spec.release_bindings:
            raise ContractError("trial release bindings differ from verified release manifests")
        self._validate_evidence_roles(spec)
        payload = {
            **spec.unsigned_dict(),
            "trial_id": spec.trial_id,
            "registered_at": iso_z(self._clock.now()),
            "trial_registry_binding_id": self.registry_binding_id,
            "repository_trial_identity_id": live_identity.identity_id,
        }
        self.registry.append(payload, unique_key="trial_id")
        return spec.trial_id

    @staticmethod
    def _validate_evidence_roles(spec: TrialSpec) -> None:
        validate_trial_evidence_roles(spec)

    def authorize(self, trial_id: str) -> Mapping[str, Any]:
        registered = [
            row["payload"]
            for row in self.registry.read_verified()
            if row["payload"].get("trial_id") == trial_id
        ]
        if len(registered) != 1:
            raise EvaluationAuthorizationError(f"trial is not uniquely registered: {trial_id}")
        registration = registered[0]
        try:
            spec = TrialSpec.from_registered_payload(registration)
            spec.validate()
        except (KeyError, TypeError, ValueError, ContractError) as exc:
            raise EvaluationAuthorizationError("registered trial payload is malformed") from exc
        if spec.trial_id != trial_id or registration.get("trial_id") != spec.trial_id:
            raise EvaluationAuthorizationError("registered trial_id does not match its frozen specification")
        if registration.get("trial_registry_binding_id") != self.registry_binding_id:
            raise EvaluationAuthorizationError("registered trial belongs to another registry binding")
        return registration

    def issue_permit(
        self,
        trial_id: str,
        *,
        evaluation_scope: str,
        execution_evidence: EvaluationExecutionEvidence | None = None,
        evaluation_input_hash: str | None = None,
        evaluator_code_hash: str | None = None,
        holdout_receipt: HoldoutStateReceipt,
        action_record: LocalIntegrityRecord,
        repository_root: Path,
        gate_policy: Any,
        initial_holdout_receipt: HoldoutStateReceipt | None = None,
    ) -> TrialPermit:
        registration = self.authorize(trial_id)
        spec = TrialSpec.from_registered_payload(registration)
        try:
            live_identity = repository_trial_identity(repository_root)
            live_identity.require_spec(spec)
            require_trial_gate_policy(
                spec,
                gate_policy,
                repository_root=repository_root,
            )
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "registered trial differs from the live repository execution contract"
            ) from exc
        if registration.get("repository_trial_identity_id") != live_identity.identity_id:
            raise EvaluationAuthorizationError(
                "registered trial lacks its exact live repository identity"
            )
        if evaluation_input_hash is not None or evaluator_code_hash is not None:
            raise EvaluationAuthorizationError(
                "permit issuance rejects caller-declared execution hashes"
            )
        if type(execution_evidence) is not EvaluationExecutionEvidence:
            raise EvaluationAuthorizationError(
                "permit issuance requires exact content-addressed execution evidence"
            )
        try:
            execution_evidence.validate(
                spec,
                repository_identity=live_identity,
            )
        except ContractError as exc:
            raise EvaluationAuthorizationError(
                "execution evidence differs from the preregistered trial"
            ) from exc
        if execution_evidence.evaluation_scope != evaluation_scope:
            raise EvaluationAuthorizationError(
                "execution evidence scope differs from the requested permit"
            )
        evaluation_input_hash = execution_evidence.evaluation_input_hash
        evaluator_code_hash = execution_evidence.evaluator_code_hash
        issued = self._clock.now()
        if issued <= parse_utc_z(str(registration["registered_at"]), "registered_at"):
            raise EvaluationAuthorizationError("permit must be issued after trial registration")
        holdout_receipt.validate()
        if holdout_receipt.trial_id != trial_id:
            raise EvaluationAuthorizationError("holdout receipt belongs to another trial")
        if evaluation_scope == "OUTER_SCREEN":
            if holdout_receipt.state != "LOCKED" or initial_holdout_receipt is not None:
                raise EvaluationAuthorizationError("outer screening requires the initial locked holdout")
        elif evaluation_scope == "FINAL_HOLDOUT":
            if initial_holdout_receipt is None:
                raise EvaluationAuthorizationError("final holdout requires the retained initial lock receipt")
            initial_holdout_receipt.validate()
            if (
                initial_holdout_receipt.trial_id != trial_id
                or initial_holdout_receipt.state != "LOCKED"
                or holdout_receipt.state != "UNLOCKED_ONCE"
                or holdout_receipt.previous_receipt_id != initial_holdout_receipt.receipt_id
            ):
                raise EvaluationAuthorizationError("final holdout receipt is not the one authorized unlock")
            self._require_closed_outer_pass(trial_id)
        else:
            raise EvaluationAuthorizationError("evaluation scope is invalid")
        permit_history = self.permits.read_verified()
        if any(
            row["payload"].get("trial_id") == trial_id
            and row["payload"].get("evaluation_scope") == evaluation_scope
            for row in permit_history
        ):
            raise EvaluationAuthorizationError(
                "evaluation scope already has an issued permit for this trial"
            )
        permit_head = (
            permit_history[-1]["record_hash"]
            if permit_history
            else "0" * 64
        )
        registration_hash = sha256_bytes(canonical_json_bytes(registration))
        required_bindings = {
            "trial_registry_binding_id": self.registry_binding_id,
            "registration_hash": registration_hash,
            "evaluation_scope": evaluation_scope,
            "evaluation_input_hash": evaluation_input_hash,
            "evaluator_code_hash": evaluator_code_hash,
            "evaluator_closure_hash": spec.evaluator_closure_hash,
            "census_anchor_id": spec.census_anchor_id,
            "trial_family_anchor_id": spec.trial_family_anchor_id,
            "governance_contract_hash": spec.governance_contract_hash,
            "primary_gate_id": spec.primary_gate_id,
            "robustness_policy_id": spec.robustness_policy_id,
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "holdout_receipt_id": holdout_receipt.receipt_id,
            "execution_evidence_id": execution_evidence.evidence_id,
            "repository_trial_identity_id": live_identity.identity_id,
        }
        action_record.validate(
            expected_scope=f"AUTHORIZE_{evaluation_scope}",
            expected_subject_id=trial_id,
            required_bindings=required_bindings,
            clock=self._clock,
        )
        unsigned = {
            "trial_registry_binding_id": self.registry_binding_id,
            "trial_id": trial_id,
            "registration_hash": registration_hash,
            "evaluation_scope": evaluation_scope,
            "evaluation_input_hash": evaluation_input_hash,
            "evaluator_code_hash": evaluator_code_hash,
            "evaluator_closure_hash": spec.evaluator_closure_hash,
            "census_anchor_id": spec.census_anchor_id,
            "trial_family_anchor_id": spec.trial_family_anchor_id,
            "governance_contract_hash": spec.governance_contract_hash,
            "primary_gate_id": spec.primary_gate_id,
            "robustness_policy_id": spec.robustness_policy_id,
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "holdout_receipt_id": holdout_receipt.receipt_id,
            "authorization_receipt_id": action_record.record_id,
            "issued_at": iso_z(issued),
            "time_authority": self._clock.mode,
            "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
        }
        permit = TrialPermit(**unsigned, permit_id=sha256_bytes(canonical_json_bytes(unsigned)))
        permit.validate()
        self.permits.append(
            permit.as_dict(),
            unique_key="permit_id",
            expected_record_count=len(permit_history),
            expected_head_hash=permit_head,
        )
        return permit

    def _require_closed_outer_pass(self, trial_id: str) -> None:
        results = [
            row["payload"]["result"]
            for row in self.evaluations.read_verified()
            if row["payload"].get("trial_id") == trial_id
            and row["payload"].get("evaluation_scope") == "OUTER_SCREEN"
        ]
        if (
            len(results) != 1
            or results[0].get("state")
            != "PASS_HISTORICAL_DISCOVERY_SCREEN"
            or results[0].get("evaluation_closed") is not True
        ):
            raise EvaluationAuthorizationError(
                "final holdout requires one closed "
                "PASS_HISTORICAL_DISCOVERY_SCREEN outer evaluation"
            )

    def verify_issued_permit(self, permit: TrialPermit) -> Mapping[str, Any]:
        permit.validate()
        if permit.trial_registry_binding_id != self.registry_binding_id:
            raise EvaluationAuthorizationError("permit belongs to another trial registry")
        issued = [
            row["payload"]
            for row in self.permits.read_verified()
            if row["payload"].get("permit_id") == permit.permit_id
        ]
        if len(issued) != 1 or issued[0] != permit.as_dict():
            raise EvaluationAuthorizationError("permit is not the exact registry-issued permit")
        self.authorize(permit.trial_id)
        return issued[0]

    def build_gate_receipt(self, permit: TrialPermit, *, policy: Any, evidence: Any):
        issued = self.verify_issued_permit(permit)
        from .gates import _build_gate_receipt_from_issued_permit

        receipt = _build_gate_receipt_from_issued_permit(
            permit=permit,
            issued_permit=issued,
            policy=policy,
            evidence=evidence,
            clock=self._clock,
        )
        history = self._gate_receipts.read_verified()
        self._gate_receipts.append(
            receipt.as_dict(),
            expected_record_count=len(history),
            expected_head_hash=(
                history[-1]["record_hash"] if history else "0" * 64
            ),
        )
        return receipt

    def verify_issued_gate_receipt(
        self,
        permit: TrialPermit,
        gate_receipt: Any,
    ) -> Mapping[str, Any]:
        self.verify_issued_permit(permit)
        from .gates import GateReceipt

        if type(gate_receipt) is not GateReceipt:
            raise EvaluationAuthorizationError(
                "evaluation requires the exact gate receipt contract"
            )
        gate_receipt.validate()
        issued = [
            row["payload"]
            for row in self._gate_receipts.read_verified()
            if row["payload"].get("evaluation_permit_id") == permit.permit_id
        ]
        if len(issued) != 1 or issued[0] != gate_receipt.as_dict():
            raise EvaluationAuthorizationError(
                "gate receipt is not the exact registry-issued gate receipt"
            )
        return issued[0]

    def record_evaluation(
        self,
        permit: TrialPermit,
        result: Mapping[str, Any],
        *,
        gate_receipt: Any,
    ) -> None:
        self.verify_issued_permit(permit)
        registration = self.authorize(permit.trial_id)
        registration_hash = sha256_bytes(canonical_json_bytes(registration))
        if permit.registration_hash != registration_hash:
            raise EvaluationAuthorizationError("permit registration hash is stale or forged")
        expected_fields = {
            "trial_id",
            "evaluation_scope",
            "evaluation_input_hash",
            "evaluator_closure_hash",
            "authorization_receipt_id",
            "holdout_receipt_id",
            "gate_receipt_id",
            "robustness_policy_id",
            "robustness_evidence_hash",
            "result_artifact_hash",
            "state",
            "evaluation_closed",
        }
        if set(result) != expected_fields:
            raise EvaluationAuthorizationError("evaluation result fields differ from the frozen closure contract")
        expected_values = {
            "trial_id": permit.trial_id,
            "evaluation_scope": permit.evaluation_scope,
            "evaluation_input_hash": permit.evaluation_input_hash,
            "evaluator_closure_hash": permit.evaluator_closure_hash,
            "authorization_receipt_id": permit.authorization_receipt_id,
            "holdout_receipt_id": permit.holdout_receipt_id,
            "robustness_policy_id": permit.robustness_policy_id,
        }
        if any(result.get(name) != value for name, value in expected_values.items()):
            raise EvaluationAuthorizationError("evaluation result differs from its permit bindings")
        from .gates import GateReceipt

        if type(gate_receipt) is not GateReceipt:
            raise EvaluationAuthorizationError("evaluation requires the exact gate receipt contract")
        gate_receipt.validate()
        if (
            gate_receipt.trial_id != permit.trial_id
            or gate_receipt.evaluation_permit_id != permit.permit_id
            or gate_receipt.permit_payload_hash
            != sha256_bytes(canonical_json_bytes(permit.as_dict()))
            or gate_receipt.robustness_policy_hash != permit.robustness_policy_id
            or result.get("gate_receipt_id") != gate_receipt.receipt_id
            or result.get("robustness_evidence_hash")
            != gate_receipt.robustness_evidence_hash
            or result.get("state") != gate_receipt.state
        ):
            raise EvaluationAuthorizationError(
                "evaluation result differs from its gate or robustness bindings"
            )
        self.verify_issued_gate_receipt(permit, gate_receipt)
        if result.get("state") not in EVALUATION_STATES or result.get("evaluation_closed") is not True:
            raise EvaluationAuthorizationError("evaluation result must be explicitly closed with a valid state")
        try:
            require_sha256(result.get("result_artifact_hash"), "evaluation.result_artifact_hash")
            require_sha256(
                result.get("robustness_evidence_hash"),
                "evaluation.robustness_evidence_hash",
            )
        except ContractError as exc:
            raise EvaluationAuthorizationError(str(exc)) from exc
        artifact_payload = {
            name: result[name]
            for name in sorted(expected_fields - {"result_artifact_hash"})
        }
        if result["result_artifact_hash"] != sha256_bytes(
            canonical_json_bytes(artifact_payload)
        ):
            raise EvaluationAuthorizationError(
                "evaluation artifact hash differs from its policy/evidence-bound content"
            )
        evaluated_at = self._clock.now()
        if evaluated_at <= parse_utc_z(permit.issued_at, "permit.issued_at"):
            raise EvaluationAuthorizationError("evaluation must occur after permit issuance")
        existing_scopes = {
            row["payload"].get("evaluation_scope")
            for row in self.evaluations.read_verified()
            if row["payload"].get("trial_id") == permit.trial_id
        }
        if permit.evaluation_scope in existing_scopes:
            raise IntegrityError("duplicate evaluation scope for trial")
        payload = {
            "trial_id": permit.trial_id,
            "evaluation_scope": permit.evaluation_scope,
            "permit_id": permit.permit_id,
            "permit": {**permit.unsigned_dict(), "permit_id": permit.permit_id},
            "registration_hash": registration_hash,
            "result": {
                **dict(result),
                "evaluated_at": iso_z(evaluated_at),
                "time_authority": self._clock.mode,
                "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
            },
        }
        self.evaluations.append(payload, unique_key="permit_id")
