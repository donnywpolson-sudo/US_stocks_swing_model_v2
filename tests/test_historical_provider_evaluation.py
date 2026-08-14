from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import shutil

import pytest

from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.historical_provider_evaluation import (
    LICENSE_FIELDS,
    LICENSE_STATUSES,
    PROVIDERS,
    RECORDS,
    REQUIREMENTS,
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


def test_checked_in_provider_decision_package_is_content_addressed_and_blocked() -> None:
    summary = load_historical_provider_decision_package(ROOT)

    assert summary == {
        "status": "NO_QUALIFIED_OPTION_FOUND",
        "provider_count": 4,
        "requirement_count": 15,
        "matrix_cell_count": 60,
        "evidence_count": 32,
        "record_ids": {
            "evidence": "ccc02ada3e274f25d1acedb51a9adf1e1c3a0feae186dc7fc821a3d4f66a7b52",
            "evaluation": "bb7fb48b037eef9f174b51a5148ea45f11d391e54771947b35b512633969cf4e",
            "comparison": "2282436f2e5a69e340e89a6e1ebf3770350865c71e3f65c9cc561bb06e2f5a94",
            "decision": "eda1cc156a76235e3c3a6e84292c29d87233cc2ca197a84fd55f8a7e8a6a2e18",
            "plan": "e53e4c6ca5ea67a3cadaf11ff1bb149a8f5f2aa1f9d703dc3e3559a9cd1e45b3",
        },
    }


def test_evaluation_covers_every_required_provider_cell_once() -> None:
    evaluation = _read("config/historical_provider_evaluation_v1.json")
    cells = {
        (provider["provider_id"], cell["requirement_id"]): cell
        for provider in evaluation["providers"]
        for cell in provider["mandatory_evaluations"]
    }

    assert set(cells) == {(provider, requirement) for provider in PROVIDERS for requirement in REQUIREMENTS}
    assert len(cells) == 60
    for cell in cells.values():
        if cell["status"] == "PASS_PRIMARY_EVIDENCE":
            assert cell["evidence_ids"]
        if cell["status"] in {"FAIL", "UNRESOLVED"}:
            assert cell["resolution"]["purchase_prohibited_until_resolved"] is True
            assert cell["resolution"]["vendor_question"].endswith("?")


def test_declared_matrix_counts_match_independent_count() -> None:
    evaluation = _read("config/historical_provider_evaluation_v1.json")
    comparison = _read("config/historical_provider_comparison_v1.json")
    declared = comparison["mandatory_gate_counts"]

    for provider in evaluation["providers"]:
        actual = Counter(cell["status"] for cell in provider["mandatory_evaluations"])
        assert dict(actual) == {key: value for key, value in declared[provider["provider_id"]].items() if value}


def test_license_questions_use_exact_frozen_taxonomy() -> None:
    evaluation = _read("config/historical_provider_evaluation_v1.json")

    for provider in evaluation["providers"]:
        assert tuple(provider["license"]) == LICENSE_FIELDS
        assert set(provider["license"].values()) <= LICENSE_STATUSES


def test_final_decision_covers_every_unresolved_mandatory_cell() -> None:
    evaluation = _read("config/historical_provider_evaluation_v1.json")
    decision = _read("config/historical_acquisition_decision_v1.json")
    unresolved = {
        (provider["provider_id"], cell["requirement_id"])
        for provider in evaluation["providers"]
        for cell in provider["mandatory_evaluations"]
        if cell["status"] == "UNRESOLVED"
    }
    covered = {
        (question["provider_id"], requirement)
        for question in decision["mandatory_unresolved_items"]
        for requirement in question["requirement_ids"]
    }

    assert unresolved <= covered


def test_all_registered_evidence_is_current_official_primary_evidence() -> None:
    registry = _read("config/historical_provider_evidence_registry_v1.json")
    urls = [item["url"] for item in registry["evidence"]]

    assert len(urls) == len(set(urls)) == 32
    assert all(item["primary_source"] is True for item in registry["evidence"])
    assert all(item["retrieved_at"].startswith("2026-08-14T") for item in registry["evidence"])
    assert {item["provider_id"] for item in registry["evidence"]} == set(PROVIDERS)


def test_decision_prohibits_purchase_and_preserves_exact_questions() -> None:
    decision = _read("config/historical_acquisition_decision_v1.json")
    questions = decision["mandatory_unresolved_items"]

    assert decision["status"] == "NO_QUALIFIED_OPTION_FOUND"
    assert decision["recommended_purchase"] is None
    assert decision["purchase_now"] == "PROHIBITED"
    assert [item["question_id"] for item in questions] == [f"Q-{index:02d}" for index in range(1, 9)]
    assert all(item["mandatory"] and item["purchase_prohibited"] for item in questions)
    assert all(value is False for value in decision["phase_execution_attestation"].values())
    assert all(value is False for value in decision["authorization_boundary"].values())


def test_conditional_plan_is_non_executable_and_contains_exact_ingestion_sequence() -> None:
    plan = _read("config/historical_acquisition_plan_v1.json")

    assert plan["plan_status"] == "CONDITIONAL_NOT_EXECUTABLE"
    assert plan["provider_id"] == "CRSP"
    assert plan["product_code"] == "C6Z"
    assert all(item["status"] == "UNRESOLVED" for item in plan["purchase_prerequisites"])
    assert [item["step"] for item in plan["subsequent_ingestion_sequence"]] == list(range(1, 21))
    assert all(value is False for value in plan["authorization_boundary"].values())
    assert set(plan["quarantined_vendor_fields"]["fields"]) >= {"DlyRet", "DelRet"}


def test_provider_records_remain_bound_to_frozen_source_contracts() -> None:
    evaluation = _read("config/historical_provider_evaluation_v1.json")
    source_contract = _read("config/historical_source_contract_v1.json")
    admission_policy = _read("config/historical_source_admission_policy_v1.json")
    acquisition_requirements = _read("config/historical_source_acquisition_requirements_v1.json")

    assert evaluation["frozen_source_contract_id"] == source_contract["contract_id"]
    assert evaluation["frozen_admission_policy_id"] == admission_policy["policy_id"]
    assert evaluation["frozen_acquisition_requirements_id"] == acquisition_requirements["requirements_id"]


def test_tampered_evidence_fails_closed(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/historical_provider_evidence_registry_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"][0]["claim_supported"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IntegrityError, match="registry_id differs"):
        load_historical_provider_decision_package(root)


def test_missing_primary_evidence_reference_fails_closed(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/historical_provider_evaluation_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["providers"][0]["mandatory_evaluations"][0]["evidence_ids"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IntegrityError, match="evaluation_id differs"):
        load_historical_provider_decision_package(root)


def test_provider_package_has_no_transport_or_acquisition_implementation() -> None:
    module_text = (ROOT / "src/us_stocks_swing_model_v2/historical_provider_evaluation.py").read_text(encoding="utf-8")

    assert "requests" not in module_text
    assert "urllib.request" not in module_text
    assert "subprocess" not in module_text
    assert "api_key" not in module_text.casefold()
    assert "password" not in module_text.casefold()


def test_outcome_firewall_remains_default_deny() -> None:
    firewall = _read("config/outcome_firewall_v1.json")

    assert firewall["real_outcome_access"] is False
    assert firewall["real_label_access"] is False
    assert firewall["holdout_access"] is False
    assert firewall["training_on_real_outcomes"] is False
    assert firewall["evaluation_on_real_outcomes"] is False
    assert firewall["backtesting"] is False
    assert firewall["broker_connectivity"] is False


def test_non_official_or_duplicate_evidence_url_is_rejected(tmp_path: Path) -> None:
    root = _copy_package(tmp_path)
    path = root / "config/historical_provider_evidence_registry_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["evidence"][0]["url"] = "https://example.com/provider-claim"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(IntegrityError, match="registry_id differs"):
        load_historical_provider_decision_package(root)


def test_authorization_boundary_cannot_be_enabled_even_if_record_is_rehashed(tmp_path: Path) -> None:
    from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes

    root = _copy_package(tmp_path)
    path = root / "config/historical_provider_evidence_registry_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["authorization_boundary"]["authorizes_purchase"] = True
    unsigned = dict(payload)
    unsigned.pop("registry_id")
    payload["registry_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    path.write_bytes(canonical_json_bytes(payload))

    with pytest.raises(ContractError, match="cannot authorize acquisition or research"):
        load_historical_provider_decision_package(root)
