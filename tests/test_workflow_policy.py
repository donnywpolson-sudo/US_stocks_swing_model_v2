from __future__ import annotations

import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.cli import (
    execute_legacy_purge,
    generate_legacy_cleanup_plan,
    hash_copy,
    plan_alpaca_discovery_proxy_features,
    plan_alpaca_discovery_proxy_outcomes,
    plan_alpaca_sip_non_active_cutover,
    publish_alpaca_historical_backfill,
    publish_alpaca_sip_qualification_receipt,
    publish_xnys_calendar_successor,
    qualify_alpaca_sip,
    qualify_identity_sources,
)


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_agents_retains_the_binding_action_and_safety_boundaries() -> None:
    agents = _read("AGENTS.md")
    audit = _read("docs/AUDIT_WORKFLOW.md")

    for action_class in (
        "LOCAL_CORRECTABLE",
        "READ_ONLY_INVOCATION",
        "MUTATING_OR_EXTERNAL",
    ):
        assert agents.count(f"| `{action_class}` |") == 1
    for boundary in (
        "Do not commit, push, or cut over unless",
        "Never read, print, copy, move, edit, or commit secrets",
        "data/**",
        "accepted release",
    ):
        assert boundary in agents
    assert "docs/AGENT_WORKFLOW.md" in agents
    assert "### Canonical Task Routing And Gate Checklist" in agents
    assert "| `LOCAL_CORRECTABLE` |" in agents
    assert "| `READ_ONLY_INVOCATION` |" in agents
    assert "| `MUTATING_OR_EXTERNAL` |" in agents
    assert "### Action And Failure Classes" not in agents
    assert "## Action Classes" not in audit


def test_agents_define_scope_simplicity_and_completion_structurally() -> None:
    agents = _read("AGENTS.md")
    normalized = " ".join(agents.split())

    assert "### Scope, Simplicity, And Completion" in agents
    assert "Follow established repository architecture when it fits" in normalized
    assert "Prefer a direct, well-tested solution" in agents
    assert "new dependency, public API or schema" in normalized
    assert "explain why a smaller alternative is insufficient" in normalized
    assert "Localized work should proceed directly" in agents
    assert "Use one agent by default" in normalized
    assert "Update documentation and comments only for" in agents
    assert "Do not add a fallback that conceals a visible failure" in normalized
    assert "Stop when the requested behavior and acceptance criteria are satisfied" in normalized
    assert "one final diff review finds no accidental scope growth" in normalized
    assert "Report optional improvements without implementing them" in normalized
    assert "more than five tracked paths" not in agents
    assert "more than one new implementation concept" not in agents


def test_workflow_routes_to_the_canonical_checklist_without_prompt_churn() -> None:
    agents = _read("AGENTS.md")
    workflow = _read("docs/AGENT_WORKFLOW.md")
    audit = _read("docs/AUDIT_WORKFLOW.md")
    normalized_agents = " ".join(agents.split())

    assert workflow.count("## Approval matrix") == 0
    assert "canonical task-routing and gate checklist" in workflow
    assert "../AGENTS.md#canonical-task-routing-and-gate-checklist" in workflow
    assert "Never require copied plan IDs,\nhashes, commands, or authorization text." in workflow
    assert "before repository changes; CLI or test invocations" in normalized_agents
    assert "Pure explanation and status work require only the evidence needed to answer" in normalized_agents
    assert "Use this checklist for every task" not in agents
    assert "## Action Classes" not in audit
    assert "## Handoffs" not in audit
    assert "Do not create or update it for ordinary same-thread work." in normalized_agents


def test_current_state_is_a_non_authoritative_snapshot_linked_from_orientation() -> None:
    readme = _read("README.md")
    state = _read("docs/CURRENT_STATE.md")

    assert "docs/CURRENT_STATE.md" in readme
    assert "not\nexecution authority" in state
    assert "current code, configuration, accepted\nreleases, Git state" in state
    assert "FREE_ONLY_DISCOVERY_AND_PROSPECTIVE_PATH_ACTIVE" in state
    assert "as `legacy_discovery_only`" in state
    assert "materialized\n  releases are not established" in state
    assert "No proxy artifact can enter trusted eligibility" in state
    assert "bounded SIP smoke capture and its non-active accepted release are" in state
    assert "unimplemented" not in state.lower()
    assert "## Operational routing" in state
    assert "canonical checklist in `AGENTS.md`" in state
    assert "## What can proceed" not in state


def test_current_state_matches_the_qualified_non_active_sip_source_contract() -> None:
    state = _read("docs/CURRENT_STATE.md")
    sources = json.loads(_read("config/sources.json"))
    alpaca = sources["sources"]["alpaca_basic_delayed_sip"]

    assert alpaca["request_contract"]["qualified_feed"] == "sip"
    assert alpaca["status"] == "qualified_sip_not_active"
    assert alpaca["enabled_for_active_pipeline"] is False
    assert "Alpaca SIP is the sole qualified bar feed, but remains non-active." in state


def test_free_only_path_is_machine_readable_and_keeps_trusted_gates_closed() -> None:
    readme = _read("README.md")
    outline = _read("PROJECT_OUTLINE.md")
    state = _read("docs/CURRENT_STATE.md")
    contract = json.loads(_read("config/research_readiness_contract.json"))
    path = contract["operating_path"]

    assert path["mode"] == "FREE_ONLY_LOCAL_GITHUB_DISCOVERY_AND_PROSPECTIVE"
    assert path["external_data_cost_policy"] == (
        "PAID_DATA_SUBSCRIPTIONS_PURCHASES_AND_COMMERCIAL_TRIALS_PROHIBITED"
    )
    assert path["storage_policy"] == "LOCAL_DESKTOP_WITH_GITHUB_BACKUP_ONLY"
    assert path["additional_hosted_infrastructure_allowed"] is False
    assert path["current_backend_state"] == (
        "BACKEND_UNSELECTED_NO_COMPLETE_FREE_BACKEND_ESTABLISHED"
    )
    assert path["future_free_evidence_is_self_authorizing"] is False
    assert path["trusted_gates"] == {
        "production_inputs": False,
        "outcome_access": False,
        "real_trial_registration": False,
        "training": False,
        "evaluation": False,
        "candidate_sealing": False,
        "source_activation": False,
        "production_readiness": False,
    }
    assert contract["readiness"]["current_state"] == (
        "FREE_ONLY_DISCOVERY_AND_PROSPECTIVE_FAIL_CLOSED"
    )
    for document in (readme, outline, state):
        assert "completely free" in document
    assert "raw-only provider-process-date evidence" in state
    assert "does not weaken the scientific contract" in outline


def test_orientation_and_outline_route_to_specialist_documents() -> None:
    readme = _read("README.md")
    outline = _read("PROJECT_OUTLINE.md")

    for document in (
        "NETWORK_ACQUISITION.md",
        "HISTORICAL_RESEARCH_HARNESS.md",
        "AUDIT_WORKFLOW.md",
    ):
        assert document in readme
    assert "Current milestone and selected next gate" in outline
    assert "Binding operation, revalidation, and approval boundaries" in outline
    assert "Ordinary multi-step workflow examples" in outline
    assert "canonical task-routing and gate checklist" in readme
    assert "Documentation of this roadmap is not permission to execute a phase." in outline


def test_audit_status_vocabulary_stays_in_the_audit_workflow() -> None:
    audit = _read("docs/AUDIT_WORKFLOW.md")

    for status in (
        "NOT_PREPARED",
        "PREPARED",
        "VALIDATED",
        "STARTED",
        "INCOMPLETE",
        "COMPLETE",
    ):
        assert status in audit


def test_recovery_policy_retains_the_same_principal_security_boundary() -> None:
    threat_model = _read("docs/FILESYSTEM_NAMESPACE_THREAT_MODEL.md")
    recovery = _read("docs/OUTCOME_LEDGER_ANCHOR_POLICY.md")

    assert "hostile process running as the same Windows account" in threat_model
    assert "outside this\nassurance boundary" in threat_model
    assert "operating-system sandbox" in threat_model
    assert "build_unanchored_tail_recovery_plan" in recovery
    assert "recover_unanchored_tail" in recovery
    assert "never changes or removes ledger bytes" in recovery
    assert "does not\nauthorize outcome creation or access" in recovery


class _CredentialReadTrap(dict[str, str]):
    def get(self, key: str, default: str | None = None) -> str | None:
        if key in {"APCA_API_KEY_ID", "APCA_API_SECRET_KEY"}:
            raise AssertionError("credentials were read before owner network confirmation")
        return super().get(key, default)


def test_identity_cli_rejects_network_before_reading_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        qualify_identity_sources.os,
        "environ",
        _CredentialReadTrap(),
    )
    monkeypatch.setattr(
        qualify_identity_sources,
        "build_alpaca_assets_request_plan",
        lambda _root: type("Plan", (), {"as_dict": lambda self: {}})(),
    )

    with pytest.raises(
        PermissionError,
        match="FREE_SOURCE_QUALIFICATION_APPROVED=YES",
    ):
        qualify_identity_sources.main(
            ["--execute-network", "--approved-plan-id", "a" * 64]
        )


def _unexpected_mutation(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("CLI default reached a mutating operation")


class _DryCopyPlan:
    def concise_summary(self) -> dict[str, object]:
        return {"entry_count": 0}

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(())


def test_mutation_capable_cli_adapters_default_to_nonmutating_plans(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        execute_legacy_purge,
        "prepare_purge_execution",
        lambda *_args, **_kwargs: {"plan": {}},
    )
    monkeypatch.setattr(execute_legacy_purge, "execute_purge", _unexpected_mutation)
    assert execute_legacy_purge.main(
        ["--plan-id", execute_legacy_purge.APPROVED_CLEANUP_PLAN_ID]
    ) == 0

    monkeypatch.setattr(hash_copy, "load_migration_config", lambda *_args: object())
    monkeypatch.setattr(hash_copy, "plan_migration", lambda *_args: _DryCopyPlan())
    monkeypatch.setattr(hash_copy, "execute_copy_plan", _unexpected_mutation)
    assert hash_copy.main(["--config", "unused.json"]) == 0

    monkeypatch.setattr(
        plan_alpaca_discovery_proxy_features,
        "build_feature_release_plan",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        plan_alpaca_discovery_proxy_features,
        "publish_feature_release",
        _unexpected_mutation,
    )
    assert plan_alpaca_discovery_proxy_features.main(
        [
            "--source-release-directory",
            "source",
            "--calendar-release-directory",
            "calendar",
        ]
    ) == 0

    monkeypatch.setattr(
        plan_alpaca_discovery_proxy_outcomes,
        "build_proxy_outcome_plan",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        plan_alpaca_discovery_proxy_outcomes,
        "publish_proxy_outcomes",
        _unexpected_mutation,
    )
    assert plan_alpaca_discovery_proxy_outcomes.main(
        [
            "--release-directory",
            "source",
            "--calendar-release-directory",
            "calendar",
        ]
    ) == 0

    monkeypatch.setattr(
        plan_alpaca_sip_non_active_cutover,
        "build_cutover_plan",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        plan_alpaca_sip_non_active_cutover,
        "execute_cutover",
        _unexpected_mutation,
    )
    assert plan_alpaca_sip_non_active_cutover.main([]) == 0

    monkeypatch.setattr(
        publish_alpaca_sip_qualification_receipt,
        "build_publication_plan",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        publish_alpaca_sip_qualification_receipt,
        "publish_receipt",
        _unexpected_mutation,
    )
    assert publish_alpaca_sip_qualification_receipt.main([]) == 0

    monkeypatch.setattr(
        publish_xnys_calendar_successor,
        "build_calendar_successor_plan",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        publish_xnys_calendar_successor,
        "publish_calendar_successor",
        _unexpected_mutation,
    )
    assert publish_xnys_calendar_successor.main([]) == 0

    monkeypatch.setattr(
        qualify_alpaca_sip,
        "build_qualification_plan",
        lambda **_kwargs: {},
    )
    monkeypatch.setattr(
        qualify_alpaca_sip,
        "execute_qualification_capture",
        _unexpected_mutation,
    )
    assert qualify_alpaca_sip.main([]) == 0
    assert "PLAN_ONLY" in capsys.readouterr().out


def test_write_only_cli_adapters_stop_before_mutation_without_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generate_legacy_cleanup_plan,
        "write_cleanup_plan",
        _unexpected_mutation,
    )
    with pytest.raises(SystemExit) as cleanup_exit:
        generate_legacy_cleanup_plan.main(["--expected-commit", "a" * 40])
    assert cleanup_exit.value.code == 2

    monkeypatch.setattr(
        publish_alpaca_historical_backfill,
        "publish_historical_backfill_release",
        _unexpected_mutation,
    )
    with pytest.raises(SystemExit) as publication_exit:
        publish_alpaca_historical_backfill.main(
            ["--created-at", "2026-08-08T00:00:00Z", "--approved-plan-id", "b" * 64]
        )
    assert publication_exit.value.code == 2
