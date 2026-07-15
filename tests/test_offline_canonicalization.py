from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from us_stocks_swing_model_v2.canonical.alpaca import (
    AlpacaNativeManifest,
    NativePageSpec,
    NativeRequestSpec,
    materialize_qualification_release,
    reparse_native_raw,
)
from us_stocks_swing_model_v2.canonical.hfdl import (
    HFDL_NATIVE_SCHEMA,
    validate_and_tag_hfdl,
    write_tagged_hfdl_legacy,
    write_tagged_hfdl_legacy_epochs,
)
from us_stocks_swing_model_v2.common import sha256_file
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError


def _hfdl_fixture(root: Path) -> tuple[Path, Path]:
    parquet = root / "ABC.parquet"
    table = pa.Table.from_pydict(
        {
            "ticker": ["ABC", "ABC"],
            "per": ["D", "D"],
            "date": ["20220303", "20220304"],
            "time": ["000000", "000000"],
            "open": [10.0, 11.0],
            "high": [11.0, 12.0],
            "low": [9.0, 10.0],
            "close": [10.5, 11.5],
            "vol": [100, 200],
            "openint": [0, 0],
        },
        schema=HFDL_NATIVE_SCHEMA,
    )
    pq.write_table(table, parquet)
    sidecar = root / "ABC.parquet.provenance.json"
    sidecar.write_text(
        json.dumps(
            {
                "canonical_symbol": "ABC",
                "created_at_utc": "2026-07-15T00:00:00Z",
                "row_count": 2,
                "sha256": sha256_file(parquet),
                "timeframe": "daily",
                "validation_passed": True,
                "version": "clean",
                "source_limitations": [
                    "Universe is a fixed snapshot.",
                    "Pre-March 2022 and post-March 2022 feeds differ.",
                    "Clean files are source-adjusted.",
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return parquet, sidecar


def test_hfdl_validator_tags_feed_break_and_is_clean_room_deterministic(tmp_path: Path) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    result = validate_and_tag_hfdl(parquet, sidecar)
    assert result.epoch_counts == {"hfdl_pitrading_consolidated": 1, "hfdl_iex_only": 1}
    assert result.table.column("point_in_time_safe").to_pylist() == [False, False]
    assert set(result.table.column("point_in_time_state").to_pylist()) == {"HISTORICAL_PROXY"}
    assert set(result.table.column("historical_availability_state").to_pylist()) == {
        "UNKNOWN_NOT_AS_RECEIVED"
    }
    assert "retrieved_at" not in result.table.column_names
    assert "source_retrieved_at" in result.table.column_names
    assert result.release_metadata()["point_in_time_state"] != "PIT_CONFIRMED"
    with pytest.raises(ContractError, match="separate releases"):
        write_tagged_hfdl_legacy(result, tmp_path / "pooled" / "bars.parquet")
    first = write_tagged_hfdl_legacy_epochs(result, tmp_path / "clean1")
    second = write_tagged_hfdl_legacy_epochs(result, tmp_path / "clean2")
    assert set(first) == {"hfdl_pitrading_consolidated", "hfdl_iex_only"}
    assert all(first[epoch].read_bytes() == second[epoch].read_bytes() for epoch in first)


def test_hfdl_sidecar_hash_and_duplicate_sessions_fail_closed(tmp_path: Path) -> None:
    parquet, sidecar = _hfdl_fixture(tmp_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    sidecar.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IntegrityError, match="hash differs"):
        validate_and_tag_hfdl(parquet, sidecar)


def _native_manifest(root: Path, *, duplicate: bool = False, broken_token: bool = False) -> AlpacaNativeManifest:
    root.mkdir(parents=True, exist_ok=True)
    page1_payload = {
        "bars": {
            "ABC": [
                {"o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100, "n": 10, "vw": 10.2, "t": "2026-07-13T04:00:00Z"}
            ]
        },
        "next_page_token": "token-2",
    }
    second_session = "2026-07-13T04:00:00Z" if duplicate else "2026-07-14T04:00:00Z"
    page2_payload = {
        "bars": {
            "ABC": [
                {"o": 10.5, "h": 12.0, "l": 10.0, "c": 11.5, "v": 120, "n": 11, "vw": 11.2, "t": second_session}
            ]
        },
        "next_page_token": None,
    }
    specs = []
    for index, payload in enumerate((page1_payload, page2_payload), start=1):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        path = root / f"page{index}.json.gz"
        path.write_bytes(gzip.compress(raw, mtime=0))
        specs.append(
            NativePageSpec(
                path=path,
                sha256=sha256_file(path),
                uncompressed_bytes=len(raw),
                page_index=index,
                page_token_in=None if index == 1 else ("wrong" if broken_token else "token-2"),
                next_page_token_expected="token-2" if index == 1 else None,
            )
        )
    return AlpacaNativeManifest(
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        asof="legacy_default_mapping",
        retrieved_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
        source_epoch="alpaca_sip_legacy_qualification_raw_v1",
        quality_state="FAIL",
        requests=(NativeRequestSpec("request-1", tuple(specs)),),
        symbol_to_asset_id={"ABC": "asset-abc"},
    )


def test_alpaca_native_reparse_and_release_are_deterministic_and_non_active(tmp_path: Path) -> None:
    result = reparse_native_raw(_native_manifest(tmp_path))
    assert result.row_count == 2
    assert result.quality_state == "FAIL"
    assert set(result.table.column("evidence_class").to_pylist()) == {"QUALIFICATION_EVIDENCE"}
    assert set(result.table.column("point_in_time_safe").to_pylist()) == {False}
    kwargs = dict(created_at="2026-07-15T00:00:00Z", code_hash="1" * 64, config_hash="2" * 64, environment_hash="3" * 64)
    first_manifest, first = materialize_qualification_release(
        result, staging_dir=tmp_path / "stage1", release_root=tmp_path / "releases1", **kwargs
    )
    second_manifest, second = materialize_qualification_release(
        result, staging_dir=tmp_path / "stage2", release_root=tmp_path / "releases2", **kwargs
    )
    assert first_manifest == second_manifest
    assert first_manifest.role == "qualification_evidence_only"
    assert first_manifest.quality_state == "FAIL"
    assert (first / "bars.parquet").read_bytes() == (second / "bars.parquet").read_bytes()


def test_alpaca_duplicate_and_pagination_poison_fail(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="duplicate"):
        reparse_native_raw(_native_manifest(tmp_path / "dup", duplicate=True))
    other = tmp_path / "token"
    other.mkdir()
    with pytest.raises(ContractError, match="token breaks"):
        reparse_native_raw(_native_manifest(other, broken_token=True))
