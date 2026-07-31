from __future__ import annotations

from pathlib import Path

import pytest

from us_stocks_swing_model_v2.alpaca_legacy_discovery_downstream import build_downstream_plan, load_contract
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest


REPO = Path(__file__).resolve().parents[1]


def _release(tmp_path: Path) -> tuple[Path, Path]:
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "bars").mkdir()
    (stage / "bars" / "year=2016.parquet").write_bytes(b"synthetic")
    (stage / "source_evidence_manifest.json").write_bytes(canonical_json_bytes({
        "input_quality_state": "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
        "historical_membership_proven": False,
        "point_in_time_safe": False,
        "survivorship_safe": False,
    }))
    manifest = build_manifest(
        stage, ("bars/year=2016.parquet", "source_evidence_manifest.json"),
        project="US_stocks_swing_model_v2", dataset="alpaca_historical_daily_bars",
        source_epoch="alpaca_sip_current_identity_seeded_20160104_20260710_v1",
        role="legacy_discovery_only", quality_state="LEGACY_CAVEATED",
        created_at="2026-07-31T20:00:00Z", row_count=1,
        event_start="2016-01-04", event_end="2016-01-04", schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    accepted = (tmp_path / "accepted").resolve()
    return AtomicReleasePublisher(accepted).publish(stage, manifest), accepted


def test_downstream_plan_is_metadata_only_and_caveated(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    plan = build_downstream_plan(release, accepted_root=accepted, repo_root=REPO)
    assert plan["release"]["release_id"] == release.name
    assert plan["eligibility"]["trusted_sleeves"] == []
    assert plan["outcomes"]["may_compute"] is False
    assert plan["discovery_proxy"]["historical_proxy"] is True
    assert plan["discovery_proxy"]["canonical_target_equivalent"] is False
    assert plan["discovery_proxy"]["training_or_evaluation_authorized"] is False
    assert plan["wfa"]["real_history_execution_authorized"] is False
    assert plan["metadata_validation_scope"]["bar_rows_opened"] == 0


def test_downstream_plan_rejects_source_caveat_drift(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    evidence = release / "source_evidence_manifest.json"
    evidence.write_bytes(canonical_json_bytes({"input_quality_state": "PASS"}))
    with pytest.raises((ContractError, IntegrityError)):
        build_downstream_plan(release, accepted_root=accepted, repo_root=REPO)


def test_contract_is_content_addressed() -> None:
    contract = load_contract(REPO)
    assert len(contract["contract_id"]) == 64
