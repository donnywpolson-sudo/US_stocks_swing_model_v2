from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pyarrow as pa
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_canonical_bars import (
    ACTIVE_ALPACA_SCHEMA,
    BARS_FILENAME,
    RECEIPT_FILENAME,
    SOURCE_NAME,
    _selected_asset_ids,
    build_canonical_bars_candidate,
    build_canonical_bars_fixture_plan,
    build_canonical_bars_publication_plan,
    publish_canonical_bars,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore


REPO = Path(__file__).resolve().parents[1]
REQUESTED_AT = datetime(2026, 7, 31, 0, 30, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 7, 31, 0, 31, tzinfo=timezone.utc)
IDENTITY_SNAPSHOT_ID = (
    "679c22119b9e3a9cdf19424ab9eccef5dae85bb5cb7be70502bdc597d2932df6"
)
ASSET_IDS = {
    "AAPL": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "SPY": "b28f4066-5c6d-479b-a2af-85dc1a8f16fb",
}


def _clock() -> TrustedClock:
    permit = SyntheticOnlyPermit.create(
        fixture_id="alpaca-canonical-bars-clock",
        scope="TRUSTED_CLOCK_FIXED_TIME",
    )
    return TrustedClock.synthetic_fixed(REQUESTED_AT, permit=permit)


def _permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="alpaca-canonical-bars-snapshot",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _payload(*, next_page_token: str | None = None) -> bytes:
    return canonical_json_bytes(
        {
            "bars": {
                "AAPL": [
                    {
                        "t": "2026-07-30T04:00:00Z",
                        "o": 210.0,
                        "h": 214.0,
                        "l": 209.0,
                        "c": 213.0,
                        "v": 1000,
                        "n": 100,
                        "vw": 212.0,
                    }
                ],
                "SPY": [
                    {
                        "t": "2026-07-30T04:00:00Z",
                        "o": 630.0,
                        "h": 634.0,
                        "l": 628.0,
                        "c": 633.0,
                        "v": 2000,
                        "n": 200,
                        "vw": 631.0,
                    }
                ],
            },
            "next_page_token": next_page_token,
        }
    )


def _identity_snapshot() -> dict[str, object]:
    return {
        "schema_version": 1,
        "snapshot_id": IDENTITY_SNAPSHOT_ID,
        "rows": [
            {
                "symbol": symbol,
                "asset_id": ASSET_IDS[symbol],
                "eligible": True,
                "active": True,
                "membership_present": True,
                "security_type": "STOCK" if symbol == "AAPL" else "ETF",
                "identity_snapshot_id": IDENTITY_SNAPSHOT_ID,
            }
            for symbol in ("AAPL", "SPY")
        ],
    }


def test_identity_snapshot_validator_uses_accepted_snapshot_id_schema() -> None:
    assert _selected_asset_ids(
        _identity_snapshot(),
        expected_snapshot_id=IDENTITY_SNAPSHOT_ID,
        expected_asset_ids=ASSET_IDS,
    ) == ASSET_IDS


def test_identity_snapshot_validator_rejects_old_or_row_drifted_binding() -> None:
    old_shape = _identity_snapshot()
    old_shape["identity_snapshot_id"] = old_shape.pop("snapshot_id")
    with pytest.raises(IntegrityError, match="snapshot identity differs"):
        _selected_asset_ids(
            old_shape,
            expected_snapshot_id=IDENTITY_SNAPSHOT_ID,
            expected_asset_ids=ASSET_IDS,
        )

    row_drift = _identity_snapshot()
    row_drift["rows"][0]["identity_snapshot_id"] = "0" * 64
    with pytest.raises(IntegrityError, match="not eligible and exact"):
        _selected_asset_ids(
            row_drift,
            expected_snapshot_id=IDENTITY_SNAPSHOT_ID,
            expected_asset_ids=ASSET_IDS,
        )


def _snapshot(
    tmp_path: Path,
    plan: dict[str, object],
    *,
    raw: bytes | None = None,
):
    allowed = tmp_path / "data"
    allowed.mkdir()
    store = AsReceivedSnapshotStore(
        allowed / "as_received",
        allowed_root=allowed,
    )
    return store.land(
        source=SOURCE_NAME,
        url=plan["request"]["url"],
        http_status=200,
        raw=raw or _payload(),
        headers={"content-type": "application/json"},
        retrieved_at=RETRIEVED_AT,
        synthetic_permit=_permit(),
        max_bytes=1048576,
        requested_at=REQUESTED_AT,
        request_plan_id=plan["network_request_plan"]["plan_id"],
    )


def test_fixture_plan_is_deterministic_no_write_and_exactly_bounded() -> None:
    before = tuple(sorted(path.as_posix() for path in REPO.glob("data/**/*")))
    first = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    second = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    after = tuple(sorted(path.as_posix() for path in REPO.glob("data/**/*")))

    assert first == second
    assert before == after
    assert first["request"] == {
        "method": "GET",
        "url": (
            "https://data.alpaca.markets/v2/stocks/bars?"
            "symbols=AAPL%2CSPY&start=2026-07-30T04%3A00%3A00Z&"
            "end=2026-07-30T23%3A30%3A00Z&timeframe=1Day&"
            "adjustment=raw&feed=sip&sort=asc&limit=10000"
        ),
        "symbols": ["AAPL", "SPY"],
        "start": "2026-07-30T04:00:00Z",
        "end": "2026-07-30T23:30:00Z",
        "feed": "sip",
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "sort": "asc",
        "limit": 10000,
        "expected_sessions": ["2026-07-30"],
    }
    assert first["network_request_plan"]["max_pages"] == 1
    assert first["network_request_plan"]["timeout_seconds"] == 30
    assert first["network_request_plan"]["max_response_bytes"] == 1048576
    assert first["host_timeout_seconds"] == 120
    assert not any(first["authorities"].values())


def test_schema_v2_snapshot_preserves_request_binding_and_builds_candidate(
    tmp_path: Path,
) -> None:
    plan = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    snapshot = _snapshot(tmp_path, plan)
    loaded = AsReceivedSnapshotStore(
        tmp_path / "data" / "as_received",
        allowed_root=tmp_path / "data",
    ).load(snapshot.root)

    assert loaded.requested_at == REQUESTED_AT
    assert loaded.request_plan_id == plan["network_request_plan"]["plan_id"]
    receipt = json.loads((loaded.root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["schema_version"] == 2
    assert receipt["requested_at"] == "2026-07-31T00:30:00Z"

    candidate = build_canonical_bars_candidate(
        loaded,
        acquisition_plan=plan,
        synthetic=True,
    )
    assert candidate.table.schema == ACTIVE_ALPACA_SCHEMA
    assert candidate.row_count == 2
    assert candidate.symbols == ("AAPL", "SPY")
    assert candidate.table.column("available_at").to_pylist() == [
        RETRIEVED_AT,
        RETRIEVED_AT,
    ]
    assert set(candidate.table.column("point_in_time_safe").to_pylist()) == {False}
    assert pa.compute.all(
        pa.compute.equal(
            candidate.table.column("source_snapshot_id"),
            snapshot.snapshot_id,
        )
    ).as_py()


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda value: value.update(next_page_token="next"),
            "pagination",
        ),
        (
            lambda value: value["bars"].pop("SPY"),
            "shape",
        ),
        (
            lambda value: value["bars"]["AAPL"].append(
                dict(value["bars"]["AAPL"][0])
            ),
            "one exact row",
        ),
        (
            lambda value: value["bars"]["SPY"][0].update(
                t="2026-07-29T04:00:00Z"
            ),
            "session differs",
        ),
    ],
)
def test_offline_candidate_fails_closed_on_response_drift(
    tmp_path: Path,
    mutator,
    message: str,
) -> None:
    plan = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    value = json.loads(_payload())
    mutator(value)
    snapshot = _snapshot(tmp_path, plan, raw=canonical_json_bytes(value))
    with pytest.raises(ContractError, match=message):
        build_canonical_bars_candidate(
            snapshot,
            acquisition_plan=plan,
            synthetic=True,
        )


def test_candidate_rejects_plan_and_snapshot_binding_drift(tmp_path: Path) -> None:
    plan = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    snapshot = _snapshot(tmp_path, plan)
    changed = dict(plan)
    changed["acquisition_plan_id"] = "0" * 64
    with pytest.raises(IntegrityError, match="plan ID differs"):
        build_canonical_bars_candidate(
            snapshot,
            acquisition_plan=changed,
            synthetic=True,
        )
    with pytest.raises(IntegrityError, match="snapshot binding differs"):
        build_canonical_bars_candidate(
            snapshot,
            acquisition_plan=plan,
            synthetic=False,
        )


def test_publication_plan_is_exact_and_production_rejects_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = build_canonical_bars_fixture_plan(repo_root=REPO, clock=_clock())
    candidate = build_canonical_bars_candidate(
        _snapshot(tmp_path, plan),
        acquisition_plan=plan,
        synthetic=True,
    )
    accepted = tmp_path / "accepted"
    work = tmp_path / "work"
    publication = build_canonical_bars_publication_plan(
        candidate,
        acquisition_plan=plan,
        accepted_root=accepted,
        work_root=work,
        synthetic=True,
    )
    assert publication["publication_count"] == 1
    assert [item["path"] for item in publication["outputs"]] == [
        BARS_FILENAME,
        RECEIPT_FILENAME,
        "release_manifest.json",
    ]
    assert publication["network_calls"] == 0
    assert not accepted.exists()
    assert not work.exists()

    monkeypatch.setenv("ALPACA_CANONICAL_BARS_PUBLICATION_APPROVED", "YES")
    with pytest.raises(ContractError, match="trust-eligible"):
        publish_canonical_bars(
            candidate,
            acquisition_plan=plan,
            approved_publication_plan_id=publication["publication_plan_id"],
            accepted_root=accepted,
            work_root=work,
            owner_confirmation="YES",
        )
    assert not accepted.exists()
    assert not work.exists()


def test_cli_and_provider_do_not_read_api_env_or_publish_during_planning() -> None:
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "acquire_canonical_bars.py"
    ).read_text(encoding="utf-8")
    provider_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "alpaca_canonical_bars.py"
    ).read_text(encoding="utf-8")
    assert "api.env" not in cli_source
    assert "api.env" not in provider_source
    assert "APCA_API_KEY_ID" in cli_source
    assert "FREE_SOURCE_QUALIFICATION_APPROVED" in cli_source
    assert "--execute-network" in cli_source
    assert "--execute-publication" in cli_source
