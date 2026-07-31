from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.alpaca_corporate_action_preflight import build_corporate_action_preflight
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, build_manifest


REPO = Path(__file__).resolve().parents[1]


def _release(tmp_path: Path) -> tuple[Path, Path]:
    stage = tmp_path / "stage"; stage.mkdir(); (stage / "bars").mkdir()
    (stage / "bars" / "year=2016.parquet").write_bytes(b"synthetic")
    manifest = build_manifest(stage, ("bars/year=2016.parquet",), project="US_stocks_swing_model_v2", dataset="alpaca_historical_daily_bars", source_epoch="synthetic", role="legacy_discovery_only", quality_state="LEGACY_CAVEATED", created_at="2026-07-31T20:00:00Z", row_count=1, event_start="2016-01-04", event_end="2016-01-05", schema_fingerprint="a" * 64, code_hash="b" * 64, config_hash="c" * 64, environment_hash="d" * 64)
    accepted = (tmp_path / "accepted").resolve()
    return AtomicReleasePublisher(accepted).publish(stage, manifest), accepted


def test_preflight_is_no_network_and_preserves_outcome_blockers(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    plan = build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("AAPL",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
    assert plan["request"]["url"].startswith("https://data.alpaca.markets/v1/corporate-actions?")
    assert plan["outcome_boundary"]["outcomes_may_compute"] is False
    assert all(value is False for value in plan["authorities"].values())


def test_preflight_rejects_noncanonical_symbols(tmp_path: Path) -> None:
    release, accepted = _release(tmp_path)
    with pytest.raises(ContractError):
        build_corporate_action_preflight(release_directory=release, accepted_root=accepted, start=date(2016, 1, 4), end=date(2016, 1, 5), symbols=("aapl",), max_pages=1, created_at="2026-07-31T20:00:00Z", repo_root=REPO)
