from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.alpaca_archive_rehabilitation import (
    ArchiveExpectations,
    inspect_alpaca_archive,
    load_alpaca_archive_rehabilitation_policy,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError


REPO = Path(__file__).resolve().parents[1]


def _bar(session: str, close: float) -> dict[str, object]:
    return {
        "c": close,
        "h": close + 1.0,
        "l": close - 1.0,
        "n": 10,
        "o": close - 0.5,
        "t": f"{session}T04:00:00Z",
        "v": 1000,
        "vw": close,
    }


def _write_page(
    path: Path,
    *,
    bars: dict[str, list[dict[str, object]]],
    next_page_token: str | None,
) -> tuple[str, int]:
    content = json.dumps(
        {"bars": bars, "next_page_token": next_page_token},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as stream:
        stream.write(content)
    return hashlib.sha256(content).hexdigest(), len(content)


def _fixture(
    tmp_path: Path,
    *,
    final_token: str | None = None,
    adjustment: str = "raw",
) -> tuple[Path, ArchiveExpectations]:
    root = tmp_path / "legacy-alpaca"
    page_root = root / "native" / "bars" / "raw" / "chunk_00001"
    first = page_root / "page_00001.json.gz"
    second = page_root / "page_00002.json.gz"
    first_identity = _write_page(
        first,
        bars={
            "AAPL": [_bar("2026-07-28", 100.0)],
            "SPY": [_bar("2026-07-28", 500.0)],
        },
        next_page_token="next",
    )
    second_identity = _write_page(
        second,
        bars={
            "AAPL": [_bar("2026-07-29", 101.0)],
            "SPY": [_bar("2026-07-29", 501.0)],
        },
        next_page_token=final_token,
    )
    page_bindings = [
        {
            "path": str(first),
            "sha256": first_identity[0],
            "uncompressed_bytes": first_identity[1],
        },
        {
            "path": str(second),
            "sha256": second_identity[0],
            "uncompressed_bytes": second_identity[1],
        },
    ]
    provenance_root = root / "bars" / "raw"
    provenance_root.mkdir(parents=True)
    for symbol in ("AAPL", "SPY"):
        payload = {
            "adjustment": adjustment,
            "canonical_symbol": symbol,
            "feed": "sip",
            "max_date": "20260729",
            "min_date": "20260728",
            "native_pages": page_bindings,
            "provider_symbol": symbol,
            "request_end_date": "2026-07-30",
            "request_start_date": "2026-07-28",
            "row_count": 2,
            "source": "alpaca_sip_v1",
            "source_route_name": "historical_stock_bars",
            "timeframe": "1Day",
            "validation_passed": False,
        }
        (provenance_root / f"{symbol}.parquet.provenance.json").write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
    expectations = ArchiveExpectations(
        source="alpaca_sip_v1",
        source_route_name="historical_stock_bars",
        feed="sip",
        timeframe="1Day",
        adjustment="raw",
        request_start_date="2026-07-28",
        request_end_date="2026-07-30",
        symbol_count=2,
        page_count=2,
        chunk_count=1,
        row_count=4,
        event_start="2026-07-28",
        event_end="2026-07-29",
        compressed_bytes=first.stat().st_size + second.stat().st_size,
        uncompressed_bytes=first_identity[1] + second_identity[1],
    )
    return root, expectations


def test_checked_in_policy_binds_caveated_alpaca_archive() -> None:
    rehabilitation, rehabilitation_id = (
        load_alpaca_archive_rehabilitation_policy(REPO)
    )

    assert rehabilitation["mode"].endswith("PLAN_ONLY_NO_WRITES")
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


def test_synthetic_archive_inventory_is_deterministic_and_no_write(
    tmp_path: Path,
) -> None:
    root, expectations = _fixture(tmp_path)
    before = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }

    first = inspect_alpaca_archive(root, expectations=expectations)
    second = inspect_alpaca_archive(root, expectations=expectations)

    assert first == second
    assert first["symbols"] == 2
    assert first["pages"] == 2
    assert first["chunks"] == 1
    assert first["rows"] == 4
    assert first["event_start"] == "2026-07-28"
    assert first["event_end"] == "2026-07-29"
    after = {
        path.relative_to(root).as_posix(): (
            path.stat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_inventory_rejects_tampering_contract_drift_and_nonterminal_page(
    tmp_path: Path,
) -> None:
    root, expectations = _fixture(tmp_path / "tamper")
    page = root / "native" / "bars" / "raw" / "chunk_00001" / "page_00001.json.gz"
    with gzip.open(page, "wb") as stream:
        stream.write(b'{"bars":{},"next_page_token":null}')
    with pytest.raises(IntegrityError, match="decompressed identity"):
        inspect_alpaca_archive(root, expectations=expectations)

    root, expectations = _fixture(tmp_path / "contract", adjustment="split")
    with pytest.raises(ContractError, match="request contract"):
        inspect_alpaca_archive(root, expectations=expectations)

    root, expectations = _fixture(tmp_path / "pagination", final_token="still-more")
    with pytest.raises(ContractError, match="terminate exactly once"):
        inspect_alpaca_archive(root, expectations=expectations)
