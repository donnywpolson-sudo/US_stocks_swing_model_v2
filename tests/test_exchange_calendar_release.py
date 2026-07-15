from __future__ import annotations

from datetime import date
import json
from pathlib import Path

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes, sha256_file
from us_stocks_swing_model_v2.exchange_calendar import (
    EXCHANGE_CALENDARS_VERSION,
    load_xnys_calendar_release,
    publish_xnys_calendar_release,
)


def test_xnys_release_is_content_addressed_and_pins_holiday_and_early_close(tmp_path) -> None:
    release = publish_xnys_calendar_release(
        staging_root=tmp_path / "stage",
        release_root=tmp_path / "releases",
        start=date(2026, 7, 1),
        end=date(2026, 11, 30),
        created_at="2026-07-15T00:00:00Z",
        code_hash="1" * 64,
        config_hash="2" * 64,
        environment_hash="3" * 64,
    )
    loaded = load_xnys_calendar_release(
        release, accepted_release_root=tmp_path / "releases"
    )
    sessions = set(loaded.calendar.sessions)
    assert date(2026, 7, 3) not in sessions  # Independence Day observed.
    assert date(2026, 7, 6) in sessions
    rows = {row["session"]: row for row in loaded.schedule.to_pylist()}
    assert rows[date(2026, 11, 27)]["early_close"] is True  # Day after Thanksgiving.
    assert loaded.provenance["calendar_version"] == EXCHANGE_CALENDARS_VERSION
    assert loaded.calendar.release_id == release.name
    assert loaded.calendar.trust_eligible
    assert len(loaded.calendar.verification_receipt_id) == 64
    assert "never_infer_sessions_from_observed_bars" in loaded.provenance["source_contract"]


def test_checked_in_calendar_receipt_binds_policy_code_and_non_authority() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (root / "config" / "xnys_calendar_release_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_id = payload.pop("receipt_id")
    assert sha256_bytes(canonical_json_bytes(payload)) == receipt_id
    assert payload["policy_sha256"] == sha256_file(
        root / "config" / "xnys_calendar_policy.json"
    )
    assert payload["code_sha256"] == sha256_file(
        root / "src" / "us_stocks_swing_model_v2" / "exchange_calendar.py"
    )
    assert payload["environment_sha256"] == sha256_file(
        root / "config" / "environment.lock.json"
    )
    assert payload["execution_authority"] is False
    assert payload["session_count"] == 9049
