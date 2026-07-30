from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agent_policy_distinguishes_local_and_high_risk_action_classes() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    assert "`LOCAL_CORRECTABLE`" in agents
    assert "`READ_ONLY_INVOCATION`" in agents
    assert "`MUTATING_OR_EXTERNAL`" in agents
    assert "up to two materially corrective edit-and-validation cycles" in agents
    assert "Do not commit, push, or cut over unless" in agents
    assert "provider" in agents.lower()
    assert "destructive" in agents.lower()


def test_same_thread_work_does_not_require_handoff_or_prompt_churn() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    handoff = (ROOT / "CODEX_HANDOFF.md").read_text(encoding="utf-8")

    assert "do not let stale coordination prose block safe" in agents
    assert "Avoid handoff-only commits for routine same-thread transitions" in agents
    assert "Do not emit a continuation prompt merely because" in agents
    assert "except `Plan Prompt`" not in agents
    assert "Do not create routine same-thread handoff-only commits" in handoff
    assert len(handoff.split()) <= 450


def test_audit_status_vocabulary_and_four_field_result_are_durable() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "AUDIT_WORKFLOW.md").read_text(encoding="utf-8")

    for field in ("Master Audit", "Meta Audit", "Project readiness", "Next gate"):
        assert field in agents
        assert field in workflow
    for status in (
        "NOT_PREPARED",
        "PREPARED",
        "VALIDATED",
        "STARTED",
        "INCOMPLETE",
        "COMPLETE",
    ):
        assert status in agents
        assert status in workflow


def test_meta_controller_separates_preparation_from_review_attempt() -> None:
    controller = (ROOT / "META_MASTER_AUDIT.md").read_text(encoding="utf-8")
    normalized = " ".join(controller.split())

    assert "Preparation is not a reviewer invocation" in controller
    assert "at most 400 numbered lines" in controller
    assert "20,000 UTF-8 bytes" in controller
    assert "no inherited turns" in normalized
    assert "no prior target access" in normalized


def test_future_workflows_bundle_local_work_and_trigger_durable_retrospectives() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(encoding="utf-8")
    normalized_agents = " ".join(agents.split())

    assert "bounded phase envelope" in agents
    assert "coalesce permissions" in agents
    assert "Before freezing a content-addressed plan" in agents
    assert "more than two avoidable user round trips" in agents
    assert "a handoff-only commit during a live thread" in normalized_agents
    assert "Do not answer a systemic failure with another one-off" in workflow
    assert "deterministic maximal safe batches" in workflow
    assert "unchanged safety and evidence quality" in workflow


def test_meta_audit_dispatch_and_transport_failure_are_fail_closed() -> None:
    agent_workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    audit_workflow = (ROOT / "docs" / "AUDIT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )

    assert "target-content-free" in agent_workflow
    assert "must not inspect" in agent_workflow
    assert "outside a declared reader command" in agent_workflow
    assert "completion footer" in agent_workflow
    assert "target-content-free dispatch" in audit_workflow
    assert "external measurement command" in audit_workflow
    assert "missing footer means transport truncation" in audit_workflow
