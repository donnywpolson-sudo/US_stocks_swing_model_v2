from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from us_stocks_swing_model_v2.alpaca_free_bounded import EvidenceClass
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.free_source_evidence import RawEvidenceStore
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2 import prospective_liquidity_warmup as warmup


ROOT = Path(__file__).resolve().parents[1]


def _calendar() -> SimpleNamespace:
    start = date(2026, 4, 1)
    sessions = [start + timedelta(days=index) for index in range(100)]
    table = pa.Table.from_pylist([
        {
            "session": session,
            "open_at": datetime.combine(session, datetime.min.time(), timezone.utc) + timedelta(hours=13, minutes=30),
            "close_at": datetime.combine(session, datetime.min.time(), timezone.utc) + timedelta(hours=20),
        }
        for session in sessions
    ])
    return SimpleNamespace(calendar=SimpleNamespace(release_id="a" * 64), schedule=table)


def _snapshot(path: Path, calendar: SimpleNamespace, count: int = 6) -> dict[str, object]:
    sessions = calendar.schedule.to_pylist()
    candidates = [
        {
            "stable_asset_id": f"{index:064x}",
            "symbol": f"S{index:03d}",
            "candidate_eligible": True,
            "inclusion_or_exclusion_reasons": ["ELIGIBLE_FOR_T_MINUS_1_LIQUIDITY_INPUT"],
            "evidence_class": "PROSPECTIVE_AS_OBSERVED",
        }
        for index in range(count)
    ]
    unsigned = {
        "schema_version": 1,
        "session": sessions[-1]["session"].isoformat(),
        "information_cutoff_session": sessions[-2]["session"].isoformat(),
        "evidence_class": "PROSPECTIVE_AS_OBSERVED",
        "candidates": candidates,
    }
    payload = {**unsigned, "universe_snapshot_id": sha256_bytes(canonical_json_bytes(unsigned))}
    path.write_bytes(canonical_json_bytes(payload))
    return payload


def test_warmup_plan_is_deterministic_bounded_and_rfc3339(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calendar = _calendar()
    monkeypatch.setattr(warmup, "load_qualified_profile_calendar", lambda **_kwargs: calendar)
    source = _snapshot(tmp_path / "source.json", calendar)
    first = warmup.build_liquidity_warmup_plan(
        repository_root=ROOT, source_snapshot_path=tmp_path / "source.json", pilot_symbol_count=5
    )
    second = warmup.build_liquidity_warmup_plan(
        repository_root=ROOT, source_snapshot_path=tmp_path / "source.json", pilot_symbol_count=5
    )
    assert first.warmup_plan_id == second.warmup_plan_id
    assert len(first.sessions) == 90
    assert sum(len(unit.symbols) for unit in first.units) == 5
    query = dict(first.units[0].source_plan.canonical_query)
    assert query["feed"] == "sip"
    assert query["timeframe"] == "1Day"
    assert query["adjustment"] == "raw"
    assert query["start"].endswith("Z") and query["end"].endswith("Z")
    assert first.source_snapshot_id == source["universe_snapshot_id"]


def test_new_soak_generation_starts_at_zero_and_preserves_failed_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calendar = _calendar()
    monkeypatch.setattr(warmup, "load_qualified_profile_calendar", lambda **_kwargs: calendar)
    monkeypatch.setattr(
        warmup,
        "validate_capture_ledger",
        lambda **_kwargs: {"soak": {"state": "PROSPECTIVE_CAPTURE_SOAK_FAILED"}},
    )
    monkeypatch.setattr(warmup, "_git_commit_exists", lambda *_args: True)
    data = tmp_path / "data"
    data.mkdir()
    result = warmup.start_soak_generation(
        repository_root=tmp_path,
        original_ledger_path=data / "old.jsonl",
        generation_ledger_path=data / "generations.jsonl",
        remediation_commit="b" * 40,
        sip_availability_rule="RFC3339_DAILY_BAR_TIMESTAMP_INTERVAL",
        warmup_checkpoint_id="c" * 64,
        universe_snapshot_id="d" * 64,
        started_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    assert result["state"] == "PROSPECTIVE_CAPTURE_SOAK_NOT_STARTED"
    assert result["completed_consecutive_sessions"] == 0
    assert result["inherited_completed_session_credit"] == 0
    assert result["original_failed_soak_state"] == "PROSPECTIVE_CAPTURE_SOAK_FAILED"
    assert warmup.validate_soak_generations(data / "generations.jsonl")["generation_count"] == 1


def test_soak_generation_rejects_nonexistent_remediation_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(warmup, "_git_commit_exists", lambda *_args: False)
    with pytest.raises(ContractError, match="does not identify"):
        warmup.start_soak_generation(
            repository_root=tmp_path,
            original_ledger_path=tmp_path / "old.jsonl",
            generation_ledger_path=tmp_path / "data" / "generations.jsonl",
            remediation_commit="b" * 40,
            sip_availability_rule="RFC3339",
            warmup_checkpoint_id="c" * 64,
            universe_snapshot_id="d" * 64,
            started_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
        )
