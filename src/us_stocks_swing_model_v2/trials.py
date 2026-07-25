from __future__ import annotations

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
)
from .capabilities import SyntheticOnlyPermit, require_synthetic_permit
from .clock import TrustedClock, require_trusted_clock
from .errors import ContractError, EvaluationAuthorizationError, IntegrityError
from .governance import (
    AuthorizationAuthority,
    ReleaseBinding,
    SignedAuthorizationReceipt,
    release_bindings_hash,
    verify_release_bindings,
)
from .ledger import HashChainLedger


REAL_EVIDENCE_CLASSES = {"REGISTERED_HISTORICAL_DISCOVERY", "PROSPECTIVE_FINAL"}
EVALUATION_SCOPES = {"OUTER_SCREEN", "FINAL_HOLDOUT"}
HOLDOUT_STATES = {"LOCKED", "UNLOCKED_ONCE", "CLOSED"}
EVALUATION_STATES = {
    "PASS",
    "FAIL",
    "INCONCLUSIVE",
    "INCONCLUSIVE_ROBUSTNESS",
    "INVALID",
}


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
        if set(payload) != expected:
            raise ContractError("registered trial payload fields differ from the frozen contract")
        parse_utc_z(str(payload["registered_at"]), "registered_at")
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"trial_id", "registered_at", "trial_registry_binding_id"}
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
        valid_shape = (
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
        )
        self.evaluations = HashChainLedger(
            evaluations_path,
            "evaluation_result_v1",
            clock=self._clock,
        )
        self.permits = HashChainLedger(
            evaluations_path.with_name(f"{evaluations_path.stem}.permits.jsonl"),
            "trial_permit_v1",
            clock=self._clock,
        )
        self.expected_project = expected_project

    def with_clock(self, clock: TrustedClock) -> "TrialRegistry":
        return TrialRegistry(
            self.registry.path,
            self.evaluations.path,
            accepted_release_root=self.accepted_release_root,
            governance_root=self.governance_root,
            synthetic_permit=self._synthetic_permit,
            expected_project=self.expected_project,
            clock=clock,
        )

    def register(self, spec: TrialSpec, *, verified_release_directories: Iterable[Path]) -> str:
        spec.validate()
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
        }
        self.registry.append(payload, unique_key="trial_id")
        return spec.trial_id

    @staticmethod
    def _validate_evidence_roles(spec: TrialSpec) -> None:
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
            if spec.evidence_class == "PROSPECTIVE_FINAL":
                if binding.role not in pass_roles or binding.quality_state != "PASS":
                    raise ContractError("prospective final evidence requires PASS trust-eligible releases")
            elif not (
                (binding.role == "legacy_discovery_only" and binding.quality_state == "LEGACY_CAVEATED")
                or (binding.role in pass_roles and binding.quality_state == "PASS")
            ):
                raise ContractError("historical discovery release role/quality is not eligible")

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
        evaluation_input_hash: str,
        evaluator_code_hash: str,
        holdout_receipt: HoldoutStateReceipt,
        authorization: SignedAuthorizationReceipt,
        authorization_authority: AuthorizationAuthority,
        initial_holdout_receipt: HoldoutStateReceipt | None = None,
    ) -> TrialPermit:
        registration = self.authorize(trial_id)
        spec = TrialSpec.from_registered_payload(registration)
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
        }
        authorization.validate(
            authority=authorization_authority,
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
            "authorization_receipt_id": authorization.receipt_id,
            "issued_at": iso_z(issued),
            "time_authority": self._clock.mode,
            "synthetic_clock_permit_id": self._clock.synthetic_permit_id,
        }
        permit = TrialPermit(**unsigned, permit_id=sha256_bytes(canonical_json_bytes(unsigned)))
        permit.validate()
        self.permits.append(permit.as_dict(), unique_key="permit_id")
        return permit

    def _require_closed_outer_pass(self, trial_id: str) -> None:
        results = [
            row["payload"]["result"]
            for row in self.evaluations.read_verified()
            if row["payload"].get("trial_id") == trial_id
            and row["payload"].get("evaluation_scope") == "OUTER_SCREEN"
        ]
        if len(results) != 1 or results[0].get("state") != "PASS" or results[0].get("evaluation_closed") is not True:
            raise EvaluationAuthorizationError("final holdout requires one closed PASS outer evaluation")

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

    def build_gate_receipt(self, permit: TrialPermit, *, policy: Any, metrics: Mapping[str, Any]):
        issued = self.verify_issued_permit(permit)
        from .gates import _build_gate_receipt_from_issued_permit

        return _build_gate_receipt_from_issued_permit(
            permit=permit,
            issued_permit=issued,
            policy=policy,
            metrics=metrics,
            clock=self._clock,
        )

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
