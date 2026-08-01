from __future__ import annotations

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from us_stocks_swing_model_v2.research.splits import TemporalSamples
from us_stocks_swing_model_v2.unregistered_alpaca_discovery_wfa import (
    FEATURE_COLUMNS,
    OUTCOME_COLUMNS,
    UnregisteredDiscoveryDataset,
    assess_streaming_join_layout,
    execute_unregistered_discovery_wfa,
    iter_caveated_parquet_batches,
)


def test_executes_eight_caveated_chronological_folds_in_memory() -> None:
    sessions = np.repeat(np.arange(2016, dtype=np.int64), 2)
    count = len(sessions)
    dataset = UnregisteredDiscoveryDataset(tuple(f"s{n}" for n in range(count)), np.column_stack((sessions / 2016, np.ones(count), np.zeros(count))).astype(float), np.where(sessions % 3 == 0, .01, -.01).astype(float), TemporalSamples(sessions, sessions + 1, sessions + 5, sessions + 5))
    result = execute_unregistered_discovery_wfa(dataset, session_count=2016)
    assert len(result["folds"]) == 8
    assert result["writes"] == 0
    assert result["trusted_result_claim"] is False


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
                "symbol": ["AAPL"], "decision_session": ["2020-01-02"],
                "d0_raw_intraday_return": [0.01], "trailing_5_session_raw_return": [0.02],
                "trailing_5_session_raw_volatility": [0.03], "status": ["READY"],
            }),
            path,
        )
        feature_paths.append(path)
    outcome_path = tmp_path / "proxy_outcomes.parquet"
    pq.write_table(
        pa.table({
            "symbol": ["AAPL"], "decision_session": ["2020-01-02"],
            "entry_session": ["2020-01-03"], "exit_session": ["2020-01-09"],
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
