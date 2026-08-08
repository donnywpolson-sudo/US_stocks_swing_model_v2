"""Owner-controlled local Git trial registration with GitHub backup checks.

The registry is intentionally simple: one canonical JSON file per real trial,
committed in this repository and present in the configured remote-tracking
branch before outcome access.  Git makes later changes visible, but a solo
owner can rewrite both local and remote history.  Nothing in this module claims
independent immutability or third-party retention.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import TrustedClock, require_trusted_clock
from .common import (
    atomic_write_new,
    canonical_json_bytes,
    iso_z,
    parse_utc_z,
    reject_link,
    require_contained_path,
    require_sha256,
    safe_relative_path,
    sha256_bytes,
)
from .errors import ContractError, EvaluationAuthorizationError
from .gates import IndependentGatePolicy
from .governance import (
    LocalIntegrityRecord,
    ReleaseBinding,
    release_bindings_hash,
    verify_release_bindings,
)
from .trials import (
    TrialSpec,
    repository_trial_identity,
    require_trial_gate_policy,
    validate_trial_evidence_roles,
)


_MAX_RECORD_BYTES = 512 * 1024
_MAX_REGISTRATION_TIME_SKEW = timedelta(minutes=5)
_REGISTRATION_ACTION_SCOPE = "AUTHORIZE_LOCAL_GIT_TRIAL_REGISTRATION"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CONSUMED_ACTION_RECORD_IDS: set[str] = set()
_ACTION_LOCK = threading.Lock()
_FIXED_POLICY_FIELDS = {
    "schema_version": 1,
    "project": "US_stocks_swing_model_v2",
    "mode": "OWNER_CONTROLLED_GIT_TRIAL_REGISTRY",
    "backend": "LOCAL_GIT_WITH_GITHUB_BACKUP",
}


def _git(
    root: Path,
    *args: str,
    text: bool = True,
    check: bool = True,
) -> str | bytes:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=text,
            encoding="utf-8" if text else None,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationAuthorizationError("local Git registry command failed") from exc
    if check and completed.returncode != 0:
        raise EvaluationAuthorizationError("local Git registry check failed")
    return completed.stdout.strip() if text else completed.stdout


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvaluationAuthorizationError("local Git ancestry check failed") from exc
    return completed.returncode == 0


@dataclass(frozen=True)
class GitTrialRegistryPolicy:
    policy_id: str
    status: str
    registry_directory: str
    remote_name: str
    remote_branch: str
    remote_url: str
    requires_clean_commit_before_outcome_access: bool
    requires_remote_backup_before_outcome_access: bool
    owner_controlled: bool
    independent_immutability: bool

    def unsigned_dict(self) -> dict[str, object]:
        return {
            **_FIXED_POLICY_FIELDS,
            "status": self.status,
            "registry_directory": self.registry_directory,
            "remote_name": self.remote_name,
            "remote_branch": self.remote_branch,
            "remote_url": self.remote_url,
            "requires_clean_commit_before_outcome_access": self.requires_clean_commit_before_outcome_access,
            "requires_remote_backup_before_outcome_access": self.requires_remote_backup_before_outcome_access,
            "owner_controlled": self.owner_controlled,
            "independent_immutability": self.independent_immutability,
        }

    def validate(self) -> None:
        if (
            self.status != "CONFIGURED_LOCAL_GIT"
            or self.remote_name != "origin"
            or self.remote_branch != "main"
            or not self.remote_url.startswith("https://github.com/")
            or not self.remote_url.endswith(".git")
            or self.requires_clean_commit_before_outcome_access is not True
            or self.requires_remote_backup_before_outcome_access is not True
            or self.owner_controlled is not True
            or self.independent_immutability is not False
        ):
            raise ContractError("local Git trial registry policy is invalid")
        safe_relative_path(self.registry_directory)
        require_sha256(self.policy_id, "Git trial registry policy ID")
        if self.policy_id != sha256_bytes(canonical_json_bytes(self.unsigned_dict())):
            raise ContractError("Git trial registry policy ID differs from its content")

    @classmethod
    def load(cls, path: Path, *, repository_root: Path) -> "GitTrialRegistryPolicy":
        candidate = require_contained_path(Path(path), Path(repository_root))
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ContractError("local Git trial registry policy is unreadable") from exc
        expected = set(_FIXED_POLICY_FIELDS) | {
            "status",
            "registry_directory",
            "remote_name",
            "remote_branch",
            "remote_url",
            "requires_clean_commit_before_outcome_access",
            "requires_remote_backup_before_outcome_access",
            "owner_controlled",
            "independent_immutability",
        }
        if type(value) is not dict or set(value) != expected:
            raise ContractError("local Git trial registry policy fields differ")
        if any(value.get(key) != expected_value for key, expected_value in _FIXED_POLICY_FIELDS.items()):
            raise ContractError("local Git trial registry policy identity differs")
        result = cls(
            policy_id=sha256_bytes(canonical_json_bytes(value)),
            status=value["status"],
            registry_directory=value["registry_directory"],
            remote_name=value["remote_name"],
            remote_branch=value["remote_branch"],
            remote_url=value["remote_url"],
            requires_clean_commit_before_outcome_access=value["requires_clean_commit_before_outcome_access"],
            requires_remote_backup_before_outcome_access=value["requires_remote_backup_before_outcome_access"],
            owner_controlled=value["owner_controlled"],
            independent_immutability=value["independent_immutability"],
        )
        result.validate()
        return result

    def registry_binding_id(self) -> str:
        self.validate()
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "policy_id": self.policy_id,
                    "registry_directory": self.registry_directory,
                    "remote_name": self.remote_name,
                    "remote_branch": self.remote_branch,
                    "remote_url": self.remote_url,
                }
            )
        )

    def relative_path(self, trial_id: str) -> str:
        require_sha256(trial_id, "trial_id")
        return f"{self.registry_directory}/{trial_id}.json"


@dataclass(frozen=True)
class GitBackedTrialRegistration:
    schema_version: int
    backend: str
    policy_id: str
    trial_id: str
    trial_registry_binding_id: str
    registration_hash: str
    registration_authorization_record_id: str
    relative_path: str
    object_sha256: str
    registered_at: str
    git_commit: str
    remote_name: str
    remote_branch: str
    remote_url_sha256: str
    remote_tip_commit: str
    backup_state: str
    registered_payload: Mapping[str, Any]
    external_anchor_receipt_id: str

    def anchor_payload(self) -> dict[str, object]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
            if name not in {"registered_payload", "external_anchor_receipt_id"}
        }

    def validate(self) -> None:
        if (
            self.schema_version != 2
            or self.backend != "LOCAL_GIT_WITH_GITHUB_BACKUP"
            or self.remote_name != "origin"
            or self.remote_branch != "main"
            or self.backup_state
            != "GITHUB_REMOTE_TRACKING_REF_VERIFIED_OWNER_CONTROLLED"
            or _COMMIT.fullmatch(self.git_commit) is None
            or _COMMIT.fullmatch(self.remote_tip_commit) is None
        ):
            raise EvaluationAuthorizationError("Git-backed trial registration contract differs")
        for name in (
            "policy_id",
            "trial_id",
            "trial_registry_binding_id",
            "registration_hash",
            "registration_authorization_record_id",
            "object_sha256",
            "remote_url_sha256",
            "external_anchor_receipt_id",
        ):
            try:
                require_sha256(getattr(self, name), f"git_registration.{name}")
            except ContractError as exc:
                raise EvaluationAuthorizationError(str(exc)) from exc
        safe_relative_path(self.relative_path)
        if Path(self.relative_path).name != f"{self.trial_id}.json":
            raise EvaluationAuthorizationError("Git registration path differs from its trial")
        parse_utc_z(self.registered_at, "git_registration.registered_at")
        if type(self.registered_payload) is not dict:
            raise EvaluationAuthorizationError("Git registration payload is invalid")
        payload_hash = sha256_bytes(canonical_json_bytes(self.registered_payload))
        if (
            payload_hash != self.registration_hash
            or payload_hash != self.object_sha256
            or self.registered_payload.get("trial_id") != self.trial_id
            or self.registered_payload.get("trial_registry_binding_id")
            != self.trial_registry_binding_id
            or self.registered_payload.get("registered_at") != self.registered_at
        ):
            raise EvaluationAuthorizationError("Git registration differs from its committed file")
        if self.external_anchor_receipt_id != sha256_bytes(
            canonical_json_bytes(self.anchor_payload())
        ):
            raise EvaluationAuthorizationError("Git registration anchor differs from its evidence")


def _action_bindings(
    *,
    policy: GitTrialRegistryPolicy,
    verified_release_bindings: Iterable[ReleaseBinding],
    repository_trial_identity_id: str,
) -> dict[str, str]:
    return {
        "policy_id": policy.policy_id,
        "release_bindings_hash": release_bindings_hash(verified_release_bindings),
        "trial_registry_binding_id": policy.registry_binding_id(),
        "repository_trial_identity_id": repository_trial_identity_id,
    }


def _validate_registration_inputs(
    *,
    policy: GitTrialRegistryPolicy,
    spec: TrialSpec,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    repository_root: Path,
    gate_policy: IndependentGatePolicy,
    action_record: LocalIntegrityRecord,
    clock: TrustedClock | None,
) -> tuple[TrustedClock, tuple[ReleaseBinding, ...], str]:
    policy.validate()
    spec.validate()
    identity = repository_trial_identity(repository_root)
    identity.require_spec(spec)
    require_trial_gate_policy(spec, gate_policy, repository_root=repository_root)
    trusted_clock = require_trusted_clock(clock)
    if not trusted_clock.trust_eligible:
        raise EvaluationAuthorizationError("real Git trial registration requires production UTC")
    verified = verify_release_bindings(
        tuple(verified_release_directories),
        accepted_release_root=Path(accepted_release_root),
        expected_project="US_stocks_swing_model_v2",
    )
    if verified != spec.release_bindings:
        raise ContractError("trial release bindings differ from verified release manifests")
    validate_trial_evidence_roles(spec)
    if type(action_record) is not LocalIntegrityRecord:
        raise EvaluationAuthorizationError("Git registration requires its exact action record")
    action_record.validate(
        expected_scope=_REGISTRATION_ACTION_SCOPE,
        expected_subject_id=spec.trial_id,
        required_bindings=_action_bindings(
            policy=policy,
            verified_release_bindings=verified,
            repository_trial_identity_id=identity.identity_id,
        ),
        clock=trusted_clock,
    )
    return trusted_clock, verified, identity.identity_id


def prepare_git_trial_registration(
    *,
    policy: GitTrialRegistryPolicy,
    spec: TrialSpec,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    repository_root: Path,
    gate_policy: IndependentGatePolicy,
    action_record: LocalIntegrityRecord,
    clock: TrustedClock | None = None,
) -> dict[str, object]:
    """Write one new local registration file; never stage, commit, or push it."""

    root = Path(repository_root).resolve(strict=True)
    trusted_clock, verified, identity_id = _validate_registration_inputs(
        policy=policy,
        spec=spec,
        verified_release_directories=verified_release_directories,
        accepted_release_root=accepted_release_root,
        repository_root=root,
        gate_policy=gate_policy,
        action_record=action_record,
        clock=clock,
    )
    if _git(root, "branch", "--show-current") != policy.remote_branch:
        raise EvaluationAuthorizationError("Git registration requires the configured branch")
    if _git(root, "remote", "get-url", policy.remote_name) != policy.remote_url:
        raise EvaluationAuthorizationError("Git registration remote differs from policy")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise EvaluationAuthorizationError("Git registration requires a clean worktree")
    registered_at = trusted_clock.now()
    payload = {
        **spec.unsigned_dict(),
        "trial_id": spec.trial_id,
        "registered_at": iso_z(registered_at),
        "trial_registry_binding_id": policy.registry_binding_id(),
        "repository_trial_identity_id": identity_id,
        "registration_authorization_record_id": action_record.record_id,
    }
    raw = canonical_json_bytes(payload)
    relative_path = policy.relative_path(spec.trial_id)
    target = require_contained_path(root / relative_path, root, must_exist=False)
    with _ACTION_LOCK:
        if action_record.record_id in _CONSUMED_ACTION_RECORD_IDS:
            raise EvaluationAuthorizationError("Git registration action record was already consumed")
        _CONSUMED_ACTION_RECORD_IDS.add(action_record.record_id)
    try:
        atomic_write_new(target, raw)
    except FileExistsError as exc:
        raise EvaluationAuthorizationError("Git trial registration already exists") from exc
    unsigned = {
        "schema_version": 1,
        "mode": "LOCAL_GIT_TRIAL_REGISTRATION_PENDING_COMMIT_AND_PUSH",
        "policy_id": policy.policy_id,
        "trial_id": spec.trial_id,
        "trial_registry_binding_id": policy.registry_binding_id(),
        "relative_path": relative_path,
        "registration_hash": sha256_bytes(raw),
        "registration_authorization_record_id": action_record.record_id,
        "release_bindings_hash": release_bindings_hash(verified),
        "outcome_access_authorized": False,
        "training_or_evaluation_authorized": False,
        "staging_commit_or_push_performed": False,
    }
    return {**unsigned, "pending_registration_id": sha256_bytes(canonical_json_bytes(unsigned))}


def load_git_backed_trial_registration(
    *,
    policy: GitTrialRegistryPolicy,
    trial_id: str,
    verified_release_directories: Iterable[Path],
    accepted_release_root: Path,
    repository_root: Path,
    gate_policy: IndependentGatePolicy,
    action_record: LocalIntegrityRecord,
    clock: TrustedClock | None = None,
) -> GitBackedTrialRegistration:
    """Verify one committed registration and its locally known GitHub backup."""

    root = Path(repository_root).resolve(strict=True)
    policy.validate()
    require_sha256(trial_id, "trial_id")
    relative_path = policy.relative_path(trial_id)
    target = require_contained_path(root / relative_path, root)
    reject_link(target)
    raw = target.read_bytes()
    if len(raw) > _MAX_RECORD_BYTES:
        raise EvaluationAuthorizationError("Git trial registration exceeds the size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationAuthorizationError("Git trial registration is not valid UTF-8 JSON") from exc
    if type(payload) is not dict or canonical_json_bytes(payload) != raw:
        raise EvaluationAuthorizationError("Git trial registration is not canonical JSON")
    try:
        spec = TrialSpec.from_registered_payload(payload)
    except (KeyError, TypeError, ValueError, ContractError) as exc:
        raise EvaluationAuthorizationError("Git trial registration payload is invalid") from exc
    trusted_clock, _verified, identity_id = _validate_registration_inputs(
        policy=policy,
        spec=spec,
        verified_release_directories=verified_release_directories,
        accepted_release_root=accepted_release_root,
        repository_root=root,
        gate_policy=gate_policy,
        action_record=action_record,
        clock=clock,
    )
    if (
        payload.get("trial_id") != trial_id
        or payload.get("trial_registry_binding_id") != policy.registry_binding_id()
        or payload.get("repository_trial_identity_id") != identity_id
        or payload.get("registration_authorization_record_id") != action_record.record_id
    ):
        raise EvaluationAuthorizationError("Git trial registration binding differs")
    if _git(root, "branch", "--show-current") != policy.remote_branch:
        raise EvaluationAuthorizationError("Git registration is not on the configured branch")
    if _git(root, "remote", "get-url", policy.remote_name) != policy.remote_url:
        raise EvaluationAuthorizationError("Git registration remote differs from policy")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise EvaluationAuthorizationError("Git registration requires a clean committed worktree")
    _git(root, "ls-files", "--error-unmatch", "--", relative_path)
    mode_line = str(_git(root, "ls-files", "-s", "--", relative_path))
    if not mode_line.startswith("100644 ") and not mode_line.startswith("100755 "):
        raise EvaluationAuthorizationError("Git registration must be a regular tracked file")
    commits = str(_git(root, "log", "--format=%H", "--", relative_path)).splitlines()
    if len(commits) != 1 or _COMMIT.fullmatch(commits[0]) is None:
        raise EvaluationAuthorizationError("Git registration must have exactly one path commit")
    registration_commit = commits[0]
    committed_raw = _git(root, "show", f"{registration_commit}:{relative_path}", text=False)
    if committed_raw != raw:
        raise EvaluationAuthorizationError("Git registration differs from committed bytes")
    remote_ref = f"refs/remotes/{policy.remote_name}/{policy.remote_branch}"
    remote_tip = str(_git(root, "rev-parse", "--verify", remote_ref))
    if _COMMIT.fullmatch(remote_tip) is None:
        raise EvaluationAuthorizationError("GitHub remote-tracking tip is invalid")
    if not _is_ancestor(root, registration_commit, "HEAD"):
        raise EvaluationAuthorizationError("Git registration commit is not in local HEAD")
    if not _is_ancestor(root, registration_commit, remote_tip):
        raise EvaluationAuthorizationError("Git registration commit is not backed up to configured GitHub branch")
    registered_at = parse_utc_z(str(payload["registered_at"]), "registered_at")
    observed_at = trusted_clock.now()
    action_at = parse_utc_z(action_record.recorded_at, "registration action recorded_at")
    if registered_at > observed_at + _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError("Git trial registration time is in the future")
    if action_at > registered_at + _MAX_REGISTRATION_TIME_SKEW:
        raise EvaluationAuthorizationError("Git registration action postdates registration")
    registration_hash = sha256_bytes(raw)
    unsigned = {
        "schema_version": 2,
        "backend": "LOCAL_GIT_WITH_GITHUB_BACKUP",
        "policy_id": policy.policy_id,
        "trial_id": trial_id,
        "trial_registry_binding_id": policy.registry_binding_id(),
        "registration_hash": registration_hash,
        "registration_authorization_record_id": action_record.record_id,
        "relative_path": relative_path,
        "object_sha256": registration_hash,
        "registered_at": str(payload["registered_at"]),
        "git_commit": registration_commit,
        "remote_name": policy.remote_name,
        "remote_branch": policy.remote_branch,
        "remote_url_sha256": sha256_bytes(policy.remote_url.encode("utf-8")),
        "remote_tip_commit": remote_tip,
        "backup_state": "GITHUB_REMOTE_TRACKING_REF_VERIFIED_OWNER_CONTROLLED",
    }
    result = GitBackedTrialRegistration(
        **unsigned,
        registered_payload=payload,
        external_anchor_receipt_id=sha256_bytes(canonical_json_bytes(unsigned)),
    )
    result.validate()
    return result


def build_git_trial_registry_plan(repository_root: Path) -> dict[str, object]:
    """Report local readiness without reading credentials, writing, or networking."""

    root = Path(repository_root).resolve(strict=True)
    policy = GitTrialRegistryPolicy.load(
        root / "config/trial_registry_git_policy.json",
        repository_root=root,
    )
    branch = str(_git(root, "branch", "--show-current"))
    head = str(_git(root, "rev-parse", "HEAD"))
    remote_url = str(_git(root, "remote", "get-url", policy.remote_name))
    remote_ref = f"refs/remotes/{policy.remote_name}/{policy.remote_branch}"
    remote_tip = str(_git(root, "rev-parse", "--verify", remote_ref))
    clean = not bool(_git(root, "status", "--porcelain=v1", "--untracked-files=all"))
    unsigned = {
        "schema_version": 1,
        "mode": "LOCAL_GIT_TRIAL_REGISTRY_PLAN_ONLY",
        "policy_id": policy.policy_id,
        "backend": "LOCAL_GIT_WITH_GITHUB_BACKUP",
        "registry_directory": policy.registry_directory,
        "branch": branch,
        "head": head,
        "clean": clean,
        "remote_name": policy.remote_name,
        "remote_branch": policy.remote_branch,
        "remote_url_matches": remote_url == policy.remote_url,
        "remote_tracking_tip": remote_tip,
        "owner_controlled": True,
        "independent_immutability": False,
        "authorities": {
            "credentials_read": False,
            "network_requests": 0,
            "registry_write": False,
            "staging": False,
            "commit": False,
            "push": False,
            "outcome_access": False,
        },
    }
    return {**unsigned, "registry_plan_id": sha256_bytes(canonical_json_bytes(unsigned))}
