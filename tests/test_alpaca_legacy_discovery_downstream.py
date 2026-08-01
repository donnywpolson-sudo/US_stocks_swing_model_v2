from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.alpaca_legacy_discovery_downstream import (
    build_downstream_plan,
    build_proxy_outcome_plan,
    build_raw_price_proxy_outcomes,
    build_raw_price_proxy_outcome_table,
    load_contract,
)
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


def _calendar_release(tmp_path: Path, accepted: Path) -> Path:
    stage = tmp_path / "calendar-stage"
    stage.mkdir()
    (stage / "sessions.parquet").write_bytes(b"synthetic-calendar")
    manifest = build_manifest(
        stage, ("sessions.parquet",),
        project="US_stocks_swing_model_v2", dataset="xnys_sessions",
        source_epoch="synthetic", role="derived_causal", quality_state="PASS",
        created_at="2026-07-31T20:00:00Z", row_count=1,
        event_start="2016-01-04", event_end="2016-01-04", schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def _proxy_source_release(tmp_path: Path) -> tuple[Path, Path]:
    stage = tmp_path / "proxy-source-stage"
    (stage / "bars").mkdir(parents=True)
    sessions = [date(2020, 1, day) for day in range(2, 9)]
    pq.write_table(
        pa.Table.from_pylist(
            [
                {"provider_symbol": "AAPL", "session": session, "open": 100.0, "close": 105.0}
                for session in sessions
            ]
        ),
        stage / "bars" / "year=2020.parquet",
    )
    (stage / "source_evidence_manifest.json").write_bytes(canonical_json_bytes({
        "input_quality_state": "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
        "historical_membership_proven": False,
        "point_in_time_safe": False,
        "survivorship_safe": False,
    }))
    manifest = build_manifest(
        stage, ("bars/year=2020.parquet", "source_evidence_manifest.json"),
        project="US_stocks_swing_model_v2", dataset="alpaca_historical_daily_bars",
        source_epoch="synthetic", role="legacy_discovery_only", quality_state="LEGACY_CAVEATED",
        created_at="2026-07-31T20:00:00Z", row_count=7,
        event_start="2020-01-02", event_end="2020-01-08", schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    accepted = (tmp_path / "proxy-accepted").resolve()
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


def test_raw_price_proxy_preserves_unresolved_horizons_and_never_claims_canonical_target() -> None:
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    bars = [
        {"symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0 + index}
        for index, session in enumerate(sessions)
    ] + [
        {"symbol": "SPY", "session": session, "open": 200.0, "close": 200.0}
        for session in sessions[:-1]
    ]
    outcomes = build_raw_price_proxy_outcomes(sessions, bars)
    ready = next(row for row in outcomes if row["symbol"] == "AAPL" and row["decision_session"] == sessions[0])
    unresolved = next(row for row in outcomes if row["symbol"] == "SPY" and row["decision_session"] == sessions[1])
    assert ready["proxy_return"] == pytest.approx(0.05)
    assert ready["status"] == "READY_UNTRUSTED_RAW_PRICE_PROXY"
    assert unresolved["proxy_return"] is None
    assert unresolved["status"] == "UNRESOLVED_RAW_HORIZON"
    assert all(row["canonical_target_equivalent"] is False for row in outcomes)
    table = build_raw_price_proxy_outcome_table(sessions, bars)
    assert table.num_rows == len(outcomes)
    assert table.schema.names == [
        "symbol", "decision_session", "entry_session", "exit_session", "entry_open",
        "exit_close", "proxy_return", "status", "target_semantics", "historical_proxy",
        "canonical_target_equivalent",
    ]


def test_proxy_outcome_plan_is_metadata_only_and_requires_later_real_build_authority(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    calendar = _calendar_release(tmp_path, accepted)
    plan = build_proxy_outcome_plan(
        release, calendar_release_directory=calendar, accepted_root=accepted, repo_root=REPO
    )
    assert len(plan["proxy_build_plan_id"]) == 64
    assert plan["output"]["release_id"].startswith("DEFERRED_")
    assert plan["plan_validation_scope"] == {
        "bar_rows_opened": 0, "calendar_rows_opened": 0, "files_written": 0
    }
    assert plan["required_execution_authority"]["real_row_access"] is True
    assert plan["transform"]["features_constructed"] is False


def test_proxy_outcome_publisher_requires_an_explicit_execution_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from us_stocks_swing_model_v2.alpaca_legacy_discovery_downstream import publish_proxy_outcomes

    release, accepted = _release(tmp_path)
    calendar = _calendar_release(tmp_path, accepted)
    monkeypatch.delenv("ALPACA_DISCOVERY_PROXY_BUILD_APPROVED", raising=False)
    with pytest.raises(ContractError, match="confirmation"):
        publish_proxy_outcomes(
            release,
            calendar_release_directory=calendar,
            accepted_root=accepted,
            work_root=tmp_path / "work",
            created_at="2026-07-31T20:00:00Z",
            approved_proxy_build_plan_id="0" * 64,
            repo_root=REPO,
        )


def test_proxy_outcome_publisher_emits_only_a_caveated_immutable_synthetic_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import us_stocks_swing_model_v2.alpaca_legacy_discovery_downstream as subject

    release, accepted = _proxy_source_release(tmp_path)
    calendar = _calendar_release(tmp_path, accepted)
    plan = build_proxy_outcome_plan(
        release, calendar_release_directory=calendar, accepted_root=accepted, repo_root=REPO
    )
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    monkeypatch.setenv("ALPACA_DISCOVERY_PROXY_BUILD_APPROVED", "YES")
    monkeypatch.setattr(subject, "validate_environment_lock", lambda _path: "e" * 64)
    monkeypatch.setattr(
        subject,
        "load_xnys_calendar_release",
        lambda *_args, **_kwargs: SimpleNamespace(calendar=SimpleNamespace(sessions=sessions)),
    )
    published = subject.publish_proxy_outcomes(
        release,
        calendar_release_directory=calendar,
        accepted_root=accepted,
        work_root=tmp_path / "work",
        created_at="2026-07-31T20:00:00Z",
        approved_proxy_build_plan_id=plan["proxy_build_plan_id"],
        repo_root=REPO,
    )
    manifest = (published / "release_manifest.json").read_text(encoding="utf-8")
    evidence = (published / "source_evidence_manifest.json").read_text(encoding="utf-8")
    assert published.parent.name == "alpaca_discovery_proxy_outcomes"
    assert "LEGACY_CAVEATED" in manifest
    assert "canonical_target_equivalent\":false" in evidence
    assert "training_or_evaluation\":false" in evidence
