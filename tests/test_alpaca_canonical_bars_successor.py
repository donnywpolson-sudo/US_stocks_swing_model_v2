from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pyarrow as pa
import pytest

from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.cli.accumulate_canonical_bars import (
    parser as successor_parser,
)
from us_stocks_swing_model_v2.common import canonical_json_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_canonical_bars import (
    ACTIVE_ALPACA_SCHEMA,
    BARS_FILENAME,
    RECEIPT_FILENAME,
    SOURCE_NAME,
)
from us_stocks_swing_model_v2.providers.alpaca_canonical_bars_successor import (
    EXPECTED_CUMULATIVE_SESSIONS,
    EXPECTED_DELTA_SESSIONS,
    build_successor_bars_candidate,
    build_successor_bars_fixture_plan,
    build_successor_bars_publication_plan,
    build_synthetic_predecessor_table,
    publish_successor_bars,
)
from us_stocks_swing_model_v2.providers.snapshots import AsReceivedSnapshotStore


REPO = Path(__file__).resolve().parents[1]
REQUESTED_AT = datetime(2026, 8, 1, 4, 19, 59, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 8, 1, 4, 20, tzinfo=timezone.utc)
ASSET_IDS = {
    "AAPL": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
    "SPY": "b28f4066-5c6d-479b-a2af-85dc1a8f16fb",
}


def _permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="alpaca-canonical-bars-successor-snapshot",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _predecessor() -> pa.Table:
    return build_synthetic_predecessor_table(ASSET_IDS)


def _payload(*, next_page_token: str | None = None) -> bytes:
    bars: dict[str, list[dict[str, object]]] = {}
    for symbol, base in (("AAPL", 210.0), ("SPY", 630.0)):
        bars[symbol] = [
            {
                "t": f"{session.isoformat()}T04:00:00Z",
                "o": base + index,
                "h": base + index + 4.0,
                "l": base + index - 1.0,
                "c": base + index + 3.0,
                "v": 1000 + index,
                "n": 100 + index,
                "vw": base + index + 2.0,
            }
            for index, session in enumerate(EXPECTED_DELTA_SESSIONS)
        ]
    return canonical_json_bytes(
        {
            "bars": bars,
            "next_page_token": next_page_token,
        }
    )


def _plan(predecessor: pa.Table | None = None) -> dict[str, object]:
    return build_successor_bars_fixture_plan(
        repo_root=REPO,
        predecessor_table=predecessor or _predecessor(),
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


def test_successor_fixture_plan_is_deterministic_bounded_and_no_write() -> None:
    predecessor = _predecessor()
    before = tuple(sorted(path.as_posix() for path in REPO.glob("data/**/*")))
    first = _plan(predecessor)
    second = _plan(predecessor)
    after = tuple(sorted(path.as_posix() for path in REPO.glob("data/**/*")))

    assert first == second
    assert before == after
    assert first["request"] == {
        "method": "GET",
        "url": (
            "https://data.alpaca.markets/v2/stocks/bars?"
            "symbols=AAPL%2CSPY&start=2026-07-31T04%3A00%3A00Z&"
            "end=2026-08-01T03%3A59%3A59Z&timeframe=1Day&"
            "adjustment=raw&feed=sip&sort=asc&limit=10000"
        ),
        "symbols": ["AAPL", "SPY"],
        "start": "2026-07-31T04:00:00Z",
        "end": "2026-08-01T03:59:59Z",
        "feed": "sip",
        "timeframe": "1Day",
        "adjustment": "raw",
        "asof": None,
        "sort": "asc",
        "limit": 10000,
        "expected_sessions": [
            item.isoformat() for item in EXPECTED_DELTA_SESSIONS
        ],
        "expected_delta_rows": 2,
    }
    assert first["earliest_execution_at"] == "2026-08-01T04:19:59Z"
    assert first["network_request_plan"]["max_pages"] == 1
    assert first["network_request_plan"]["timeout_seconds"] == 30
    assert first["network_request_plan"]["max_response_bytes"] == 1048576
    assert first["host_timeout_seconds"] == 120
    assert first["cumulative"]["row_count"] == 4
    assert not any(first["authorities"].values())


def test_successor_builds_exact_cumulative_candidate(tmp_path: Path) -> None:
    predecessor = _predecessor()
    plan = _plan(predecessor)
    snapshot = _snapshot(tmp_path, plan)
    candidate = build_successor_bars_candidate(
        snapshot,
        acquisition_plan=plan,
        predecessor_table=predecessor,
        synthetic=True,
    )

    assert candidate.table.schema == ACTIVE_ALPACA_SCHEMA
    assert candidate.delta_row_count == 2
    assert candidate.row_count == 4
    assert candidate.delta_sessions == EXPECTED_DELTA_SESSIONS
    assert candidate.sessions == EXPECTED_CUMULATIVE_SESSIONS
    assert candidate.predecessor_release_id == "f" * 64
    rows = candidate.table.to_pylist()
    assert [(row["provider_symbol"], row["session"]) for row in rows] == [
        (symbol, session)
        for symbol in ("AAPL", "SPY")
        for session in EXPECTED_CUMULATIVE_SESSIONS
    ]
    assert set(row["point_in_time_safe"] for row in rows) == {False}


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
            lambda value: value["bars"]["AAPL"].pop(),
            "one exact row",
        ),
        (
            lambda value: value["bars"]["SPY"][0].update(
                t="2026-07-30T04:00:00Z"
            ),
            "session census differs",
        ),
        (
            lambda value: value["bars"]["AAPL"].append(
                dict(value["bars"]["AAPL"][0])
            ),
            "one exact row",
        ),
    ],
)
def test_successor_rejects_response_and_session_drift(
    tmp_path: Path,
    mutator,
    message: str,
) -> None:
    predecessor = _predecessor()
    plan = _plan(predecessor)
    value = json.loads(_payload())
    mutator(value)
    snapshot = _snapshot(tmp_path, plan, raw=canonical_json_bytes(value))
    with pytest.raises(ContractError, match=message):
        build_successor_bars_candidate(
            snapshot,
            acquisition_plan=plan,
            predecessor_table=predecessor,
            synthetic=True,
        )


def test_successor_rejects_plan_snapshot_and_predecessor_drift(
    tmp_path: Path,
) -> None:
    predecessor = _predecessor()
    plan = _plan(predecessor)
    snapshot = _snapshot(tmp_path, plan)
    changed = dict(plan)
    changed["acquisition_plan_id"] = "0" * 64
    with pytest.raises(IntegrityError, match="plan ID differs"):
        build_successor_bars_candidate(
            snapshot,
            acquisition_plan=changed,
            predecessor_table=predecessor,
            synthetic=True,
        )
    with pytest.raises(IntegrityError, match="snapshot binding differs"):
        build_successor_bars_candidate(
            snapshot,
            acquisition_plan=plan,
            predecessor_table=predecessor,
            synthetic=False,
        )
    drifted = predecessor.set_column(
        predecessor.schema.get_field_index("close"),
        "close",
        pa.array([999.0, 999.0], type=pa.float64()),
    )
    with pytest.raises(IntegrityError, match="predecessor table binding differs"):
        build_successor_bars_candidate(
            snapshot,
            acquisition_plan=plan,
            predecessor_table=drifted,
            synthetic=True,
        )


def test_successor_publication_plan_is_exact_and_production_rejects_synthetic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predecessor = _predecessor()
    plan = _plan(predecessor)
    candidate = build_successor_bars_candidate(
        _snapshot(tmp_path, plan),
        acquisition_plan=plan,
        predecessor_table=predecessor,
        synthetic=True,
    )
    accepted = tmp_path / "accepted"
    work = tmp_path / "work"
    publication = build_successor_bars_publication_plan(
        candidate,
        acquisition_plan=plan,
        accepted_root=accepted,
        work_root=work,
        synthetic=True,
    )

    assert publication["predecessor_release_id"] == "f" * 64
    assert publication["delta_row_count"] == 2
    assert publication["cumulative_row_count"] == 4
    assert publication["publication_count"] == 1
    assert [item["path"] for item in publication["outputs"]] == [
        BARS_FILENAME,
        RECEIPT_FILENAME,
        "release_manifest.json",
    ]
    assert publication["network_calls"] == 0
    assert publication["eligible_universe"] is False
    assert not accepted.exists()
    assert not work.exists()

    monkeypatch.setenv(
        "ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION_APPROVED",
        "YES",
    )
    with pytest.raises(ContractError, match="trust-eligible"):
        publish_successor_bars(
            candidate,
            acquisition_plan=plan,
            approved_publication_plan_id=publication["publication_plan_id"],
            accepted_root=accepted,
            work_root=work,
            owner_confirmation="YES",
        )
    assert not accepted.exists()
    assert not work.exists()


def test_successor_cli_and_provider_keep_credentials_and_writes_gated() -> None:
    cli_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "cli"
        / "accumulate_canonical_bars.py"
    ).read_text(encoding="utf-8")
    provider_source = (
        REPO
        / "src"
        / "us_stocks_swing_model_v2"
        / "providers"
        / "alpaca_canonical_bars_successor.py"
    ).read_text(encoding="utf-8")

    assert "api.env" not in cli_source
    assert "api.env" not in provider_source
    assert "APCA_API_KEY_ID" in cli_source
    assert "FREE_SOURCE_QUALIFICATION_APPROVED" in cli_source
    assert "--execute-network" in cli_source
    assert "--execute-publication" in cli_source
    assert "ALPACA_CANONICAL_BARS_SUCCESSOR_PUBLICATION_APPROVED" in (
        provider_source
    )


def test_successor_cli_defaults_to_plan_only() -> None:
    args = successor_parser().parse_args([])

    assert args.execute_network is False
    assert args.verify_snapshot is None
    assert args.execute_publication is None
    assert args.approved_plan_id is None
    assert args.approved_publication_plan_id is None
