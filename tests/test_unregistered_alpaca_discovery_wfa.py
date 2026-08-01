from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.research.splits import TemporalSamples
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, ReleaseManifest, build_manifest
from us_stocks_swing_model_v2.unregistered_alpaca_discovery_wfa import (
    FEATURE_COLUMNS,
    OUTCOME_COLUMNS,
    UnregisteredDiscoveryDataset,
    assess_streaming_join_layout,
    build_caveated_joined_trial_input_plan,
    build_caveated_joined_publication_plan,
    build_unregistered_wfa_plan,
    publish_caveated_joined_release,
    execute_caveated_joined_trial_input,
    build_caveated_joined_trial_input,
    execute_unregistered_discovery_wfa,
    execute_streaming_unregistered_discovery_wfa,
    execute_planned_streaming_unregistered_wfa,
    iter_caveated_parquet_batches,
)


REPO = Path(__file__).resolve().parents[1]


def _joined_stage(tmp_path):
    stage = tmp_path / "joined-stage"
    joined = stage / "joined"
    joined.mkdir(parents=True)
    table = pa.table({
        "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
        "d0_raw_intraday_return": [0.01], "trailing_5_session_raw_return": [0.02],
        "trailing_5_session_raw_volatility": [0.03], "proxy_return": [0.01],
    })
    for number in range(64):
        pq.write_table(table, joined / f"bucket={number:03d}.parquet")
    return stage


def test_executes_eight_caveated_chronological_folds_in_memory() -> None:
    sessions = np.repeat(np.arange(2016, dtype=np.int64), 2)
    count = len(sessions)
    dataset = UnregisteredDiscoveryDataset(tuple(f"s{n}" for n in range(count)), np.column_stack((sessions / 2016, np.ones(count), np.zeros(count))).astype(float), np.where(sessions % 3 == 0, .01, -.01).astype(float), TemporalSamples(sessions, sessions + 1, sessions + 5, sessions + 5))
    result = execute_unregistered_discovery_wfa(dataset, session_count=2016)
    assert len(result["folds"]) == 8
    assert result["writes"] == 0
    assert result["trusted_result_claim"] is False


def test_streaming_executor_uses_two_bounded_passes_only() -> None:
    sessions = tuple(date(2016, 1, 1) + timedelta(days=number) for number in range(2016))
    decisions = sessions[:-5]
    table = pa.table({
        "symbol": ["AAPL"] * len(decisions), "decision_session": decisions,
        "d0_raw_intraday_return": [0.01] * len(decisions), "trailing_5_session_raw_return": [0.02] * len(decisions),
        "trailing_5_session_raw_volatility": [0.03] * len(decisions), "proxy_return": [0.01 if number % 3 == 0 else -0.01 for number in range(len(decisions))],
    })
    result = execute_streaming_unregistered_discovery_wfa(lambda: iter(table.to_batches(max_chunksize=65536)), sessions=sessions)
    assert len(result["folds"]) == 8
    assert result["batch_passes"] == 2
    assert result["writes"] == 0


def test_planned_streaming_executor_rejects_missing_approval_before_opening_release(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_DISCOVERY_WFA_EXECUTION_APPROVED", raising=False)
    with pytest.raises(Exception, match="confirmation"):
        execute_planned_streaming_unregistered_wfa(
            tmp_path / "joined", calendar_release_directory=tmp_path / "calendar",
            accepted_root=(tmp_path / "accepted").resolve(), repo_root=REPO,
            approved_unregistered_wfa_plan_id="a" * 64,
        )


def test_streams_only_bounded_exact_schema_batches(tmp_path) -> None:
    path = tmp_path / "fixture.parquet"
    pq.write_table(pa.table({"a": list(range(3)), "b": [1.0] * 3}), path)
    batches = list(iter_caveated_parquet_batches(str(path), columns=("a", "b"), batch_size=2))
    assert [batch.num_rows for batch in batches] == [2, 1]


def test_layout_assessment_refuses_to_promise_an_unsafe_direct_join(tmp_path) -> None:
    feature_paths = []
    for year in (2020, 2021):
        path = tmp_path / f"year={year}.parquet"
        pq.write_table(
            pa.table({
                "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
                "d0_raw_intraday_return": [0.01], "trailing_5_session_raw_return": [0.02],
                "trailing_5_session_raw_volatility": [0.03], "status": ["READY"],
            }),
            path,
        )
        feature_paths.append(path)
    outcome_path = tmp_path / "proxy_outcomes.parquet"
    pq.write_table(
        pa.table({
            "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
            "entry_session": [date(2020, 1, 3)], "exit_session": [date(2020, 1, 9)],
            "entry_open": [1.0], "exit_close": [1.01], "proxy_return": [0.01],
            "status": ["READY"], "target_semantics": ["proxy"],
            "historical_proxy": [True], "canonical_target_equivalent": [False],
        }),
        outcome_path,
    )
    assessment = assess_streaming_join_layout(tuple(feature_paths), outcome_path=outcome_path)
    assert assessment["feature_files"] == 2
    assert assessment["direct_single_pass_join"] is False
    assert assessment["rows_opened"] == 0


def test_bounded_join_stages_only_ready_exact_keys(tmp_path) -> None:
    feature = tmp_path / "year=2020.parquet"
    pq.write_table(pa.table({
        "symbol": ["AAPL", "SPY"], "decision_session": [date(2020, 1, 2), date(2020, 1, 2)],
        "d0_raw_intraday_return": [0.01, None], "trailing_5_session_raw_return": [0.02, None],
        "trailing_5_session_raw_volatility": [0.03, None],
        "status": ["READY_CAUSAL_RAW_PRICE_FEATURES", "UNRESOLVED_CAUSAL_LOOKBACK"],
    }), feature)
    outcome = tmp_path / "proxy_outcomes.parquet"
    pq.write_table(pa.table({
        "symbol": ["AAPL", "SPY"], "decision_session": [date(2020, 1, 2), date(2020, 1, 2)],
        "entry_session": [date(2020, 1, 3), date(2020, 1, 3)], "exit_session": [date(2020, 1, 9), date(2020, 1, 9)],
        "entry_open": [1.0, 1.0], "exit_close": [1.01, None], "proxy_return": [0.01, None],
        "status": ["READY_UNTRUSTED_RAW_PRICE_PROXY", "UNRESOLVED_RAW_HORIZON"],
        "target_semantics": ["proxy", "proxy"], "historical_proxy": [True, True],
        "canonical_target_equivalent": [False, False],
    }), outcome)
    result = build_caveated_joined_trial_input((feature,), outcome_path=outcome, stage_root=tmp_path / "stage", bucket_count=2, batch_size=1, maximum_stage_bytes=1_000_000)
    assert result["joined_rows"] == 1
    assert result["excluded_feature_rows"] == 1
    assert result["excluded_outcome_rows"] == 1
    assert result["trusted_result_claim"] is False


def test_join_stops_when_the_declared_stage_byte_bound_is_exceeded(tmp_path) -> None:
    feature = (tmp_path / "year=2020.parquet").resolve()
    pq.write_table(pa.table({
        "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
        "d0_raw_intraday_return": [0.01], "trailing_5_session_raw_return": [0.02],
        "trailing_5_session_raw_volatility": [0.03], "status": ["READY_CAUSAL_RAW_PRICE_FEATURES"],
    }), feature)
    outcome = (tmp_path / "proxy_outcomes.parquet").resolve()
    pq.write_table(pa.table({
        "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
        "entry_session": [date(2020, 1, 3)], "exit_session": [date(2020, 1, 9)],
        "entry_open": [1.0], "exit_close": [1.01], "proxy_return": [0.01],
        "status": ["READY_UNTRUSTED_RAW_PRICE_PROXY"], "target_semantics": ["proxy"],
        "historical_proxy": [True], "canonical_target_equivalent": [False],
    }), outcome)
    with pytest.raises(Exception, match="byte bound"):
        build_caveated_joined_trial_input((feature,), outcome_path=outcome, stage_root=tmp_path / "stage", bucket_count=2, batch_size=1, maximum_stage_bytes=1)


def test_join_plan_binds_absolute_metadata_and_never_authorizes_wfa(tmp_path) -> None:
    feature = (tmp_path / "year=2020.parquet").resolve()
    pq.write_table(pa.table({
        "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
        "d0_raw_intraday_return": [0.01], "trailing_5_session_raw_return": [0.02],
        "trailing_5_session_raw_volatility": [0.03], "status": ["READY_CAUSAL_RAW_PRICE_FEATURES"],
    }), feature)
    outcome = (tmp_path / "proxy_outcomes.parquet").resolve()
    pq.write_table(pa.table({
        "symbol": ["AAPL"], "decision_session": [date(2020, 1, 2)],
        "entry_session": [date(2020, 1, 3)], "exit_session": [date(2020, 1, 9)],
        "entry_open": [1.0], "exit_close": [1.01], "proxy_return": [0.01],
        "status": ["READY_UNTRUSTED_RAW_PRICE_PROXY"], "target_semantics": ["proxy"],
        "historical_proxy": [True], "canonical_target_equivalent": [False],
    }), outcome)
    plan = build_caveated_joined_trial_input_plan((feature,), feature_release_id="a" * 64, outcome_path=outcome, outcome_release_id="b" * 64, work_root=(tmp_path / "work").resolve())
    assert len(plan["join_build_plan_id"]) == 64
    assert len(plan["implementation_sha256"]) == 64
    assert plan["required_authority"]["real_row_access"] is True
    assert plan["limits"]["staging_bytes_at_most"] == (feature.stat().st_size + outcome.stat().st_size) * 4
    assert plan["output"]["accepted_release"] is False


def test_execution_rejects_missing_approval_before_opening_any_input(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_DISCOVERY_JOIN_BUILD_APPROVED", raising=False)
    with pytest.raises(Exception, match="confirmation"):
        execute_caveated_joined_trial_input(
            tmp_path / "feature", outcome_release_directory=tmp_path / "outcome",
            accepted_root=tmp_path / "accepted", work_root=(tmp_path / "work").resolve(),
            approved_join_build_plan_id="a" * 64,
        )


def test_publication_and_wfa_plans_are_caveated_and_no_write(tmp_path) -> None:
    stage = _joined_stage(tmp_path).resolve()
    publication = build_caveated_joined_publication_plan(
        stage, join_build_plan_id="a" * 64, feature_release_id="b" * 64,
        outcome_release_id="c" * 64, repo_root=REPO,
        created_at="2026-08-01T00:00:00Z",
    )
    assert publication["prospective_release"]["dataset"] == "alpaca_discovery_joined_trial_inputs"
    assert publication["publication"]["spool_files_included"] is False
    assert publication["publication"]["writes"] == 0
    accepted = (tmp_path / "accepted").resolve()
    package = tmp_path / "package"
    (package / "joined").mkdir(parents=True)
    for source in sorted((stage / "joined").glob("*.parquet")):
        (package / "joined" / source.name).write_bytes(source.read_bytes())
    (package / "source_evidence_manifest.json").write_bytes(canonical_json_bytes(publication["source_evidence"]))
    joined_release = AtomicReleasePublisher(accepted).publish(package, ReleaseManifest.from_dict(publication["prospective_release"]))
    calendar_stage = tmp_path / "calendar-stage"
    calendar_stage.mkdir()
    (calendar_stage / "sessions.parquet").write_bytes(b"synthetic")
    calendar_manifest = build_manifest(calendar_stage, ("sessions.parquet",), project="US_stocks_swing_model_v2", dataset="xnys_sessions", source_epoch="synthetic", role="derived_causal", quality_state="PASS", created_at="2026-08-01T00:00:00Z", row_count=2016, event_start="2016-01-01", event_end="2026-01-01", schema_fingerprint="d" * 64, code_hash="e" * 64, config_hash="f" * 64, environment_hash="0" * 64)
    calendar = AtomicReleasePublisher(accepted).publish(calendar_stage, calendar_manifest)
    wfa = build_unregistered_wfa_plan(joined_release, calendar_release_directory=calendar, accepted_root=accepted, repo_root=REPO)
    assert wfa["validation_scope"]["writes"] == 0
    assert wfa["claims"]["trusted_result_claim"] is False


def test_publisher_requires_approval_then_publishes_only_joined_shards(tmp_path, monkeypatch) -> None:
    stage = _joined_stage(tmp_path).resolve()
    created_at = "2026-08-01T00:00:00Z"
    kwargs = {"join_build_plan_id": "a" * 64, "feature_release_id": "b" * 64, "outcome_release_id": "c" * 64, "repo_root": REPO, "created_at": created_at}
    plan = build_caveated_joined_publication_plan(stage, **kwargs)
    with pytest.raises(Exception, match="confirmation"):
        publish_caveated_joined_release(stage, accepted_root=(tmp_path / "accepted").resolve(), work_root=(tmp_path / "work").resolve(), approved_publication_plan_id=plan["publication_plan_id"], **kwargs)
    monkeypatch.setenv("ALPACA_DISCOVERY_JOIN_PUBLICATION_APPROVED", "YES")
    published = publish_caveated_joined_release(stage, accepted_root=(tmp_path / "accepted").resolve(), work_root=(tmp_path / "work").resolve(), approved_publication_plan_id=plan["publication_plan_id"], **kwargs)
    assert published.name == plan["prospective_release"]["release_id"]
    assert len(tuple((published / "joined").glob("*.parquet"))) == 64
    assert not (published / "feature_spool").exists()
