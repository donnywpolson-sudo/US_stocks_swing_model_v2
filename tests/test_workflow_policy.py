from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_agents_retains_the_binding_action_and_safety_boundaries() -> None:
    agents = _read("AGENTS.md")

    for action_class in (
        "`LOCAL_CORRECTABLE`",
        "`READ_ONLY_INVOCATION`",
        "`MUTATING_OR_EXTERNAL`",
    ):
        assert action_class in agents
    for boundary in (
        "Do not commit, push, or cut over unless",
        "Never read, print, copy, move, edit, or commit secrets",
        "data/**",
        "accepted release",
    ):
        assert boundary in agents
    assert "docs/AGENT_WORKFLOW.md" in agents


def test_policies_prefer_verified_outcome_progress_over_surface_complexity() -> None:
    agents = _read("AGENTS.md")
    workflow = _read("docs/AGENT_WORKFLOW.md")

    assert "demonstrated progress toward the stated outcome" in agents
    assert "apparent sophistication, abstraction count" in agents
    assert "direct, well-tested solution that advances the outcome" in agents
    assert "concrete\noutcome or required safeguard it improves" in workflow
    assert "simplest robust approach\nthat produces verified progress" in workflow


def test_workflow_has_one_approval_matrix_and_no_prompt_churn() -> None:
    agents = _read("AGENTS.md")
    workflow = _read("docs/AGENT_WORKFLOW.md")
    handoff = _read("CODEX_HANDOFF.md")
    normalized_agents = " ".join(agents.split())
    normalized_handoff = " ".join(handoff.split())

    assert workflow.count("## Approval matrix") == 1
    for row in (
        "Local edits, read-only diagnostics, focused synthetic tests, and static checks",
        "Provider/network activity, generated releases or receipts, research",
        "Push, trading, or destructive work",
    ):
        assert row in workflow
    assert "Never require copied plan IDs,\nhashes, commands, or authorization text." in workflow
    assert "After two avoidable clarification or approval exchanges" in agents
    assert "After two avoidable clarification or approval exchanges" in workflow
    assert "Do not create or update it for ordinary same-thread work." in normalized_agents
    assert (
        "Do not use it for routine progress, continuation prompts, or handoff-only commits."
        in normalized_handoff
    )
    assert len(handoff.split()) <= 450


def test_current_state_is_a_non_authoritative_snapshot_linked_from_orientation() -> None:
    readme = _read("README.md")
    state = _read("docs/CURRENT_STATE.md")

    assert "docs/CURRENT_STATE.md" in readme
    assert "not\nexecution authority" in state
    assert "current code, configuration, accepted\nreleases, Git state" in state
    assert "ALPACA_HISTORICAL_BACKFILL_PUBLICATION_PLANNING_IMPLEMENTED" in state
    assert "backfill as a caveated `legacy_discovery_only` release" in state
    assert "first bounded active-SIP canonical-bars build is a separate pending path" in state
    assert "unimplemented" not in state.lower()


def test_current_state_matches_the_active_sip_source_contract() -> None:
    state = _read("docs/CURRENT_STATE.md")
    sources = json.loads(_read("config/sources.json"))
    alpaca = sources["sources"]["alpaca_basic_delayed_sip"]

    assert alpaca["request_contract"]["qualified_feed"] == "sip"
    assert alpaca["status"] == "active_sip_qualified_pending_canonical_bars"
    assert "Alpaca SIP is the selected, qualified bar feed." in state


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
