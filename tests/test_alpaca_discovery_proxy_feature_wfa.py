from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.alpaca_discovery_proxy_feature_wfa import (
    build_feature_release_plan,
    build_feature_wfa_plan,
    build_price_only_proxy_features,
    load_feature_wfa_contract,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest


REPO = Path(__file__).resolve().parents[1]


def test_feature_contract_is_content_addressed_and_discovery_only() -> None:
    contract = load_feature_wfa_contract(REPO)
    assert len(contract["contract_id"]) == 64
    assert contract["features"]["may_read_outcomes"] is False
    assert contract["wfa"]["real_history_execution_authorized"] is False
    assert contract["claims"]["alpha_claim"] is False


def test_price_features_are_causal_and_preserve_an_unresolved_lookback() -> None:
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    bars = [{"symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0 + index} for index, session in enumerate(sessions)] + [{"symbol": "SPY", "session": session, "open": 200.0, "close": 200.0} for session in sessions[1:]]
    rows = build_price_only_proxy_features(sessions, bars)
    ready = next(row for row in rows if row["symbol"] == "AAPL" and row["decision_session"] == sessions[5])
    unresolved = next(row for row in rows if row["symbol"] == "SPY" and row["decision_session"] == sessions[5])
    assert ready["status"] == "READY_CAUSAL_RAW_PRICE_FEATURES"
    assert ready["d0_raw_intraday_return"] == pytest.approx(0.05)
    assert ready["trailing_5_session_raw_return"] == pytest.approx(0.05)
    assert unresolved["status"] == "UNRESOLVED_CAUSAL_LOOKBACK"
    assert unresolved["d0_raw_intraday_return"] is None


def test_price_features_reject_duplicate_sessions() -> None:
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    rows = [{"symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0} for session in sessions]
    rows.append(dict(rows[0]))
    with pytest.raises(ContractError, match="session"):
        build_price_only_proxy_features(sessions, rows)


def _source_release(tmp_path: Path) -> tuple[Path, Path]:
    stage = tmp_path / "source-stage"
    (stage / "bars").mkdir(parents=True)
    sessions = [date(2020, 1, day) for day in range(2, 9)]
    pq.write_table(pa.Table.from_pylist([
        {"provider_symbol": "AAPL", "session": session, "open": 100.0, "close": 100.0 + index}
        for index, session in enumerate(sessions)
    ]), stage / "bars" / "year=2020.parquet")
    manifest = build_manifest(
        stage, ("bars/year=2020.parquet",), project="US_stocks_swing_model_v2",
        dataset="alpaca_historical_daily_bars", source_epoch="synthetic",
        role="legacy_discovery_only", quality_state="LEGACY_CAVEATED",
        created_at="2026-08-01T01:00:00Z", row_count=7,
        event_start="2020-01-02", event_end="2020-01-08", schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    accepted = (tmp_path / "accepted").resolve()
    return AtomicReleasePublisher(accepted).publish(stage, manifest), accepted


def _calendar_release(tmp_path: Path, accepted: Path) -> Path:
    stage = tmp_path / "calendar-stage"
    stage.mkdir()
    (stage / "sessions.parquet").write_bytes(b"synthetic")
    manifest = build_manifest(
        stage, ("sessions.parquet",), project="US_stocks_swing_model_v2",
        dataset="xnys_sessions", source_epoch="synthetic", role="derived_causal",
        quality_state="PASS", created_at="2026-08-01T01:00:00Z", row_count=1,
        event_start="2020-01-02", event_end="2020-01-02", schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def test_feature_release_plan_is_metadata_only_and_caveated(tmp_path: Path) -> None:
    source, accepted = _source_release(tmp_path)
    calendar = _calendar_release(tmp_path, accepted)
    plan = build_feature_release_plan(source, calendar_release_directory=calendar, accepted_root=accepted, repo_root=REPO)
    assert len(plan["feature_build_plan_id"]) == 64
    assert plan["validation_scope"] == {"bar_rows_opened": 0, "calendar_rows_opened": 0, "files_written": 0}
    assert plan["required_execution_authority"]["training_or_evaluation"] is False


def test_feature_publisher_requires_confirmation_and_emits_caveated_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import us_stocks_swing_model_v2.alpaca_discovery_proxy_feature_wfa as subject

    source, accepted = _source_release(tmp_path)
    calendar = _calendar_release(tmp_path, accepted)
    plan = build_feature_release_plan(source, calendar_release_directory=calendar, accepted_root=accepted, repo_root=REPO)
    with pytest.raises(ContractError, match="confirmation"):
        subject.publish_feature_release(source, calendar_release_directory=calendar, accepted_root=accepted, work_root=tmp_path / "work", created_at="2026-08-01T01:00:00Z", approved_feature_build_plan_id=plan["feature_build_plan_id"], repo_root=REPO)
    monkeypatch.setenv("ALPACA_DISCOVERY_FEATURE_BUILD_APPROVED", "YES")
    monkeypatch.setattr(subject, "validate_environment_lock", lambda _path: "e" * 64)
    sessions = tuple(date(2020, 1, day) for day in range(2, 9))
    monkeypatch.setattr(subject, "load_xnys_calendar_release", lambda *_args, **_kwargs: SimpleNamespace(calendar=SimpleNamespace(sessions=sessions)))
    published = subject.publish_feature_release(source, calendar_release_directory=calendar, accepted_root=accepted, work_root=tmp_path / "work", created_at="2026-08-01T01:00:00Z", approved_feature_build_plan_id=plan["feature_build_plan_id"], repo_root=REPO)
    evidence = (published / "source_evidence_manifest.json").read_bytes()
    assert published.parent.name == "alpaca_discovery_proxy_features"
    assert b'"outcomes_read":false' in evidence
    assert b'"training_or_evaluation":false' in evidence


def _caveated_release(tmp_path: Path, accepted: Path, *, dataset: str, evidence: dict[str, object], event_start: str = "2020-01-02", event_end: str = "2020-01-08") -> Path:
    stage = tmp_path / f"{dataset}-stage"
    stage.mkdir()
    (stage / "payload.bin").write_bytes(b"synthetic")
    (stage / "source_evidence_manifest.json").write_bytes(canonical_json_bytes(evidence))
    manifest = build_manifest(
        stage, ("payload.bin", "source_evidence_manifest.json"),
        project="US_stocks_swing_model_v2", dataset=dataset, source_epoch="synthetic",
        role="legacy_discovery_only", quality_state="LEGACY_CAVEATED",
        created_at="2026-08-01T01:00:00Z", row_count=1,
        event_start=event_start, event_end=event_end, schema_fingerprint="a" * 64,
        code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64,
    )
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def test_wfa_plan_binds_separate_feature_and_outcome_releases_without_opening_rows(tmp_path: Path) -> None:
    accepted = (tmp_path / "wfa-accepted").resolve()
    feature = _caveated_release(tmp_path, accepted, dataset="alpaca_discovery_proxy_features", event_start="2020-01-06", event_end="2020-01-08", evidence={
        "feature_names": ["d0_raw_intraday_return", "trailing_5_session_raw_return", "trailing_5_session_raw_volatility"],
        "outcomes_read": False, "training_or_evaluation": False,
    })
    outcome = _caveated_release(tmp_path, accepted, dataset="alpaca_discovery_proxy_outcomes", evidence={
        "historical_proxy": True, "canonical_target_equivalent": False,
        "survivorship_safe": False, "training_or_evaluation": False,
    })
    plan = build_feature_wfa_plan(feature, proxy_outcome_release_directory=outcome, accepted_root=accepted, repo_root=REPO)
    assert plan["feature_release"]["release_id"] == feature.name
    assert plan["proxy_outcome_release"]["release_id"] == outcome.name
    assert plan["validation_scope"] == {"feature_rows_opened": 0, "proxy_outcome_rows_opened": 0, "files_written": 0}
