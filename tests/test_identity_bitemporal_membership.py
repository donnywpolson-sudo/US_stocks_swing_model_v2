from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.errors import ContractError
from us_stocks_swing_model_v2.identity import (
    BitemporalIdentityLedger,
    merge_identity_snapshot,
    parse_alpaca_assets,
)
from us_stocks_swing_model_v2.providers.nasdaq import (
    NASDAQ_TRADED_URL,
    NasdaqCompletenessPolicy,
    parse_nasdaq_traded,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore


def _nasdaq_bytes(symbols: tuple[str, ...], file_time: str) -> bytes:
    header = (
        "Nasdaq Traded|Symbol|Security Name|Listing Exchange|Market Category|ETF|"
        "Round Lot Size|Test Issue|Financial Status|CQS Symbol|NASDAQ Symbol|NextShares\n"
    )
    rows = "".join(
        f"Y|{symbol}|{symbol} COMMON STOCK|N|Q|N|100|N|N|{symbol}|{symbol}|N\n"
        for symbol in symbols
    )
    return f"{header}{rows}File Creation Time: {file_time}|||||||||||\n".encode()


def _nasdaq_policy() -> NasdaqCompletenessPolicy:
    return NasdaqCompletenessPolicy.synthetic_fixture(
        permit=SyntheticOnlyPermit.create(
            fixture_id="identity-membership",
            scope="NASDAQ_COMPLETENESS_FIXTURE",
        )
    )


def _snapshot_permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="identity-snapshots",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _identity_ledger() -> BitemporalIdentityLedger:
    return BitemporalIdentityLedger(
        synthetic_permit=SyntheticOnlyPermit.create(
            fixture_id="identity-ledger",
            scope="SYNTHETIC_IDENTITY_LEDGER",
        )
    )


def _complete_snapshot(
    store: AsReceivedSnapshotStore,
    *,
    retrieved_at: datetime,
    file_time: str,
    assets: tuple[tuple[str, str, str], ...],
    nasdaq_symbols: tuple[str, ...] | None = None,
):
    symbols = nasdaq_symbols or tuple(symbol for _, symbol, _ in assets)
    alpaca_raw = json.dumps(
        [
            {
                "id": asset_id,
                "symbol": symbol,
                "class": "us_equity",
                "exchange": "NASDAQ",
                "status": status,
                "tradable": status == "active",
            }
            for asset_id, symbol, status in assets
        ]
    ).encode()
    alpaca = parse_alpaca_assets(
        store.land(
            source="alpaca_assets",
            url="https://paper-api.alpaca.markets/v2/assets",
            http_status=200,
            raw=alpaca_raw,
            headers={"etag": f"alpaca-{retrieved_at.isoformat()}"},
            retrieved_at=retrieved_at,
            synthetic_permit=_snapshot_permit(),
        )
    )
    nasdaq = parse_nasdaq_traded(
        store.land(
            source="nasdaqtraded",
            url=NASDAQ_TRADED_URL,
            http_status=200,
            raw=_nasdaq_bytes(symbols, file_time),
            headers={"etag": f"nasdaq-{retrieved_at.isoformat()}"},
            retrieved_at=retrieved_at,
            synthetic_permit=_snapshot_permit(),
        ),
        policy=_nasdaq_policy(),
    )
    return merge_identity_snapshot(alpaca, nasdaq)


def test_alpaca_only_symbol_is_an_explicit_nonmember_until_nasdaq_reappears(
    tmp_path: Path,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    first = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "active"),),
    )
    absent = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc),
        file_time="0716202613:00",
        assets=(("asset-abc", "ABC", "active"),),
        nasdaq_symbols=("XYZ",),
    )
    reappeared = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc),
        file_time="0717202613:00",
        assets=(("asset-abc", "ABC", "active"),),
    )
    absent_row = next(row for row in absent.rows if row.asset_id == "asset-abc")
    assert not absent_row.membership_present
    assert not absent_row.eligible
    assert absent_row.abstention_reason == "missing_nasdaq_identity"

    ledger = _identity_ledger()
    for snapshot in (first, absent, reappeared):
        ledger.append_snapshot(snapshot)
    visible = {
        row.asset_id: row
        for row in ledger.visible_as_of(
            effective_as_of=reappeared.effective_at,
            known_as_of=reappeared.known_at,
        )
    }
    assert visible["asset-abc"].membership_present
    assert visible["asset-abc"].eligible


def test_complete_snapshots_tombstone_disappearance_reuse_and_symbol_change(tmp_path: Path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    first = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-old", "ABC", "active"),),
    )
    reused = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc),
        file_time="0716202613:00",
        assets=(("asset-new", "ABC", "active"),),
    )
    renamed = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc),
        file_time="0717202613:00",
        assets=(("asset-new", "ABD", "active"),),
    )
    ledger = _identity_ledger()
    ledger.append_snapshot(first)
    ledger.append_snapshot(reused)
    after_reuse = {
        row.asset_id: row
        for row in ledger.visible_as_of(
            effective_as_of=reused.effective_at,
            known_as_of=reused.known_at,
        )
    }
    assert not after_reuse["asset-old"].membership_present
    assert not after_reuse["asset-old"].eligible
    assert after_reuse["asset-new"].membership_present
    assert after_reuse["asset-new"].symbol == "ABC"
    ledger.append_snapshot(renamed)
    after_change = {
        row.asset_id: row
        for row in ledger.visible_as_of(
            effective_as_of=renamed.effective_at,
            known_as_of=renamed.known_at,
        )
    }
    assert after_change["asset-new"].symbol == "ABD"
    assert after_change["asset-new"].eligible


def test_late_same_effective_revision_is_not_visible_before_known_at(tmp_path: Path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    initial = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "active"),),
    )
    late_revision = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "inactive"),),
    )
    ledger = _identity_ledger()
    ledger.append_snapshot(initial)
    ledger.append_snapshot(late_revision)
    before_revision = ledger.visible_as_of(
        effective_as_of=initial.effective_at,
        known_as_of=datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc),
    )[0]
    after_revision = ledger.visible_as_of(
        effective_as_of=initial.effective_at,
        known_as_of=late_revision.known_at,
    )[0]
    assert before_revision.eligible
    assert not after_revision.eligible
    assert after_revision.abstention_reason == "inactive_or_not_tradable"
    receipt = late_revision.receipt_dict()
    assert receipt["nasdaq_file_created_at"] == "2026-07-15T17:00:00Z"


def test_nasdaq_file_creation_cannot_be_future_relative_to_retrieval(tmp_path: Path) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    landed = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_nasdaq_bytes(("ABC",), "0715202613:00"),
        headers={"etag": "future"},
        retrieved_at=datetime(2026, 7, 15, 16, 59, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    with pytest.raises(ContractError, match="later than retrieval"):
        parse_nasdaq_traded(landed, policy=_nasdaq_policy())
