from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from us_stocks_swing_model_v2.alpaca_archive_rehabilitation import (
    load_alpaca_archive_rehabilitation_policy,
    verify_rehabilitated_alpaca_release,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes


REPO = Path(__file__).resolve().parents[1]


def test_checked_in_policy_binds_caveated_alpaca_archive() -> None:
    rehabilitation, rehabilitation_id = (
        load_alpaca_archive_rehabilitation_policy(REPO)
    )

    assert rehabilitation["mode"].endswith("RETIRED_ACCEPTED_RELEASE_ONLY")
    assert rehabilitation["schema_version"] == 2
    assert "legacy_archive_root" not in rehabilitation
    assert rehabilitation["accepted_release"] == {
        "accepted_root": "data/vault/accepted",
        "relative_directory": (
            "alpaca_legacy_daily_bars/"
            "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1"
        ),
        "release_id": "20f0fe6c054db312d83ce479c7bd14ea83be501bc19c17dfc83af830ba68c2e1",
        "required_file_count": 201,
        "required_payload_bytes": 99_868_172,
    }
    assert rehabilitation["input_contract"]["expected_symbol_count"] == 780
    assert rehabilitation["input_contract"]["expected_page_count"] == 198
    assert rehabilitation["input_contract"]["expected_row_count"] == 1_878_977
    assert rehabilitation["evidence_boundary"][
        "input_is_original_http_response_bytes"
    ] is False
    assert rehabilitation["prospective_release"]["role"] == "legacy_discovery_only"
    assert rehabilitation["legacy_universe_boundary"]["selection_state"] == (
        "legacy_universe_selection_unresolved"
    )
    assert rehabilitation["legacy_universe_boundary"]["active_source_eligible"] is False
    assert rehabilitation["legacy_universe_boundary"]["training_or_evaluation_eligible"] is False
    assert rehabilitation_id == sha256_bytes(canonical_json_bytes(rehabilitation))


def test_retired_rehabilitation_verifies_only_the_v2_accepted_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = json.loads(
        (REPO / "config" / "alpaca_archive_rehabilitation_policy.json").read_text(
            encoding="utf-8"
        )
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "alpaca_archive_rehabilitation_policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    release_dir = (
        tmp_path
        / "data"
        / "vault"
        / "accepted"
        / "alpaca_legacy_daily_bars"
        / policy["accepted_release"]["release_id"]
    )
    release_dir.mkdir(parents=True)
    files = [
        SimpleNamespace(path="bars.parquet", size=99_868_172),
        SimpleNamespace(path="rehabilitation_receipt.json", size=0),
        SimpleNamespace(path="source_evidence_manifest.json", size=0),
    ]
    files.extend(
        SimpleNamespace(path=f"native_pages/page_{index:03d}.json.gz", size=0)
        for index in range(198)
    )
    manifest = SimpleNamespace(
        release_id=policy["accepted_release"]["release_id"],
        dataset="alpaca_legacy_daily_bars",
        source_epoch="alpaca_sip_legacy_canonicalized_payload_20160104_20260710_v1",
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
        row_count=1_878_977,
        files=tuple(files),
    )
    calls: list[tuple[Path, Path]] = []

    def verify(release: Path, *, accepted_root: Path):
        calls.append((release, accepted_root))
        return manifest

    monkeypatch.setattr(
        "us_stocks_swing_model_v2.alpaca_archive_rehabilitation.verify_accepted_release",
        verify,
    )
    result = verify_rehabilitated_alpaca_release(tmp_path)

    assert calls == [(release_dir.resolve(), (tmp_path / "data/vault/accepted").resolve())]
    assert result["accepted_release"]["release_id"] == manifest.release_id
    assert result["accepted_release"]["file_count"] == 201
    assert result["accepted_release"]["payload_bytes"] == 99_868_172
    assert result["accepted_release"]["role"] == "legacy_discovery_only"
    assert result["accepted_release"]["quality_state"] == "LEGACY_CAVEATED"
