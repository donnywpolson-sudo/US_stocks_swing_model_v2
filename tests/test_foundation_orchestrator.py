from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

import pytest

import test_hfdl_legacy_publisher as hfdl_support
import us_stocks_swing_model_v2.foundation_orchestrator as orchestrator_module
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.canonical.hfdl_legacy_publisher import (
    HfdlPublishContract,
)
from us_stocks_swing_model_v2.cli import build_historical_foundation as cli_module
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.exchange_calendar import publish_xnys_calendar_release
from us_stocks_swing_model_v2.foundation_orchestrator import (
    AGGREGATE_COMPONENT_COUNT,
    AGGREGATE_DATASET,
    run_stock_historical_foundation,
)
from us_stocks_swing_model_v2.historical_foundation import HFDL_EPOCHS, OUTPUT_KINDS
from us_stocks_swing_model_v2.releases import verify_accepted_release


CREATED_AT = "2026-07-15T00:00:00Z"


@pytest.fixture
def orchestrator_tmp() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="sfo-"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _inputs(root: Path):
    migration = hfdl_support._completed_migration_release(root / "migration")
    permit = hfdl_support._permit("stock-foundation-orchestrator")
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    accepted = root / "accepted"
    calendar = publish_xnys_calendar_release(
        staging_root=root / "calendar-stage",
        release_root=accepted,
        start=date(2022, 2, 25),
        end=date(2022, 3, 15),
        created_at=CREATED_AT,
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
    )
    return migration, permit, contract, accepted, calendar


def _execution_kwargs(root: Path) -> dict[str, object]:
    return {
        "execution_synthetic_permit": SyntheticOnlyPermit.create(
            fixture_id="stock-foundation-orchestrator",
            scope=orchestrator_module.SYNTHETIC_EXECUTION_SCOPE,
        ),
        "execution_allowed_root": root,
    }


def _run(root: Path):
    migration, permit, contract, accepted, calendar = _inputs(root)
    result = run_stock_historical_foundation(
        migration_release_directory=migration,
        accepted_release_root=accepted,
        derived_work_root=root / "work",
        created_at=CREATED_AT,
        calendar_release_directory=calendar,
        hfdl_contract=contract,
        hfdl_synthetic_permit=permit,
        **_execution_kwargs(root),
    )
    return migration, permit, contract, accepted, calendar, result


def test_orchestrator_publishes_only_verified_non_active_foundation(
    orchestrator_tmp: Path,
) -> None:
    migration, permit, contract, accepted, calendar, result = _run(orchestrator_tmp)
    manifest = verify_accepted_release(
        result.aggregate_set_release_directory, accepted_root=accepted
    )
    assert manifest.dataset == AGGREGATE_DATASET
    assert manifest.row_count == AGGREGATE_COMPONENT_COUNT == 11
    receipt = json.loads(
        (result.aggregate_set_release_directory / "foundation_set.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["migration"]["manifest_schema_version"] == 2
    assert receipt["migration"]["payload_layout_version"] == "flat_object_160bit_v1"
    assert receipt["historical_evidence_scope"] == "LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    for field in (
        "point_in_time_safe",
        "epochs_may_be_pooled",
        "provider_calls_made",
        "legacy_paths_read",
        "model_or_evaluation_inputs_read",
        "real_history_hypothesis_executed",
        "wfa_executed",
        "labels_emitted",
        "matured_outcomes_emitted",
        "alpha_evidence",
        "candidate_eligible",
    ):
        assert receipt[field] is False
    rows = [
        json.loads(line)
        for line in (
            result.aggregate_set_release_directory / "foundation_index.jsonl"
        ).read_bytes().splitlines()
    ]
    assert [row["sequence"] for row in rows] == list(range(11))
    assert len({row["release_id"] for row in rows}) == 11
    assert set(result.hfdl_publication.epoch_release_directories) == set(HFDL_EPOCHS)
    assert sum(
        len(kinds)
        for kinds in result.historical_foundation.epoch_release_directories.values()
    ) == len(HFDL_EPOCHS) * len(OUTPUT_KINDS) == 6

    rerun = run_stock_historical_foundation(
        migration_release_directory=migration,
        accepted_release_root=accepted,
        derived_work_root=orchestrator_tmp / "work",
        created_at=CREATED_AT,
        calendar_release_directory=calendar,
        hfdl_contract=contract,
        hfdl_synthetic_permit=permit,
        **_execution_kwargs(orchestrator_tmp),
    )
    assert rerun == result
    checkpoint_path = next(
        (orchestrator_tmp / "work" / "o").glob(
            "*/checkpoint.json"
        )
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["state"] == "COMPLETE_NON_ACTIVE_FOUNDATION"
    assert all(checkpoint["phases"][phase] is not None for phase in checkpoint["phases"])


@pytest.mark.parametrize("crash_phase", ["hfdl", "bridge", "aggregate"])
def test_orchestrator_resumes_after_each_published_phase_before_checkpoint(
    orchestrator_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_phase: str,
) -> None:
    migration, permit, contract, accepted, calendar = _inputs(orchestrator_tmp)
    original = orchestrator_module._record_phase
    crashed = False

    def crash_before_record(checkpoint_path, checkpoint, phase, binding):
        nonlocal crashed
        if phase == crash_phase and not crashed:
            crashed = True
            raise RuntimeError(f"synthetic {phase} checkpoint interruption")
        return original(checkpoint_path, checkpoint, phase, binding)

    monkeypatch.setattr(orchestrator_module, "_record_phase", crash_before_record)
    with pytest.raises(RuntimeError, match="checkpoint interruption"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
            calendar_release_directory=calendar,
            hfdl_contract=contract,
            hfdl_synthetic_permit=permit,
            **_execution_kwargs(orchestrator_tmp),
        )
    monkeypatch.setattr(orchestrator_module, "_record_phase", original)
    result = run_stock_historical_foundation(
        migration_release_directory=migration,
        accepted_release_root=accepted,
        derived_work_root=orchestrator_tmp / "work",
        created_at=CREATED_AT,
        calendar_release_directory=calendar,
        hfdl_contract=contract,
        hfdl_synthetic_permit=permit,
        **_execution_kwargs(orchestrator_tmp),
    )
    verify_accepted_release(result.aggregate_set_release_directory, accepted_root=accepted)


def test_checkpoint_and_accepted_aggregate_tamper_fail_closed(
    orchestrator_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, permit, contract, accepted, calendar = _inputs(orchestrator_tmp)
    original = orchestrator_module._record_phase

    def crash_hfdl(checkpoint_path, checkpoint, phase, binding):
        if phase == "hfdl":
            raise RuntimeError("checkpoint tamper setup")
        return original(checkpoint_path, checkpoint, phase, binding)

    monkeypatch.setattr(orchestrator_module, "_record_phase", crash_hfdl)
    with pytest.raises(RuntimeError, match="tamper setup"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
            calendar_release_directory=calendar,
            hfdl_contract=contract,
            hfdl_synthetic_permit=permit,
            **_execution_kwargs(orchestrator_tmp),
        )
    checkpoint_path = next(
        (orchestrator_tmp / "work" / "o").glob(
            "*/checkpoint.json"
        )
    )
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["phases"]["migration"]["plan_id"] = "0" * 64
    unsigned = dict(checkpoint)
    unsigned.pop("checkpoint_id")
    checkpoint["checkpoint_id"] = sha256_bytes(canonical_json_bytes(unsigned))
    checkpoint_path.write_bytes(canonical_json_bytes(checkpoint))
    monkeypatch.setattr(orchestrator_module, "_record_phase", original)
    with pytest.raises(IntegrityError, match="checkpoint evidence differs"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
            calendar_release_directory=calendar,
            hfdl_contract=contract,
            hfdl_synthetic_permit=permit,
            **_execution_kwargs(orchestrator_tmp),
        )

    clean_root = Path(tempfile.mkdtemp(prefix="sft-"))
    try:
        migration2, permit2, contract2, accepted2, calendar2, result = _run(clean_root)
        receipt_path = result.aggregate_set_release_directory / "foundation_set.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["candidate_eligible"] = True
        receipt_path.write_bytes(canonical_json_bytes(receipt))
        with pytest.raises(IntegrityError, match="hash mismatch"):
            run_stock_historical_foundation(
                migration_release_directory=migration2,
                accepted_release_root=accepted2,
                derived_work_root=clean_root / "work",
                created_at=CREATED_AT,
                calendar_release_directory=calendar2,
                hfdl_contract=contract2,
                hfdl_synthetic_permit=permit2,
                **_execution_kwargs(clean_root),
            )
    finally:
        shutil.rmtree(clean_root)


def test_production_execution_is_disabled_and_cli_is_plan_only(
    orchestrator_tmp: Path,
) -> None:
    migration = hfdl_support._completed_migration_release(orchestrator_tmp / "migration")
    accepted = orchestrator_tmp / "accepted"
    accepted.mkdir()
    with pytest.raises(
        ContractError,
        match="SYNTHETIC_FOUNDATION_EXECUTION requires",
    ):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
        )
    with pytest.raises(PermissionError, match="production foundation execution is disabled"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
            **_execution_kwargs(orchestrator_tmp),
        )
    options = {option for action in cli_module.parser()._actions for option in action.option_strings}
    assert "--execute" not in options
    assert "--work-root" not in options
    assert not {
        "--provider",
        "--download",
        "--model",
        "--fit",
        "--wfa",
        "--label",
        "--candidate",
    } & options
    tree = ast.parse(
        (Path(orchestrator_module.__file__)).read_text(encoding="utf-8")
    )
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any("providers" in name or ".research" in name for name in imported)


def test_synthetic_execution_rejects_output_outside_bound_root(
    orchestrator_tmp: Path,
) -> None:
    migration, permit, contract, accepted, calendar = _inputs(orchestrator_tmp)
    outside_work = orchestrator_tmp.parent / f"{orchestrator_tmp.name}-outside"
    with pytest.raises(ContractError, match="work path escapes"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=outside_work,
            created_at=CREATED_AT,
            calendar_release_directory=calendar,
            hfdl_contract=contract,
            hfdl_synthetic_permit=permit,
            **_execution_kwargs(orchestrator_tmp),
        )
    assert not outside_work.exists()


def test_synthetic_execution_permit_must_bind_the_input_fixture(
    orchestrator_tmp: Path,
) -> None:
    migration, permit, contract, accepted, calendar = _inputs(orchestrator_tmp)
    mismatched = SyntheticOnlyPermit.create(
        fixture_id="different-foundation-fixture",
        scope=orchestrator_module.SYNTHETIC_EXECUTION_SCOPE,
    )
    with pytest.raises(ContractError, match="same synthetic fixture"):
        run_stock_historical_foundation(
            migration_release_directory=migration,
            accepted_release_root=accepted,
            derived_work_root=orchestrator_tmp / "work",
            created_at=CREATED_AT,
            calendar_release_directory=calendar,
            hfdl_contract=contract,
            hfdl_synthetic_permit=permit,
            execution_synthetic_permit=mismatched,
            execution_allowed_root=orchestrator_tmp,
        )
    assert not (orchestrator_tmp / "work").exists()
