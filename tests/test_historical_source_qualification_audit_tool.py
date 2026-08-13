from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tools import audit_historical_source_qualification as audit


def test_audit_safe_path_denies_outcome_and_escape_components(tmp_path: Path) -> None:
    (tmp_path / "safe").mkdir()
    target = tmp_path / "safe" / "source.json"
    target.write_text("{}", encoding="utf-8")
    assert audit._safe_path(tmp_path.resolve(), "safe/source.json") == target
    with pytest.raises(audit.AuditError, match="denied component"):
        audit._safe_path(tmp_path.resolve(), "outcomes/source.json")
    with pytest.raises(audit.AuditError, match="unsafe"):
        audit._safe_path(tmp_path.resolve(), "../source.json")


def test_bar_census_reads_only_declared_outcome_free_columns(tmp_path: Path) -> None:
    bars = tmp_path / "bars"
    bars.mkdir()
    path = bars / "year=2021.parquet"
    table = pa.table(
        {
            "asset_id": ["asset-a", "asset-b"],
            "close": [10.5, 20.5],
            "evidence_class": ["LEGACY_DISCOVERY", "LEGACY_DISCOVERY"],
            "high": [11.0, 21.0],
            "historical_membership_proven": [False, False],
            "input_quality_state": [
                "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
                "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
            ],
            "low": [9.5, 19.5],
            "open": [10.0, 20.0],
            "point_in_time_safe": [False, False],
            "provider_symbol": ["AAA", "BBB"],
            "security_type": ["STOCK", "STOCK"],
            "session": [date(2021, 1, 4), date(2021, 1, 4)],
            "volume": [100, 0],
        }
    )
    pq.write_table(table, path)
    result = audit._bar_census(tmp_path, SimpleNamespace(row_count=2))
    assert result["row_count"] == 2
    assert result["unique_asset_ids"] == 2
    assert result["point_in_time_safe_true_rows"] == 0
    assert result["historical_membership_proven_true_rows"] == 0
    assert result["invalid_ohlc_rows"] == 0
    assert result["zero_volume_rows"] == 1


def test_structured_census_reports_counts_and_hashes_without_raw_records(
    tmp_path: Path,
) -> None:
    objects = tmp_path / "source" / "objects"
    objects.mkdir(parents=True)
    (objects / "listing.bin").write_text(
        "symbol,status,ipoDate,delistingDate\nAAA,Active,2020-01-02,null\n"
        "OLD,Delisted,2010-01-04,2021-05-03\n",
        encoding="utf-8",
    )
    result, used = audit._structured_census(
        tmp_path.resolve(),
        {
            "source_id": "listing-fixture",
            "directory": "source/objects",
            "format": "csv",
            "maximum_files": 1,
            "maximum_file_bytes": 4096,
            "date_fields": ["ipoDate", "delistingDate"],
            "type_fields": ["status"],
        },
        maximum_total_bytes=4096,
    )
    assert result["row_count"] == 2
    assert result["status_counts"] == {"Active": 1, "Delisted": 1}
    assert len(result["content_hashes"]) == 1
    assert used > 0
    assert "records" not in result
