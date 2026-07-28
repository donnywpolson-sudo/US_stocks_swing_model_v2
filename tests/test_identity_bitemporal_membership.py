from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.identity import (
    BitemporalIdentityLedger,
    _load_identity_release_payload,
    merge_identity_snapshot,
    parse_alpaca_assets,
    project_active_us_equity_assets,
)
from us_stocks_swing_model_v2.providers.identity_readiness import (
    load_alpaca_asset_projection_policy,
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


def _projected_snapshot(
    store: AsReceivedSnapshotStore,
    *,
    retrieved_at: datetime,
    file_time: str,
    assets: tuple[tuple[str, str, str], ...],
):
    raw = json.dumps(
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
    landed = store.land(
        source="alpaca_assets",
        url="https://paper-api.alpaca.markets/v2/assets",
        http_status=200,
        raw=raw,
        headers={"etag": f"projected-{retrieved_at.isoformat()}"},
        retrieved_at=retrieved_at,
        synthetic_permit=_snapshot_permit(),
    )
    policy = load_alpaca_asset_projection_policy(Path(__file__).parents[1])
    projection = project_active_us_equity_assets(
        landed,
        projection_contract=policy["projection_contract"],
        projection_contract_id=policy["projection_contract_id"],
    )
    nasdaq = parse_nasdaq_traded(
        store.land(
            source="nasdaqtraded",
            url=NASDAQ_TRADED_URL,
            http_status=200,
            raw=_nasdaq_bytes(("ABC",), file_time),
            headers={"etag": f"projected-nasdaq-{retrieved_at.isoformat()}"},
            retrieved_at=retrieved_at,
            synthetic_permit=_snapshot_permit(),
        ),
        policy=_nasdaq_policy(),
    )
    return merge_identity_snapshot(projection.records, nasdaq)


def _write_identity_payload(
    root: Path,
    snapshot,
    *,
    row_changes: dict[str, object] | None = None,
) -> None:
    receipt = snapshot.receipt_dict()
    receipt["rows"][0].update(row_changes or {})
    unsigned = json.loads(json.dumps(receipt))
    unsigned.pop("snapshot_id")
    for row in unsigned["rows"]:
        row.pop("identity_snapshot_id")
    snapshot_id = sha256_bytes(canonical_json_bytes(unsigned))
    receipt["snapshot_id"] = snapshot_id
    for row in receipt["rows"]:
        row["identity_snapshot_id"] = snapshot_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "identity_snapshots.json").write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 1,
                "snapshots": [receipt],
            }
        )
    )


def test_identity_release_payload_accepts_exact_generated_snapshot(
    tmp_path: Path,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "active"),),
    )
    payload = tmp_path / "valid-identity-release"
    _write_identity_payload(payload, snapshot)
    assert _load_identity_release_payload(payload, 1) == (snapshot,)


@pytest.mark.parametrize(
    "row_changes",
    [
        {"active": False},
        {"membership_present": False},
        {"nasdaq_snapshot_id": None},
        {"nasdaq_file_created_at": None},
        {"security_type": "UNKNOWN"},
        {"asset_id": ""},
        {"asset_id": None},
        {"symbol": "abc"},
        {"listing_exchange": ""},
        {"abstention_reason": "unexpected"},
        {"eligible": False, "abstention_reason": None},
    ],
)
def test_identity_release_payload_rejects_invalid_canonical_or_eligibility_state(
    tmp_path: Path,
    row_changes: dict[str, object],
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    snapshot = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "active"),),
    )
    payload = tmp_path / "invalid-identity-release"
    _write_identity_payload(payload, snapshot, row_changes=row_changes)
    with pytest.raises((ContractError, IntegrityError)):
        _load_identity_release_payload(payload, 1)


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
    assert absent_row.nasdaq_snapshot_id is None
    assert absent_row.nasdaq_file_created_at is None

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


def test_nasdaq_only_identity_is_superseded_by_later_resolved_asset(
    tmp_path: Path,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    unresolved = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-abc", "ABC", "active"),),
        nasdaq_symbols=("XYZ",),
    )
    resolved = _complete_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc),
        file_time="0716202613:00",
        assets=(("asset-xyz", "XYZ", "active"),),
    )
    unresolved_row = next(row for row in unresolved.rows if row.symbol == "XYZ")
    assert unresolved_row.asset_id.startswith("NASDAQ_UNRESOLVED_")
    assert not unresolved_row.eligible

    ledger = _identity_ledger()
    ledger.append_snapshot(unresolved)
    ledger.append_snapshot(resolved)
    historical = ledger.visible_as_of(
        effective_as_of=unresolved.effective_at,
        known_as_of=unresolved.known_at,
    )
    assert any(row.asset_id == unresolved_row.asset_id for row in historical)
    current = ledger.visible_as_of(
        effective_as_of=resolved.effective_at,
        known_as_of=resolved.known_at,
    )
    xyz = [row for row in current if row.symbol == "XYZ"]
    assert len(xyz) == 1
    assert xyz[0].asset_id == "asset-xyz"
    assert xyz[0].eligible


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
    assert after_reuse["asset-old"].nasdaq_snapshot_id is None
    assert after_reuse["asset-old"].nasdaq_file_created_at is None
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


def test_projection_tombstones_inactive_uuid_and_binds_symbol_reuse_causally(
    tmp_path: Path,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    first = _projected_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc),
        file_time="0715202613:00",
        assets=(("asset-old", "ABC", "active"),),
    )
    reused = _projected_snapshot(
        store,
        retrieved_at=datetime(2026, 7, 16, 19, 0, tzinfo=timezone.utc),
        file_time="0716202613:00",
        assets=(
            ("asset-old", "ABC", "inactive"),
            ("asset-new", "ABC", "active"),
        ),
    )
    assert reused.effective_at == datetime(
        2026, 7, 16, 19, 0, tzinfo=timezone.utc
    )
    assert reused.effective_at > reused.nasdaq_file_created_at
    current_row = next(row for row in reused.rows if row.symbol == "ABC")
    assert current_row.asset_id == "asset-new"
    assert current_row.eligible

    ledger = _identity_ledger()
    ledger.append_snapshot(first)
    ledger.append_snapshot(reused)
    current = {
        row.asset_id: row
        for row in ledger.visible_as_of(
            effective_as_of=reused.effective_at,
            known_as_of=reused.known_at,
        )
    }
    assert not current["asset-old"].membership_present
    assert not current["asset-old"].eligible
    assert current["asset-new"].membership_present
    assert current["asset-new"].eligible


def test_later_observed_alpaca_state_is_not_backdated_to_nasdaq_file_time(
    tmp_path: Path,
) -> None:
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
    before_observation = ledger.visible_as_of(
        effective_as_of=initial.effective_at,
        known_as_of=late_revision.known_at,
    )[0]
    after_observation = ledger.visible_as_of(
        effective_as_of=late_revision.effective_at,
        known_as_of=late_revision.known_at,
    )[0]
    assert initial.effective_at == datetime(
        2026, 7, 15, 18, 0, tzinfo=timezone.utc
    )
    assert late_revision.effective_at == datetime(
        2026, 7, 15, 19, 0, tzinfo=timezone.utc
    )
    assert before_observation.eligible
    assert not after_observation.eligible
    assert after_observation.abstention_reason == "inactive_or_not_tradable"
    assert late_revision.nasdaq_file_created_at.tzinfo is timezone.utc
    assert late_revision.rows[0].nasdaq_file_created_at is not None
    assert late_revision.rows[0].nasdaq_file_created_at.tzinfo is timezone.utc
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


@pytest.mark.parametrize(
    ("file_time", "message"),
    [
        ("0310202402:30", "nonexistent local time"),
        ("1103202401:30", "ambiguous local time"),
    ],
)
def test_nasdaq_file_creation_rejects_dst_gap_and_fold(
    tmp_path: Path,
    file_time: str,
    message: str,
) -> None:
    store = AsReceivedSnapshotStore(tmp_path / "snapshots", allowed_root=tmp_path)
    landed = store.land(
        source="nasdaqtraded",
        url=NASDAQ_TRADED_URL,
        http_status=200,
        raw=_nasdaq_bytes(("ABC",), file_time),
        headers={"etag": "dst-boundary"},
        retrieved_at=datetime(2024, 11, 3, 8, 0, tzinfo=timezone.utc),
        synthetic_permit=_snapshot_permit(),
    )
    with pytest.raises(ContractError, match=message):
        parse_nasdaq_traded(landed, policy=_nasdaq_policy())
