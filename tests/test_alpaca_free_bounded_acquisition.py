from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from us_stocks_swing_model_v2.alpaca_free_bounded import (
    EvidenceClass,
    build_historical_backfill_plan,
    load_profile,
    retry_disposition,
    validate_bars_payload,
)
from us_stocks_swing_model_v2.errors import ContractError, IntegrityError, NetworkGuardError
from us_stocks_swing_model_v2.free_acquisition import _parse_success, execute_one_source_request
from us_stocks_swing_model_v2.free_source_evidence import (
    RawEvidenceStore,
    alpha_vantage_listing_plan,
    alpaca_bars_plan,
    parse_alpha_vantage_listing_csv,
    parse_alpaca_asset_master,
    parse_corporate_action_groups,
    parse_nasdaq_symbol_directory,
    prospective_source_plans,
    validate_accepted_bars_receipts,
    validate_complete_pagination,
)
from us_stocks_swing_model_v2.local_credentials import (
    CANONICAL_CREDENTIAL_VARIABLES,
    load_local_api_env,
)
from us_stocks_swing_model_v2.clock import TrustedClock
from us_stocks_swing_model_v2.providers.snapshots import NetworkAcquisitionRegistry


REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 10, 20, tzinfo=timezone.utc)


def test_profile_is_explicit_free_sip_long_short_and_fail_closed() -> None:
    profile = load_profile(REPO)
    assert profile["profile_id"] == "ALPACA_FREE_BOUNDED_V1"
    assert profile["bars"]["required_feed"] == "sip"
    assert profile["bars"]["adjustment"] == "raw"
    assert profile["bars"]["minimum_end_lag_minutes"] == 20
    assert profile["outcome"]["supported_sides"] == ["LONG", "SHORT"]
    assert profile["outcome"]["stock_borrow_fee"] == 0.0
    assert profile["readiness"]["training"] == "BLOCKED"
    assert profile["credentials"] == {
        "alpaca": ["APCA_API_KEY_ID", "APCA_API_SECRET_KEY"],
        "alpha_vantage": ["ALPHA_VANTAGE_API_KEY"],
        "storage": "ENVIRONMENT_ONLY",
    }


def test_backfill_plan_pins_sip_raw_order_and_is_deterministic() -> None:
    first = build_historical_backfill_plan(
        repository_root=REPO,
        symbols=["META", "AAPL", "AAPL"],
        requested_start=date(2016, 1, 1),
        requested_end=date(2017, 1, 2),
        requested_at=NOW,
    )
    second = build_historical_backfill_plan(
        repository_root=REPO,
        symbols=["AAPL", "META"],
        requested_start=date(2016, 1, 1),
        requested_end=date(2017, 1, 2),
        requested_at=NOW,
    )
    assert first.plan_id == second.plan_id
    assert len(first.units) == 2
    assert all(unit.symbols == ("AAPL", "META") for unit in first.units)
    assert all(("feed", "sip") in unit.canonical_query for unit in first.units)
    assert all(("adjustment", "raw") in unit.canonical_query for unit in first.units)
    assert all("iex" not in unit.sanitized_url.lower() for unit in first.units)
    assert [dict(unit.canonical_query)["end"] for unit in first.units] == [
        "2016-12-31",
        "2017-01-02",
    ]


def test_backfill_boundary_resume_and_unknown_checkpoint_fail_closed() -> None:
    with pytest.raises(ContractError, match="before 2016"):
        build_historical_backfill_plan(
            repository_root=REPO,
            symbols=["AAPL"],
            requested_start=date(2015, 12, 31),
            requested_end=date(2016, 1, 2),
            requested_at=NOW,
        )
    with pytest.raises(ContractError, match="delay boundary"):
        build_historical_backfill_plan(
            repository_root=REPO,
            symbols=["AAPL"],
            requested_start=date(2026, 8, 10),
            requested_end=date(2026, 8, 10),
            requested_at=datetime(2026, 8, 10, 0, 10, tzinfo=timezone.utc),
        )
    plan = build_historical_backfill_plan(
        repository_root=REPO,
        symbols=["AAPL"],
        requested_start=date(2016, 1, 1),
        requested_end=date(2016, 1, 2),
        requested_at=NOW,
    )
    resumed = build_historical_backfill_plan(
        repository_root=REPO,
        symbols=["AAPL"],
        requested_start=date(2016, 1, 1),
        requested_end=date(2016, 1, 2),
        requested_at=NOW,
        completed_unit_ids=[plan.units[0].unit_id],
    )
    assert resumed.pending_units == ()
    with pytest.raises(IntegrityError, match="outside the exact plan"):
        build_historical_backfill_plan(
            repository_root=REPO,
            symbols=["AAPL"],
            requested_start=date(2016, 1, 1),
            requested_end=date(2016, 1, 2),
            requested_at=NOW,
            completed_unit_ids=["0" * 64],
        )


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_terminal_client_statuses_never_retry(status: int) -> None:
    result = retry_disposition(http_status=status, attempt_number=1, request_id="x")
    assert result.state == "TERMINAL_FAILURE"
    assert not result.retryable


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_rate_limit_and_5xx_have_bounded_deterministic_retry(status: int) -> None:
    first = retry_disposition(http_status=status, attempt_number=1, request_id="x")
    same = retry_disposition(http_status=status, attempt_number=1, request_id="x")
    exhausted = retry_disposition(http_status=status, attempt_number=4, request_id="x")
    assert first == same
    assert first.state == "RETRY_REQUIRED_NEW_INVOCATION"
    assert 0 < first.delay_seconds <= 30
    assert exhausted.state == "RETRY_BUDGET_EXHAUSTED"
    assert not exhausted.retryable


def _bars_payload(*, next_page_token=None, extra=None):
    payload = {
        "bars": {
            "AAPL": [
                {"t": "2020-08-28T04:00:00Z", "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000}
            ]
        },
        "next_page_token": next_page_token,
    }
    if extra:
        payload[extra] = True
    return payload


def test_bars_validation_preserves_terminal_state_and_quarantines_schema_drift() -> None:
    accepted = validate_bars_payload(_bars_payload(), expected_symbols=("AAPL",))
    assert accepted["accepted"] and accepted["terminal_page"]
    continued = validate_bars_payload(_bars_payload(next_page_token="p2"), expected_symbols=("AAPL",))
    assert continued["next_page_token"] == "p2"
    quarantined = validate_bars_payload(_bars_payload(extra="new_field"), expected_symbols=("AAPL",))
    assert not quarantined["accepted"]
    assert quarantined["unknown_fields"] == ["new_field"]


def test_bars_validation_rejects_duplicates_bad_prices_and_unexpected_symbols() -> None:
    duplicate = _bars_payload()
    duplicate["bars"]["AAPL"].append(dict(duplicate["bars"]["AAPL"][0]))
    with pytest.raises(ContractError, match="duplicate"):
        validate_bars_payload(duplicate, expected_symbols=("AAPL",))
    bad = _bars_payload()
    bad["bars"]["AAPL"][0]["h"] = 98
    with pytest.raises(ContractError, match="OHLCV"):
        validate_bars_payload(bad, expected_symbols=("AAPL",))
    with pytest.raises(ContractError, match="unexpected symbol"):
        validate_bars_payload(_bars_payload(), expected_symbols=("META",))
    wrong_time = _bars_payload()
    wrong_time["bars"]["AAPL"][0]["t"] = "2020-08-28T12:00:00Z"
    with pytest.raises(ContractError, match="New York midnight"):
        validate_bars_payload(wrong_time, expected_symbols=("AAPL",))
    with pytest.raises(ContractError, match="non-session"):
        validate_bars_payload(
            _bars_payload(), expected_symbols=("AAPL",), expected_sessions=(date(2020, 8, 27),)
        )


def test_source_plans_redact_credentials_and_pin_explicit_provider_contracts() -> None:
    alpha = alpha_vantage_listing_plan(repository_root=REPO, as_of=date(2020, 1, 2), state="delisted")
    assert "REDACTED" in alpha.sanitized_url
    assert "secret-value" not in alpha.sanitized_url
    assert "secret-value" in alpha.transport_url(secret="secret-value")
    bars = alpaca_bars_plan(
        repository_root=REPO,
        symbols=["AAPL"],
        start=date(2020, 1, 2),
        end_exclusive=date(2020, 1, 3),
        evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    query = dict(bars.canonical_query)
    assert query == {
        "symbols": "AAPL", "start": "2020-01-02", "end": "2020-01-02",
        "timeframe": "1Day", "adjustment": "raw", "feed": "sip", "sort": "asc", "limit": "10000",
    }
    assert {plan.source for plan in prospective_source_plans(repository_root=REPO, observed_for=date(2026, 8, 10))} == {
        "alpaca_free_bounded_assets", "alpaca_free_bounded_corporate_actions",
        "nasdaq_free_bounded_listed", "nasdaq_free_bounded_otherlisted",
    }


def test_append_only_store_hashes_receipts_deduplicates_bytes_and_retains_changes(tmp_path: Path) -> None:
    store = RawEvidenceStore(tmp_path / "evidence", allowed_root=tmp_path)
    plan = alpha_vantage_listing_plan(repository_root=REPO, as_of=date(2020, 1, 2), state="active")
    first = store.append(
        plan=plan, raw=b"same", requested_at=NOW, retrieved_at=NOW + timedelta(seconds=1),
        response_headers={"content-type": "text/csv"}, http_status=200, page_index=0,
        requested_page_token=None, next_page_token=None, retry_attempt=1,
        parent_request_id=None, parsing_status="PARSED", validation_status="PASS",
    )
    duplicate = store.append(
        plan=plan, raw=b"same", requested_at=NOW + timedelta(seconds=2), retrieved_at=NOW + timedelta(seconds=3),
        response_headers={"content-type": "text/csv"}, http_status=200, page_index=0,
        requested_page_token=None, next_page_token=None, retry_attempt=1,
        parent_request_id=None, parsing_status="PARSED", validation_status="PASS",
    )
    changed = store.append(
        plan=plan, raw=b"changed", requested_at=NOW + timedelta(seconds=4), retrieved_at=NOW + timedelta(seconds=5),
        response_headers={"content-type": "text/csv"}, http_status=200, page_index=0,
        requested_page_token=None, next_page_token=None, retry_attempt=1,
        parent_request_id=None, parsing_status="PARSED", validation_status="PASS",
    )
    assert first.raw_sha256 == duplicate.raw_sha256 != changed.raw_sha256
    assert duplicate.prior_receipt_id == first.receipt_id
    assert changed.prior_receipt_id == duplicate.receipt_id
    assert len(list((tmp_path / "evidence" / plan.source / "objects").glob("*/raw.bin"))) == 2
    assert store.validate()["receipt_count"] == 3
    assert "secret" not in json.dumps(changed.as_dict()).lower()


def test_receipt_tampering_and_incomplete_pagination_fail_closed(tmp_path: Path) -> None:
    store = RawEvidenceStore(tmp_path / "evidence", allowed_root=tmp_path)
    plan = alpaca_bars_plan(
        repository_root=REPO, symbols=["AAPL"], start=date(2020, 1, 1),
        end_exclusive=date(2020, 1, 2), evidence_class=EvidenceClass.HISTORICAL_RECONSTRUCTED,
    )
    first = store.append(
        plan=plan, raw=b"p1", requested_at=NOW, retrieved_at=NOW + timedelta(seconds=1),
        response_headers={}, http_status=200, page_index=0, requested_page_token=None,
        next_page_token="p2", retry_attempt=1, parent_request_id=None,
        parsing_status="PARSED", validation_status="PASS",
    )
    with pytest.raises(IntegrityError, match="terminal"):
        validate_complete_pagination([first])
    second = store.append(
        plan=plan, raw=b"p2", requested_at=NOW + timedelta(seconds=2), retrieved_at=NOW + timedelta(seconds=3),
        response_headers={}, http_status=200, page_index=1, requested_page_token="p2",
        next_page_token=None, retry_attempt=1, parent_request_id=first.logical_request_id,
        parsing_status="PARSED", validation_status="PASS",
    )
    validate_complete_pagination([first, second])
    assert validate_accepted_bars_receipts([first, second])["feed"] == "sip"
    receipt_path = sorted((tmp_path / "evidence" / plan.source / "receipts").glob("*.json"))[0]
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["validation_status"] = "TAMPERED"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(IntegrityError, match="canonical content"):
        store.validate()


def test_source_parsers_preserve_candidate_unknown_and_file_metadata() -> None:
    csv_payload = (
        b"symbol,name,exchange,assetType,ipoDate,delistingDate,status,newField\n"
        b"ABC,ABC Inc,NYSE,Stock,2010-01-01,,Active,x\n"
    )
    parsed = parse_alpha_vantage_listing_csv(csv_payload)
    assert parsed["rows"][0]["newField"] == "x"
    assets = parse_alpaca_asset_master(json.dumps([{
        "id": "id-1", "class": "us_equity", "exchange": "NYSE", "symbol": "ABC",
        "name": "ABC", "status": "active", "tradable": True, "marginable": True,
        "shortable": True, "borrow_status": "easy_to_borrow", "easy_to_borrow": True,
        "fractionable": True, "attributes": [], "future_field": {"x": 1},
    }]).encode())
    assert assets[0].unknown_fields == {"future_field": {"x": 1}}
    placeholder = parse_alpaca_asset_master(json.dumps([{
        "id": "id-2", "class": "us_equity", "exchange": "", "symbol": "26885b100",
        "name": "Inactive placeholder", "status": "inactive", "tradable": False,
        "marginable": False, "shortable": False, "borrow_status": None,
        "easy_to_borrow": False, "fractionable": False, "attributes": [],
    }]).encode())
    assert placeholder[0].symbol == "26885b100"
    assert placeholder[0].status == "inactive"
    actions = parse_corporate_action_groups(
        b'{"cash_mergers": [{"id": "1"}], "future_events": [{"id": "2"}], "next_page_token": null}',
        known_groups={"cash_mergers"},
    )
    assert actions["unknown_actions"][0]["group"] == "UNKNOWN"
    nested_actions = parse_corporate_action_groups(
        b'{"corporate_actions": {"forward_splits": [{"id": "3"}]}, "next_page_token": null}',
        known_groups={"forward_splits"},
    )
    assert nested_actions["envelope_schema"] == "NESTED_CORPORATE_ACTIONS"
    assert nested_actions["known_actions"][0]["source_group"] == "forward_splits"
    nasdaq = parse_nasdaq_symbol_directory(
        b"Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size\n"
        b"ABC|ABC Inc|Q|N|N|100\nFile Creation Time: 0810202612:00|||||\n",
        source_name="nasdaqlisted.txt", retrieved_at=NOW,
    )
    assert nasdaq["row_count"] == 1
    assert nasdaq["file_created_at"].endswith("Z")


def test_alpha_vantage_candidate_parse_is_transport_accepted_without_semantics_promotion() -> None:
    plan = alpha_vantage_listing_plan(
        repository_root=REPO,
        as_of=date(2020, 6, 25),
        state="active",
    )
    result = _parse_success(
        plan,
        (
            b"symbol,name,exchange,assetType,ipoDate,delistingDate,status\n"
            b"ABC,ABC Inc,NYSE,Stock,2010-01-01,,Active\n"
        ),
        retrieved_at=NOW,
    )
    assert result[0] == "PARSED"
    assert result[1] == "PASS_CANDIDATE_PENDING_LIVE_SEMANTICS_VALIDATION"
    assert result[3] == 1


def test_network_execution_requires_explicit_gate_and_exact_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = alpha_vantage_listing_plan(repository_root=REPO, as_of=date(2020, 1, 2), state="active")
    store = RawEvidenceStore(tmp_path / "evidence", allowed_root=tmp_path)
    registry = NetworkAcquisitionRegistry.load(
        REPO / "config/alpaca_free_bounded_network_registry.json", allowed_root=REPO
    )
    monkeypatch.delenv("FREE_SOURCE_QUALIFICATION_APPROVED", raising=False)
    with pytest.raises(NetworkGuardError):
        execute_one_source_request(
            plan=plan, approved_plan_id=plan.plan_id, evidence_store=store,
            network_registry=registry,
            clock=TrustedClock.production(), network_enabled=False,
        )
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    with pytest.raises(PermissionError, match="plan ID"):
        execute_one_source_request(
            plan=plan, approved_plan_id="0" * 64, evidence_store=store,
            network_registry=registry,
            clock=TrustedClock.production(), network_enabled=True,
        )


def test_local_api_env_loads_only_canonical_names_without_overriding(tmp_path: Path) -> None:
    sentinel_id = "credential-id-sentinel"
    sentinel_secret = "credential-secret-sentinel"
    sentinel_alpha = "credential-alpha-sentinel"
    (tmp_path / "api.env").write_text(
        "# local credentials\n\n"
        f"APCA_API_KEY_ID={sentinel_id}\n"
        f"APCA_API_SECRET_KEY={sentinel_secret}\n"
        f"ALPHA_VANTAGE_API_KEY={sentinel_alpha}\n",
        encoding="utf-8",
    )
    environment = {"APCA_API_KEY_ID": "inherited-value"}
    result = load_local_api_env(tmp_path, environment=environment)
    assert tuple(result["presence"]) == CANONICAL_CREDENTIAL_VARIABLES
    assert result["presence"] == {name: True for name in CANONICAL_CREDENTIAL_VARIABLES}
    assert result["preserved"] == ["APCA_API_KEY_ID"]
    assert environment["APCA_API_KEY_ID"] == "inherited-value"
    assert environment["APCA_API_SECRET_KEY"] == sentinel_secret
    assert environment["ALPHA_VANTAGE_API_KEY"] == sentinel_alpha
    serialized = json.dumps(result, sort_keys=True)
    assert all(value not in serialized for value in (sentinel_id, sentinel_secret, sentinel_alpha))


def test_rejected_alpha_alias_is_not_accepted_or_retained(tmp_path: Path) -> None:
    rejected_name = "ALPHA" + "VANTAGE_API_KEY"
    (tmp_path / "api.env").write_text(f"{rejected_name}=credential-sentinel\n", encoding="utf-8")
    environment: dict[str, str] = {}
    with pytest.raises(ContractError, match="unsupported variable name") as raised:
        load_local_api_env(tmp_path, environment=environment)
    assert not environment
    assert "credential-sentinel" not in str(raised.value)
    (tmp_path / "api.env").unlink()
    environment = {rejected_name: "credential-sentinel"}
    result = load_local_api_env(tmp_path, environment=environment)
    assert result["presence"]["ALPHA_VANTAGE_API_KEY"] is False


def test_missing_credentials_name_only_and_secrets_absent_from_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_values = ("id-value-sentinel", "secret-value-sentinel", "alpha-value-sentinel")
    plan = alpha_vantage_listing_plan(repository_root=REPO, as_of=date(2020, 1, 2), state="active")
    store = RawEvidenceStore(tmp_path / "evidence", allowed_root=tmp_path)
    registry = NetworkAcquisitionRegistry.load(
        REPO / "config/alpaca_free_bounded_network_registry.json", allowed_root=REPO
    )
    monkeypatch.setenv("FREE_SOURCE_QUALIFICATION_APPROVED", "YES")
    with pytest.raises(ContractError, match="ALPHA_VANTAGE_API_KEY") as raised:
        execute_one_source_request(
            plan=plan,
            approved_plan_id=plan.plan_id,
            evidence_store=store,
            network_registry=registry,
            clock=TrustedClock.production(),
            network_enabled=True,
        )
    captured = capsys.readouterr()
    surfaces = [
        str(raised.value),
        caplog.text,
        json.dumps(plan.as_dict(), sort_keys=True),
        json.dumps({"report": plan.as_dict()}, sort_keys=True),
        captured.out,
        captured.err,
    ]
    assert "ALPHA_VANTAGE_API_KEY" in str(raised.value)
    assert "api.env" not in json.dumps(plan.as_dict(), sort_keys=True)
    assert all(value not in surface for value in secret_values for surface in surfaces)
