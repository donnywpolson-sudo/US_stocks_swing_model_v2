from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pyarrow as pa
import pytest

import us_stocks_swing_model_v2.providers.alpaca_historical_backfill as backfill
from us_stocks_swing_model_v2.capabilities import SyntheticOnlyPermit
from us_stocks_swing_model_v2.cli import plan_alpaca_historical_backfill as cli
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_historical_backfill import (
    build_historical_backfill_group_continuation,
    build_historical_backfill_fixture_plan,
    continuation_plan_summary,
    load_historical_backfill_policy,
    plan_summary,
    run_historical_backfill_group,
    verify_historical_backfill_unit,
)
from us_stocks_swing_model_v2.providers.snapshots import (
    AsReceivedSnapshotStore,
    NetworkAcquisitionRegistry,
)


REPO = Path(__file__).resolve().parents[1]
IDENTITY_SNAPSHOT_ID = (
    "679c22119b9e3a9cdf19424ab9eccef5dae85bb5cb7be70502bdc597d2932df6"
)
REQUESTED_AT = datetime(2026, 7, 30, 4, 20, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 7, 30, 4, 21, tzinfo=timezone.utc)
NEW_YORK = ZoneInfo("America/New_York")


def _row(
    symbol: str,
    *,
    security_type: str = "STOCK",
    eligible: bool = True,
    active: bool = True,
    membership_present: bool = True,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "asset_id": f"asset-{symbol}",
        "security_type": security_type,
        "eligible": eligible,
        "active": active,
        "membership_present": membership_present,
        "identity_snapshot_id": IDENTITY_SNAPSHOT_ID,
    }


def _digest(symbols: list[str]) -> str:
    return sha256_bytes(canonical_json_bytes(sorted(symbols)))


def _selection(
    eligible: list[str],
    rehabilitated: list[str],
) -> dict[str, object]:
    eligible_set = set(eligible)
    legacy_set = set(rehabilitated)
    overlap = sorted(eligible_set & legacy_set)
    missing = sorted(eligible_set - legacy_set)
    legacy_only = sorted(legacy_set - eligible_set)
    return {
        "eligible": True,
        "active": True,
        "membership_present": True,
        "security_types": ["ETF", "STOCK"],
        "expected_eligible_count": len(eligible_set),
        "expected_eligible_symbols_sha256": _digest(sorted(eligible_set)),
        "expected_overlap_count": len(overlap),
        "expected_overlap_symbols_sha256": _digest(overlap),
        "expected_missing_count": len(missing),
        "expected_missing_symbols_sha256": _digest(missing),
        "expected_legacy_only_count": len(legacy_only),
        "expected_legacy_only_symbols_sha256": _digest(legacy_only),
    }


def _fixture_plan() -> dict[str, object]:
    rows = [
        _row("AAA"),
        _row("BBB", security_type="ETF"),
        _row("CCC"),
        _row("OLD"),
        _row("INACTIVE", active=False),
        _row("ABSENT", membership_present=False),
        _row("INELIGIBLE", eligible=False),
        _row("UNIT", security_type="UNIT"),
    ]
    rehabilitated = ["LEGACY", "OLD"]
    return build_historical_backfill_fixture_plan(
        repo_root=REPO,
        identity_rows=rows,
        rehabilitated_symbols=rehabilitated,
        sessions=[date(2016, 1, 4), date(2016, 12, 30), date(2017, 1, 3)],
        expected_selection=_selection(["AAA", "BBB", "CCC", "OLD"], rehabilitated),
    )


def _registry() -> NetworkAcquisitionRegistry:
    return NetworkAcquisitionRegistry.load(
        REPO / "config" / "alpaca_historical_backfill_network_registry.json",
        allowed_root=REPO,
    )


def _permit() -> SyntheticOnlyPermit:
    return SyntheticOnlyPermit.create(
        fixture_id="alpaca-historical-backfill-group",
        scope="SYNTHETIC_AS_RECEIVED_SNAPSHOT",
    )


def _bar(session: date) -> dict[str, object]:
    event_at = datetime.combine(session, time.min, NEW_YORK).astimezone(
        timezone.utc
    )
    return {
        "t": event_at.isoformat().replace("+00:00", "Z"),
        "o": 100.0,
        "h": 102.0,
        "l": 99.0,
        "c": 101.0,
        "v": 1000,
        "n": 100,
        "vw": 100.5,
    }


def _land_unit_page(
    tmp_path: Path,
    unit: dict[str, object],
    *,
    payload: dict[str, object] | None = None,
    url: str | None = None,
    requested_at: datetime = REQUESTED_AT,
    retrieved_at: datetime = RETRIEVED_AT,
):
    allowed = tmp_path / "data"
    allowed.mkdir(exist_ok=True)
    store = AsReceivedSnapshotStore(
        allowed / "snapshots",
        allowed_root=allowed,
    )
    value = payload or {
        "bars": {
            unit["symbols"][0]: [_bar(date.fromisoformat(unit["window"]["first_session"]))]
        },
        "next_page_token": None,
    }
    return store.land(
        source=backfill.SOURCE_NAME,
        url=url or unit["network_request_plan"]["initial_url"],
        http_status=200,
        raw=canonical_json_bytes(value),
        headers={"content-type": "application/json"},
        retrieved_at=retrieved_at,
        synthetic_permit=_permit(),
        max_bytes=16777216,
        requested_at=requested_at,
        request_plan_id=unit["network_request_plan"]["plan_id"],
    )


def _snapshot_store(tmp_path: Path) -> AsReceivedSnapshotStore:
    allowed = tmp_path / "data"
    allowed.mkdir(exist_ok=True)
    return AsReceivedSnapshotStore(
        allowed / "snapshots",
        allowed_root=allowed,
    )


def test_fixture_plan_is_deterministic_bounded_and_plan_only() -> None:
    first = _fixture_plan()
    second = _fixture_plan()

    assert first == second
    assert first["mode"] == "SYNTHETIC_PLAN_ONLY"
    assert first["cohort"]["eligible_count"] == 4
    assert first["cohort"]["eligible_security_types"] == {"ETF": 1, "STOCK": 3}
    assert first["cohort"]["overlap_count"] == 1
    assert first["cohort"]["missing_count"] == 3
    assert first["cohort"]["legacy_only_count"] == 1
    assert first["batch_count"] == 1
    assert first["request_unit_count"] == 2
    assert first["command_census"]["maximum_gets"] == 6
    assert first["credential_boundary"]["required_for_plan"] is False
    assert not any(first["authorities"].values())
    assert first["outputs"]["plan_disposition"] == "PROCESS_LOCAL_CONVERSATION_ONLY"
    assert first["outputs"]["publication"] is False
    assert first["execution_contract"]["groups_per_invocation"] == 1
    assert first["execution_contract"]["retry"] is False
    assert first["execution_contract"]["offline_unit_verification_before_next_unit"] is True
    assert first["execution_contract"]["approved_continuation_plan_id_required"] is True
    assert first["execution_contract"]["retained_complete_unit_revalidation_before_network"] is True
    assert first["evidence_boundary"] == {
        "evidence_class": "LEGACY_DISCOVERY",
        "quality_state": "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED",
        "historical_membership_proven": False,
        "survivorship_safe": False,
        "may_support_confirmation": False,
        "hfdl_included": False,
    }


def test_request_units_pin_sip_contract_boundaries_and_order() -> None:
    plan = _fixture_plan()
    units = plan["request_units"]
    assert [unit["unit_index"] for unit in units] == [1, 2]
    assert [unit["window"]["year"] for unit in units] == [2016, 2017]
    assert units[0]["symbols"] == ["AAA", "BBB", "CCC"]
    assert units[0]["asset_ids"] == ["asset-AAA", "asset-BBB", "asset-CCC"]
    assert units[0]["window"]["start"] == "2016-01-04T05:00:00Z"
    assert units[0]["window"]["end"] == "2016-12-31T04:59:59Z"

    request = units[0]["network_request_plan"]
    query = parse_qs(urlparse(request["initial_url"]).query)
    assert query == {
        "symbols": ["AAA,BBB,CCC"],
        "start": ["2016-01-04T05:00:00Z"],
        "end": ["2016-12-31T04:59:59Z"],
        "timeframe": ["1Day"],
        "adjustment": ["raw"],
        "feed": ["sip"],
        "sort": ["asc"],
        "limit": ["10000"],
    }
    assert "asof" not in query
    assert "page_token" not in query
    assert request["method"] == "GET"
    assert request["timeout_seconds"] == 30
    assert request["max_pages"] == 3
    assert request["max_response_bytes"] == 16777216
    assert request["pagination_parameter"] == "page_token"


def test_batching_and_page_bound_are_deterministic() -> None:
    symbols = [f"S{index:03d}" for index in range(101)]
    rows = [_row(symbol) for symbol in symbols]
    sessions: list[date] = []
    current = date(2020, 1, 2)
    while len(sessions) < 253:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    plan = build_historical_backfill_fixture_plan(
        repo_root=REPO,
        identity_rows=rows,
        rehabilitated_symbols=[],
        sessions=sessions,
        expected_selection=_selection(symbols, []),
    )

    assert plan["batch_count"] == 2
    assert plan["request_unit_count"] == 2
    assert plan["request_units"][0]["maximum_possible_rows"] == 25300
    assert plan["request_units"][0]["expected_maximum_pages"] == 3
    assert plan["request_units"][1]["maximum_possible_rows"] == 253
    assert plan["request_units"][1]["expected_maximum_pages"] == 1
    assert plan["request_units"][0]["symbols"] == symbols[:100]
    assert plan["request_units"][1]["symbols"] == symbols[100:]


def test_cohort_drift_and_duplicate_fail_closed() -> None:
    rows = [_row("AAA"), _row("BBB")]
    selection = _selection(["AAA", "BBB"], [])
    changed = dict(selection)
    changed["expected_missing_count"] = 3
    with pytest.raises(IntegrityError, match="cohort differs"):
        build_historical_backfill_fixture_plan(
            repo_root=REPO,
            identity_rows=rows,
            rehabilitated_symbols=[],
            sessions=[date(2020, 1, 2)],
            expected_selection=changed,
        )

    with pytest.raises(IntegrityError, match="invalid duplicate"):
        build_historical_backfill_fixture_plan(
            repo_root=REPO,
            identity_rows=[_row("AAA"), _row("AAA")],
            rehabilitated_symbols=[],
            sessions=[date(2020, 1, 2)],
            expected_selection=_selection(["AAA"], []),
        )


def test_plan_summary_omits_full_request_payload_but_binds_it() -> None:
    plan = _fixture_plan()
    summary = plan_summary(plan)

    assert "request_units" not in summary
    assert summary["backfill_plan_id"] == plan["backfill_plan_id"]
    assert summary["request_units_sha256"] == plan["request_units_sha256"]
    assert summary["request_unit_count"] == 2
    assert summary["execution_groups"] == plan["execution_groups"]


def test_cli_is_plan_only_and_prints_only_summary(monkeypatch, capsys) -> None:
    expected = _fixture_plan()
    monkeypatch.setattr(cli, "build_historical_backfill_plan", lambda **_kwargs: expected)

    assert cli.main(["--repo-root", str(REPO)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["backfill_plan_id"] == expected["backfill_plan_id"]
    assert "request_units" not in output


def test_cli_continuation_planning_is_network_free_and_summary_only(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    expected = _fixture_plan()
    retained = _land_unit_page(tmp_path, expected["request_units"][0])
    continuation = build_historical_backfill_group_continuation(
        backfill_plan=expected,
        group_index=1,
        snapshot_store=_snapshot_store(tmp_path),
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
    )
    monkeypatch.setattr(cli, "build_historical_backfill_plan", lambda **_kwargs: expected)
    monkeypatch.setattr(
        cli,
        "build_historical_backfill_group_continuation_plan",
        lambda **_kwargs: continuation,
    )
    monkeypatch.setattr(
        cli,
        "execute_historical_backfill_group",
        lambda **_kwargs: pytest.fail("network execution must not start"),
    )

    assert cli.main(["--repo-root", str(REPO), "--plan-group-continuation", "1"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["continuation_plan_id"] == continuation["continuation_plan_id"]
    assert output["retained_unit_count"] == 1
    assert output["retained_page_count"] == 1
    assert retained.snapshot_id not in json.dumps(output)
    assert "retained_units" not in output


def test_checked_in_policy_is_non_authorizing_and_content_addressed() -> None:
    policy, policy_id = load_historical_backfill_policy(REPO)

    assert policy_id == sha256_bytes(canonical_json_bytes(policy))
    assert policy["request_contract"]["feed"] == "sip"
    assert policy["request_contract"]["asof"] is None
    assert policy["selection"]["expected_missing_count"] == 9415
    assert policy["selection"]["expected_missing_symbols_sha256"] == (
        "1f6ed8101032cf31359eb358be10cd0883178a5c0567f0ffadcbaffe92608f63"
    )
    assert not any(policy["authorities"].values())


def test_rehabilitated_release_requires_exact_published_role(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy, _ = load_historical_backfill_policy(REPO)
    policy = json.loads(json.dumps(policy))
    binding = policy["rehabilitated_release"]
    binding["bars_sha256"] = "1" * 64
    binding["symbol_count"] = 2
    manifest = SimpleNamespace(
        release_id=binding["release_id"],
        dataset="alpaca_legacy_daily_bars",
        role="legacy_discovery_only",
        quality_state="LEGACY_CAVEATED",
    )
    monkeypatch.setattr(backfill, "verify_accepted_release", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(backfill, "sha256_file", lambda _path: "1" * 64)
    monkeypatch.setattr(
        backfill.pq,
        "read_table",
        lambda *_args, **_kwargs: pa.table({"provider_symbol": ["AAA", "BBB"]}),
    )

    assert backfill._rehabilitated_symbols(tmp_path, policy, tmp_path) == [
        "AAA",
        "BBB",
    ]

    manifest.role = "legacy_discovery"
    with pytest.raises(IntegrityError, match="release binding differs"):
        backfill._rehabilitated_symbols(tmp_path, policy, tmp_path)


def test_synthetic_group_runner_is_ordered_bounded_and_no_write_assessment(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    group = plan["execution_groups"][0]
    order: list[int] = []

    def capture(unit):
        order.append(unit["unit_index"])
        return (_land_unit_page(tmp_path, unit),)

    snapshots, assessment = run_historical_backfill_group(
        backfill_plan=plan,
        approved_backfill_plan_id=plan["backfill_plan_id"],
        group_index=1,
        approved_group_request_plan_ids_sha256=group[
            "request_plan_ids_sha256"
        ],
        capture_unit=capture,
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
    )

    assert order == [1, 2]
    assert len(snapshots) == 2
    assert assessment["unit_count"] == 2
    assert assessment["page_count"] == 2
    assert assessment["bar_count"] == 2
    assert assessment["normalized_zero_vwap_rows"] == 0
    assert assessment["retained_unit_count"] == 0
    assert assessment["captured_unit_count"] == 2
    assert assessment["zero_row_symbol_unit_count"] == 4
    assert assessment["publication"] is False
    assert assessment["activation"] is False
    assert assessment["research"] is False
    assert assessment["local_integrity_verified"] is False
    assert assessment["quality_state"] == "CURRENT_IDENTITY_SEEDED_PIT_UNRESOLVED"


def test_group_approval_mismatch_fails_before_capture() -> None:
    plan = _fixture_plan()
    called = False

    def capture(_unit):
        nonlocal called
        called = True
        return ()

    with pytest.raises(PermissionError, match="plan ID differs"):
        run_historical_backfill_group(
            backfill_plan=plan,
            approved_backfill_plan_id="0" * 64,
            group_index=1,
            approved_group_request_plan_ids_sha256=plan["execution_groups"][0][
                "request_plan_ids_sha256"
            ],
            capture_unit=capture,
            calendar_sessions=[date(2016, 1, 4)],
            registry=_registry(),
            synthetic=True,
        )
    assert called is False

    with pytest.raises(PermissionError, match="execution group differs"):
        run_historical_backfill_group(
            backfill_plan=plan,
            approved_backfill_plan_id=plan["backfill_plan_id"],
            group_index=1,
            approved_group_request_plan_ids_sha256="0" * 64,
            capture_unit=capture,
            calendar_sessions=[date(2016, 1, 4)],
            registry=_registry(),
            synthetic=True,
        )
    assert called is False


def test_unit_verifier_accepts_exact_two_page_terminal_lineage(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    first = _land_unit_page(
        tmp_path,
        unit,
        payload={
            "bars": {"AAA": [_bar(date(2016, 1, 4))]},
            "next_page_token": "page-2",
        },
    )
    second = _land_unit_page(
        tmp_path,
        unit,
        payload={
            "bars": {"BBB": [_bar(date(2016, 12, 30))]},
            "next_page_token": None,
        },
        url=unit["network_request_plan"]["initial_url"] + "&page_token=page-2",
    )

    assessment = verify_historical_backfill_unit(
        unit,
        (first, second),
        calendar_sessions=[date(2016, 1, 4), date(2016, 12, 30)],
        registry=_registry(),
        synthetic=True,
    )
    assert assessment["bar_count"] == 2
    assert assessment["normalized_zero_vwap_rows"] == 0
    assert assessment["zero_row_symbols"] == ["CCC"]
    assert assessment["terminal_pagination"] is True


@pytest.mark.parametrize(
    ("volume", "trade_count"),
    [(0, 0), (0, None), (117, 6)],
)
def test_unit_verifier_normalizes_exact_zero_vwap_without_mutating_raw(
    tmp_path: Path,
    volume: int,
    trade_count: int | None,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    bar = _bar(date(2016, 1, 4))
    bar.update(v=volume, n=trade_count, vw=0)
    snapshot = _land_unit_page(
        tmp_path,
        unit,
        payload={
            "bars": {unit["symbols"][0]: [bar]},
            "next_page_token": None,
        },
    )
    raw_before = snapshot.read_verified_bytes()

    assessment = verify_historical_backfill_unit(
        unit,
        (snapshot,),
        calendar_sessions=[date(2016, 1, 4), date(2016, 12, 30)],
        registry=_registry(),
        synthetic=True,
    )

    assert assessment["bar_count"] == 1
    assert assessment["normalized_zero_vwap_rows"] == 1
    assert assessment["pages"][0]["normalized_zero_vwap_rows"] == 1
    assert snapshot.read_verified_bytes() == raw_before


def test_unit_verifier_rejects_negative_vwap(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    bar = _bar(date(2016, 1, 4))
    bar.update(v=117, n=6, vw=-1)
    snapshot = _land_unit_page(
        tmp_path,
        unit,
        payload={
            "bars": {unit["symbols"][0]: [bar]},
            "next_page_token": None,
        },
    )

    with pytest.raises(ContractError, match="violates OHLCV invariants"):
        verify_historical_backfill_unit(
            unit,
            (snapshot,),
            calendar_sessions=[date(2016, 1, 4), date(2016, 12, 30)],
            registry=_registry(),
            synthetic=True,
        )


def test_continuation_plan_reuses_verified_pages_and_captures_only_missing_units(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    first_unit, second_unit = plan["request_units"]
    retained = _land_unit_page(tmp_path, first_unit)
    continuation = build_historical_backfill_group_continuation(
        backfill_plan=plan,
        group_index=1,
        snapshot_store=_snapshot_store(tmp_path),
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
    )

    assert continuation["retained_unit_count"] == 1
    assert continuation["retained_page_count"] == 1
    assert continuation["capture_unit_count"] == 1
    assert continuation["capture_unit_indices"] == [second_unit["unit_index"]]
    assert continuation["command_census"]["maximum_new_gets"] == 3
    assert continuation["command_census"]["maximum_new_response_bytes"] == 50331648
    assert continuation_plan_summary(continuation)["continuation_plan_id"] == (
        continuation["continuation_plan_id"]
    )
    tampered = json.loads(json.dumps(continuation))
    tampered["retained_unit_count"] = 2
    with pytest.raises(IntegrityError, match="continuation plan ID differs"):
        continuation_plan_summary(tampered)

    capture_order: list[int] = []

    def capture(unit):
        capture_order.append(unit["unit_index"])
        return (_land_unit_page(tmp_path, unit),)

    snapshots, assessment = run_historical_backfill_group(
        backfill_plan=plan,
        approved_backfill_plan_id=plan["backfill_plan_id"],
        group_index=1,
        approved_group_request_plan_ids_sha256=plan["execution_groups"][0][
            "request_plan_ids_sha256"
        ],
        capture_unit=capture,
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
        continuation_plan=continuation,
        retained_pages_by_unit={first_unit["unit_index"]: (retained,)},
    )

    assert capture_order == [second_unit["unit_index"]]
    assert len(snapshots) == 2
    assert assessment["continuation_plan_id"] == continuation["continuation_plan_id"]
    assert assessment["retained_unit_indices"] == [first_unit["unit_index"]]
    assert assessment["captured_unit_indices"] == [second_unit["unit_index"]]


def test_continuation_runner_rejects_retained_substitution_before_capture(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    first_unit = plan["request_units"][0]
    _land_unit_page(tmp_path, first_unit)
    continuation = build_historical_backfill_group_continuation(
        backfill_plan=plan,
        group_index=1,
        snapshot_store=_snapshot_store(tmp_path),
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
    )
    later_request = REQUESTED_AT + timedelta(days=1)
    substitute = _land_unit_page(
        tmp_path,
        first_unit,
        requested_at=later_request,
        retrieved_at=later_request + timedelta(minutes=1),
    )
    captured = False

    def capture(_unit):
        nonlocal captured
        captured = True
        return ()

    with pytest.raises(IntegrityError, match="retained selection differs"):
        run_historical_backfill_group(
            backfill_plan=plan,
            approved_backfill_plan_id=plan["backfill_plan_id"],
            group_index=1,
            approved_group_request_plan_ids_sha256=plan["execution_groups"][0][
                "request_plan_ids_sha256"
            ],
            capture_unit=capture,
            calendar_sessions=[
                date(2016, 1, 4),
                date(2016, 12, 30),
                date(2017, 1, 3),
            ],
            registry=_registry(),
            synthetic=True,
            continuation_plan=continuation,
            retained_pages_by_unit={first_unit["unit_index"]: (substitute,)},
        )
    assert captured is False


def test_continuation_plan_selects_newest_complete_valid_lineage(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    old_requested = REQUESTED_AT - timedelta(days=1)
    old = _land_unit_page(
        tmp_path,
        unit,
        requested_at=old_requested,
        retrieved_at=old_requested + timedelta(minutes=1),
    )
    new = _land_unit_page(tmp_path, unit)

    continuation = build_historical_backfill_group_continuation(
        backfill_plan=plan,
        group_index=1,
        snapshot_store=_snapshot_store(tmp_path),
        calendar_sessions=[
            date(2016, 1, 4),
            date(2016, 12, 30),
            date(2017, 1, 3),
        ],
        registry=_registry(),
        synthetic=True,
    )

    selected = continuation["retained_units"][0]
    assert selected["snapshot_ids"] == [new.snapshot_id]
    assert selected["snapshot_ids"] != [old.snapshot_id]
    assert continuation["candidate_lineage_count"] == 2
    assert continuation["superseded_valid_lineage_count"] == 1


def test_continuation_plan_rejects_equal_time_lineage_ambiguity(
    tmp_path: Path,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    _land_unit_page(tmp_path, unit)
    changed = _bar(date(2016, 1, 4))
    changed["vw"] = 101.0
    _land_unit_page(
        tmp_path,
        unit,
        payload={
            "bars": {unit["symbols"][0]: [changed]},
            "next_page_token": None,
        },
        retrieved_at=RETRIEVED_AT + timedelta(minutes=1),
    )

    with pytest.raises(IntegrityError, match="lineage is ambiguous"):
        build_historical_backfill_group_continuation(
            backfill_plan=plan,
            group_index=1,
            snapshot_store=_snapshot_store(tmp_path),
            calendar_sessions=[
                date(2016, 1, 4),
                date(2016, 12, 30),
                date(2017, 1, 3),
            ],
            registry=_registry(),
            synthetic=True,
        )


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda payload, _unit: payload.update(next_page_token="more"),
            "not terminal",
        ),
        (
            lambda payload, _unit: payload["bars"].update(UNEXPECTED=[_bar(date(2016, 1, 4))]),
            "response schema differs",
        ),
        (
            lambda payload, unit: payload["bars"][unit["symbols"][0]].append(
                dict(payload["bars"][unit["symbols"][0]][0])
            ),
            "duplicate Alpaca symbol/session",
        ),
        (
            lambda payload, unit: payload["bars"][unit["symbols"][0]][0].update(
                t="2016-01-05T05:00:00Z"
            ),
            "outside the pinned calendar",
        ),
    ],
)
def test_unit_verifier_rejects_schema_pagination_duplicate_and_calendar_drift(
    tmp_path: Path,
    mutator,
    message: str,
) -> None:
    plan = _fixture_plan()
    unit = plan["request_units"][0]
    payload = {
        "bars": {unit["symbols"][0]: [_bar(date(2016, 1, 4))]},
        "next_page_token": None,
    }
    mutator(payload, unit)
    snapshot = _land_unit_page(tmp_path, unit, payload=payload)
    with pytest.raises((ContractError, IntegrityError), match=message):
        verify_historical_backfill_unit(
            unit,
            (snapshot,),
            calendar_sessions=[date(2016, 1, 4), date(2016, 12, 30)],
            registry=_registry(),
            synthetic=True,
        )


def test_cli_rejects_execution_approval_drift_before_environment_access(
    monkeypatch,
) -> None:
    plan = _fixture_plan()
    monkeypatch.setattr(cli, "build_historical_backfill_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        cli,
        "execute_historical_backfill_group",
        lambda **_kwargs: pytest.fail("execution must not start"),
    )

    with pytest.raises(PermissionError, match="plan ID differs"):
        cli.main(
            [
                "--repo-root",
                str(REPO),
                "--execute-group",
                "1",
                "--approved-plan-id",
                "0" * 64,
                "--approved-group-request-plan-ids-sha256",
                plan["execution_groups"][0]["request_plan_ids_sha256"],
                "--approved-continuation-plan-id",
                "0" * 64,
            ]
        )

    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "api.env" not in source
    assert "--execute-group" in source
    assert "FREE_SOURCE_QUALIFICATION_APPROVED" in source


def test_cli_rejects_continuation_approval_drift_before_environment_access(
    monkeypatch,
) -> None:
    plan = _fixture_plan()
    monkeypatch.setattr(cli, "build_historical_backfill_plan", lambda **_kwargs: plan)
    monkeypatch.setattr(
        cli,
        "build_historical_backfill_group_continuation_plan",
        lambda **_kwargs: {"continuation_plan_id": "1" * 64},
    )
    monkeypatch.setattr(
        cli,
        "execute_historical_backfill_group",
        lambda **_kwargs: pytest.fail("execution must not start"),
    )
    monkeypatch.delenv("FREE_SOURCE_QUALIFICATION_APPROVED", raising=False)

    with pytest.raises(PermissionError, match="continuation plan ID differs"):
        cli.main(
            [
                "--repo-root",
                str(REPO),
                "--execute-group",
                "1",
                "--approved-plan-id",
                plan["backfill_plan_id"],
                "--approved-group-request-plan-ids-sha256",
                plan["execution_groups"][0]["request_plan_ids_sha256"],
                "--approved-continuation-plan-id",
                "0" * 64,
            ]
        )
