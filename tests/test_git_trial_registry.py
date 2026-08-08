from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest

from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes, iso_z, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, EvaluationAuthorizationError
from us_stocks_swing_model_v2.gates import IndependentGatePolicy
from us_stocks_swing_model_v2.git_trial_registry import (
    GitTrialRegistryPolicy,
    build_git_trial_registry_plan,
    load_git_backed_trial_registration,
    prepare_git_trial_registration,
)
from us_stocks_swing_model_v2.governance import (
    create_local_integrity_record,
    release_bindings_hash,
)
from us_stocks_swing_model_v2.trials import TrialSpec, repository_trial_identity


REPO = Path(__file__).resolve().parents[1]


def _run(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _gate_policy() -> IndependentGatePolicy:
    return IndependentGatePolicy(
        minimum_effective_sessions=20,
        sleeve_economic_hurdles={
            sleeve: 0.001
            for sleeve in ("stock_long", "stock_short", "etf_long", "etf_short")
        },
        minimum_confidence_lower=0.0,
        rw_alpha=0.05,
        minimum_dsr_probability=0.95,
        maximum_conservative_pbo=0.20,
        pbo_failure_threshold=0.50,
    )


def _spec() -> TrialSpec:
    identity = repository_trial_identity(REPO)
    payload: dict[str, object] = {
        "hypothesis_id": "fixed_discovery_hypothesis",
        "evidence_class": "REGISTERED_HISTORICAL_DISCOVERY",
        "data_release_ids": ["a" * 64],
        "release_bindings": [{
            "release_id": "a" * 64,
            "project": "US_stocks_swing_model_v2",
            "dataset": "eligible_features",
            "source_epoch": "accepted_causal_v1",
            "role": "active_historical",
            "quality_state": "PASS",
            "created_at": "2026-07-30T00:00:00Z",
            "event_start": "2016-01-04T00:00:00Z",
            "event_end": "2026-07-10T00:00:00Z",
        }],
        "feature_schema_id": identity.feature_schema_id,
        "outcome_schema_id": identity.outcome_schema_id,
        "split_plan_id": identity.split_plan_id,
        "model_family": identity.model_family,
        "primary_metric": identity.primary_metric,
        "primary_gate_id": sha256_bytes(canonical_json_bytes(_gate_policy().as_dict())),
        "robustness_policy_id": identity.robustness_policy_id,
        "cost_policy_id": identity.cost_policy_id,
        "trial_family_id": "discovery_family_v1",
        "census_anchor_id": "2" * 64,
        "trial_family_anchor_id": "3" * 64,
        "evaluator_closure_hash": identity.evaluator_closure_hash,
        "governance_contract_hash": identity.governance_contract_hash,
        "code_hash": identity.code_hash,
        "config_hash": identity.config_hash,
        "environment_hash": identity.environment_hash,
        "trial_id": "0" * 64,
        "registered_at": iso_z(datetime.now(timezone.utc)),
        "trial_registry_binding_id": "0" * 64,
    }
    provisional = TrialSpec.from_registered_payload(payload)
    payload["trial_id"] = provisional.trial_id
    return TrialSpec.from_registered_payload(payload)


def _action(policy: GitTrialRegistryPolicy, spec: TrialSpec):
    return create_local_integrity_record(
        scope="AUTHORIZE_LOCAL_GIT_TRIAL_REGISTRATION",
        subject_id=spec.trial_id,
        bindings={
            "policy_id": policy.policy_id,
            "release_bindings_hash": release_bindings_hash(spec.release_bindings),
            "trial_registry_binding_id": policy.registry_binding_id(),
            "repository_trial_identity_id": repository_trial_identity(REPO).identity_id,
        },
        clock=TrustedClock.production(),
    )


def _policy() -> GitTrialRegistryPolicy:
    return GitTrialRegistryPolicy.load(
        REPO / "config" / "trial_registry_git_policy.json",
        repository_root=REPO,
    )


def _mock_inputs(monkeypatch: pytest.MonkeyPatch, spec: TrialSpec) -> str:
    identity_id = repository_trial_identity(REPO).identity_id
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.git_trial_registry._validate_registration_inputs",
        lambda **_kwargs: (TrustedClock.production(), spec.release_bindings, identity_id),
    )
    return identity_id


def _init_registry_repo(
    root: Path,
    policy: GitTrialRegistryPolicy,
    spec: TrialSpec,
    action_record,
    identity_id: str,
) -> tuple[str, str]:
    _run(root, "init", "--initial-branch", "main")
    _run(root, "config", "user.email", "local-test@example.invalid")
    _run(root, "config", "user.name", "Local Test")
    _run(root, "remote", "add", "origin", policy.remote_url)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _run(root, "add", "--", "seed.txt")
    _run(root, "commit", "-m", "seed")
    seed_commit = _run(root, "rev-parse", "HEAD")
    payload = {
        **spec.unsigned_dict(),
        "trial_id": spec.trial_id,
        "registered_at": action_record.recorded_at,
        "trial_registry_binding_id": policy.registry_binding_id(),
        "repository_trial_identity_id": identity_id,
        "registration_authorization_record_id": action_record.record_id,
    }
    relative = policy.relative_path(spec.trial_id)
    target = root / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(canonical_json_bytes(payload))
    _run(root, "add", "--", relative)
    _run(root, "commit", "-m", "register trial")
    registration_commit = _run(root, "rev-parse", "HEAD")
    return seed_commit, registration_commit


def test_policy_selects_only_local_git_and_the_configured_github_remote() -> None:
    policy = _policy()
    assert policy.status == "CONFIGURED_LOCAL_GIT"
    assert policy.registry_directory == "research_registry/trials"
    assert policy.owner_controlled is True
    assert policy.independent_immutability is False
    assert policy.remote_url == "https://github.com/donnywpolson-sudo/US_stocks_swing_model_v2.git"
    forged = replace(policy, independent_immutability=True)
    with pytest.raises(ContractError, match="policy is invalid"):
        forged.validate()


def test_plan_is_read_only_and_does_not_claim_independent_immutability() -> None:
    plan = build_git_trial_registry_plan(REPO)
    assert plan["remote_url_matches"] is True
    assert plan["owner_controlled"] is True
    assert plan["independent_immutability"] is False
    assert plan["authorities"] == {
        "credentials_read": False,
        "network_requests": 0,
        "registry_write": False,
        "staging": False,
        "commit": False,
        "push": False,
        "outcome_access": False,
    }


def test_prepare_writes_only_one_pending_local_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    spec = _spec()
    action = _action(policy, spec)
    _mock_inputs(monkeypatch, spec)
    monkeypatch.setattr(
        "us_stocks_swing_model_v2.git_trial_registry._git",
        lambda _root, *args, **_kwargs: (
            "main" if args[:2] == ("branch", "--show-current")
            else policy.remote_url if args[:2] == ("remote", "get-url")
            else ""
        ),
    )
    pending = prepare_git_trial_registration(
        policy=policy,
        spec=spec,
        verified_release_directories=(),
        accepted_release_root=tmp_path,
        repository_root=tmp_path,
        gate_policy=_gate_policy(),
        action_record=action,
    )
    target = tmp_path / policy.relative_path(spec.trial_id)
    assert target.is_file()
    assert pending["staging_commit_or_push_performed"] is False
    assert pending["outcome_access_authorized"] is False
    assert pending["training_or_evaluation_authorized"] is False
    with pytest.raises(EvaluationAuthorizationError, match="already consumed"):
        prepare_git_trial_registration(
            policy=policy,
            spec=spec,
            verified_release_directories=(),
            accepted_release_root=tmp_path,
            repository_root=tmp_path,
            gate_policy=_gate_policy(),
            action_record=action,
        )


def test_load_requires_registration_commit_in_local_head_and_github_tracking_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    spec = _spec()
    action = _action(policy, spec)
    identity_id = _mock_inputs(monkeypatch, spec)
    _seed, registration_commit = _init_registry_repo(
        tmp_path, policy, spec, action, identity_id
    )
    _run(tmp_path, "update-ref", "refs/remotes/origin/main", registration_commit)
    registration = load_git_backed_trial_registration(
        policy=policy,
        trial_id=spec.trial_id,
        verified_release_directories=(),
        accepted_release_root=tmp_path,
        repository_root=tmp_path,
        gate_policy=_gate_policy(),
        action_record=action,
    )
    assert registration.git_commit == registration_commit
    assert registration.remote_tip_commit == registration_commit
    assert registration.backup_state == "GITHUB_REMOTE_TRACKING_REF_VERIFIED_OWNER_CONTROLLED"
    assert len(registration.external_anchor_receipt_id) == 64


def test_load_fails_when_registration_commit_is_not_in_github_tracking_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy()
    spec = _spec()
    action = _action(policy, spec)
    identity_id = _mock_inputs(monkeypatch, spec)
    seed_commit, _registration_commit = _init_registry_repo(
        tmp_path, policy, spec, action, identity_id
    )
    _run(tmp_path, "update-ref", "refs/remotes/origin/main", seed_commit)
    with pytest.raises(EvaluationAuthorizationError, match="not backed up"):
        load_git_backed_trial_registration(
            policy=policy,
            trial_id=spec.trial_id,
            verified_release_directories=(),
            accepted_release_root=tmp_path,
            repository_root=tmp_path,
            gate_policy=_gate_policy(),
            action_record=action,
        )
