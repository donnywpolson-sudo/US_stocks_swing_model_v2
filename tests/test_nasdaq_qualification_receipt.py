import json
from pathlib import Path

from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes, sha256_file


def test_nasdaq_qualification_receipt_is_code_bound_and_non_active() -> None:
    root = Path(__file__).resolve().parents[1]
    payload = json.loads(
        (root / "config" / "nasdaq_qualification_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    receipt_id = payload.pop("receipt_id")
    assert sha256_bytes(canonical_json_bytes(payload)) == receipt_id
    assert payload["parser_code_sha256"] == sha256_file(
        root / "src" / "us_stocks_swing_model_v2" / "providers" / "nasdaq.py"
    )
    assert payload["snapshot_store_code_sha256"] == sha256_file(
        root / "src" / "us_stocks_swing_model_v2" / "providers" / "snapshots.py"
    )
    assert payload["record_count"] == sum(payload["security_type_counts"].values())
    assert payload["eligible_stock_or_etf_count"] == (
        payload["security_type_counts"]["STOCK"]
        + payload["security_type_counts"]["ETF"]
    )
    assert payload["activation_state"].startswith("NOT_ACTIVE_")
    assert "historical_membership_backfill" in payload["prohibitions"]
