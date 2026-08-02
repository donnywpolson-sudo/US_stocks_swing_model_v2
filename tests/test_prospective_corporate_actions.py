from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.prospective_corporate_actions import (
    build_prospective_corporate_action_capture_plan,
    build_prospective_corporate_action_publication_plan,
)
from us_stocks_swing_model_v2.releases import AtomicReleasePublisher, ReleaseFile, ReleaseManifest

REPO = Path(__file__).parents[1]


def _release(tmp_path: Path, accepted: Path, *, dataset: str, role: str, name: str, upstream: tuple[str, ...] = ()) -> Path:
    stage = tmp_path / f"stage-{name}"
    stage.mkdir()
    payload = canonical_json_bytes({"name": name})
    (stage / "payload.json").write_bytes(payload)
    manifest = ReleaseManifest(schema_version=1, project="US_stocks_swing_model_v2", dataset=dataset, source_epoch="prospective_sip_v1", role=role, quality_state="PASS", created_at="2026-08-04T00:00:00Z", row_count=1, event_start="2026-08-03", event_end="2026-08-03", upstream_release_ids=tuple(sorted(upstream)), schema_fingerprint="1" * 64, code_hash="2" * 64, config_hash="3" * 64, environment_hash="4" * 64, files=(ReleaseFile("payload.json", len(payload), sha256_bytes(payload)),), release_id="")
    manifest = replace(manifest, release_id=sha256_bytes(canonical_json_bytes(manifest.unsigned_dict())))
    return AtomicReleasePublisher(accepted).publish(stage, manifest)


def _inputs(tmp_path: Path):
    accepted = tmp_path / "accepted"; accepted.mkdir(parents=True)
    identity = _release(tmp_path, accepted, dataset="identity", role="prospective_as_received", name="identity")
    calendar = _release(tmp_path, accepted, dataset="xnys_sessions", role="derived_causal", name="calendar")
    bars = _release(tmp_path, accepted, dataset="alpaca_daily_bars", role="prospective_as_received", name="bars", upstream=(identity.name, calendar.name))
    return accepted, identity, bars, calendar


def test_capture_plan_binds_only_prospective_lineage_and_has_zero_authority(tmp_path: Path) -> None:
    accepted, identity, bars, calendar = _inputs(tmp_path)
    plan = build_prospective_corporate_action_capture_plan(repository_root=REPO, accepted_root=accepted, identity_release_directory=identity, bars_release_directory=bars, calendar_release_directory=calendar, symbols=("AAPL", "SPY"), effective_start_session=date(2026, 7, 28), effective_end_session=date(2026, 8, 8))
    assert plan["authorities"]["network_calls"] == 0
    assert plan["coverage"]["unresolved_rows_remain_in_denominator"] is True
    assert len(plan["capture_plan_id"]) == 64


def test_capture_plan_rejects_orphan_bars_and_publication_binds_raw_census(tmp_path: Path) -> None:
    accepted, identity, _, calendar = _inputs(tmp_path)
    orphan = _release(tmp_path, accepted, dataset="alpaca_daily_bars", role="prospective_as_received", name="orphan")
    with pytest.raises(ContractError, match="identity/calendar lineage"):
        build_prospective_corporate_action_capture_plan(repository_root=REPO, accepted_root=accepted, identity_release_directory=identity, bars_release_directory=orphan, calendar_release_directory=calendar, symbols=("AAPL",), effective_start_session=date(2026, 7, 28), effective_end_session=date(2026, 8, 8))
    accepted, identity, bars, calendar = _inputs(tmp_path / "second")
    plan = build_prospective_corporate_action_capture_plan(repository_root=REPO, accepted_root=accepted, identity_release_directory=identity, bars_release_directory=bars, calendar_release_directory=calendar, symbols=("AAPL",), effective_start_session=date(2026, 7, 28), effective_end_session=date(2026, 8, 8))
    publication = build_prospective_corporate_action_publication_plan(capture_plan=plan, snapshot_ids=("a" * 64,), raw_sha256=("b" * 64,), coverage_id="c" * 64)
    assert publication["authorities"]["release_publication"] is False
    assert publication["landed_raw"]["snapshot_ids"] == ["a" * 64]
