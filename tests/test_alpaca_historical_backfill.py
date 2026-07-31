from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pyarrow as pa
import pytest

import us_stocks_swing_model_v2.providers.alpaca_historical_backfill as backfill
from us_stocks_swing_model_v2.cli import plan_alpaca_historical_backfill as cli
from us_stocks_swing_model_v2.common import canonical_json_bytes, sha256_bytes
from us_stocks_swing_model_v2.errors import IntegrityError
from us_stocks_swing_model_v2.providers.alpaca_historical_backfill import (
    build_historical_backfill_fixture_plan,
    load_historical_backfill_policy,
    plan_summary,
)


REPO = Path(__file__).resolve().parents[1]
IDENTITY_SNAPSHOT_ID = (
    "679c22119b9e3a9cdf19424ab9eccef5dae85bb5cb7be70502bdc597d2932df6"
)


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
