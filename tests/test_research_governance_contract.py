from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "config" / "research_readiness_contract.json"
DOC_PATH = ROOT / "docs" / "HISTORICAL_RESEARCH_HARNESS.md"
MONITORING_POLICY_PATH = ROOT / "config" / "prospective_monitoring_policy.json"
MASTER_AUDIT_PATH = ROOT / "MASTER_AUDIT.md"
META_MASTER_AUDIT_PATH = ROOT / "META_MASTER_AUDIT.md"
TEST_EVIDENCE_POLICY_PATH = ROOT / "docs" / "TEST_EXECUTION_EVIDENCE_POLICY.md"


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_controlled_rebuild_authority_and_repo_independence_are_explicit() -> None:
    contract = _contract()
    authorization = contract["authorization"]
    assert authorization["this_contract_grants_execution_authority"] is False
    assert authorization["owner_operated_mode"] == "LOCAL_INTEGRITY_RECORDS"
    assert authorization["external_authorization_receipt_required"] is False
    assert authorization["local_integrity_record_schema_version"] == 2
    assert authorization["local_integrity_record_required"] is True
    assert set(authorization["current_controlled_rebuild_receipt_allows"]) == {
        "approved_hash_copy",
        "non_alpha_data_validation",
        "synthetic_fixture_model_fitting",
        "synthetic_fixture_wfa",
        "bounded_free_alpaca_qualification",
        "bounded_free_nasdaq_qualification",
    }
    assert set(authorization["hard_pauses_requiring_new_user_authorization"]) == {
        "paid_data_acquisition",
        "real_history_hypothesis_or_wfa_execution",
        "candidate_sealing",
        "destructive_cutover",
        "external_push",
        "trading",
        "legacy_repository_write",
    }
    assert authorization["legacy_repositories_read_only"] is True
    independence = contract["independence"]
    assert independence["shared_mutable_data_paths"] is False
    assert independence["shared_trial_artifact_or_state_paths"] is False
    assert independence["no_cross_import_test_required"] is True
    assert independence["no_cross_write_test_required"] is True


def test_harness_readiness_is_separate_from_pit_evidence_scope() -> None:
    readiness = _contract()["readiness"]
    assert readiness["target_state"] == "HISTORICAL_RESEARCH_READY"
    assert readiness["historical_evidence_scope"] == (
        "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    )
    assert readiness["candidate_eligibility"] == "BLOCKED_PENDING_PROSPECTIVE_PIT"
    assert readiness["alpha_claim"] is False
    assert readiness["live_readiness"] is False
    assert readiness["readiness_is_execution_authority"] is False


def test_source_epochs_and_free_feed_qualification_are_binding() -> None:
    sources = _contract()["source_roles"]
    assert sources["hfdl_history"] == "legacy_discovery_only"
    assert sources["hfdl_epochs"] == [
        {"id": "HFDL_PITRADING_CONSOLIDATED", "through": "2022-03-03"},
        {"id": "HFDL_IEX_ONLY", "from": "2022-03-04"},
    ]
    assert sources["hfdl_epochs_may_be_silently_pooled_as_identical_feed"] is False
    assert sources["failed_legacy_alpaca_capsules"] == (
        "qualification_evidence_only_not_research_bars"
    )
    assert sources["actual_free_feed_requires_entitlement_qualification_receipt"]
    assert sources["assumed_feed_allowed"] is False
    assert sources["asof_policy"].startswith("omit_or_null")


def test_trial_holdout_chronology_and_pit_sleeves_fail_closed() -> None:
    contract = _contract()
    ledger = contract["trial_ledger"]
    assert ledger["append_contract"]["whole_chain_rewrite_allowed"] is False
    assert ledger["pre_outcome_anchor"]["required"] is True
    assert ledger["legacy_trial_census"]["unresolved_status"] == (
        "INVALID_TRIAL_CENSUS_UNRESOLVED"
    )
    holdout = contract["nested_wfa"]["final_holdout"]
    assert holdout["outer_oos_role"] == "screen_only"
    assert holdout["one_time_access"] is True
    assert holdout["pooled_for_selection"] is False
    assert holdout["retune_rescue_retry_or_reuse_allowed"] is False
    assert holdout["pass_authorizes_candidate_sealing"] is False
    assert contract["sample_contract"]["required_time_order"] == (
        "feature_window_start <= feature_available_at <= decision_at "
        "< intended_entry_at = label_start_at < label_end_at = intended_exit_at"
    )
    sleeves = contract["sleeves"]
    assert sleeves["proxy_assigned_sleeves_while_pit_unresolved"] == "diagnostic_only"
    assert sleeves["proxy_sleeves_may_satisfy_trusted_gate"] is False


def test_multiple_testing_book_charter_and_mees_gate_are_binding() -> None:
    contract = _contract()
    assert contract["forecast_metrics"]["binding_primary"] == "multiclass_log_loss"
    multiple = contract["multiple_testing"]
    assert "every_negative_control" in multiple["family_scope"]
    assert (
        multiple["outcome_informed_family_split_metric_substitution_or_class_choice_allowed"]
        is False
    )
    assert multiple["pbo"]["not_applicable_receives_positive_credit"] is False
    assert multiple["pbo"]["diagnostic_may_replace_chronological_wfa"] is False
    frozen = contract["economic_translation"]["charter_must_freeze_before_outer_access"]
    assert "weight_construction" in frozen
    assert "missing_exit_and_delisting_handling" in frozen
    assert contract["power"]["design_alternative_strictly_greater_than_mees_required"]
    gate = contract["binding_gate"]
    assert gate["decision_order"][-1] == "PASS_HISTORICAL_DISCOVERY_SCREEN"
    screen = gate["PASS_HISTORICAL_DISCOVERY_SCREEN"]
    assert screen["multiplicity_adjusted_one_sided_95pct_lower_bound_gt_mees"]
    assert screen["candidate_sealed"] is False
    assert screen["candidate_eligibility_while_pit_unresolved"] == (
        "BLOCKED_PENDING_PROSPECTIVE_PIT"
    )


def test_document_does_not_convert_readiness_or_screen_into_alpha_authority() -> None:
    text = DOC_PATH.read_text(encoding="utf-8")
    normalized = " ".join(text.split()).replace("- ", "-")
    assert "Harness readiness and evidence scope are independent." in normalized
    assert "A diagnostic screen pass does not seal a candidate" in normalized
    assert "all writes to the legacy repository remain hard-paused" in normalized
    assert "Outer OOS is a screen, not the final holdout." in normalized


def test_audit_command_manifests_fail_closed_without_supplementary_imports() -> None:
    master = MASTER_AUDIT_PATH.read_text(encoding="utf-8")
    meta = META_MASTER_AUDIT_PATH.read_text(encoding="utf-8")
    policy = TEST_EVIDENCE_POLICY_PATH.read_text(encoding="utf-8")

    master_normalized = " ".join(master.split())
    meta_normalized = " ".join(meta.split())
    policy_normalized = " ".join(policy.split())

    for text in (master_normalized, meta_normalized, policy_normalized):
        assert "ordered command manifest" in text
        assert "`src/` package layout" in text
        assert "supplementary import" in text
        assert "undeclared command" in text.lower()
        assert "unexpected nonzero exit" in text.lower()

    assert (
        "stop before any later command or report publication"
        in master_normalized
    )
    assert (
        "Preserve partial evidence, publish nothing, and stop"
        in meta_normalized
    )
    assert (
        "stops before later commands or report publication"
        in policy_normalized
    )
    assert "REPORTABLE_TEST_RESULT" in meta_normalized


def test_meta_audit_keeps_specification_and_project_verdicts_separate() -> None:
    meta = META_MASTER_AUDIT_PATH.read_text(encoding="utf-8")
    normalized = " ".join(meta.split())

    assert "Mode: `MASTER_SPECIFICATION_REVIEW`" in normalized
    assert (
        "The Meta Audit grades defects in the Master specification. "
        "It does not grade the project's present readiness"
    ) in normalized
    assert (
        "Missing current executable evidence is not a Meta finding when the "
        "Master explicitly requires it"
    ) in normalized
    assert (
        "The Meta review does not execute Master gates or require current "
        "runtime evidence to exist."
    ) in normalized
    assert "The review must not apply those amendments." in normalized


def test_robustness_and_monitoring_governance_are_binding_and_fail_closed() -> None:
    contract = _contract()
    robustness = contract["robustness"]
    assert robustness["policy_hash_must_be_bound_to_trial_and_evaluation"] is True
    assert (
        robustness[
            "evidence_hash_must_be_bound_to_gate_receipt_evaluation_record_and_bundle"
        ]
        is True
    )
    assert robustness["explicit_sleeve_and_book_state"] == "INCONCLUSIVE_ROBUSTNESS"
    assert robustness["definite_failure_precedes_inconclusive_robustness"] is True
    order = contract["binding_gate"]["decision_order"]
    assert order.index("FAIL_MULTIPLICITY_OR_CONTROL") < order.index(
        "INCONCLUSIVE_ROBUSTNESS"
    )

    monitoring = contract["prospective_monitoring"]
    assert monitoring["records_append_only_and_bundle_bound"] is True
    assert monitoring["records_locally_head_anchored"] is True
    assert monitoring["recovery_scope"] == "AUTHORIZE_MONITORING_RECOVERY"
    assert monitoring["automatic_retraining_retuning_source_substitution_or_resume"] is False
    policy = json.loads(MONITORING_POLICY_PATH.read_text(encoding="utf-8"))
    assert policy["policy_version"] == (
        "1.2.0-owner-operated-local-integrity"
    )
    assert policy["state_precedence"] == [
        "MONITORING_INVALID",
        "MONITORING_PAUSED",
        "MONITORING_PENDING",
        "MONITORING_WARNING",
        "MONITORING_OK",
    ]
    assert policy["record_contract"]["append_only_hash_chain"] is True
    assert policy["record_contract"]["automatic_actions_must_be_empty"] is True
    assert policy["recovery"]["automatic_resume"] is False
    assert policy["recovery"]["local_integrity_scope"] == (
        "AUTHORIZE_MONITORING_RECOVERY"
    )
    assert (
        policy["recovery"]["owner_review_record_required_after_paused_or_invalid"]
        is True
    )
