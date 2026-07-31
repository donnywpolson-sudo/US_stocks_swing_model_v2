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
    normalized_agents = " ".join(agents.split()).lower()

    assert "do not let stale coordination prose block safe" in agents
    assert "Avoid handoff-only commits for routine same-thread transitions" in agents
    assert "within the same thread, continue safe" in normalized_agents
    assert "never require the user to copy hashes, commands" in normalized_agents
    assert "A Continue Prompt never grants authority" in agents
    assert "Do not emit a `Continue Prompt` during ordinary same-thread work" in agents
    assert "Do not create routine same-thread handoff-only commits" in handoff
    assert len(handoff.split()) <= 450


def test_substantive_results_use_plain_language_checkpoint_structure() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

    headings = (
        "`Status`",
        "`Completed`",
        "`Checks`",
        "`Needs attention`",
        "`Checkpoint`",
    )
    positions = [agents.index(heading) for heading in headings]

    assert positions == sorted(positions)
    assert "Use this order for every substantive same-thread final response and report" in agents
    assert "use exactly `done`, `in progress`, or `blocked`" in agents
    assert "what Codex accomplished in plain English" in agents
    assert "whether it passed" in agents
    assert "only the one current blocker, decision, or exact" in agents
    assert "one literal approval line" in agents
    assert "current outcome, blocker, and next meaningful" in agents
    assert "Do not emit a `Continue Prompt` during ordinary same-thread work" in agents
    assert "only when the user explicitly requests a new-thread handoff" in agents
    assert "never ask the\n  user to paste a continuation prompt" in agents
    assert "optional, self-contained handoff for a different" in agents
    assert "must not\n  compete with `Needs attention`" in agents
    assert "After the user provides an exact approval" in agents
    assert "resume\n  the same outcome-level plan automatically" in agents
    assert "name the overall goal and completion condition" in agents
    assert "what has\n  already been completed" in agents
    assert "current blocker or next action" in agents
    assert "recheck the live repository evidence" in agents
    assert "until\n  completion or the next genuine approval boundary" in agents
    assert "Plan Prompt" not in agents


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
    assert "A plain-language request to implement or fix a local phase" in agents
    assert "coalesce permissions" in agents
    assert "one bounded invocation" in agents
    assert '"Yes" or "Run that gate"' in agents
    assert "one separately authorized commit" in agents
    assert "Before freezing a content-addressed plan" in agents
    assert "more than two avoidable user round trips" in agents
    assert "a handoff-only commit during a live thread" in normalized_agents
    assert "Do not answer a systemic failure with another one-off" in workflow
    assert "deterministic maximal safe batches" in workflow
    assert "unchanged safety and evidence quality" in workflow


def test_windows_powershell_51_json_transport_is_preflighted() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(encoding="utf-8")

    for contract in (agents, workflow):
        assert "Windows PowerShell 5.1" in contract
        assert "ConvertFrom-Json" in contract
        assert "unsupported" in contract
        assert "synthetic" in contract
        assert "before provider access" in " ".join(contract.split())


def test_high_reasoning_requires_the_smallest_proportional_design() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(encoding="utf-8")
    normalized_agents = " ".join(agents.split())
    normalized_workflow = " ".join(workflow.split())

    assert "simplest correct design" in normalized_agents
    assert "Start with the existing implementation" in normalized_agents
    assert "speculative extension points" in normalized_agents
    assert "duplicate planners or validators" in normalized_agents
    assert "more than five tracked paths" in normalized_agents
    assert "smallest correct solution" in normalized_workflow
    assert "one direct change over a new module" in normalized_workflow
    assert "generic framework for one use" in normalized_workflow
    assert "internal simplicity checkpoint" in normalized_workflow
    assert (
        "Simplicity never weakens an evidence or safety gate"
        in normalized_workflow
    )


def test_external_phase_covers_verification_without_approval_essay() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(encoding="utf-8")
    normalized_agents = " ".join(agents.split())
    normalized_workflow = " ".join(workflow.split())

    for phrase in (
        "metadata-only preflight",
        "declared one-time credential handling",
        "atomic landing",
        "no-write verification or assessment",
        "final conversation report",
    ):
        assert phrase in normalized_agents
        assert phrase in normalized_workflow
    assert "Never require the user to copy plan IDs, hashes" in workflow
    assert "Activation and cutover remain a separate gate" in normalized_workflow


def test_meta_reviewer_transport_remains_frozen_until_capability_changes() -> None:
    audit_workflow = (ROOT / "docs" / "AUDIT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(audit_workflow.split())

    assert "Reviewer Transport Freeze" in audit_workflow
    assert "While that host capability is unchanged" in normalized
    assert "target-free synthetic transport check" in normalized
    assert "unchanged and checksum-verified" in normalized
    assert "without a retained dispatch file or undeclared process" in normalized


def test_meta_audit_dispatch_and_transport_failure_are_fail_closed() -> None:
    agent_workflow = (ROOT / "docs" / "AGENT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    audit_workflow = (ROOT / "docs" / "AUDIT_WORKFLOW.md").read_text(
        encoding="utf-8"
    )
    normalized_agent_workflow = " ".join(agent_workflow.split())

    assert "target-content-free" in agent_workflow
    assert "must not inspect" in agent_workflow
    assert "outside a declared reader command" in agent_workflow
    assert "completion footer" in agent_workflow
    assert "entire canonical dispatch" in agent_workflow
    assert "positional `argv` assumptions" in normalized_agent_workflow
    assert "target-content-free dispatch" in audit_workflow
    assert "external measurement command" in audit_workflow
    assert "missing footer means transport truncation" in audit_workflow
    assert "without projection or reconstruction" in audit_workflow


def test_hfdl_retirement_and_alpaca_rehabilitation_are_durable() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    outline = (ROOT / "PROJECT_OUTLINE.md").read_text(encoding="utf-8")
    harness = (ROOT / "docs" / "HISTORICAL_RESEARCH_HARNESS.md").read_text(
        encoding="utf-8"
    )
    sources = (ROOT / "config" / "sources.json").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    network = (ROOT / "docs" / "NETWORK_ACQUISITION.md").read_text(
        encoding="utf-8"
    )

    assert "HF Data Library is retired and excluded" in agents
    assert "Never mix HFDL with" in agents
    assert "HFDL is retired and excluded" in outline
    assert "HFDL bridge is retired" in harness
    assert (
        "ALPACA_HISTORICAL_BACKFILL_PUBLICATION_PLANNING_IMPLEMENTED" in outline
    )
    assert "hfdl_retirement_policy.json" in sources
    assert "alpaca_archive_rehabilitation_policy.json" in sources
    assert "alpaca_archive_rehabilitation_publication_policy.json" in sources
    assert "validated 198 compressed payload pages" in harness
    assert "regenerated" in readme
    assert "deterministic Parquet" in readme
    assert "Plan-only Alpaca SIP historical backfill" in network
    assert "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED" in network
    assert "fresh single-use local network session" in network
    assert "verified before the next unit begins" in network
    assert "exact numeric `vw=0` sentinel" in network
    assert "without changing the retained raw response" in network
    assert "--plan-group-continuation" in network
    assert "calls Alpaca only for the" in network
    assert "Offline completeness and publication planning" in network
    assert "plan_alpaca_historical_backfill_publication" in network
    assert "release ID" in network
    normalized_network = " ".join(network.split())
    assert "computes the complete release ID without writing" in normalized_network
    assert "Publication execution is implemented but remains separately authorized" in normalized_network
