from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import tempfile
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import test_hfdl_legacy_publisher as hfdl_support
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.canonical.hfdl import HFDL_TAGGED_SCHEMA
from us_stocks_swing_model_v2.canonical.hfdl_legacy_publisher import (
    HfdlPublishContract,
    publish_hfdl_legacy_discovery,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.exchange_calendar import (
    SYNTHETIC_CALENDAR_PUBLICATION_SCOPE,
    calendar_environment_hash,
    calendar_policy_hash,
    calendar_publication_binding_id,
    publish_xnys_calendar_release,
)
from us_stocks_swing_model_v2.historical_foundation import (
    ACTION_STATE,
    BRIDGE_SET_DATASET,
    HFDL_EPOCHS,
    MEMBERSHIP_STATE,
    OUTPUT_KINDS,
    SECURITY_TYPE_STATE,
    _derive_symbol_tables,
    _source_index,
    load_hfdl_historical_foundation,
    publish_hfdl_historical_foundation,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.releases import ReleaseFile, verify_accepted_release


CREATED_AT = "2026-07-15T00:00:00Z"


@pytest.fixture
def bridge_tmp() -> Iterator[Path]:
    root = Path(tempfile.mkdtemp(prefix="hfb-"))
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _accepted_inputs(root: Path):
    migration = hfdl_support._completed_migration_release(root / "migration")
    permit = hfdl_support._permit("hfdl-foundation-bridge")
    accepted = root / "accepted"
    contract = HfdlPublishContract.synthetic_fixture(2, permit=permit)
    hfdl = publish_hfdl_legacy_discovery(
        migration_release_directory=migration,
        accepted_release_root=accepted,
        derived_work_root=root / "hfdl-work",
        created_at=CREATED_AT,
        contract=contract,
        **hfdl_support._publication_kwargs(root, contract),
    )
    calendar_kwargs = {
        "staging_root": root / "calendar-stage",
        "release_root": accepted,
        "start": date(2022, 2, 25),
        "end": date(2022, 3, 15),
        "created_at": CREATED_AT,
        "code_hash": "1" * 64,
        "config_hash": calendar_policy_hash(),
        "environment_hash": calendar_environment_hash(),
    }
    calendar = publish_xnys_calendar_release(
        **calendar_kwargs,
        publication_synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id=calendar_publication_binding_id(**calendar_kwargs),
            scope=SYNTHETIC_CALENDAR_PUBLICATION_SCOPE,
        ),
        publication_allowed_root=root,
    )
    return accepted, hfdl, calendar, permit


def _publish(root: Path):
    accepted, hfdl, calendar, permit = _accepted_inputs(root)
    result = publish_hfdl_historical_foundation(
        hfdl_epoch_set_release_directory=hfdl.epoch_set_release_directory,
        calendar_release_directory=calendar,
        accepted_release_root=accepted,
        derived_work_root=root / "bridge-work",
        created_at=CREATED_AT,
        hfdl_synthetic_permit=permit,
    )
    return accepted, hfdl, calendar, permit, result


def test_source_index_reconciles_every_declaration_to_parquet(
    bridge_tmp: Path,
) -> None:
    _accepted, hfdl, _calendar, _permit = _accepted_inputs(bridge_tmp)
    source = hfdl.epoch_release_directories[HFDL_EPOCHS[0]]
    assert _source_index(source)
    original_rows = [
        json.loads(line)
        for line in (source / "symbol_index.jsonl").read_bytes().splitlines()
    ]
    original = original_rows[0]
    poisons = {
        "sha256": "0" * 64 if original["sha256"] != "0" * 64 else "1" * 64,
        "size": original["size"] + 1,
        "row_count": original["row_count"] + 1,
        "session_start": "2022-03-02",
        "session_end": "2022-03-05",
    }
    for field, value in poisons.items():
        poisoned = bridge_tmp / f"poison-{field}"
        shutil.copytree(source, poisoned)
        rows = [dict(row) for row in original_rows]
        rows[0][field] = value
        (poisoned / "symbol_index.jsonl").write_bytes(
            b"".join(canonical_json_bytes(row) for row in rows)
        )
        with pytest.raises(IntegrityError, match="source index"):
            _source_index(poisoned)


def test_bridge_publishes_six_unpooled_legacy_releases_with_exact_censuses(
    bridge_tmp: Path,
) -> None:
    accepted, hfdl, calendar, permit, result = _publish(bridge_tmp)
    with pytest.raises(ContractError, match="exact HFDL permit"):
        load_hfdl_historical_foundation(
            result.bridge_set_release_directory,
            accepted_release_root=accepted,
        )
    verified = load_hfdl_historical_foundation(
        result.bridge_set_release_directory,
        accepted_release_root=accepted,
        hfdl_synthetic_permit=permit,
    )
    assert verified == result
    assert set(result.epoch_release_directories) == set(HFDL_EPOCHS)
    assert len(
        {
            path
            for releases in result.epoch_release_directories.values()
            for path in releases.values()
        }
    ) == 6
    for epoch in HFDL_EPOCHS:
        assert set(result.epoch_release_directories[epoch]) == set(OUTPUT_KINDS)
        for kind, directory in result.epoch_release_directories[epoch].items():
            manifest = verify_accepted_release(directory, accepted_root=accepted)
            assert manifest.source_epoch == epoch
            assert manifest.role == "legacy_discovery_only"
            assert manifest.quality_state == "LEGACY_CAVEATED"
            census = json.loads((directory / "census.json").read_text(encoding="utf-8"))
            assert census["source_series_count"] == 2
            assert census["source_rows"] == 2
            assert census["calendar_symbol_session_denominator"] == 2
            assert census["output_rows"] == 2
            assert census["evidence_denominator_rows"] == 2
            assert census["membership_evidence_available_rows"] == 0
            assert census["membership_evidence_unknown_rows"] == 2
            assert census["security_type_evidence_available_rows"] == 0
            assert census["security_type_evidence_unknown_rows"] == 2
            assert census["action_evidence_available_rows"] == 0
            assert census["action_evidence_unavailable_rows"] == 2
            assert census["delisting_evidence_available_rows"] == 0
            assert census["delisting_evidence_unavailable_rows"] == 2
            assert census["outcome_evaluable_rows"] == 0
            assert census["matured_outcome_rows"] == 0
            data_file = next((directory / "data").glob("*.parquet"))
            table = pq.read_table(data_file)
            assert set(table.column("source_epoch").to_pylist()) == {epoch}
            assert set(table.column("membership_evidence_status").to_pylist()) == {
                MEMBERSHIP_STATE
            }
            assert set(table.column("security_type_evidence_status").to_pylist()) == {
                SECURITY_TYPE_STATE
            }
            assert set(table.column("action_evidence_status").to_pylist()) == {ACTION_STATE}
            if kind == "outcome_inputs":
                assert table.column("split_normalized_price_return").null_count == table.num_rows

    rerun = publish_hfdl_historical_foundation(
        hfdl_epoch_set_release_directory=hfdl.epoch_set_release_directory,
        calendar_release_directory=calendar,
        accepted_release_root=accepted,
        derived_work_root=bridge_tmp / "bridge-work",
        created_at=CREATED_AT,
        hfdl_synthetic_permit=permit,
    )
    assert rerun == result


@pytest.mark.parametrize(
    ("target", "field"),
    [
        ("set", "code_hash"),
        ("set", "environment_hash"),
        ("physical", "code_hash"),
        ("physical", "environment_hash"),
    ],
)
def test_bridge_loader_reconstructs_code_and_environment_closure(
    bridge_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    field: str,
) -> None:
    accepted, _hfdl, _calendar, permit, result = _publish(bridge_tmp)
    import us_stocks_swing_model_v2.historical_foundation as bridge_module

    original = bridge_module.verify_accepted_release

    def poisoned_manifest(directory: Path, *, accepted_root: Path):
        manifest = original(directory, accepted_root=accepted_root)
        is_set = Path(directory) == result.bridge_set_release_directory
        is_physical = manifest.dataset in {
            dataset
            for epoch_datasets in bridge_module.BRIDGE_DATASETS.values()
            for dataset in epoch_datasets.values()
        }
        if (target == "set" and is_set) or (target == "physical" and is_physical):
            current = getattr(manifest, field)
            replacement = "0" * 64 if current != "0" * 64 else "1" * 64
            return replace(manifest, **{field: replacement})
        return manifest

    monkeypatch.setattr(
        bridge_module,
        "verify_accepted_release",
        poisoned_manifest,
    )
    with pytest.raises(IntegrityError):
        load_hfdl_historical_foundation(
            result.bridge_set_release_directory,
            accepted_release_root=accepted,
            hfdl_synthetic_permit=permit,
        )


def test_bridge_loader_requires_exact_singleton_set_payload_census(
    bridge_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, _hfdl, _calendar, permit, result = _publish(bridge_tmp)
    import us_stocks_swing_model_v2.historical_foundation as bridge_module

    original = bridge_module.verify_accepted_release

    def extra_set_payload(directory: Path, *, accepted_root: Path):
        manifest = original(directory, accepted_root=accepted_root)
        if Path(directory) == result.bridge_set_release_directory:
            return replace(
                manifest,
                files=manifest.files
                + (ReleaseFile("extra.json", 0, "0" * 64),),
            )
        return manifest

    monkeypatch.setattr(
        bridge_module,
        "verify_accepted_release",
        extra_set_payload,
    )
    with pytest.raises(IntegrityError, match="payload census"):
        load_hfdl_historical_foundation(
            result.bridge_set_release_directory,
            accepted_release_root=accepted,
            hfdl_synthetic_permit=permit,
        )


def _tagged_source(*, future_close: float = 13.0, epoch: str = "hfdl_iex_only") -> pa.Table:
    retrieved = datetime(2026, 7, 15, tzinfo=timezone.utc)
    rows = [
        {
            "symbol": "ABC",
            "session": date(2022, 3, 4),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 100,
            "source_epoch": epoch,
            "source_adjustment": "hfdl_clean_source_adjusted",
            "evidence_class": "LEGACY_DISCOVERY",
            "point_in_time_safe": False,
            "point_in_time_state": "HISTORICAL_PROXY",
            "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
            "source_retrieved_at": retrieved,
        },
        {
            "symbol": "ABC",
            "session": date(2022, 3, 8),
            "open": 11.0,
            "high": 12.0,
            "low": 10.0,
            "close": 11.5,
            "volume": 110,
            "source_epoch": epoch,
            "source_adjustment": "hfdl_clean_source_adjusted",
            "evidence_class": "LEGACY_DISCOVERY",
            "point_in_time_safe": False,
            "point_in_time_state": "HISTORICAL_PROXY",
            "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
            "source_retrieved_at": retrieved,
        },
        {
            "symbol": "ABC",
            "session": date(2022, 3, 9),
            "open": 12.0,
            "high": max(13.5, future_close),
            "low": 11.0,
            "close": future_close,
            "volume": 120,
            "source_epoch": epoch,
            "source_adjustment": "hfdl_clean_source_adjusted",
            "evidence_class": "LEGACY_DISCOVERY",
            "point_in_time_safe": False,
            "point_in_time_state": "HISTORICAL_PROXY",
            "historical_availability_state": "UNKNOWN_NOT_AS_RECEIVED",
            "source_retrieved_at": retrieved,
        },
    ]
    return pa.Table.from_pylist(rows, schema=HFDL_TAGGED_SCHEMA)


def _calendar_rows():
    sessions = tuple(date(2022, 3, day) for day in (4, 7, 8, 9, 10, 11, 14))
    rows = {
        session: {
            "close_at": datetime(2022, 3, session.day, 21, tzinfo=timezone.utc)
        }
        for session in sessions
    }
    return sessions, rows


def _poison_tagged_source(
    *,
    field: str,
    value: object,
) -> pa.Table:
    rows = _tagged_source().to_pylist()
    rows[0][field] = value
    return pa.Table.from_pylist(rows, schema=HFDL_TAGGED_SCHEMA)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open", 0.0),
        ("close", float("nan")),
        ("high", 9.5),
        ("low", 10.25),
        ("volume", -1),
    ],
)
def test_feature_derivation_rejects_invalid_canonical_numeric_values(
    field: str,
    value: object,
) -> None:
    sessions, calendar_rows = _calendar_rows()
    with pytest.raises(IntegrityError, match="OHLCV values"):
        _derive_symbol_tables(
            source_series_id="a" * 64,
            symbol="ABC",
            source=_poison_tagged_source(field=field, value=value),
            epoch="hfdl_iex_only",
            calendar_sessions=sessions,
            calendar_rows=calendar_rows,
            calendar_release_id="b" * 64,
        )


def test_future_missing_action_membership_and_cross_epoch_poisons_fail_closed() -> None:
    sessions, calendar_rows = _calendar_rows()
    baseline = _derive_symbol_tables(
        source_series_id="a" * 64,
        symbol="ABC",
        source=_tagged_source(),
        epoch="hfdl_iex_only",
        calendar_sessions=sessions,
        calendar_rows=calendar_rows,
        calendar_release_id="b" * 64,
    )
    future_poisoned = _derive_symbol_tables(
        source_series_id="a" * 64,
        symbol="ABC",
        source=_tagged_source(future_close=1_000_000.0),
        epoch="hfdl_iex_only",
        calendar_sessions=sessions,
        calendar_rows=calendar_rows,
        calendar_release_id="b" * 64,
    )
    baseline_features = baseline["feature_inputs"].to_pylist()
    attacked_features = future_poisoned["feature_inputs"].to_pylist()
    # A future 2022-03-09 mutation cannot change feature inputs through 2022-03-08.
    assert baseline_features[:3] == attacked_features[:3]
    assert baseline["causal_bars"].to_pylist()[1]["bar_status"] == (
        "MISSING_SOURCE_SESSION_UNKNOWN_CAUSE"
    )
    assert baseline_features[2]["feature_status"] == (
        "MISSING_PREVIOUS_SOURCE_SESSION_OR_EPOCH_BOUNDARY"
    )
    assert all(row["membership_evidence_status"] == MEMBERSHIP_STATE for row in baseline_features)
    assert all(row["security_type_evidence_status"] == SECURITY_TYPE_STATE for row in baseline_features)
    outcomes = baseline["outcome_inputs"].to_pylist()
    assert all(row["split_normalized_price_return"] is None for row in outcomes)
    assert all(row["action_evidence_status"] == ACTION_STATE for row in outcomes)
    assert all(row["delisting_evidence_status"] == "UNAVAILABLE_NOT_AS_RECEIVED" for row in outcomes)
    assert all("asset_id" not in row and "security_type" not in row for row in baseline_features)

    with pytest.raises(IntegrityError, match="identity/session/epoch"):
        _derive_symbol_tables(
            source_series_id="a" * 64,
            symbol="ABC",
            source=_tagged_source(epoch="hfdl_pitrading_consolidated"),
            epoch="hfdl_iex_only",
            calendar_sessions=sessions,
            calendar_rows=calendar_rows,
            calendar_release_id="b" * 64,
        )


def test_interruption_resumes_partial_derived_files_without_partial_set(
    bridge_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted, hfdl, calendar, permit = _accepted_inputs(bridge_tmp)
    import us_stocks_swing_model_v2.historical_foundation as bridge_module

    original = bridge_module._write_or_verify_parquet
    calls = 0

    def crash_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic bridge interruption")
        return original(*args, **kwargs)

    monkeypatch.setattr(bridge_module, "_write_or_verify_parquet", crash_second)
    with pytest.raises(RuntimeError, match="bridge interruption"):
        publish_hfdl_historical_foundation(
            hfdl_epoch_set_release_directory=hfdl.epoch_set_release_directory,
            calendar_release_directory=calendar,
            accepted_release_root=accepted,
            derived_work_root=bridge_tmp / "bridge-work",
            created_at=CREATED_AT,
            hfdl_synthetic_permit=permit,
        )
    assert not (accepted / BRIDGE_SET_DATASET).exists()
    monkeypatch.setattr(bridge_module, "_write_or_verify_parquet", original)
    result = publish_hfdl_historical_foundation(
        hfdl_epoch_set_release_directory=hfdl.epoch_set_release_directory,
        calendar_release_directory=calendar,
        accepted_release_root=accepted,
        derived_work_root=bridge_tmp / "bridge-work",
        created_at=CREATED_AT,
        hfdl_synthetic_permit=permit,
    )
    load_hfdl_historical_foundation(
        result.bridge_set_release_directory,
        accepted_release_root=accepted,
        hfdl_synthetic_permit=permit,
    )


def test_member_action_provenance_tamper_invalidates_accepted_bridge(bridge_tmp: Path) -> None:
    accepted, _hfdl, _calendar, permit, result = _publish(bridge_tmp)
    feature_directory = result.epoch_release_directories["hfdl_iex_only"]["feature_inputs"]
    provenance_path = feature_directory / "provenance.json"
    payload = json.loads(provenance_path.read_text(encoding="utf-8"))
    payload["membership_evidence_status"] = "PIT_CONFIRMED"
    payload["action_evidence_status"] = "COMPLETE"
    provenance_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash mismatch"):
        load_hfdl_historical_foundation(
            result.bridge_set_release_directory,
            accepted_release_root=accepted,
            hfdl_synthetic_permit=permit,
        )


def test_readiness_records_real_discovery_foundation_without_pit_claim() -> None:
    root = Path(__file__).resolve().parents[1]
    contract = json.loads(
        (root / "config" / "research_readiness_contract.json").read_text(encoding="utf-8")
    )
    anchors = contract["production_evidence_anchors"]
    assert anchors["historical_foundation_bridge_mechanics_status"] == (
        "IMPLEMENTED_SYNTHETIC_ADVERSARIAL_TESTED"
    )
    assert anchors["historical_foundation_real_release_status"] == (
        "BUILT_NON_ACTIVE_LEGACY_DISCOVERY_ONLY_PIT_UNRESOLVED"
    )
    assert "ready" not in contract["readiness"]
    assert (
        contract["readiness"]["mechanical_assessment_status"]
        == "PASS_NON_AUTHORIZING_LEGACY_DISCOVERY_ONLY"
    )
    assert contract["readiness"]["candidate_eligibility"] == "BLOCKED_PENDING_PROSPECTIVE_PIT"
