from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.crsp_vendor_response_adjudication import (
    DECISION_STATUS,
    EVIDENCE_HIERARCHY,
    EXPECTED_GATES,
    EXPECTED_MISSING_DOCUMENTS,
    MISSING_REASON,
    RECORDS,
    load_crsp_vendor_response_adjudication_package,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.historical_provider_evaluation import (
    load_historical_provider_decision_package,
)


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> dict[str, object]:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _copy_package(tmp_path: Path) -> Path:
    for relative, _, _ in RECORDS.values():
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tmp_path


def _rehash(path: Path, id_field: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    unsigned = dict(payload)
    unsigned.pop(id_field)
    payload[id_field] = sha256_bytes(canonical_json_bytes(unsigned))
    path.write_bytes(canonical_json_bytes(payload))


def test_checked_in_crsp_readiness_package_is_content_addressed_and_blocked() -> None:
    summary = load_crsp_vendor_response_adjudication_package(ROOT)

    assert summary == {
        "status": "REQUIRES_FOLLOW_UP",
        "reason": "MISSING_VENDOR_RESPONSE_PACKAGE",
        "substantive_vendor_document_count": 0,
        "mandatory_gate_count": 22,
        "follow_up_question_count": 8,
        "future_ingestion_step_count": 28,
        "record_ids": {
            "intake": "cf89c6a910555d7dd29145f485d161a9381ab471c4e06b436a43096560cd6d28",
            "evidence": "e1f4fcb272e37c1ff396d3e078ba1e9a6f6817578257e6da6232f570dfe88e8e",
            "questions": "8743c9026e012835c9ca54f67633ffd78da7355f4f5c46a55f04ae6da937766e",
            "plan": "6fdc80f5bc1554594d3859e2f1f5f7cdd9a25ee1f54714041073f6f2914c4bc9",
            "decision": "dbf99be52b6a2833f2d7d4dc23b33b7dc8615621af9a8b5a89bacb488da80c82",
        },
    }


def test_intake_hashes_the_only_supplied_file_without_misclassifying_it() -> None:
    intake = _read("config/crsp_vendor_response_intake_v1.json")
    item = intake["non_vendor_inputs"][0]

    assert intake["substantive_vendor_document_count"] == 0
    assert intake["vendor_documents"] == []
    assert item["file_name"] == "pasted-text.txt"
    assert item["content_sha256"] == "b1a333b1a5663dfd3d5a6c4d14d193263a580df33d5254f4c5fe0e8474307b30"
    assert item["bytes"] == 47433
    assert item["classification"] == "PHASE_SPECIFICATION_NOT_VENDOR_EVIDENCE"
    assert item["binding_status"] == "AUTHORITATIVE_USER_PHASE_INSTRUCTION"


def test_intake_missing_document_census_is_exact_and_sensitive_safe() -> None:
    intake = _read("config/crsp_vendor_response_intake_v1.json")

    assert tuple(intake["missing_expected_documents"]) == EXPECTED_MISSING_DOCUMENTS
    assert intake["duplicate_groups"] == []
    assert intake["superseded_documents"] == []
    assert intake["sensitive_document_handling"] == {
        "committed_sensitive_vendor_documents": False,
        "originals_modified": False,
        "status": "NO_SENSITIVE_VENDOR_DOCUMENTS_RECEIVED",
        "tracked_content": "METADATA_AND_HASH_ONLY",
    }


def test_evidence_registry_is_empty_and_preserves_evidence_hierarchy() -> None:
    registry = _read("config/crsp_vendor_evidence_registry_v1.json")

    assert registry["vendor_evidence"] == []
    assert registry["vendor_evidence_status"] == "NO_VENDOR_EVIDENCE_RECEIVED"
    assert registry["adjudication_permitted"] is False
    assert tuple(registry["evidence_hierarchy"]) == EVIDENCE_HIERARCHY


def test_every_mandatory_gate_is_unresolved_without_vendor_evidence() -> None:
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")
    gates = decision["mandatory_gate_results"]

    assert tuple(item["gate_id"] for item in gates) == EXPECTED_GATES
    assert all(item["status"] == "UNRESOLVED" for item in gates)
    assert all(item["evidence_ids"] == [] for item in gates)
    assert all(item["required_question_ids"] for item in gates)


def test_follow_up_questions_cover_every_gate_and_remain_unsent() -> None:
    package = _read("config/crsp_follow_up_questions_v1.json")
    questions = package["questions"]
    covered = {gate for item in questions for gate in item["gate_ids"]}

    assert package["status"] == "NOT_SENT"
    assert package["do_not_send_automatically"] is True
    assert [item["question_id"] for item in questions] == [f"Q-{index:02d}" for index in range(1, 9)]
    assert covered == set(EXPECTED_GATES)
    assert all(item["current_evidence"].startswith("No ") for item in questions)


def test_missing_quote_produces_no_cost_arithmetic() -> None:
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")
    pricing = decision["pricing_adjudication"]

    assert pricing["status"] == "UNRESOLVED"
    assert all(value is None for key, value in pricing.items() if key != "status")


def test_archive_retention_and_eligibility_gates_remain_unresolved() -> None:
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")
    statuses = {
        item["gate_id"]: item["status"] for item in decision["mandatory_gate_results"]
    }

    assert statuses["G11_HISTORICAL_RELEASE_ARCHIVE"] == "UNRESOLVED"
    assert statuses["G17_BACKUP_RIGHTS"] == "UNRESOLVED"
    assert statuses["G18_POST_CANCELLATION_RETENTION"] == "UNRESOLVED"
    assert statuses["G19_INDIVIDUAL_ELIGIBILITY"] == "UNRESOLVED"


def test_contradictions_are_not_invented_without_vendor_documents() -> None:
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")

    assert decision["contradiction_analysis"] == {
        "status": "NOT_EVALUABLE_MISSING_VENDOR_RESPONSE_PACKAGE",
        "contradictions": [],
    }


def test_decision_prohibits_purchase_and_manifest_creation() -> None:
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")

    assert decision["status"] == DECISION_STATUS
    assert decision["reason"] == MISSING_REASON
    assert decision["purchase_now"] == "PROHIBITED"
    assert decision["recommended_purchase"] is None
    assert decision["conditional_acquisition_manifest_created"] is False
    assert decision["conditional_acquisition_manifest"] is None
    assert not (ROOT / "config/crsp_conditional_acquisition_manifest_v1.json").exists()


def test_future_ingestion_plan_has_28_non_executable_steps() -> None:
    plan = _read("config/crsp_future_ingestion_plan_v1.json")

    assert plan["plan_status"] == "NOT_EXECUTABLE_MISSING_VENDOR_RESPONSE_PACKAGE"
    assert [item["step"] for item in plan["steps"]] == list(range(1, 29))
    assert all(item["execution_authorized"] is False for item in plan["steps"])
    assert plan["steps"][-1]["name"] == "KEEP_REAL_OUTCOMES_DISABLED"


def test_phase_attestation_and_boundaries_deny_every_prohibited_action() -> None:
    for relative, _, _ in RECORDS.values():
        payload = _read(relative)
        assert all(value is False for value in payload["authorization_boundary"].values())
    decision = _read("config/crsp_purchase_authorization_decision_v1.json")
    assert all(value is False for value in decision["phase_execution_attestation"].values())


def test_outcome_firewall_remains_default_deny() -> None:
    firewall = _read("config/outcome_firewall_v1.json")

    assert firewall["real_outcome_access"] is False
    assert firewall["real_label_access"] is False
    assert firewall["holdout_access"] is False
    assert firewall["training_on_real_outcomes"] is False
    assert firewall["evaluation_on_real_outcomes"] is False
    assert firewall["backtesting"] is False
    assert firewall["broker_connectivity"] is False


def test_validator_has_no_transport_purchase_or_secret_implementation() -> None:
    text = (ROOT / "src/us_stocks_swing_model_v2/crsp_vendor_response_adjudication.py").read_text(encoding="utf-8")

    assert "requests" not in text
    assert "urllib.request" not in text
    assert "subprocess" not in text
    assert "api_key" not in text.casefold()
    assert "password" not in text.casefold()


def test_sensitive_vendor_document_types_are_not_added() -> None:
    prohibited_suffixes = {".doc", ".docx", ".eml", ".msg", ".pdf", ".rtf", ".xls", ".xlsx"}
    added = {
        Path(relative)
        for relative, _, _ in RECORDS.values()
    } | {
        Path("docs/CRSP_VENDOR_RESPONSE_ADJUDICATION.md"),
        Path("docs/CRSP_PURCHASE_AUTHORIZATION_DECISION.md"),
        Path("src/us_stocks_swing_model_v2/crsp_vendor_response_adjudication.py"),
        Path("tests/test_crsp_vendor_response_adjudication.py"),
    }

    assert not {path for path in added if path.suffix.casefold() in prohibited_suffixes}


def test_prior_provider_evaluation_checkpoint_still_loads() -> None:
    summary = load_historical_provider_decision_package(ROOT)

    assert summary["status"] == "NO_QUALIFIED_OPTION_FOUND"
    assert summary["provider_count"] == 4
    assert summary["requirement_count"] == 15


def test_duplicate_non_vendor_input_is_rejected_after_rehash(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_vendor_response_intake_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["non_vendor_inputs"][0])
    duplicate["input_id"] = "INPUT-002"
    payload["non_vendor_inputs"].append(duplicate)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "intake_id")

    with pytest.raises(ContractError, match="duplicate non-vendor input bytes"):
        load_crsp_vendor_response_adjudication_package(root)


def test_vendor_document_count_cannot_be_fabricated(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_vendor_response_intake_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["substantive_vendor_document_count"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "intake_id")

    with pytest.raises(ContractError, match="vendor-document count must be zero"):
        load_crsp_vendor_response_adjudication_package(root)


def test_decision_cannot_be_made_purchase_ready_after_rehash(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_purchase_authorization_decision_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "READY_FOR_USER_PURCHASE_AUTHORIZATION"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "decision_id")

    with pytest.raises(ContractError, match="must require follow-up"):
        load_crsp_vendor_response_adjudication_package(root)


def test_mandatory_gate_cannot_pass_without_vendor_evidence(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_purchase_authorization_decision_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["mandatory_gate_results"][0]["status"] = "PASS_TECHNICAL"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "decision_id")

    with pytest.raises(ContractError, match="cannot pass without vendor evidence"):
        load_crsp_vendor_response_adjudication_package(root)


def test_follow_up_cannot_omit_an_unresolved_gate(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_follow_up_questions_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["questions"][0]["gate_ids"].remove("G01_STABLE_HISTORICAL_SECURITY_IDENTITY")
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "question_package_id")

    with pytest.raises(ContractError, match="does not cover every unresolved gate"):
        load_crsp_vendor_response_adjudication_package(root)


def test_future_plan_step_cannot_be_authorized(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_future_ingestion_plan_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["steps"][0]["execution_authorized"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "plan_id")

    with pytest.raises(ContractError, match="cannot be executable"):
        load_crsp_vendor_response_adjudication_package(root)


def test_authorization_boundary_cannot_be_enabled_after_rehash(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_vendor_response_intake_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["authorization_boundary"]["authorizes_purchase"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    _rehash(path, "intake_id")

    with pytest.raises(ContractError, match="cannot authorize purchase or research"):
        load_crsp_vendor_response_adjudication_package(root)


def test_tampered_content_address_is_rejected(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/crsp_vendor_evidence_registry_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["public_evidence_reuse_policy"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IntegrityError, match="registry_id differs"):
        load_crsp_vendor_response_adjudication_package(root)
